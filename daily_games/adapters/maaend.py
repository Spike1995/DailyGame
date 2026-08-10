"""明日方舟：终末地 / MaaEnd 适配器。

关键约束（依任务书）：
- 通过 MXU 官方 --autostart/--instance/--quit-after-run 参数启动指定实例。
- MaaEnd 自退本身不代表成功；必须核验本次 PID 的 MaaFramework 顶层任务事件。
- 随后仅关闭本轮明确接管的 Endfield。
- 保留现有日常内容（当前激活实例“全套日常”）。
- 不得占用或重设 MaaEnd 使用的 127.0.0.1:12701。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Mapping

from .base import (
    AdapterBase,
    CODE_COMPLETION_SIGNAL_UNSET,
    CODE_ENV_NOT_READY,
    CODE_HELPER_EXITED_EARLY,
    CODE_MANUAL_CONFIGURATION_REQUIRED,
    CODE_OK,
    CODE_START_FAILED,
    CODE_STOP_REQUESTED,
    CODE_WAIT_TIMEOUT,
    StepResult,
)


CODE_TASK_EVIDENCE_MISSING = "task_evidence_missing"
CODE_TASK_ABORTED = "task_aborted"
CODE_TASK_INCOMPLETE = "task_incomplete"
CODE_TASK_FAILED = "task_failed"
CODE_TASK_COMPLETED_WITH_FAILURES = "task_completed_with_failures"

_TASK_EVENT_RE = re.compile(
    r"\[msg=Tasker\.Task\.(Starting|Succeeded|Failed)\].*"
    r"\[details=(\{.*\})\]\s*$"
)
_ABORT_NODE_EVENT_RE = re.compile(r"\[msg=Node\.[A-Za-z]+\.(?:Starting|Succeeded|Failed)\]")


class MaaEndAdapter(AdapterBase):
    """明日方舟：终末地 → MaaEnd 适配器。"""

    def __init__(self, *, game_cfg: Mapping[str, object], process_monitor, logger=None):
        super().__init__(
            game_id="endfield",
            display_name=str(game_cfg.get("display_name", "明日方舟：终末地")),
            game_cfg=game_cfg,
            process_monitor=process_monitor,
            logger=logger,
        )
        helper = game_cfg.get("helper", {}) if isinstance(game_cfg.get("helper"), dict) else {}
        self._helper = helper
        self._launcher = game_cfg.get("launcher", {}) if isinstance(game_cfg.get("launcher"), dict) else {}
        start_button = self._launcher.get("start_button_automation", {})
        self._start_button_automation = (
            start_button if isinstance(start_button, dict) else {}
        )
        self._started_helper_pid: int | None = None
        self._baseline_game_pids: set[int] = set()
        self._owned_game_pids: set[int] = set()
        self._scheduled_game_task_name = str(
            self._launcher.get("scheduled_task_name", "")
        ).strip()
        self._scheduled_helper_task_name = str(
            self._helper.get("scheduled_task_name", "")
        ).strip()
        self._scheduled_game_task_started = False
        self._scheduled_helper_task_started = False
        self._expected_task_count = 0
        self._task_evidence_start_offset: int | None = None
        self._task_evidence_file_identity: tuple[int, int] | None = None
        self._task_evidence_file_offsets: dict[tuple[int, int], int] | None = None

    def check_environment(self) -> StepResult:
        problems: list[str] = []
        details: dict[str, object] = {}

        lex = self._launcher.get("executable")
        if not lex or not Path(str(lex)).is_file():
            problems.append("鹰角启动器可执行文件不存在")
        else:
            details["launcher_executable"] = lex

        if bool(self._launcher.get("requires_administrator", False)):
            task_matches = getattr(self._pm, "scheduled_task_matches", None)
            launcher_arguments = subprocess.list2cmdline(
                [str(value) for value in self._launcher.get("args", [])]
            )
            if not self._scheduled_game_task_name or not callable(task_matches):
                problems.append("终末地启动器管理员计划任务不可用")
            elif lex and not task_matches(
                self._scheduled_game_task_name,
                str(lex),
                str(Path(str(lex)).parent),
                launcher_arguments,
                expected_action_count=1,
                expected_wrapper_target=(
                    str(self._game_cfg.get("game_process_name", ""))
                    if bool(self._start_button_automation.get("enabled", False))
                    else ""
                ),
            ):
                problems.append("终末地启动器管理员计划任务不存在或定义不匹配")
            details["game_administrator_task"] = (
                self._scheduled_game_task_name or "未配置"
            )
            details["start_button_automation_verified"] = bool(
                self._start_button_automation.get("verified", False)
            )
            if (
                bool(self._start_button_automation.get("enabled", False))
                and not bool(self._start_button_automation.get("verified", False))
            ):
                problems.append("终末地启动器开始游戏按钮自动化未验证")

        game_exe = self._game_cfg.get("game_executable")
        if not game_exe or not Path(str(game_exe)).is_file():
            problems.append("终末地可执行文件不存在")
        else:
            details["game_executable"] = game_exe

        maaend_exe = self._helper.get("executable")
        if not maaend_exe or not Path(str(maaend_exe)).is_file():
            problems.append("MaaEnd.exe 不存在")
        else:
            details["maaend_executable"] = maaend_exe

        if bool(self._helper.get("requires_administrator", False)):
            task_matches = getattr(self._pm, "scheduled_task_matches", None)
            helper_arguments = subprocess.list2cmdline(
                [str(value) for value in self._helper.get("cli_args", [])]
            )
            if not self._scheduled_helper_task_name or not callable(task_matches):
                problems.append("MaaEnd 管理员计划任务不可用")
            elif maaend_exe and not task_matches(
                self._scheduled_helper_task_name,
                str(maaend_exe),
                str(Path(str(maaend_exe)).parent),
                helper_arguments,
            ):
                problems.append("MaaEnd 管理员计划任务不存在或定义不匹配")
            details["helper_administrator_task"] = (
                self._scheduled_helper_task_name or "未配置"
            )

        # 连接端口：仅显示，本程序不使用
        details["web_server_port"] = self._helper.get("web_server_port", 12701)
        details["web_server_port_do_not_use"] = True

        signal_ready, signal_reason, signal_details = self._completion_signal_state()
        details.update(signal_details)
        if signal_reason in {
            "mxu_configuration_unavailable",
            "active_instance_unavailable",
            "active_instance_name_unavailable",
            "controller_unsupported",
        }:
            problems.append("无法确认 MaaEnd 当前 Win32 活动实例")

        if problems:
            return StepResult(False, CODE_ENV_NOT_READY, "；".join(problems), details)
        if not signal_ready:
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "MaaEnd 命令行自动执行/完成后退出或本轮游戏关闭策略尚未验证",
                details,
            )
        if not callable(getattr(self._pm, "take_ownership", None)):
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                "进程监控器不支持安全接管本轮启动的 Endfield",
                details,
            )
        return StepResult(True, CODE_OK, "MaaEnd 官方命令行完成信号检查通过", details)

    def start_game(self) -> StepResult:
        lex = self._launcher.get("executable")
        args = self._launcher.get("args", [])
        if not lex or not Path(str(lex)).is_file():
            return StepResult(False, CODE_ENV_NOT_READY, "启动器可执行文件不存在", {})
        game_name = str(self._game_cfg.get("game_process_name", "Endfield.exe"))
        self._baseline_game_pids = {
            rec.pid for rec in self._pm.find_by_executable(game_name)
        }
        command = [str(lex)] + [str(a) for a in args]
        if bool(self._launcher.get("requires_administrator", False)):
            start_task = getattr(self._pm, "start_scheduled_task", None)
            if not self._scheduled_game_task_name or not callable(start_task):
                return StepResult(
                    False, CODE_ENV_NOT_READY, "终末地启动器管理员任务不可用", {}
                )
            if not start_task(self._scheduled_game_task_name):
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "无法启动终末地启动器管理员计划任务",
                    {"scheduled_task": self._scheduled_game_task_name},
                )
            self._scheduled_game_task_started = True
            return StepResult(
                True,
                CODE_OK,
                (
                    "终末地启动器与开始游戏按钮自动化已通过管理员计划任务启动"
                    if bool(self._start_button_automation.get("enabled", False))
                    else "终末地启动器已通过管理员计划任务启动"
                ),
                {
                    "scheduled_task": self._scheduled_game_task_name,
                    "command": command,
                    "launcher_verified": bool(self._launcher.get("verified", False)),
                },
            )
        try:
            rec = self._pm.launch(command)
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"启动器启动失败：{exc}", {})
        verified = bool(self._launcher.get("verified", False))
        return StepResult(
            True, CODE_OK,
            ("启动器已启动（参数已验证）" if verified else "启动器已启动，--game 参数未验证，需观察"),
            {"pid": rec.pid, "command": command, "launcher_verified": verified},
        )

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        name = str(self._game_cfg.get("game_process_name", "Endfield.exe"))
        recs = self._pm.wait_for_process(name, timeout_s, stop_event)
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待游戏进程时收到停止要求", {})
        if not recs:
            return StepResult(False, CODE_WAIT_TIMEOUT, f"等待 {name} 超时", {"timeout_s": timeout_s})

        new_records = [rec for rec in recs if rec.pid not in self._baseline_game_pids]
        if not new_records:
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "未发现本轮新启动的 Endfield，无法安全接管并在任务后关闭",
                {"pids": [rec.pid for rec in recs]},
            )
        for rec in new_records:
            claimed = self._pm.take_ownership(rec.pid)
            if claimed is None:
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    "无法接管本轮启动的 Endfield，已停止后续自动化",
                    {"pid": rec.pid},
                )
            self._owned_game_pids.add(rec.pid)

        # 进一步等待主窗口（window_regex=Endfield，已验证）
        title_regex = self._game_cfg.get("game_window_title_regex")
        if title_regex:
            wins = self._pm.wait_for_window(recs[0].pid, timeout_s, stop_event, title_regex=str(title_regex))
            if _stop_requested(stop_event):
                return StepResult(False, CODE_STOP_REQUESTED, "等待窗口时收到停止要求", {})
            if not wins:
                return StepResult(False, CODE_WAIT_TIMEOUT,
                                  f"等待 {name} 主窗口超时（title_regex={title_regex}）",
                                  {"timeout_s": timeout_s})
        return StepResult(True, CODE_OK, f"{name} 进程与主窗口已就绪",
                          {"pids": [r.pid for r in recs]})

    def start_helper(self) -> StepResult:
        """以 MXU 官方一次性运行参数启动当前活动实例。"""
        signal_ready, _reason, signal_details = self._completion_signal_state()
        if not signal_ready:
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "MaaEnd 一次性自动执行参数尚未通过配置检查",
                signal_details,
            )
        self._expected_task_count = int(signal_details.get("enabled_task_count", 0))
        existing = self._pm.find_by_executable(str(self._helper.get("process_name", "MaaEnd.exe")))
        if existing:
            rec = existing[0]
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "检测到现有 MaaEnd；无法确认其是否带一次性运行参数，已拒绝复用",
                {"reused_pid": rec.pid},
            )

        exe = self._helper.get("executable")
        if not exe or not Path(str(exe)).is_file():
            return StepResult(False, CODE_ENV_NOT_READY, "MaaEnd.exe 不存在", {})
        # 在触发新进程之前记录日志边界，防止 Windows 复用旧 PID 时把历史
        # Tasker 事件误当成本轮证据。
        self._capture_task_evidence_baseline()
        instance_name = signal_details.get("active_instance_name")
        command = [
            str(exe),
            "--autostart",
            "--instance",
            str(instance_name),
            "--quit-after-run",
        ]
        if bool(self._helper.get("requires_administrator", False)):
            start_task = getattr(self._pm, "start_scheduled_task", None)
            if not self._scheduled_helper_task_name or not callable(start_task):
                return StepResult(
                    False, CODE_ENV_NOT_READY, "MaaEnd 管理员任务不可用", {}
                )
            if not start_task(self._scheduled_helper_task_name):
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "无法启动 MaaEnd 管理员计划任务",
                    {"scheduled_task": self._scheduled_helper_task_name},
                )
            self._scheduled_helper_task_started = True
            helper_name = str(self._helper.get("process_name", "MaaEnd.exe"))
            start_timeout = float(
                self._helper.get("scheduled_task_start_timeout_s", 30.0)
            )
            records = list(
                self._pm.wait_for_process(helper_name, start_timeout, None)
            )
            if not records:
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "管理员计划任务已触发，但未发现 MaaEnd 进程",
                    {"timeout_s": start_timeout},
                )
            self._started_helper_pid = records[0].pid
            return StepResult(
                True,
                CODE_OK,
                "MaaEnd 已以管理员权限按指定实例启动",
                {
                    "pids": sorted(record.pid for record in records),
                    "scheduled_task": self._scheduled_helper_task_name,
                    "command": command,
                },
            )
        try:
            rec = self._pm.launch(command, cwd=str(Path(str(exe)).parent))
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"MaaEnd 启动失败：{exc}", {})
        self._started_helper_pid = rec.pid
        # 不占用 12701：仅记录提醒
        return StepResult(
            True, CODE_OK, "MaaEnd 已按指定实例启动，任务结束后将自动退出",
            {
                "pid": rec.pid,
                "command": command,
                "web_server_port_note": "MaaEnd 使用 127.0.0.1:12701，本程序不占用也不连接",
            },
        )

    def wait_completion(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        """等待由 --quit-after-run 触发的 MaaEnd 自退。"""
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
        signal_ready, _signal_reason, signal_details = self._completion_signal_state()
        if not signal_ready:
            return StepResult(
                False, CODE_COMPLETION_SIGNAL_UNSET,
                "MaaEnd 一次性运行完成信号不足",
                signal_details,
            )

        helper_name = str(self._helper.get("process_name", "MaaEnd.exe"))
        helper_recs = list(self._pm.find_by_executable(helper_name))
        helper_pids = {rec.pid for rec in helper_recs}
        if self._started_helper_pid is not None:
            # MaaEnd 可能在 start_helper 与本阶段之间极快完成；启动阶段记录的
            # PID 仍可用于 wait_for_exit（已退出时会立即返回）和日志归属。
            helper_pids.add(self._started_helper_pid)
        if not helper_pids:
            return StepResult(
                False, CODE_HELPER_EXITED_EARLY,
                "未找到 MaaEnd，无法确认任务是否曾进入运行态",
                signal_details,
            )

        started = time.monotonic()
        for helper_pid in sorted(helper_pids):
            remaining = max(0.0, timeout_s - (time.monotonic() - started))
            exited = self._pm.wait_for_exit(helper_pid, remaining, stop_event)
            if _stop_requested(stop_event):
                return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
            if not exited:
                return StepResult(
                    False,
                    CODE_WAIT_TIMEOUT,
                    "等待 MaaEnd 完成任务并自动退出超时",
                    {"timeout_s": remaining},
                )
        helper_pids = sorted(helper_pids)
        self._started_helper_pid = None
        if len(helper_pids) != 1:
            return StepResult(
                False,
                CODE_TASK_EVIDENCE_MISSING,
                "本轮发现多个 MaaEnd 进程，无法把任务日志唯一归属到本次运行",
                {"helper_pids": helper_pids},
            )
        expected_count = self._expected_task_count
        if expected_count <= 0:
            expected_count = int(signal_details.get("enabled_task_count", 0))
        return self._validate_task_evidence(helper_pids[0], expected_count)

    def cleanup(self) -> StepResult:
        """结束本轮启动的 MaaEnd，并关闭本轮明确接管的 Endfield。"""
        failed: list[int] = []
        ended: list[int] = []
        if self._scheduled_helper_task_started and self._scheduled_helper_task_name:
            stop_task = getattr(self._pm, "stop_scheduled_task", None)
            if callable(stop_task):
                stop_task(self._scheduled_helper_task_name)
        if self._started_helper_pid is not None:
            if self._scheduled_helper_task_started:
                ok = self._pm.wait_for_exit(self._started_helper_pid, 5.0, None)
                if not ok:
                    ok = self._pm.terminate_if_owned(self._started_helper_pid)
            else:
                ok = self._pm.terminate_if_owned(self._started_helper_pid)
            (ended if ok else failed).append(self._started_helper_pid)
            self._started_helper_pid = None
        if self._scheduled_game_task_started and self._scheduled_game_task_name:
            stop_task = getattr(self._pm, "stop_scheduled_task", None)
            if callable(stop_task):
                stop_task(self._scheduled_game_task_name)
        for pid in sorted(self._owned_game_pids):
            if self._scheduled_game_task_started:
                ok = self._pm.wait_for_exit(pid, 5.0, None)
                if not ok:
                    ok = self._pm.terminate_if_owned(pid)
            else:
                ok = self._pm.terminate_if_owned(pid)
            (ended if ok else failed).append(pid)
        self._scheduled_helper_task_started = False
        self._scheduled_game_task_started = False
        self._owned_game_pids.clear()
        self._baseline_game_pids.clear()
        if failed:
            return StepResult(
                False,
                "not_owned",
                "部分本轮进程已先行退出或无法按 owned 规则关闭；将由退出屏障复查",
                {"ended_pids": ended, "failed_pids": failed},
            )
        return StepResult(True, CODE_OK, "MaaEnd 与本轮 Endfield 清理完成", {"ended_pids": ended})

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #

    def _completion_signal_state(self) -> tuple[bool, str, dict[str, object]]:
        """只读确认官方一次性运行参数所需的活动 Win32 实例。"""
        cfg_dir = self._helper.get("config_dir")
        cfg_file = self._helper.get("config_file")
        details: dict[str, object] = {
            "active_instance_found": False,
            "active_instance_name": "",
            "win32_controller": False,
            "enabled_task_count": 0,
            "cli_autostart_verified": bool(self._helper.get("cli_autostart_verified", False)),
            "quit_after_run_verified": bool(self._helper.get("quit_after_run_verified", False)),
            "close_owned_game_after_tasks": bool(
                self._helper.get("close_owned_game_after_tasks", False)
            ),
        }
        if not (cfg_dir and cfg_file):
            return False, "mxu_configuration_unavailable", details
        path = Path(str(cfg_dir)) / str(cfg_file)
        if not path.is_file():
            return False, "mxu_configuration_unavailable", details
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False, "mxu_configuration_unavailable", details
        if not isinstance(data, dict):
            return False, "mxu_configuration_unavailable", details

        active_id = data.get("lastActiveInstanceId")
        if not isinstance(active_id, str) or not active_id:
            return False, "active_instance_unavailable", details
        instances = data.get("instances", [])
        if not isinstance(instances, list):
            return False, "active_instance_unavailable", details
        active_instance = next(
            (
                instance for instance in instances
                if isinstance(instance, dict) and instance.get("id") == active_id
            ),
            None,
        )
        if not isinstance(active_instance, dict):
            return False, "active_instance_unavailable", details
        details["active_instance_found"] = True
        instance_name = active_instance.get("name")
        if not isinstance(instance_name, str) or not instance_name.strip():
            return False, "active_instance_name_unavailable", details
        details["active_instance_name"] = instance_name
        controller = active_instance.get("controllerName")
        details["win32_controller"] = (
            isinstance(controller, str) and controller.lower().startswith("win32")
        )
        if not details["win32_controller"]:
            return False, "controller_unsupported", details
        tasks = active_instance.get("tasks", [])
        if not isinstance(tasks, list):
            return False, "active_instance_has_no_enabled_tasks", details
        enabled_task_count = 0
        for task in tasks:
            if not isinstance(task, dict) or not bool(task.get("enabled", True)):
                continue
            enabled_by_controller = task.get("enabledByController")
            if (
                isinstance(enabled_by_controller, dict)
                and controller in enabled_by_controller
                and not bool(enabled_by_controller.get(controller))
            ):
                continue
            enabled_task_count += 1
        details["enabled_task_count"] = enabled_task_count
        if enabled_task_count <= 0:
            return False, "active_instance_has_no_enabled_tasks", details
        if not (
            details["cli_autostart_verified"]
            and details["quit_after_run_verified"]
            and details["close_owned_game_after_tasks"]
        ):
            return False, "cli_or_cleanup_unverified", details
        return True, "ok", details

    def _task_evidence_log_path(self) -> Path:
        configured = self._helper.get("task_evidence_log")
        if configured:
            return Path(str(configured))
        executable = Path(str(self._helper.get("executable", "")))
        return executable.parent / "debug" / "maafw.log"

    def _task_evidence_log_files(self) -> list[Path]:
        """返回主日志及 MaaFramework 在同目录生成的轮转日志。"""
        main_log = self._task_evidence_log_path()
        rotated_pattern = f"{main_log.stem}.bak.*{main_log.suffix}"
        try:
            rotated = list(main_log.parent.glob(rotated_pattern))
        except OSError:
            rotated = []
        return sorted(
            {path for path in [*rotated, main_log] if path.is_file()},
            key=lambda path: str(path).casefold(),
        )

    def _capture_task_evidence_baseline(self) -> None:
        """记录启动前所有日志分片的字节边界，仅接受本轮新增事件。"""
        log_path = self._task_evidence_log_path()
        offsets: dict[tuple[int, int], int] = {}
        self._task_evidence_start_offset = 0
        self._task_evidence_file_identity = None
        for candidate in self._task_evidence_log_files():
            try:
                stat = candidate.stat()
            except OSError:
                continue
            identity = (stat.st_dev, stat.st_ino)
            offsets[identity] = stat.st_size
            if candidate == log_path:
                self._task_evidence_start_offset = stat.st_size
                self._task_evidence_file_identity = identity
        self._task_evidence_file_offsets = offsets

    def _validate_task_evidence(
        self,
        helper_pid: int,
        expected_count: int,
    ) -> StepResult:
        """核验本次 MaaEnd PID 的 MaaFramework 顶层任务事件。

        ``--quit-after-run`` 只证明进程退出，不能证明任务执行成功。MaaEnd 的
        ``PostStop`` 会把场景检查中止包装成顶层 ``Task.Succeeded``，因此还必须
        显式拒绝 ``__SceneCheckAbortPipeline``，并确认全部启用任务都进入终态。
        """
        log_path = self._task_evidence_log_path()
        base_details: dict[str, object] = {
            "helper_pid": helper_pid,
            "expected_task_count": expected_count,
            "task_log": str(log_path),
            "task_log_start_offset": self._task_evidence_start_offset,
        }
        if expected_count <= 0:
            return StepResult(
                False,
                CODE_TASK_EVIDENCE_MISSING,
                "MaaEnd 活动实例没有可核验的启用任务",
                base_details,
            )
        lines: list[str] = []
        files_read: list[dict[str, object]] = []
        try:
            candidates = self._task_evidence_log_files()
            if not candidates:
                raise FileNotFoundError(str(log_path))
            for candidate in candidates:
                current_stat = candidate.stat()
                current_identity = (current_stat.st_dev, current_stat.st_ino)
                start_offset = 0
                if self._task_evidence_file_offsets is not None:
                    start_offset = self._task_evidence_file_offsets.get(
                        current_identity, 0
                    )
                    if current_stat.st_size < start_offset:
                        # 日志被原地截断后，本文件全部属于新的日志周期。
                        start_offset = 0
                with candidate.open("rb") as fh:
                    fh.seek(start_offset)
                    lines.extend(
                        fh.read().decode("utf-8", errors="replace").splitlines()
                    )
                files_read.append({
                    "path": str(candidate),
                    "start_offset": start_offset,
                    "end_offset": current_stat.st_size,
                })
        except OSError as exc:
            return StepResult(
                False,
                CODE_TASK_EVIDENCE_MISSING,
                "MaaEnd 已退出，但无法读取本次 MaaFramework 任务日志",
                {**base_details, "log_error": type(exc).__name__},
            )
        base_details["task_log_files"] = files_read

        pid_marker = f"[Px{helper_pid}]"
        started: dict[str, str] = {}
        succeeded: dict[str, str] = {}
        failed: dict[str, str] = {}
        abort_detected = False
        for line in lines:
            if pid_marker not in line:
                continue
            if (
                "__SceneCheckAbortPipeline" in line
                and _ABORT_NODE_EVENT_RE.search(line) is not None
            ):
                abort_detected = True
            match = _TASK_EVENT_RE.search(line)
            if match is None:
                continue
            try:
                payload = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            task_id = str(payload.get("task_id", "")).strip()
            entry = str(payload.get("entry", "")).strip()
            if not task_id or not entry or entry.startswith("MaaTasker"):
                continue
            event = match.group(1)
            if event == "Starting":
                started[task_id] = entry
            elif event == "Succeeded":
                succeeded[task_id] = entry
            elif event == "Failed":
                failed[task_id] = entry

        terminal_ids = set(succeeded) | set(failed)
        missing_terminal_ids = sorted(set(started) - terminal_ids)
        terminal_without_start_ids = sorted(terminal_ids - set(started))
        details = {
            **base_details,
            "started_task_count": len(started),
            "succeeded_task_count": len(succeeded),
            "failed_task_count": len(failed),
            "started_entries": list(started.values()),
            "failed_entries": list(failed.values()),
            "missing_terminal_task_ids": missing_terminal_ids,
            "terminal_without_start_task_ids": terminal_without_start_ids,
            "abort_detected": abort_detected,
        }
        if abort_detected:
            return StepResult(
                False,
                CODE_TASK_ABORTED,
                "MaaEnd 场景检查中止了整个任务链；常见原因是 HDR 或显卡滤镜未关闭",
                details,
            )
        if len(started) < expected_count:
            return StepResult(
                False,
                CODE_TASK_INCOMPLETE,
                "MaaEnd 未实际启动全部启用任务，已拒绝把进程退出判定为成功",
                details,
            )
        if len(terminal_ids) < expected_count or missing_terminal_ids:
            return StepResult(
                False,
                CODE_TASK_INCOMPLETE,
                "MaaEnd 退出时仍有启用任务缺少完成或失败结果",
                details,
            )
        if not succeeded:
            return StepResult(
                False,
                CODE_TASK_FAILED,
                "MaaEnd 已尝试任务，但没有任何启用任务成功",
                details,
            )
        if failed:
            return StepResult(
                True,
                CODE_TASK_COMPLETED_WITH_FAILURES,
                f"MaaEnd 已执行全部启用任务，其中 {len(failed)} 项失败",
                details,
            )
        return StepResult(
            True,
            CODE_OK,
            "MaaEnd 全部启用任务均有成功证据并已自动退出",
            details,
        )


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


__all__ = ["MaaEndAdapter"]
