"""以线程安全方式依次编排三个游戏适配器。

``run_all`` 和 ``run_one`` 是阻塞调用。Tk UI 在线程中运行它们；
``request_stop``、``retry_current`` 等方法可以从 Tk 主线程安全调用。
监听器回调会在触发状态转换的线程中同步执行，通常是工作线程，
因此 UI 监听器必须将界面更新转交给 Tk 主线程。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from daily_games.adapters import build_adapters
from daily_games.config_manager import BackupInfo, CheckResult, ConfigChange, ConfigManager
from daily_games.process_monitor import ProcessMonitor


GAME_ORDER = ("arknights", "endfield", "limbus")

STATE_IDLE = "idle"
STATE_CHECKING = "checking"
STATE_STARTING_GAME = "starting_game"
STATE_WAITING_GAME = "waiting_game"
STATE_STARTING_HELPER = "starting_helper"
STATE_RUNNING = "running"
STATE_CLEANING = "cleaning"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_PAUSED = "paused"
STATE_SKIPPED = "skipped"
STATE_STOPPING = "stopping"
STATE_STOPPED = "stopped"

_REDACTED_LOG_VALUE = "<已脱敏>"
_SENSITIVE_LOG_NAME = (
    r"(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|token|secret|"
    r"password|passwd|api[_-]?key)"
)
_LOG_VALUE = (
    r'(?:(?:"(?:\\.|[^"\\\r\n])*")'
    r"|(?:'(?:\\.|[^'\\\r\n])*')"
    r'|(?:[^,\s"\'\}\]\r\n]+))'
)
_AUTHORIZATION_VALUE = (
    r'(?:(?:"(?:\\.|[^"\\\r\n])*")'
    r"|(?:'(?:\\.|[^'\\\r\n])*')"
    r'|(?:(?:bearer|basic)\s+[^,\s"\'\}\]\r\n]+)'
    r'|(?:[^,\s"\'\}\]\r\n]+))'
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    rf"(?i)(\bauthorization\b[\"']?\s*[:=]\s*){_AUTHORIZATION_VALUE}"
)
_AUTHORIZATION_FLAG = re.compile(
    rf"(?i)((?<!\w)(?:--?|/)authorization\b(?:\s+|=))"
    rf"{_AUTHORIZATION_VALUE}"
)
_SENSITIVE_LOG_ASSIGNMENT = re.compile(
    rf"(?i)(\b{_SENSITIVE_LOG_NAME}\b[\"']?\s*[:=]\s*){_LOG_VALUE}"
)
_SENSITIVE_LOG_FLAG = re.compile(
    rf"(?i)((?<!\w)(?:--?|/){_SENSITIVE_LOG_NAME}\b(?:\s+|=))"
    rf"{_LOG_VALUE}"
)


def _is_sensitive_log_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return (
        normalized == "authorization"
        or normalized.endswith("token")
        or normalized.endswith("secret")
        or normalized.endswith("password")
        or normalized.endswith("passwd")
        or normalized.endswith("apikey")
    )


def _is_sensitive_command_flag(value: str) -> bool:
    match = re.fullmatch(r"(?:--?|/)([A-Za-z0-9_-]+)", value.strip())
    return bool(match and _is_sensitive_log_key(match.group(1)))


def _redact_log_text(value: object) -> str:
    text = str(value)
    text = _AUTHORIZATION_ASSIGNMENT.sub(
        rf"\1{_REDACTED_LOG_VALUE}", text,
    )
    text = _AUTHORIZATION_FLAG.sub(
        rf"\1{_REDACTED_LOG_VALUE}", text,
    )
    text = _SENSITIVE_LOG_ASSIGNMENT.sub(
        rf"\1{_REDACTED_LOG_VALUE}", text,
    )
    return _SENSITIVE_LOG_FLAG.sub(
        rf"\1{_REDACTED_LOG_VALUE}", text,
    )


def _redact_log_value(value: object, *, force: bool = False) -> object:
    """递归遮罩日志证据，同时处理命令列的旗标和值分离形式。"""
    if force:
        return _REDACTED_LOG_VALUE
    if isinstance(value, Mapping):
        return {
            key: _redact_log_value(item, force=_is_sensitive_log_key(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted: list[object] = []
        redact_next = False
        for item in value:
            if redact_next:
                redacted.append(_REDACTED_LOG_VALUE)
                redact_next = False
                continue
            if isinstance(item, str) and _is_sensitive_command_flag(item):
                redacted.append(item)
                redact_next = True
                continue
            redacted.append(_redact_log_value(item))
        return tuple(redacted) if isinstance(value, tuple) else redacted
    if isinstance(value, str):
        return _redact_log_text(value)
    return value


def _now_iso() -> str:
    """返回带明确时区偏移的 ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class StatusEvent:
    """可观察的状态转换事件，可安全地从工作线程传给 UI。"""

    game_id: str | None
    state: str
    message: str
    error_code: str | None
    timestamp: str


@dataclass(frozen=True)
class GameResult:
    """一次运行中某个已选游戏的最终结果。"""

    game_id: str
    state: str
    ok: bool
    code: str
    message: str
    started_at: str
    ended_at: str


@dataclass(frozen=True)
class RunReport:
    """阻塞调用 ``run_all`` 或 ``run_one`` 后得到的不可变结果。"""

    ok: bool
    stopped: bool
    results: tuple[GameResult, ...]


@dataclass(frozen=True)
class _StepFailure:
    """适配器违反契约时使用的私有备用结果。"""

    ok: bool
    code: str
    message: str
    details: Mapping[str, object]


