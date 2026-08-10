"""边狱巴士 / AALC 适配器。

关键约束（依任务书）：
- 以 Steam 协议启动游戏，直接可执行文件仅作为可配置的备用方案。
- 通过 AALC 官方 ``start --exit 6`` 一次性命令自动开始任务并退出双方。
- 预检要指出 game_path、一次性命令与游戏语言风险。
- 韩文界面不得静默视为兼容（用户已装汉化，预检仅提示列表值）。
- 完成判断不得仅以挂机程序消失推断成功。
"""

from __future__ import annotations

import os
import subprocess
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


class AalcAdapter(AdapterBase):
    """边狱巴士 → AALC 适配器。"""

    def __init__(self, *, game_cfg: Mapping[str, object], process_monitor, logger=None):
        super().__init__(
            game_id="limbus",
            display_name=str(game_cfg.get("display_name", "边狱巴士")),
            game_cfg=game_cfg,
            process_monitor=process_monitor,
            logger=logger,
        )
        self._launch_cfg = game_cfg.get("launch", {}) if isinstance(game_cfg.get("launch"), dict) else {}
        self._helper = game_cfg.get("helper", {}) if isinstance(game_cfg.get("helper"), dict) else {}
        self._language = game_cfg.get("language", {}) if isinstance(game_cfg.get("language"), dict) else {}
        self._scheduled_helper_task_name = str(
            self._helper.get("scheduled_task_name", "")
        ).strip()
        self._scheduled_helper_task_started = False
        self._started_helper_pid: int | None = None
        self._started_game_pid: int | None = None
        raw_cli_args = self._helper.get("cli_args", ())
        if isinstance(raw_cli_args, (list, tuple)):
            self._cli_args = tuple(str(value) for value in raw_cli_args)
        else:
            self._cli_args = ()

    def _one_shot_command_state(self) -> tuple[bool, dict[str, object]]:
        """核对 AALC 官方一次性命令及其双退出语义。"""
        expected = ("start", "--exit", "6")
        details: dict[str, object] = {
            "cli_args": list(self._cli_args),
            "expected_cli_args": list(expected),
            "cli_start_verified": bool(
                self._helper.get("cli_start_verified", False)
            ),
            "cli_exit_both_verified": bool(
                self._helper.get("cli_exit_both_verified", False)
            ),
        }
        ready = bool(
            self._cli_args == expected
            and details["cli_start_verified"]
            and details["cli_exit_both_verified"]
        )
        return ready, details

    def check_environment(self) -> StepResult:
        problems: list[str] = []
        details: dict[str, object] = {}
        warnings: list[str] = []

        # 备用可执行文件
        fallback = self._launch_cfg.get("fallback_executable")
        if not fallback or not Path(str(fallback)).is_file():
            problems.append("LimbusCompany.exe 备用可执行文件不存在")
        else:
            details["game_executable"] = fallback

        # AALC 可执行文件
        aalc_exe = self._helper.get("executable")
        if not aalc_exe or not Path(str(aalc_exe)).is_file():
            problems.append("AALC.exe 不存在")
        else:
            details["aalc_executable"] = aalc_exe

        if bool(self._helper.get("requires_administrator", False)):
            details["administrator_task"] = (
                self._scheduled_helper_task_name or "未配置"
            )
            task_matches = getattr(self._pm, "scheduled_task_matches", None)
            helper_arguments = subprocess.list2cmdline(list(self._cli_args))
            if not self._scheduled_helper_task_name or not callable(task_matches):
                problems.append("AALC 管理员计划任务不可用")
            elif aalc_exe and not task_matches(
                self._scheduled_helper_task_name,
                str(aalc_exe),
                str(Path(str(aalc_exe)).parent),
                helper_arguments,
            ):
                problems.append("AALC 管理员计划任务不存在或定义不匹配")

        # game_path 风险
        gp_current = self._helper.get("game_path_current")
        gp_correct = self._helper.get("game_path_correct")
        if gp_current and gp_correct and str(gp_current) != str(gp_correct):
            warnings.append(
                f"AALC game_path 指向 {gp_current}（跨行/缺右括号），与实际 {gp_correct} 不符"
            )
        details["game_path_current"] = gp_current
        details["game_path_correct"] = gp_correct
        details["game_path_verified"] = bool(self._helper.get("game_path_verified", False))

        # 官方一次性命令：不再依赖启动时可能被界面重置的 autostart / 完成动作。
        one_shot_ready, one_shot_details = self._one_shot_command_state()
        details.update(one_shot_details)

        # 语言风险：用户已确认汉化，仅提示列表值
        details["language_steam_manifest"] = self._language.get("steam_manifest_language")
        details["language_actual"] = self._language.get("actual_language", "未知")
        details["language_user_confirmed_hanization"] = bool(
            self._language.get("user_confirmed_hanization", False))
        if not self._language.get("verified") and not self._language.get("user_confirmed_hanization"):
            # 未确认汉化时才视为风险
            warnings.append(
                f"Steam 列表语言={self._language.get('steam_manifest_language')}，"
                f"AALC 仅支持中文/英文；需先确认实际游戏语言"
            )

        if problems:
            return StepResult(False, CODE_ENV_NOT_READY, "；".join(problems), details)
        if not one_shot_ready:
            return StepResult(
                False,
                CODE_COMPLETION_SIGNAL_UNSET,
                "AALC 官方一次性命令未验证为 start --exit 6，无法安全开始任务",
                details,
            )
        return StepResult(
            True, CODE_OK,
            ("AALC 环境检查通过" + ("；注意：" + "；".join(warnings) if warnings else "")),
            {**details, "warnings": warnings},
        )

    def start_game(self) -> StepResult:
        """以 Steam 协议启动；必要时可用可配置的备用方案（直接可执行文件）。"""
        method = self._launch_cfg.get("method", "steam_protocol")
        if method == "steam_protocol":
            uri = str(self._launch_cfg.get("steam_uri", "steam://run/1973530"))
            # Steam 协议通过 shell 启动，交由 OS 处理（含空格路径安全）
            try:
                if os.name == "nt":
                    # 使用 os.starton 最安全，不手工拼接
                    os.startfile(uri)  # type: ignore[attr-defined]
                    return StepResult(True, CODE_OK, "已通过 Steam 协议启动 Limbus",
                                      {"method": "steam_protocol", "uri": uri})
                else:  # pragma: no cover - 平台降级
                    subprocess.Popen(["xdg-open", uri], close_fds=True)
                    return StepResult(True, CODE_OK, "已通过 Steam 协议启动 Limbus",
                                      {"method": "steam_protocol", "uri": uri})
            except OSError as exc:
                # 退到备用
                return self._launch_fallback(exc)
        return self._launch_fallback(None)

    def _launch_fallback(self, prior_exc: Exception | None) -> StepResult:
        exe = self._launch_cfg.get("fallback_executable")
        if not exe or not Path(str(exe)).is_file():
            msg = "Steam 协议启动失败且备用可执行文件不存在"
            if prior_exc:
                msg += f"（{prior_exc}）"
            return StepResult(False, CODE_START_FAILED, msg, {})
        try:
            rec = self._pm.launch([str(exe)])
            self._started_game_pid = rec.pid
            return StepResult(
                True, CODE_OK, "已以备用直接可执行文件启动 Limbus",
                {"method": "direct_executable", "pid": rec.pid, "prior_error": str(prior_exc) if prior_exc else None},
            )
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"备用启动失败：{exc}", {})

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        name = str(self._game_cfg.get("game_process_name", "LimbusCompany.exe"))
        recs = self._pm.wait_for_process(name, timeout_s, stop_event)
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待游戏进程时收到停止要求", {})
        if not recs:
            return StepResult(False, CODE_WAIT_TIMEOUT, f"等待 {name} 超时", {"timeout_s": timeout_s})
        return StepResult(True, CODE_OK, f"{name} 已出现", {"pids": [r.pid for r in recs]})

    def start_helper(self) -> StepResult:
        one_shot_ready, one_shot_details = self._one_shot_command_state()
        if not one_shot_ready:
            return StepResult(
                False,
                CODE_COMPLETION_SIGNAL_UNSET,
                "AALC 官方一次性命令未验证为 start --exit 6",
                one_shot_details,
            )
        existing = self._pm.find_by_executable(str(self._helper.get("process_name", "AALC.exe")))
        if existing:
            rec = existing[0]
            return StepResult(
                False,
                CODE_MANUAL_CONFIGURATION_REQUIRED,
                "检测到现有 AALC；无法确认本轮 autostart 已触发，已拒绝复用",
                {"reused_pid": rec.pid},
            )

        exe = self._helper.get("executable")
        if not exe or not Path(str(exe)).is_file():
            return StepResult(False, CODE_ENV_NOT_READY, "AALC.exe 不存在", {})

        if bool(self._helper.get("requires_administrator", False)):
            start_task = getattr(self._pm, "start_scheduled_task", None)
            if not self._scheduled_helper_task_name or not callable(start_task):
                return StepResult(
                    False, CODE_ENV_NOT_READY, "AALC 管理员计划任务不可用", {}
                )
            if not start_task(self._scheduled_helper_task_name):
                return StepResult(
                    False,
                    CODE_START_FAILED,
                    "无法启动 AALC 管理员计划任务",
                    {"scheduled_task": self._scheduled_helper_task_name},
                )
            self._scheduled_helper_task_started = True
            helper_name = str(self._helper.get("process_name", "AALC.exe"))
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
                    "管理员计划任务已触发，但未发现 AALC 进程",
                    {"timeout_s": start_timeout},
                )
            self._started_helper_pid = records[0].pid
            return StepResult(
                True,
                CODE_OK,
                "AALC 已以管理员权限执行官方一次性任务命令",
                {
                    "pids": sorted(record.pid for record in records),
                    "scheduled_task": self._scheduled_helper_task_name,
                    **one_shot_details,
                },
            )
        try:
            rec = self._pm.launch(
                [str(exe), *self._cli_args],
                cwd=str(Path(str(exe)).parent),
            )
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"AALC 启动失败：{exc}", {})
        self._started_helper_pid = rec.pid
        return StepResult(
            True,
            CODE_OK,
            "AALC 已执行官方一次性任务命令",
            {"pid": rec.pid, **one_shot_details},
        )

    def wait_completion(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        """等待 AALC 完成。

        完成信号策略：
        - 官方 ``start --exit 6`` 命令已验证会在任务结束后退出游戏和 AALC。
        - LimbusCompany.exe 与 AALC.exe 皆结束才视为完成信号。
        - 不以单一进程消失推断成功。
        """
        one_shot_ready, one_shot_details = self._one_shot_command_state()
        if not one_shot_ready:
            return StepResult(
                False, CODE_COMPLETION_SIGNAL_UNSET,
                "AALC 官方一次性命令未验证，无法可靠判断完成",
                one_shot_details,
            )

        game_name = str(self._game_cfg.get("game_process_name", "LimbusCompany.exe"))
        helper_name = str(self._helper.get("process_name", "AALC.exe"))

        game_recs = self._pm.find_by_executable(game_name)
        helper_recs = self._pm.find_by_executable(helper_name)
        if not game_recs and not helper_recs:
            return StepResult(
                False, CODE_HELPER_EXITED_EARLY,
                "Limbus 与 AALC 进程皆未找到，无法判断完成",
                {},
            )
        if not game_recs:
            return StepResult(
                False, CODE_HELPER_EXITED_EARLY,
                "未找到 Limbus 进程，不能仅以 AALC 退出推断完成",
                {"missing": "game"},
            )
        if not helper_recs:
            return StepResult(
                False, CODE_HELPER_EXITED_EARLY,
                "未找到 AALC 进程，不能仅以 Limbus 退出推断完成",
                {"missing": "helper"},
            )

        # 两方 PID 均已捕获，等待双方结束才是完成信号。
        deadline_remaining = timeout_s
        import time as _time
        start = _time.monotonic()
        for name, recs in ((game_name, game_recs), (helper_name, helper_recs)):
            for rec in recs:
                elapsed = _time.monotonic() - start
                remaining = max(0.0, deadline_remaining - elapsed)
                exited = self._pm.wait_for_exit(rec.pid, remaining, stop_event)
                if _stop_requested(stop_event):
                    return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
                if not exited:
                    return StepResult(False, CODE_WAIT_TIMEOUT,
                                      f"等待 {name} 结束超时", {"timeout_s": remaining})
        return StepResult(
            True,
            CODE_OK,
            "Limbus 与 AALC 皆已结束（start --exit 6 完成信号）",
            {},
        )

    def cleanup(self) -> StepResult:
        """只结束本轮启动的 AALC；既有进程与 Steam 游戏均不接管。"""
        ended: list[int] = []
        failed: list[int] = []
        if self._scheduled_helper_task_started and self._scheduled_helper_task_name:
            stop_task = getattr(self._pm, "stop_scheduled_task", None)
            stop_requested = bool(
                callable(stop_task) and stop_task(self._scheduled_helper_task_name)
            )
        else:
            stop_requested = False
        if self._started_helper_pid is not None:
            if self._scheduled_helper_task_started:
                ok = self._pm.wait_for_exit(
                    self._started_helper_pid, 5.0, None
                )
                if not ok:
                    ok = self._pm.terminate_if_owned(self._started_helper_pid)
            else:
                ok = self._pm.terminate_if_owned(self._started_helper_pid)
            (ended if ok else failed).append(self._started_helper_pid)
            self._started_helper_pid = None
        elif self._scheduled_helper_task_started and not stop_requested:
            failed.append(-1)
        self._scheduled_helper_task_started = False
        if failed:
            return StepResult(
                False,
                "not_owned",
                "部分本轮 AALC 进程已先行退出或无法按计划任务安全关闭；将由退出屏障复查",
                {"ended_pids": ended, "failed_pids": failed},
            )
        return StepResult(True, CODE_OK, "清理完成", {"ended_pids": ended})


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


__all__ = ["AalcAdapter"]
