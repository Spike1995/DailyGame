"""明日方舟 / MAA 适配器。

关键约束（依任务书）：
- 检测现有 MAA.exe；生产配置拒绝复用，避免漏触发 RunDirectly。
- 直接读取 gui.new.json 的 RunDirectly 与 PostActions，不使用坐标点击。
- PC 窗口绑定模式必须使用 ExitArknights；ExitEmulator 在该模式下不会关闭游戏。
- 只有 MAA 与游戏的全部已捕获 PID 都退出，才视为本轮完成。
- 不依赖目录名称判断 MAA 版本（已验证文件版本 6.16.5 与目录 v5.6.0 不一致）。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Mapping

from .base import (
    AdapterBase,
    CODE_ENV_NOT_READY,
    CODE_COMPLETION_SIGNAL_UNSET,
    CODE_HELPER_EXITED_EARLY,
    CODE_MANUAL_CONFIGURATION_REQUIRED,
    CODE_OK,
    CODE_START_FAILED,
    CODE_STOP_REQUESTED,
    CODE_WAIT_TIMEOUT,
    StepResult,
)


class MaaAdapter(AdapterBase):
    """明日方舟 → MAA 适配器。"""

    def __init__(self, *, game_cfg: Mapping[str, object], process_monitor, logger=None):
        super().__init__(
            game_id="arknights",
            display_name=str(game_cfg.get("display_name", "明日方舟")),
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
        self._helper_launch_time = 0.0
        self._owned_helper_pids: set[int] = set()
        self._scheduled_task_name = str(helper.get("scheduled_task_name", "")).strip()
        self._scheduled_task_started = False
        self._scheduled_game_task_name = str(
            self._launcher.get("scheduled_task_name", "")
        ).strip()
        self._scheduled_game_task_started = False

    def check_environment(self) -> StepResult:
        """检查启动器、游戏可执行文件与 MAA 可执行文件是否存在；并说明自动执行验证状态。"""
        problems: list[str] = []
        details: dict[str, object] = {}

        lex = self._launcher.get("executable")
        if not lex or not Path(str(lex)).is_file():
            problems.append("鹰角启动器可执行文件不存在")
        else:
            details["launcher_executable"] = lex

        if bool(self._launcher.get("requires_administrator", False)):
            details["game_administrator_task"] = (
                self._scheduled_game_task_name or "未配置"
            )
            task_matches = getattr(self._pm, "scheduled_task_matches", None)
            launcher_arguments = subprocess.list2cmdline(
                [str(value) for value in self._launcher.get("args", [])]
            )
            if not self._scheduled_game_task_name or not callable(task_matches):
                problems.append("鹰角启动器管理员计划任务不可用")
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
                problems.append("鹰角启动器管理员计划任务不存在或定义不匹配")
            details["start_button_automation_verified"] = bool(
                self._start_button_automation.get("verified", False)
            )
            if (
                bool(self._start_button_automation.get("enabled", False))
                and not bool(self._start_button_automation.get("verified", False))
            ):
                problems.append("鹰角启动器开始游戏按钮自动化未验证")

        game_exe = self._game_cfg.get("game_executable")
        if not game_exe or not Path(str(game_exe)).is_file():
            problems.append("明日方舟可执行文件不存在")
        else:
            details["game_executable"] = game_exe

        maa_exe = self._helper.get("executable")
        if not maa_exe or not Path(str(maa_exe)).is_file():
            problems.append("MAA.exe 不存在")
        else:
            details["maa_executable"] = maa_exe
            # 明确说明：不可依目录名判断版本
            details["version_note"] = "MAA 实际文件版本 6.16.5，与目录名 v5.6.0 不一致；不可依目录名判断版本"

        # 启动器参数验证状态
        launcher_verified = bool(self._launcher.get("verified", False))
        details["launcher_verified"] = launcher_verified

        signal_ready, signal_reason, signal_details = self._completion_signal_state()
        details.update(signal_details)

        if bool(self._helper.get("requires_administrator", False)):
            details["administrator_task"] = self._scheduled_task_name or "未配置"
            task_matches = getattr(self._pm, "scheduled_task_matches", None)
            if not self._scheduled_task_name or not callable(task_matches):
                problems.append("MAA 管理员计划任务不可用")
            elif maa_exe and not task_matches(
                self._scheduled_task_name,
                str(maa_exe),
                str(Path(str(maa_exe)).parent),
            ):
                problems.append("MAA 管理员计划任务不存在或定义不匹配")

        if problems:
            return StepResult(False, CODE_ENV_NOT_READY, "；".join(problems), details)
        if signal_reason == "maa_configuration_unavailable":
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                "无法读取 MAA 的 gui.new.json，不能验证自动执行与退出动作",
                details,
            )
        if signal_reason == "autorun_disabled":
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "MAA 的 RunDirectly 尚未开启，需先确认配置变更",
                details,
            )
        if not signal_ready:
            return StepResult(
                False,
                CODE_COMPLETION_SIGNAL_UNSET,
                "MAA 的 PostActions 必须同时包含 ExitArknights 与 ExitSelf",
                details,
            )
        return StepResult(True, CODE_OK, "MAA 环境与双进程退出信号检查通过", details)

    def start_game(self) -> StepResult:
        """通过鹰角启动器启动明日方舟。

        启动器参数未验证时，仍尝试启动（让用户观察），但会在返回中标记未验证，
        供编排器决定是否需要改用 UI Automation。
        """
        lex = self._launcher.get("executable")
        args = self._launcher.get("args", [])
        if not lex or not Path(str(lex)).is_file():
            return StepResult(False, CODE_ENV_NOT_READY, "启动器可执行文件不存在", {})
        try:
            from ..process_monitor import ProcessMonitor  # 仅类型提示用，实际使用注入的监控器
        except Exception:  # pragma: no cover
            pass

        command = [str(lex)] + [str(a) for a in args]
        if bool(self._launcher.get("requires_administrator", False)):
            start_task = getattr(self._pm, "start_scheduled_task", None)
            if not self._scheduled_game_task_name or not callable(start_task):
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    "鹰角启动器管理员任务不可用",
                    {},
                )
            if not start_task(self._scheduled_game_task_name):
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "无法启动鹰角启动器管理员计划任务",
                    {"scheduled_task": self._scheduled_game_task_name},
                )
            self._scheduled_game_task_started = True
            return StepResult(
                True,
                CODE_OK,
                (
                    "鹰角启动器与开始游戏按钮自动化已通过管理员计划任务启动"
                    if bool(self._start_button_automation.get("enabled", False))
                    else "鹰角启动器已通过管理员计划任务启动"
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
            ("启动器已启动（参数已验证）" if verified else "启动器已启动，但 --game 参数未经真机验证，需观察是否真的开始游戏"),
            {"pid": rec.pid, "command": command, "launcher_verified": verified},
        )

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        """等待 Arknights.exe 进程出现。"""
        name = str(self._game_cfg.get("game_process_name", "Arknights.exe"))
        recs = self._pm.wait_for_process(name, timeout_s, stop_event)
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待游戏进程时收到停止要求", {})
        if not recs:
            return StepResult(False, CODE_WAIT_TIMEOUT, f"等待 {name} 超时", {"timeout_s": timeout_s})
        return StepResult(True, CODE_OK, f"{name} 已出现", {"pids": [r.pid for r in recs]})

    def start_helper(self) -> StepResult:
        """启动新的 MAA.exe；自动执行完全由已验证的原生配置触发。"""
        signal_ready, signal_reason, signal_details = self._completion_signal_state()
        if signal_reason == "autorun_disabled":
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "MAA 的 RunDirectly 尚未开启，未启动 MAA",
                signal_details,
            )
        if not signal_ready:
            return StepResult(
                False,
                CODE_COMPLETION_SIGNAL_UNSET,
                "MAA 的双进程退出信号未配置完整，未启动 MAA",
                signal_details,
            )

        # RunDirectly 只在 MAA 启动时触发。无法证明现有进程带着本轮任务启动，
        # 因此生产配置禁止复用，避免把“MAA 已打开”误判成“日常已开始”。
        existing = self._pm.find_by_executable(str(self._helper.get("process_name", "MAA.exe")))
        if existing:
            rec = existing[0]
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "检测到现有 MAA；无法确认本轮自动任务已触发，已拒绝复用",
                {"reused_pid": rec.pid},
            )

        exe = self._helper.get("executable")
        if not exe or not Path(str(exe)).is_file():
            return StepResult(False, CODE_ENV_NOT_READY, "MAA.exe 不存在", {})

        if bool(self._helper.get("requires_administrator", False)):
            start_task = getattr(self._pm, "start_scheduled_task", None)
            if not self._scheduled_task_name or not callable(start_task):
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    "MAA 管理员启动任务不可用",
                    {},
                )
            requested_at = time.time()
            if not start_task(self._scheduled_task_name):
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "无法启动 MAA 管理员计划任务",
                    {"scheduled_task": self._scheduled_task_name},
                )
            self._scheduled_task_started = True
            helper_name = str(self._helper.get("process_name", "MAA.exe"))
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
                    "管理员计划任务已触发，但未发现 MAA 进程",
                    {
                        "scheduled_task": self._scheduled_task_name,
                        "timeout_s": start_timeout,
                    },
                )
            self._started_helper_pid = records[0].pid
            self._helper_launch_time = requested_at
            self._owned_helper_pids.clear()
            self._claim_helper_handoffs(records)
            # 即使系统不允许普通权限进程打开管理员 MAA 的终止句柄，
            # 仍须记录 PID，以便通过对应计划任务安全停止并等待退出。
            self._owned_helper_pids.update(record.pid for record in records)
            return StepResult(
                True,
                CODE_OK,
                "MAA 已以管理员权限启动并将自动执行任务",
                {
                    "pids": sorted(record.pid for record in records),
                    "scheduled_task": self._scheduled_task_name,
                },
            )

        try:
            rec = self._pm.launch([str(exe)], cwd=str(Path(str(exe)).parent))
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"MAA 启动失败：{exc}", {})
        self._started_helper_pid = rec.pid
        self._helper_launch_time = rec.create_time
        self._owned_helper_pids = {rec.pid}
        return StepResult(True, CODE_OK, "MAA 已启动并将自动执行任务", {"pid": rec.pid})

    def wait_completion(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        """等待 PostActions 关闭明日方舟与 MAA 的全部已捕获 PID。"""
        signal_ready, signal_reason, signal_details = self._completion_signal_state()
        if signal_reason == "autorun_disabled":
            return StepResult(
                False, CODE_MANUAL_CONFIGURATION_REQUIRED,
                "MAA 的 RunDirectly 尚未开启，无法等待自动任务",
                signal_details,
            )
        if not signal_ready:
            return StepResult(
                False, CODE_COMPLETION_SIGNAL_UNSET,
                "MAA 的 PostActions 未形成游戏与助手双退出信号",
                signal_details,
            )
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})

        game_name = str(self._game_cfg.get("game_process_name", "Arknights.exe"))
        helper_name = str(self._helper.get("process_name", "MAA.exe"))
        initial_game_recs = list(self._pm.find_by_executable(game_name))
        initial_helper_recs = list(self._pm.find_by_executable(helper_name))
        if not initial_game_recs or not initial_helper_recs:
            missing = "game" if not initial_game_recs else "helper"
            return StepResult(
                False,
                CODE_HELPER_EXITED_EARLY,
                "未同时找到明日方舟与 MAA，不能以单方消失推断完成",
                {"missing": missing},
            )

        deadline = time.monotonic() + max(0.0, timeout_s)
        exited_pids: set[int] = set()
        observed_game_pids = {rec.pid for rec in initial_game_recs}
        observed_helper_pids = {rec.pid for rec in initial_helper_recs}
        self._claim_helper_handoffs(initial_helper_recs)

        # 使用短轮询同时观察游戏和 MAA。MAA 的 PC 模式可能先退出普通权限
        # 启动进程，再生成管理员进程；只等待最初 PID 会误判任务完成。
        while True:
            if _stop_requested(stop_event):
                return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})

            game_recs = [
                rec for rec in self._pm.find_by_executable(game_name)
                if rec.pid not in exited_pids
            ]
            helper_recs = [
                rec for rec in self._pm.find_by_executable(helper_name)
                if rec.pid not in exited_pids
            ]
            self._claim_helper_handoffs(helper_recs)
            observed_game_pids.update(rec.pid for rec in game_recs)
            observed_helper_pids.update(rec.pid for rec in helper_recs)

            live_records: list[tuple[str, object]] = []
            for name, records in ((game_name, game_recs), (helper_name, helper_recs)):
                for record in records:
                    if self._pm.wait_for_exit(record.pid, 0.0, stop_event):
                        exited_pids.add(record.pid)
                    else:
                        live_records.append((name, record))

            # 再取一次快照，捕获旧 MAA 退出和管理员 MAA 出现之间的换 PID。
            new_game_recs = [
                rec for rec in self._pm.find_by_executable(game_name)
                if rec.pid not in exited_pids
            ]
            new_helper_recs = [
                rec for rec in self._pm.find_by_executable(helper_name)
                if rec.pid not in exited_pids
            ]
            self._claim_helper_handoffs(new_helper_recs)
            observed_game_pids.update(rec.pid for rec in new_game_recs)
            observed_helper_pids.update(rec.pid for rec in new_helper_recs)

            current_pids = {record.pid for _, record in live_records}
            current_pids.update(rec.pid for rec in new_game_recs)
            current_pids.update(rec.pid for rec in new_helper_recs)
            if not current_pids:
                self._reset_helper_tracking()
                return StepResult(
                    True,
                    CODE_OK,
                    "明日方舟与 MAA 均已退出",
                    {
                        "game_pids": sorted(observed_game_pids),
                        "helper_pids": sorted(observed_helper_pids),
                    },
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                running_names = sorted({name for name, _ in live_records})
                if new_game_recs:
                    running_names.append(game_name)
                if new_helper_recs:
                    running_names.append(helper_name)
                return StepResult(
                    False,
                    CODE_WAIT_TIMEOUT,
                    "等待明日方舟与 MAA 全部退出超时",
                    {
                        "timeout_s": timeout_s,
                        "running_processes": sorted(set(running_names)),
                        "helper_pids": sorted(observed_helper_pids),
                    },
                )
            time.sleep(min(0.2, remaining))

    def cleanup(self) -> StepResult:
        """只结束本程序启动的 MAA（如有）。现有 MAA 从不接管。"""
        pids = set(self._owned_helper_pids)
        if self._started_helper_pid is not None:
            pids.add(self._started_helper_pid)
        ended: list[int] = []
        failed: list[int] = []
        if self._scheduled_task_started and self._scheduled_task_name:
            stop_task = getattr(self._pm, "stop_scheduled_task", None)
            stop_requested = bool(
                callable(stop_task) and stop_task(self._scheduled_task_name)
            )
            for pid in sorted(pids):
                if self._pm.wait_for_exit(pid, 5.0, None):
                    ended.append(pid)
                elif self._pm.terminate_if_owned(pid):
                    ended.append(pid)
                else:
                    failed.append(pid)
            if not stop_requested and not pids:
                failed.append(-1)
        else:
            for pid in sorted(pids):
                ok = self._pm.terminate_if_owned(pid)
                (ended if ok else failed).append(pid)
        self._reset_helper_tracking()
        if failed:
            result = StepResult(
                False,
                "not_owned",
                "部分本轮 MAA 已先行退出或无法按 owned 规则关闭；将由退出屏障复查",
                {"ended_pids": ended, "failed_pids": failed},
            )
        elif ended:
            result = StepResult(
                True, CODE_OK, "已结束本程序启动的 MAA", {"ended_pids": ended}
            )
        else:
            result = StepResult(True, CODE_OK, "无需清理（本轮未启动 MAA）", {})

        if self._scheduled_game_task_started and self._scheduled_game_task_name:
            stop_task = getattr(self._pm, "stop_scheduled_task", None)
            if callable(stop_task):
                stop_task(self._scheduled_game_task_name)
        self._scheduled_game_task_started = False
        return result

    def _claim_helper_handoffs(self, records: list[object]) -> None:
        """安全接管本轮 MAA 提权重启后生成的新 PID。"""
        if self._started_helper_pid is None:
            return
        take_ownership = getattr(self._pm, "take_ownership", None)
        if not callable(take_ownership):
            return
        for record in records:
            if record.pid in self._owned_helper_pids:
                continue
            # 启动前的全局屏障已确认没有 MAA；创建时间再加一道保护，避免
            # 把明显早于本轮启动的外部进程标记为 owned。
            create_time = float(getattr(record, "create_time", 0.0) or 0.0)
            if (
                create_time > 0.0
                and self._helper_launch_time > 0.0
                and create_time < self._helper_launch_time - 1.0
            ):
                continue
            claimed = take_ownership(record.pid)
            if claimed is not None:
                self._owned_helper_pids.add(record.pid)

    def _reset_helper_tracking(self) -> None:
        self._started_helper_pid = None
        self._helper_launch_time = 0.0
        self._owned_helper_pids.clear()
        self._scheduled_task_started = False

    def _completion_signal_state(self) -> tuple[bool, str, dict[str, object]]:
        """读取 MAA 当前配置，验证原生自动执行与双退出动作。"""
        path = self._maa_config_path()
        details: dict[str, object] = {
            "configuration_found": False,
            "current_configuration_found": False,
            "run_directly": False,
            "exit_arknights": False,
            "exit_self": False,
        }
        if path is None or not path.is_file():
            return False, "maa_configuration_unavailable", details
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False, "maa_configuration_unavailable", details
        if not isinstance(data, dict):
            return False, "maa_configuration_unavailable", details
        details["configuration_found"] = True

        current = data.get("Current")
        configurations = data.get("Configurations", {})
        if not isinstance(current, str) or not isinstance(configurations, dict):
            return False, "maa_configuration_unavailable", details
        selected = configurations.get(current)
        if not isinstance(selected, dict):
            return False, "maa_configuration_unavailable", details
        gui = selected.get("Gui", {})
        if not isinstance(gui, dict):
            return False, "maa_configuration_unavailable", details
        details["current_configuration_found"] = True

        startup = gui.get("StartUpSettings", {})
        details["run_directly"] = (
            isinstance(startup, dict) and startup.get("RunDirectly") is True
        )
        raw_actions = gui.get("PostActions", "")
        if isinstance(raw_actions, str):
            actions = {part.strip() for part in raw_actions.split(",") if part.strip()}
        elif isinstance(raw_actions, list):
            actions = {str(part).strip() for part in raw_actions if str(part).strip()}
        else:
            actions = set()
        details["exit_arknights"] = "ExitArknights" in actions
        details["exit_self"] = "ExitSelf" in actions

        if not details["run_directly"]:
            return False, "autorun_disabled", details
        if not (details["exit_arknights"] and details["exit_self"]):
            return False, "post_actions_incomplete", details
        return True, "ok", details

    def _maa_config_path(self) -> Path | None:
        raw = self._helper.get("config_file")
        if isinstance(raw, str) and raw:
            path = Path(raw)
            if path.is_absolute():
                return path
            config_dir = self._helper.get("config_dir")
            if isinstance(config_dir, str) and config_dir:
                return Path(config_dir) / path
        exe = self._helper.get("executable")
        if isinstance(exe, str) and exe:
            return Path(exe).parent / "config" / "gui.new.json"
        return None


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


__all__ = ["MaaAdapter"]