class _ConfigurationGateError(Exception):
    """运行环境构建前抛出的私有错误，其文本可以安全显示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DailyOrchestrator:
    """使用协作式控制逐个运行日常游戏适配器。

    可选的仅关键字协作者用于测试注入。生产调用方只使用
    ``DailyOrchestrator(config_path)``；正常构造会通过冻结工厂创建
    ``ConfigManager``、``ProcessMonitor`` 和适配器。配置相关方法仅做
    ``ConfigManager`` 的轻量委派。
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        config_manager: ConfigManager | Any | None = None,
        process_monitor: ProcessMonitor | Any | None = None,
        adapters: Mapping[str, Any] | None = None,
        config_manager_factory: Callable[[str | Path, Path], Any] = ConfigManager,
        process_monitor_factory: Callable[..., Any] = ProcessMonitor,
        adapters_factory: Callable[[Mapping[str, object], Any, Any], Mapping[str, Any]] = build_adapters,
        logger: Any | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._lock = threading.RLock()
        self._control = threading.Condition(self._lock)
        self._listeners: list[Callable[[StatusEvent], None]] = []
        self._stop_event = threading.Event()
        self._pending_action: str | None = None
        self._is_running = False
        self._current_game: str | None = None
        self._current_state = STATE_IDLE

        # ``config/user.json`` 位于项目目录下。这里根据传入的相对项目结构推导，
        # 不依赖特定设备的绝对路径。
        default_backup_root = self._config_path.parent.parent / "backups"
        self._config_manager = (
            config_manager
            if config_manager is not None
            else config_manager_factory(config_path, default_backup_root)
        )
        self._injected_process_monitor = process_monitor
        self._process_monitor_factory = process_monitor_factory
        self._injected_adapters = dict(adapters) if adapters is not None else None
        self._adapters_factory = adapters_factory

        self._managed_logger = logger is None
        if logger is None:
            # 模块树命名：便于用 logging.config 统一配置，也便于按模块过滤。
            # 不再使用 id(self)，避免每次实例化都产生新 logger 名。
            logger = logging.getLogger("daily_games.orchestrator")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(logging.NullHandler())
        self._logger = logger
        self._run_log_handler: logging.Handler | None = None

    # ------------------------------------------------------------------
    # 公开的状态观察与控制
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """阻塞运行是否仍处于活动状态，其中包括等待决定的暂停阶段。

        ``stopped`` 事件表示适配器清理已经完成；该事件可能在运行方法的
        ``finally`` 块把本值改为 ``False`` 前短暂可见。新任务防重入必须
        使用本属性，不能只判断状态。
        """
        with self._lock:
            return self._is_running

    @property
    def current_game(self) -> str | None:
        """返回 ``GAME_ORDER`` 中的值；队列边界和运行结束后返回 ``None``。"""
        with self._lock:
            return self._current_game

    @property
    def current_state(self) -> str:
        """返回冻结状态词表中的最新稳定状态字符串。"""
        with self._lock:
            return self._current_state

    def add_listener(self, listener: Callable[[StatusEvent], None]) -> Callable[[], None]:
        """订阅状态事件，并返回可重复调用的取消订阅函数。

        回调在触发转换的调用线程中同步运行，通常是执行 ``run_all`` 或
        ``run_one`` 的工作线程，也可能是调用 ``request_stop`` 的线程。
        每次转换都会创建监听器快照，因此并发取消订阅后，已进入快照的回调
        仍可能执行一次。回调异常会被隔离，不会中止或破坏编排流程。
        """
        if not callable(listener):
            raise TypeError("listener 必须可调用")
        with self._lock:
            self._listeners.append(listener)

        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            with self._lock:
                if removed:
                    return
                removed = True
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def retry_current(self) -> None:
        """当前失败任务等待决定时，请求重试。"""
        self._set_control_action("retry")

    def skip_current(self) -> None:
        """请求将当前失败任务记录为已跳过。"""
        self._set_control_action("skip")

    def request_stop(self) -> None:
        """发出协作式停止请求，等待当前适配器调用安全返回。

        内置等待会收到本次运行的停止事件和超时时间。不支持协作停止的
        第三方适配器调用可能无限延迟完成；本方法既不会终止它，也不会并发
        强制清理。
        """
        with self._control:
            if not self._is_running:
                return
            self._stop_event.set()
            self._pending_action = "stop"
            game_id = self._current_game
            self._control.notify_all()
        self._transition(
            game_id,
            STATE_STOPPING,
            "收到安全停止请求，等待当前阶段收尾。",
            "stop_requested",
        )

    # ------------------------------------------------------------------
    # 公开配置委派
    # ------------------------------------------------------------------

    def dry_run(self) -> list[CheckResult]:
        """运行只读配置与进程诊断，不启动任何程序。

        已存在的游戏或挂机程序在只读诊断中只作提示；正式运行会由全局
        启动屏障拒绝并发。本方法不会调用适配器方法，也不会调用
        ``ProcessMonitor`` 的可变操作。
        """
        try:
            results: list[CheckResult] = list(self._config_manager.preflight())
        except Exception as exc:
            return [self._diagnostic_error(
                "config.preflight", "preflight_failed", type(exc).__name__,
            )]

        try:
            changes = self._config_manager.plan_required_changes()
        except Exception as exc:
            results.append(self._diagnostic_error(
                "configuration.plan", "configuration_plan_failed", type(exc).__name__,
            ))
        else:
            for change in changes:
                self._append_configuration_change_diagnostic(results, change)

        try:
            config = self._config_manager.load()
        except Exception as exc:
            results.append(self._diagnostic_error(
                "config.load", "config_load_failed", type(exc).__name__,
            ))
            return results
        if not isinstance(config, Mapping):
            results.append(self._diagnostic_error(
                "config.load", "config_type_invalid", "load() 未返回映射对象",
            ))
            return results

        try:
            process_monitor = self._get_process_monitor(config)
        except Exception as exc:
            results.append(self._diagnostic_error(
                "process_monitor", "process_monitor_unavailable", type(exc).__name__,
            ))
            return results

        games = config.get("games", {})
        if not isinstance(games, Mapping):
            games = {}
        for game_id in GAME_ORDER:
            game = games.get(game_id)
            game_mapping = game if isinstance(game, Mapping) else {}
            self._append_process_diagnostic(
                results,
                process_monitor,
                f"games.{game_id}.game_process_name",
                game_mapping.get("game_process_name"),
            )
            helper = game_mapping.get("helper")
            helper_mapping = helper if isinstance(helper, Mapping) else {}
            self._append_process_diagnostic(
                results,
                process_monitor,
                f"games.{game_id}.helper.process_name",
                helper_mapping.get("process_name"),
            )
        return results

    def check_configuration(self) -> list[CheckResult]:
        """把只读配置检查委派给 ``ConfigManager``。"""
        return self._config_manager.preflight()

    def plan_configuration_changes(self) -> list[ConfigChange]:
        """委派配置变更规划，本层不解释具体键。"""
        return self._config_manager.plan_required_changes()

    def apply_configuration_changes(self, change_ids: Sequence[str]) -> list[CheckResult]:
        """委派选定变更；ConfigManager 只允许应用已验证项。"""
        return self._config_manager.apply_required_changes(change_ids)

    def list_backups(self) -> list[BackupInfo]:
        """把备份枚举委派给 ``ConfigManager``。"""
        return self._config_manager.list_backups()

    def restore_backup(self, backup_id: str) -> list[CheckResult]:
        """把备份恢复委派给 ``ConfigManager``。"""
        return self._config_manager.restore_backup(backup_id)

    # ------------------------------------------------------------------
    # 阻塞运行入口
    # ------------------------------------------------------------------

    def run_all(self) -> RunReport:
        """按 arknights、endfield、limbus 的顺序阻塞执行。"""
        return self._run(GAME_ORDER)

    def run_one(self, game_id: str) -> RunReport:
        """阻塞执行一个受支持的游戏。"""
        if game_id not in GAME_ORDER:
            raise ValueError(f"未知 game_id：{game_id}")
        return self._run((game_id,))

    # ------------------------------------------------------------------
    # 主运行流程
    # ------------------------------------------------------------------

    def _run(self, game_ids: tuple[str, ...]) -> RunReport:
        self._begin_run()
        results: list[GameResult] = []
        stopped = False
        try:
            try:
                config, adapter_map, process_monitor = self._prepare_runtime()
            except _ConfigurationGateError as exc:
                self._log("warning", f"配置门禁阻止运行：{exc.code}")
                self._transition(None, STATE_FAILED, exc.message, exc.code)
                return RunReport(ok=False, stopped=False, results=())
            except Exception as exc:  # UI 需要结构化且可安全显示的结果。
                code = "runtime_setup_failed"
                message = f"无法准备运行环境：{type(exc).__name__}"
                self._log("error", f"运行环境准备失败：{type(exc).__name__}")
                self._transition(None, STATE_FAILED, message, code)
                return RunReport(ok=False, stopped=False, results=())

            self._log("debug", f"[队列] 串行执行顺序={','.join(game_ids)}")

            for game_id in game_ids:
                if self._stop_event.is_set():
                    stopped = True
                    self._transition(None, STATE_STOPPED, "队列已在下一款游戏前停止。", "stop_requested")
                    break

                adapter = adapter_map[game_id]
                while True:
                    result = self._execute_game(
                        game_id,
                        adapter,
                        config,
                        process_monitor,
                    )
                    if result.state == STATE_COMPLETED:
                        results.append(result)
                        break

                    if result.state == STATE_STOPPED:
                        results.append(result)
                        stopped = True
                        break

                    # 普通阶段失败已经发布 ``failed``。如果暂停期间收到停止请求，
                    # 该失败仍是主结果；清理或停止事件不得覆盖它。
                    action = self._wait_for_decision(game_id, result, config)
                    if action == "retry":
                        continue
                    if action == "skip":
                        skipped = GameResult(
                            game_id=game_id,
                            state=STATE_SKIPPED,
                            ok=False,
                            code=result.code,
                            message=f"已跳过：{result.message}",
                            started_at=result.started_at,
                            ended_at=_now_iso(),
                        )
                        results.append(skipped)
                        self._transition(game_id, STATE_SKIPPED, skipped.message, result.code)
                        break

                    results.append(result)
                    stopped = True
                    self._transition(
                        game_id,
                        STATE_STOPPED,
                        "队列在失败暂停时停止。",
                        "stop_requested",
                    )
                    break

                if stopped:
                    break

            if stopped:
                self._transition(None, STATE_STOPPED, "队列已安全停止。", "stop_requested")
            elif len(results) == len(game_ids):
                if any(result.state == STATE_SKIPPED for result in results):
                    message = "队列已完成，但包含已跳过的任务。"
                else:
                    message = "队列已完成。"
                self._transition(None, STATE_COMPLETED, message)

            ok = (
                not stopped
                and len(results) == len(game_ids)
                and all(result.ok for result in results)
            )
            return RunReport(ok=ok, stopped=stopped, results=tuple(results))
        finally:
            self._finish_run()
            self._close_run_log()

    def _execute_game(
        self,
        game_id: str,
        adapter: Any,
        config: Mapping[str, object],
        process_monitor: Any,
    ) -> GameResult:
        """运行单个适配器的固定阶段序列，并始终调用 cleanup。

        每轮启动前要求六个受管游戏/助手进程全部不存在；每轮清理后还会
        再次确认本轮游戏与助手均已退出。只有两道屏障都通过，队列才会
        进入下一款游戏。
        """
        started_at = _now_iso()
        failure: _StepFailure | Any | None = None
        stopped = False
        last_success: _StepFailure | Any = _StepFailure(True, "ok", "任务完成", {})
        game_ready_timeout = self._game_ready_timeout(config)
        completion_timeout = self._game_timeout(config, game_id, "completion_s")
        cleanup_timeout = self._timeout(config, "cleanup_s")

        self._transition(game_id, STATE_CHECKING, "正在确认没有其他受管游戏或助手运行。")
        start_barrier = self._check_managed_processes_clear(config, process_monitor)
        self._log_step_evidence(game_id, "start_process_barrier", start_barrier)
        if not start_barrier.ok:
            failure = start_barrier

        stages: tuple[tuple[str, str, tuple[Any, ...], str], ...] = (
            (STATE_CHECKING, "check_environment", (), "正在检查环境。"),
            (STATE_STARTING_GAME, "start_game", (), "正在启动游戏。"),
            (
                STATE_WAITING_GAME,
                "wait_game_ready",
                (game_ready_timeout, self._stop_event),
                "正在等待游戏就绪。",
            ),
            (STATE_STARTING_HELPER, "start_helper", (), "正在启动挂机进程。"),
            (
                STATE_RUNNING,
                "wait_completion",
                (completion_timeout, self._stop_event),
                "正在等待日常任务完成。",
            ),
        )

        for state, method_name, args, message in (() if failure is not None else stages):
            if self._stop_event.is_set():
                stopped = True
                break
            self._transition(game_id, state, message)
            step = self._call_adapter_step(adapter, method_name, *args)
            self._log_step_evidence(game_id, method_name, step)
            if not step.ok:
                if self._stop_event.is_set() or step.code == "stop_requested":
                    stopped = True
                else:
                    failure = step
                break
            last_success = step
            if self._stop_event.is_set():
                stopped = True
                break

        # 清理操作始终执行。阶段失败或收到协作式停止请求后，
        # 这是唯一允许调用的适配器方法。
        self._transition(game_id, STATE_CLEANING, "正在清理本阶段已接管的进程。")
        cleanup = self._call_adapter_step(adapter, "cleanup")
        self._log_step_evidence(game_id, "cleanup", cleanup)

        # 启动屏障失败时本轮从未启动任何程序；不要等待用户原有进程退出。
        # 其余路径必须确认本轮游戏和助手都已消失，才可能报告完成。
        exit_barrier: _StepFailure | Any = _StepFailure(
            True,
            "managed_processes_exited",
            "本轮游戏与助手均已退出。",
            {},
        )
        if start_barrier.ok:
            exit_barrier = self._wait_for_current_processes_exit(
                game_id,
                config,
                process_monitor,
                cleanup_timeout,
            )
            self._log_step_evidence(game_id, "exit_process_barrier", exit_barrier)

        # 即使清理也报告错误，或清理期间收到停止请求，
        # 前面的阶段失败仍保持为主结果。
        if failure is not None:
            if not cleanup.ok:
                self._log(
                    "warning",
                    f"[{game_id}] 阶段失败后的清理也失败：{cleanup.code}",
                )
            result = self._game_result(
                game_id,
                STATE_FAILED,
                False,
                failure.code,
                failure.message,
                started_at,
            )
            self._transition(game_id, STATE_FAILED, result.message, result.code)
            return result

        # 仅当前面没有阶段失败时，停止结果才具有更高优先级。
        if stopped or self._stop_event.is_set():
            if not cleanup.ok:
                self._log("warning", f"[{game_id}] 停止期间清理失败：{cleanup.code}")
            result = self._stopped_game_result(game_id, cleanup, exit_barrier, started_at)
            self._transition(game_id, STATE_STOPPED, result.message, result.code)
            return result

        if not exit_barrier.ok:
            result = self._game_result(
                game_id,
                STATE_FAILED,
                False,
                exit_barrier.code,
                exit_barrier.message,
                started_at,
            )
            self._transition(game_id, STATE_FAILED, result.message, result.code)
            return result

        if not cleanup.ok:
            # Sol 的明确兼容规则：在这个严格限定的场景中，not_owned 清理结果
            # 不会把此前已成功的游戏改为失败，但仍记录为警告。
            if cleanup.code == "not_owned":
                warning = "清理返回 not_owned；此前各阶段均成功，按兼容规则保留成功结果。"
                self._log("warning", f"[{game_id}] {warning}")
                result = self._game_result(
                    game_id,
                    STATE_COMPLETED,
                    True,
                    last_success.code,
                    f"{last_success.message}（{warning}）",
                    started_at,
                )
                self._transition(game_id, STATE_COMPLETED, result.message)
                return result

            result = self._game_result(
                game_id,
                STATE_FAILED,
                False,
                cleanup.code,
                f"清理失败：{cleanup.message}",
                started_at,
            )
            self._transition(game_id, STATE_FAILED, result.message, result.code)
            return result

        result = self._game_result(
            game_id,
            STATE_COMPLETED,
            True,
            last_success.code,
            last_success.message,
            started_at,
        )
        self._transition(game_id, STATE_COMPLETED, result.message)
        return result

    @staticmethod
    def _stopped_game_result(
        game_id: str,
        cleanup: Any,
        exit_barrier: Any,
        started_at: str,
    ) -> GameResult:
        """组合停止路径上的清理与退出屏障结论，供 stopped 分支使用。

        ``GameResult`` 没有 stopped 字段；``RunReport.stopped`` 由 ``_run``
        维护。四种组合里 cleanup 失败与屏障失败的次要结论没有 details
        可依赖，必须完整写进 message：
        ① cleanup ok + 屏障 ok → ``stop_requested``；
        ② cleanup 失败（含 not_owned）+ 屏障 ok → ``cleanup.code``，
           且不得把屏障已确认退出的结果写成残留；
        ③ cleanup ok + 屏障失败 → ``exit_barrier.code``，保留屏障原因；
        ④ 两者均失败 → 以 ``exit_barrier.code`` 为最终安全码，
           同时列出 cleanup.code 与屏障码/原因。
        """
        if exit_barrier.ok:
            if cleanup.ok:
                return DailyOrchestrator._game_result(
                    game_id,
                    STATE_STOPPED,
                    False,
                    "stop_requested",
                    "调度已停止；本轮游戏与助手均已退出。",
                    started_at,
                )
            return DailyOrchestrator._game_result(
                game_id,
                STATE_STOPPED,
                False,
                str(cleanup.code),
                f"调度已停止；清理返回 {cleanup.code}，随后屏障确认全部退出。",
                started_at,
            )
        barrier_conclusion = DailyOrchestrator._stopped_barrier_conclusion(exit_barrier)
        if cleanup.ok:
            return DailyOrchestrator._game_result(
                game_id,
                STATE_STOPPED,
                False,
                str(exit_barrier.code),
                f"调度已停止；本轮清理已完成，但{barrier_conclusion}",
                started_at,
            )
        return DailyOrchestrator._game_result(
            game_id,
            STATE_STOPPED,
            False,
            str(exit_barrier.code),
            f"调度已停止；清理返回 {cleanup.code}；{barrier_conclusion}",
            started_at,
        )

    @staticmethod
    def _stopped_barrier_conclusion(exit_barrier: Any) -> str:
        """把屏障失败结论转成停止消息里的受限措辞。

        只有 ``managed_process_still_running`` 有枚举证据，允许断言
        「仍有进程未退出」；查询或等待失败等未知结果只能写
        「无法确认是否全部退出」，不得断言进程仍在运行或已全部退出。
        """
        code = str(getattr(exit_barrier, "code", ""))
        reason = str(getattr(exit_barrier, "message", "")).strip()
        if code == "managed_process_still_running":
            return f"仍有进程未退出（{code}）：{reason}"
        return f"无法确认是否全部退出（{code}）：{reason}"

    def _wait_for_decision(
        self,
        game_id: str,
        result: GameResult,
        config: Mapping[str, object] | None = None,
    ) -> str:
        """发布 paused 状态，等待重试、跳过或停止请求。

        没有用户决定时也必须有界等待，避免 GUI 关闭或无人值守时工作线程
        永久阻塞，进而导致单实例锁无法释放。超时时间来自配置
        ``timeouts.paused_s``；到期后自动按停止处理，使队列安全收尾。
        """
        self._transition(
            game_id,
            STATE_PAUSED,
            "任务失败，等待重试、跳过或停止。",
            result.code,
        )
        paused_timeout = self._timeout(config or {}, "paused_s")
        # 0 或负值表示禁用自动超时；仅用于测试或显式要求无限等待的场景。
        deadline = None if paused_timeout <= 0 else time.monotonic() + paused_timeout
        with self._control:
            while True:
                if self._stop_event.is_set() or self._pending_action == "stop":
                    self._pending_action = None
                    return "stop"
                if self._pending_action in {"retry", "skip"}:
                    action = self._pending_action
                    self._pending_action = None
                    return action
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._pending_action = None
                        self._stop_event.set()
                        self._log(
                            "warning",
                            f"[{game_id}] 暂停超过 {paused_timeout:.0f}s，自动停止队列。",
                        )
                        return "stop"
                    self._control.wait(timeout=remaining)
                else:
                    self._control.wait()

    # ------------------------------------------------------------------
    # 运行环境准备
    # ------------------------------------------------------------------

    def _prepare_runtime(self) -> tuple[Mapping[str, object], dict[str, Any], Any]:
        try:
            preflight_results = list(self._config_manager.preflight())
        except Exception as exc:
            raise _ConfigurationGateError(
                "configuration_not_ready",
                "配置前置检查无法完成。",
            ) from exc

        failed_preflight = [
            result for result in preflight_results
            if getattr(result, "ok", None) is not True
        ]
        if failed_preflight:
            raise _ConfigurationGateError(
                "configuration_not_ready",
                self._configuration_gate_message(
                    "配置前置检查未通过",
                    failed_preflight,
                    "name",
                ),
            )

        try:
            pending_changes = list(self._config_manager.plan_required_changes())
        except Exception as exc:
            raise _ConfigurationGateError(
                "configuration_not_ready",
                "配置变更规划无法完成。",
            ) from exc
        if pending_changes:
            raise _ConfigurationGateError(
                "configuration_confirmation_required",
                self._configuration_gate_message(
                    "仍有待确认的配置变更",
                    pending_changes,
                    "change_id",
                ),
            )

        try:
            config = self._config_manager.load()
        except Exception as exc:
            raise _ConfigurationGateError(
                "configuration_not_ready",
                "无法读取执行配置。",
            ) from exc
        if not isinstance(config, Mapping):
            raise _ConfigurationGateError(
                "configuration_not_ready",
                "执行配置格式无效。",
            )
        self._configure_run_logger(config)

        process_monitor = self._get_process_monitor(config)

        if self._injected_adapters is not None:
            adapter_map = dict(self._injected_adapters)
        else:
            adapter_map = dict(self._adapters_factory(config, process_monitor, self._logger))

        missing = [game_id for game_id in GAME_ORDER if game_id not in adapter_map]
        if missing:
            raise ValueError(f"build_adapters 缺少必要的游戏 ID：{missing}")
        return config, adapter_map, process_monitor

    @staticmethod
    def _configured_process_names(
        config: Mapping[str, object],
        game_ids: Sequence[str],
    ) -> list[str]:
        """按配置顺序返回去重后的有效游戏与助手进程名。"""
        games = config.get("games", {})
        if not isinstance(games, Mapping):
            return []
        names: list[str] = []
        for game_id in game_ids:
            raw_game = games.get(game_id, {})
            game = raw_game if isinstance(raw_game, Mapping) else {}
            helper_raw = game.get("helper", {})
            helper = helper_raw if isinstance(helper_raw, Mapping) else {}
            for raw_name in (game.get("game_process_name"), helper.get("process_name")):
                if isinstance(raw_name, str) and raw_name.strip() and raw_name not in names:
                    names.append(raw_name)
        return names

    def _check_managed_processes_clear(
        self,
        config: Mapping[str, object],
        process_monitor: Any,
    ) -> _StepFailure:
        """启动任何程序前确认六个受管进程均不存在。

        屏障失败时返回结构化建议：列出残留进程的可执行文件名与 PID，并给出
        人工清理指引（关闭对应窗口或任务管理器结束），避免用户在 paused 死等
        中不知道下一步该做什么。本程序不会自动结束这些进程，因为它们可能属于
        用户当前正在使用的会话。
        """
        running: dict[str, list[int]] = {}
        for name in self._configured_process_names(config, GAME_ORDER):
            try:
                records = list(process_monitor.find_by_executable(name))
            except Exception as exc:
                return _StepFailure(
                    False,
                    "managed_process_query_failed",
                    f"无法确认受管进程是否已退出：{type(exc).__name__}。",
                    {},
                )
            if records:
                running[name] = [
                    record.pid
                    for record in records
                    if isinstance(getattr(record, "pid", None), int)
                ]
        if running:
            display_names = self._managed_process_display_names(config)
            hints: list[str] = []
            for proc_name, pids in running.items():
                label = display_names.get(proc_name.casefold(), proc_name)
                hints.append(f"{label}({proc_name}, PID={','.join(str(p) for p in pids)})")
            suggestion = (
                "请先关闭对应游戏窗口或挂机工具，或在任务管理器中结束以上进程，"
                "再点重试；本程序不会自动结束用户已打开的进程。"
            )
            return _StepFailure(
                False,
                "managed_process_already_running",
                (
                    "检测到已有游戏或助手进程；为避免并发，本轮未启动任何程序。"
                    f"残留：{'；'.join(hints)}。{suggestion}"
                ),
                {"running": running, "cleanup_hint": suggestion},
            )
        return _StepFailure(True, "managed_processes_clear", "受管进程启动屏障已通过。", {})

    @staticmethod
    def _managed_process_display_names(
        config: Mapping[str, object],
    ) -> dict[str, str]:
        """返回进程名(小写)到展示名的映射，用于屏障失败时的人话提示。"""
        games = config.get("games", {})
        if not isinstance(games, Mapping):
            return {}
        mapping: dict[str, str] = {}
        for game_id in GAME_ORDER:
            raw_game = games.get(game_id, {})
            game = raw_game if isinstance(raw_game, Mapping) else {}
            display = str(game.get("display_name", game_id))
            game_name = game.get("game_process_name")
            if isinstance(game_name, str) and game_name:
                mapping[game_name.casefold()] = display
            helper_raw = game.get("helper", {})
            helper = helper_raw if isinstance(helper_raw, Mapping) else {}
            helper_name = helper.get("process_name")
            if isinstance(helper_name, str) and helper_name:
                mapping[helper_name.casefold()] = f"{display}挂机工具"
        return mapping

    def _wait_for_current_processes_exit(
        self,
        game_id: str,
        config: Mapping[str, object],
        process_monitor: Any,
        timeout_s: float,
    ) -> _StepFailure:
        """用同一截止时间等待本轮游戏与助手全部退出，并稳定复查。

        Windows 在进程刚退出时可能短暂保留一帧枚举结果。即使
        ``wait_for_exit`` 已确认退出，也要在截止时间内继续复查，避免把这类
        瞬时快照误判为失败；只有持续到超时仍存在才阻止下一款游戏。
        """
        names = self._configured_process_names(config, (game_id,))
        if not names:
            # 测试注入可以只提供最小配置；生产配置的 preflight 会验证名称。
            return _StepFailure(True, "process_names_unconfigured", "未配置进程名，跳过退出屏障。", {})

        deadline = time.monotonic() + max(0.0, timeout_s)
        consecutive_clear_samples = 0
        while True:
            remaining_processes: dict[str, list[int]] = {}
            records_by_name: dict[str, list[Any]] = {}
            for name in names:
                try:
                    records = list(process_monitor.find_by_executable(name))
                except Exception as exc:
                    return _StepFailure(
                        False,
                        "managed_process_query_failed",
                        f"无法复查 {name} 是否退出：{type(exc).__name__}。",
                        {},
                    )
                if records:
                    records_by_name[name] = records
                    remaining_processes[name] = [
                        record.pid
                        for record in records
                        if isinstance(getattr(record, "pid", None), int)
                    ]

            if not records_by_name:
                consecutive_clear_samples += 1
                remaining = max(0.0, deadline - time.monotonic())
                if consecutive_clear_samples >= 2 or remaining <= 0.0:
                    return _StepFailure(
                        True,
                        "managed_processes_exited",
                        "本轮游戏与助手均已退出，可以进入下一项。",
                        {},
                    )
                time.sleep(min(0.05, remaining))
                continue

            consecutive_clear_samples = 0

            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0.0:
                return _StepFailure(
                    False,
                    "managed_process_still_running",
                    "本轮游戏或助手尚未退出；队列不会启动下一款游戏。",
                    {"running": remaining_processes, "timeout_s": timeout_s},
                )

            for name, records in records_by_name.items():
                for record in records:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0.0:
                        break
                    try:
                        process_monitor.wait_for_exit(record.pid, remaining, None)
                    except Exception as exc:
                        return _StepFailure(
                            False,
                            "managed_process_wait_failed",
                            f"等待 {name} 退出时发生异常：{type(exc).__name__}。",
                            {},
                        )

            # 即使监控器刚报告退出，Windows 的进程快照也可能短暂滞后。
            # 小幅退让后重新枚举；绝不因为一次残留快照启动下一款游戏。
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0.0:
                time.sleep(min(0.05, remaining))

    @staticmethod
    def _configuration_gate_message(prefix: str, entries: Sequence[Any], field: str) -> str:
        """为全局配置门禁生成仅包含数量和标识符的消息。"""
        labels: list[str] = []
        for entry in entries[:3]:
            raw = str(getattr(entry, field, "配置"))
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_.-")
            labels.append(safe[:80] or "配置")
        suffix = f"（{', '.join(labels)}）" if labels else ""
        return f"{prefix}，共 {len(entries)} 项{suffix}。"

    def _configure_run_logger(self, config: Mapping[str, object]) -> None:
        """尽量创建单次运行日志，且不写入配置值。"""
        if not self._managed_logger:
            return
        self._close_run_log()
        logging_config = config.get("logging", {})
        configured_level = "INFO"
        if isinstance(logging_config, Mapping):
            raw_level = logging_config.get("level", configured_level)
            if isinstance(raw_level, str) and raw_level.upper() == "DEBUG":
                configured_level = "DEBUG"
        log_level = logging.DEBUG if configured_level == "DEBUG" else logging.INFO
        self._logger.setLevel(log_level)
        paths = config.get("paths", {})
        if not isinstance(paths, Mapping):
            return
        log_dir_value = paths.get("logs_dir")
        if not isinstance(log_dir_value, str) or not log_dir_value:
            return
        try:
            log_dir = Path(log_dir_value)
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"daily_games_{stamp}_{uuid4().hex[:8]}.log"
            handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
            handler.setLevel(log_level)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
            ))
            self._logger.addHandler(handler)
            self._run_log_handler = handler
            if log_level == logging.DEBUG:
                self._log("debug", "调试日志已启用；仅记录脱敏运行证据。")
        except OSError:
            # 日志错误不得阻止安全停止，也不得让队列停在半运行状态。
            self._log("warning", "无法创建本次运行的日志文件。")

    def _get_process_monitor(self, config: Mapping[str, object]) -> Any:
        """返回注入的监控器，或构造正常且读写安全的监控器。"""
        if self._injected_process_monitor is not None:
            return self._injected_process_monitor
        polling = config.get("polling", {})
        poll_interval = 1.0
        if isinstance(polling, Mapping):
            try:
                poll_interval = float(polling.get("interval_s", poll_interval))
            except (TypeError, ValueError):
                pass
        try:
            return self._process_monitor_factory(poll_interval_s=poll_interval)
        except TypeError:
            # 单元测试中的小型模拟工厂可能故意不接收参数；
            # 生产环境的 ProcessMonitor 接受该关键字参数。
            return self._process_monitor_factory()

    def _close_run_log(self) -> None:
        handler = self._run_log_handler
        if handler is None:
            return
        self._run_log_handler = None
        try:
            self._logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 线程安全的状态、阶段与辅助方法
    # ------------------------------------------------------------------

    def _begin_run(self) -> None:
        with self._control:
            if self._is_running:
                raise RuntimeError("DailyOrchestrator 已经在运行")
            self._is_running = True
            self._current_game = None
            self._current_state = STATE_IDLE
            self._pending_action = None
            self._stop_event.clear()

    def _finish_run(self) -> None:
        with self._control:
            # 每个游戏的 ``stopped`` 会在最终可观察运行标志清除前发布。
            self._is_running = False
            self._current_game = None
            self._pending_action = None
            self._control.notify_all()

    def _set_control_action(self, action: str) -> None:
        with self._control:
            if not self._is_running:
                return
            # 这里也接受 ``failed``，避免监听器在失败事件与随后 paused 事件之间
            # 的短暂间隔中丢失决定。
            if self._current_state not in {STATE_FAILED, STATE_PAUSED}:
                return
            self._pending_action = action
            self._control.notify_all()

    def _transition(
        self,
        game_id: str | None,
        state: str,
        message: str,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            self._current_game = game_id
            self._current_state = state
            listeners = tuple(self._listeners)
        event = StatusEvent(
            game_id=game_id,
            state=state,
            message=message,
            error_code=error_code,
            timestamp=_now_iso(),
        )
        level = "error" if state == STATE_FAILED else "info"
        self._log(level, f"[{game_id or '队列'}] {state} 错误码={error_code or '-'} {message}")
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:  # 监听器错误不得影响运行。
                self._log("warning", f"已忽略监听器错误：{type(exc).__name__}")

    def _call_adapter_step(self, adapter: Any, method_name: str, *args: Any) -> _StepFailure | Any:
        method = getattr(adapter, method_name, None)
        if not callable(method):
            return _StepFailure(
                False,
                "adapter_method_missing",
                f"适配器缺少 {method_name} 方法。",
                {},
            )
        try:
            result = method(*args)
        except Exception as exc:
            self._log("error", f"适配器 {method_name} 抛出 {type(exc).__name__}")
            return _StepFailure(
                False,
                "adapter_exception",
                f"适配器 {method_name} 异常：{type(exc).__name__}",
                {},
            )
        if not all(hasattr(result, field) for field in ("ok", "code", "message", "details")):
            return _StepFailure(
                False,
                "invalid_step_result",
                f"适配器 {method_name} 返回了不完整的 StepResult。",
                {},
            )
        return result

    @staticmethod
    def _diagnostic_error(name: str, code: str, reason: str) -> CheckResult:
        return CheckResult(
            name=name,
            ok=False,
            code=code,
            message=f"只读诊断失败：{reason}",
            severity="error",
            details={},
        )

    def _append_process_diagnostic(
        self,
        results: list[CheckResult],
        process_monitor: Any,
        name: str,
        process_name: object,
    ) -> None:
        """追加一项只读进程状态，不暴露命令行。"""
        if not isinstance(process_name, str) or not process_name.strip():
            results.append(self._diagnostic_error(name, "process_name_missing", "缺少有效 process_name"))
            return
        try:
            records = list(process_monitor.find_by_executable(process_name))
        except Exception as exc:
            results.append(self._diagnostic_error(name, "process_query_failed", type(exc).__name__))
            return

        pids = [record.pid for record in records if isinstance(getattr(record, "pid", None), int)]
        if records:
            results.append(CheckResult(
                name=name,
                ok=True,
                code="process_running",
                message=f"检测到正在运行的 {process_name}；正式运行会拒绝启动，以避免并发。",
                severity="info",
                details={"count": len(records), "pids": pids},
            ))
            return
        results.append(CheckResult(
            name=name,
            ok=True,
            code="process_not_running",
            message=f"未检测到正在运行的 {process_name}；实际运行时将由适配器启动或等待。",
            severity="info",
            details={"count": 0, "pids": []},
        ))

    @staticmethod
    def _append_configuration_change_diagnostic(
        results: list[CheckResult],
        change: object,
    ) -> None:
        """显示必要的配置变更，但不泄露具体值。"""
        change_id = getattr(change, "change_id", None)
        tool = getattr(change, "tool", None)
        field = getattr(change, "field", None)
        verified = getattr(change, "verified", None)
        warning = getattr(change, "warning", "")
        if not isinstance(change_id, str) or not change_id:
            results.append(DailyOrchestrator._diagnostic_error(
                "configuration.plan", "configuration_change_invalid", "ConfigChange 无效",
            ))
            return
        verified_bool = bool(verified)
        code = (
            "configuration_change_required"
            if verified_bool
            else "manual_configuration_required"
        )
        message = f"需要配置变更：{tool or '未知'} / {field or '未知'}。"
        if isinstance(warning, str) and warning:
            message += f"警告：{warning}"
        results.append(CheckResult(
            name=f"configuration.change.{change_id}",
            ok=False,
            code=code,
            message=message,
            severity="warning",
            details={
                "change_id": change_id,
                "tool": str(tool or ""),
                "field": str(field or ""),
                "verified": verified_bool,
            },
        ))

    def _log_step_evidence(self, game_id: str, method_name: str, result: Any) -> None:
        """记录经过结构化遮罩的最小运行证据。

        适配器 ``details`` 可能包含任意诊断数据，其中也可能有源自配置的值。
        本方法只允许进程 ID 和启动命令，并在格式化前递归遮罩敏感值。
        """
        fields: dict[str, object] = {}
        details = getattr(result, "details", {})
        if isinstance(details, Mapping):
            for key in ("pid", "pids", "command"):
                if key in details:
                    fields[key] = _redact_log_value(details[key])
        suffix = f" 证据={fields!r}" if fields else ""
        self._log(
            "info",
            f"[{game_id}] 阶段={method_name} 错误码={getattr(result, 'code', '未知')}{suffix}",
        )

    def _game_ready_timeout(self, config: Mapping[str, object]) -> float:
        # 即使适配器内部同时观察进程和窗口就绪状态，对外也只提供一个
        # wait_game_ready 超时时间。优先使用最终窗口时间预算；配置没有
        # 窗口专用值时，回退到进程时间预算。
        timeout = self._timeout(config, "game_window_s")
        return timeout if timeout > 0 else self._timeout(config, "game_process_s")

    def _game_timeout(
        self,
        config: Mapping[str, object],
        game_id: str,
        key: str,
    ) -> float:
        """优先使用单款游戏的超时配置，再回退到全局值。"""
        games = config.get("games", {})
        if isinstance(games, Mapping):
            game = games.get(game_id, {})
            if isinstance(game, Mapping):
                timeouts = game.get("timeouts", {})
                if isinstance(timeouts, Mapping) and key in timeouts:
                    try:
                        return max(0.0, float(timeouts[key]))
                    except (TypeError, ValueError):
                        pass
        return self._timeout(config, key)

    @staticmethod
    def _timeout(config: Mapping[str, object], key: str) -> float:
        timeouts = config.get("timeouts", {})
        if not isinstance(timeouts, Mapping):
            return 0.0
        try:
            return max(0.0, float(timeouts.get(key, 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _game_result(
        game_id: str,
        state: str,
        ok: bool,
        code: str,
        message: str,
        started_at: str,
    ) -> GameResult:
        return GameResult(
            game_id=game_id,
            state=state,
            ok=ok,
            code=str(code),
            message=str(message),
            started_at=started_at,
            ended_at=_now_iso(),
        )

    def _log(self, level: str, message: str) -> None:
        """记录状态证据，同时过滤疑似凭据值。"""
        safe_message = _redact_log_text(message)
        method = getattr(self._logger, level, None)
        if callable(method):
            try:
                method(safe_message)
            except Exception:
                pass


__all__ = [
    "DailyOrchestrator",
    "GameResult",
    "RunReport",
    "StatusEvent",
    "GAME_ORDER",
    "STATE_IDLE",
    "STATE_CHECKING",
    "STATE_STARTING_GAME",
    "STATE_WAITING_GAME",
    "STATE_STARTING_HELPER",
    "STATE_RUNNING",
    "STATE_CLEANING",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_PAUSED",
    "STATE_SKIPPED",
    "STATE_STOPPING",
    "STATE_STOPPED",
]
