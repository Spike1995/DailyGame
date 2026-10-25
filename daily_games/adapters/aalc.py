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

    def __init__(self, *, game_cfg: Mapping[str, object], process_monitor, logger=None, global_options=None):
        super().__init__(
            game_id="limbus",
            display_name=str(game_cfg.get("display_name", "边狱巴士")),
            game_cfg=game_cfg,
            process_monitor=process_monitor,
            logger=logger,
            global_options=global_options,
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
        self._baseline_game_pids: set[int] = set()
        self._owned_game_pids: set[int] = set()
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
        # T-008 #2：本轮 Limbus 必须能被安全接管并纳入清理。放在信号检查
        # 之后，使配置侧诊断码不被监控器问题遮蔽（与 maa/maaend 一致）。
        if not callable(getattr(self._pm, "take_ownership", None)):
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                "进程监控器不支持安全接管本轮启动的 Limbus",
                details,
            )
        return StepResult(
            True, CODE_OK,
            ("AALC 环境检查通过" + ("；注意：" + "；".join(warnings) if warnings else "")),
            {**details, "warnings": warnings},
        )

    def start_game(self) -> StepResult:
        """以 Steam 协议启动；必要时可用可配置的备用方案（直接可执行文件）。

        启动前先记录基线游戏 PID，``wait_game_ready`` 据此安全接管本轮新出现
        的 Limbus 进程。Steam 协议无法直接拿到子进程 PID，但接管机制保证
        AALC 异常退出时 ``--exit 6`` 未关闭的游戏也能在 cleanup 中被收尾，
        避免残留进程触发下一轮启动屏障死锁。
        """
        self._baseline_game_pids = {
            rec.pid
            for rec in self._pm.find_by_executable(
                str(self._game_cfg.get("game_process_name", "LimbusCompany.exe")),
                expected_path=str(self._game_cfg.get("game_executable", "")),
            )
        }
        method = self._launch_cfg.get("method", "steam_protocol")
        if method == "steam_protocol":
            uri = str(self._launch_cfg.get("steam_uri", "steam://run/1973530"))
            # Steam 协议通过 shell 启动，交由 OS 处理（含空格路径安全）
            try:
                if os.name == "nt":
                    # 使用 os.startfile 最安全，不手工拼接
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
            # T-008 #2：fallback 直接启动的游戏同样登记为本轮归属，
            # 保证 AALC 异常退出时 cleanup 也能收尾。
            self._owned_game_pids.add(rec.pid)
            return StepResult(
                True, CODE_OK, "已以备用直接可执行文件启动 Limbus",
                {"method": "direct_executable", "pid": rec.pid, "prior_error": str(prior_exc) if prior_exc else None},
            )
        except OSError as exc:
            return StepResult(False, CODE_START_FAILED, f"备用启动失败：{exc}", {})

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        """等待本轮新启动的 Limbus 出现，并按精确路径与原生创建身份接管。

        Steam 协议拿不到子进程 PID，靠 ``start_game`` 记录的基线区分本轮
        实例。基线中的原有实例不接管；空结果、无本轮新 PID（且无已归属的
        fallback PID）、缺可信原生标识或任一接管失败都返回
        ``environment_not_ready``，不再返回 ok（T-008 #2）。
        """
        name = str(self._game_cfg.get("game_process_name", "LimbusCompany.exe"))
        game_path = str(self._game_cfg.get("game_executable", ""))
        recs = self._pm.wait_for_process(
            name,
            timeout_s,
            stop_event,
            expected_path=game_path,
        )
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待游戏进程时收到停止要求", {})
        if not recs:
            # 空结果与「快照全属基线」同属无本轮新 PID 的失败契约
            # （T-011 R2 教训），保留超时/无进程诊断，零接管。
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                f"等待窗口内未出现本轮新启动的 {name}（超时或未启动），无法安全接管并在清理时关闭",
                {"timeout_s": timeout_s, "pids": []},
            )
        # R4：缺接管接口必须按约定失败关闭，而非 AttributeError。
        take_ownership = getattr(self._pm, "take_ownership", None)
        if not callable(take_ownership):
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                "进程监控器不支持安全接管本轮启动的 Limbus",
                {"pids": [r.pid for r in recs]},
            )
        confirmed_pids: list[int] = []
        for rec in recs:
            if rec.pid in self._baseline_game_pids:
                # 基线中的原有实例从不接管（T-008 #2）。
                continue
            if rec.pid in self._owned_game_pids or rec.pid == self._started_game_pid:
                # 已归属（fallback 或此前接管）：视为本轮有效证据。
                confirmed_pids.append(rec.pid)
                continue
            if rec.native_create_time <= 0:
                # 缺原生创建标识无法证明是本轮实例：失败关闭（T-010 语义）。
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    f"无法取得 {name}（PID {rec.pid}）的可信创建标识，已停止后续自动化",
                    {"pid": rec.pid},
                )
            claimed = take_ownership(
                rec.pid,
                expected_path=game_path,
                expected_native_create_time=rec.native_create_time,
            )
            if claimed is None:
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    f"无法接管本轮启动的 {name}，已停止后续自动化",
                    {"pid": rec.pid},
                )
            self._owned_game_pids.add(rec.pid)
            if self._started_game_pid is None:
                self._started_game_pid = rec.pid
            confirmed_pids.append(rec.pid)
        if not confirmed_pids:
            # R1：快照里只有基线中的原有实例不算就绪——本轮没有任何新归属。
            return StepResult(
                False,
                CODE_ENV_NOT_READY,
                f"等待窗口内只有原有 Limbus 实例，未发现本轮新启动的 {name}",
                {
                    "pids": [r.pid for r in recs],
                    "baseline": sorted(self._baseline_game_pids),
                },
            )
        return StepResult(True, CODE_OK, f"{name} 已出现并完成接管", {"pids": [r.pid for r in recs]})

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
            helper_name = str(self._helper.get("process_name", "AALC.exe"))

            def _mark_helper_task_started() -> None:
                # /Run 成功即登记归属（T-008 #4）：等待超时/抛异常时 cleanup
                # 仍能精确 /End 本轮已触发的任务。
                self._scheduled_helper_task_started = True

            outcome = self._launch_helper_via_scheduled_task(
                task_name=self._scheduled_helper_task_name,
                helper_name=helper_name,
                helper_executable=str(exe),
                start_timeout_s=float(
                    self._helper.get("scheduled_task_start_timeout_s", 30.0)
                ),
                not_ready_message="AALC 管理员计划任务不可用",
                start_failed_message="无法启动 AALC 管理员计划任务",
                no_process_message="管理员计划任务已触发，但未发现 AALC 进程",
                on_task_started=_mark_helper_task_started,
            )
            if isinstance(outcome, StepResult):
                return outcome
            _status, records = outcome
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
        - 与 MAA 对齐：等待循环中重新快照，捕获原 PID 退出后被重启的新 PID，
          避免在 Limbus/AALC 自我重启时把单次快照的退出误判为完成。
        """
        one_shot_ready, one_shot_details = self._one_shot_command_state()
        if not one_shot_ready:
            return StepResult(
                False, CODE_COMPLETION_SIGNAL_UNSET,
                "AALC 官方一次性命令未验证，无法可靠判断完成",
                one_shot_details,
            )
        # R2：停止优先于快照、单边缺失判断与初始接管。
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})

        game_name = str(self._game_cfg.get("game_process_name", "LimbusCompany.exe"))
        helper_name = str(self._helper.get("process_name", "AALC.exe"))
        game_path = str(self._game_cfg.get("game_executable", ""))
        helper_path = str(self._helper.get("executable", ""))

        game_recs = self._pm.find_by_executable(
            game_name, expected_path=game_path
        )
        helper_recs = self._pm.find_by_executable(
            helper_name, expected_path=helper_path
        )
        if _stop_requested(stop_event):
            return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
        if not game_recs and not helper_recs:
            # T-008 #6（AALC）：双方极快退出。只有两侧都有本轮已知 PID，且
            # 全部确认退出，才按完成处理；停止优先（含快照之后、确认之后
            # 到达的停止，T-011 R1 教训）。任一侧缺少本轮已知 PID、仍存活
            # 或无法确认退出，都维持 helper_exited_early。
            known_game_pids = set(self._owned_game_pids)
            known_helper_pids: set[int] = set()
            if self._started_helper_pid is not None:
                known_helper_pids.add(self._started_helper_pid)
            if not known_game_pids or not known_helper_pids:
                if _stop_requested(stop_event):
                    return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
                return StepResult(
                    False, CODE_HELPER_EXITED_EARLY,
                    "双方进程均已不在快照中，且缺少本轮已知 PID，不能推断完成",
                    {
                        "known_game_pids": sorted(known_game_pids),
                        "known_helper_pids": sorted(known_helper_pids),
                    },
                )
            for pid in sorted(known_game_pids | known_helper_pids):
                if not self._pm.wait_for_exit(pid, 0, stop_event):
                    if _stop_requested(stop_event):
                        return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
                    return StepResult(
                        False, CODE_HELPER_EXITED_EARLY,
                        "双方进程均已不在快照中，但本轮已知 PID 未确认退出",
                        {"unconfirmed_pid": pid},
                    )
            # 停止请求在确认退出后到达时仍然优先，且不走成功重置。
            if _stop_requested(stop_event):
                return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
            self._reset_process_tracking()
            return StepResult(
                True, CODE_OK,
                "Limbus 与 AALC 皆已结束（start --exit 6 完成信号）",
                {
                    "game_pids": sorted(known_game_pids),
                    "helper_pids": sorted(known_helper_pids),
                },
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
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout_s)
        observed_game_pids = {rec.pid for rec in game_recs}
        observed_helper_pids = {rec.pid for rec in helper_recs}
        exited_pids: set[int] = set()
        # T-008 #2：进入等待前对初始快照做接管，任何失败立即结束等待。
        for rec in game_recs:
            failure = self._take_ownership_of_game(rec, game_path)
            if failure is not None:
                return StepResult(
                    False,
                    CODE_ENV_NOT_READY,
                    f"无法接管本轮启动的 {game_name}（PID {rec.pid}），已停止等待",
                    {"pid": rec.pid, "reason": failure},
                )

        while True:
            if _stop_requested(stop_event):
                return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})

            current_game = [
                rec for rec in self._pm.find_by_executable(
                    game_name, expected_path=game_path
                )
                if rec.pid not in exited_pids
            ]
            current_helper = [
                rec for rec in self._pm.find_by_executable(
                    helper_name, expected_path=helper_path
                )
                if rec.pid not in exited_pids
            ]
            observed_game_pids.update(rec.pid for rec in current_game)
            observed_helper_pids.update(rec.pid for rec in current_helper)
            # T-008 #2：运行中重新快照的新 Limbus 同样必须接管成功，
            # 不得忽略失败（含自我重启换 PID 的场景）。
            for rec in current_game:
                failure = self._take_ownership_of_game(rec, game_path)
                if failure is not None:
                    return StepResult(
                        False,
                        CODE_ENV_NOT_READY,
                        f"无法接管本轮启动的 {game_name}（PID {rec.pid}），已停止等待",
                        {"pid": rec.pid, "reason": failure},
                    )

            live_pids: set[int] = set()
            for rec in (*current_game, *current_helper):
                if self._pm.wait_for_exit(rec.pid, 0.0, stop_event):
                    exited_pids.add(rec.pid)
                else:
                    live_pids.add(rec.pid)

            if not live_pids and not current_game and not current_helper:
                # R2：快照/确认期间到达的停止请求优先，不走成功重置。
                if _stop_requested(stop_event):
                    return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
                # T-008 #9/R3：快照为空不代表本轮已知 PID 都已确认退出
                # （如路径查询丢失）；未确认的保留跟踪与诊断，不误判完成。
                known_pids = (
                    (observed_game_pids - self._baseline_game_pids)
                    | self._owned_game_pids
                    | observed_helper_pids
                )
                if self._started_helper_pid is not None:
                    known_pids.add(self._started_helper_pid)
                unconfirmed_pids = [
                    pid
                    for pid in sorted(known_pids - exited_pids)
                    if not self._pm.wait_for_exit(pid, 0, stop_event)
                ]
                if _stop_requested(stop_event):
                    return StepResult(False, CODE_STOP_REQUESTED, "等待完成时收到停止要求", {})
                if unconfirmed_pids:
                    return StepResult(
                        False, CODE_HELPER_EXITED_EARLY,
                        "快照已空，但本轮已知 PID 未确认退出",
                        {"unconfirmed_pids": unconfirmed_pids},
                    )
                # T-008 #9：确认双方退出后才清空跟踪，避免 cleanup 对已
                # 回收的 PID 再 terminate 制造假 not_owned。
                self._reset_process_tracking()
                return StepResult(
                    True,
                    CODE_OK,
                    "Limbus 与 AALC 皆已结束（start --exit 6 完成信号）",
                    {
                        "game_pids": sorted(observed_game_pids),
                        "helper_pids": sorted(observed_helper_pids),
                    },
                )

            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                running_names: list[str] = []
                if current_game:
                    running_names.append(game_name)
                if current_helper:
                    running_names.append(helper_name)
                return StepResult(
                    False,
                    CODE_WAIT_TIMEOUT,
                    f"等待 {'/'.join(sorted(set(running_names))) or 'Limbus/AALC'} 结束超时",
                    {
                        "timeout_s": timeout_s,
                        "running_processes": sorted(set(running_names)),
                        "game_pids": sorted(observed_game_pids),
                        "helper_pids": sorted(observed_helper_pids),
                    },
                )
            _time.sleep(min(self._completion_poll_interval(), remaining))

    def cleanup(self) -> StepResult:
        """结束本轮启动的 AALC，并关闭本轮明确接管的 Limbus。

        ``start --exit 6`` 正常情况下会同时关闭游戏与 AALC；这里只作为
        安全网，接管机制保证 AALC 异常退出时未关闭的 Limbus 也能被收尾，
        避免残留进程触发下一轮启动屏障死锁。既有进程与未接管的 Steam 游戏
        仍不会被强行结束。
        """
        ended: list[int] = []
        exited: list[int] = []
        unknown: list[int] = []
        task_stop_attempted = False
        task_stop_ok: bool | None = None
        if self._scheduled_helper_task_started and self._scheduled_helper_task_name:
            task_stop_attempted = True
            task_stop_ok = self._stop_scheduled_task_with_warning(
                self._scheduled_helper_task_name, kind="AALC"
            )
            stop_requested = bool(task_stop_ok)
        else:
            stop_requested = False
        if self._started_helper_pid is not None:
            pid = self._started_helper_pid
            if self._pm.terminate_if_owned(pid):
                ended.append(pid)
            elif self._pm.wait_for_exit(pid, 5.0, None):
                exited.append(pid)
            else:
                unknown.append(pid)
        elif self._scheduled_helper_task_started and not stop_requested:
            # 任务已触发但未发现助手进程：退出/身份未知。
            unknown.append(-1)
        # 清理本轮明确接管的 Limbus 进程（Steam 协议启动 + take_ownership 路径）。
        # 先终止；终止不可能时用 wait_for_exit 区分「已确认退出」与「未知」。
        for pid in sorted(self._owned_game_pids):
            if self._pm.terminate_if_owned(pid):
                ended.append(pid)
            elif self._pm.wait_for_exit(pid, self._cleanup_grace_timeout(), None):
                exited.append(pid)
            else:
                unknown.append(pid)
        # T-008 #9/R5：诊断先于重置保留进 details，不用清空集合冒充已退出。
        self._reset_process_tracking()
        if unknown:
            return StepResult(
                False,
                "not_owned",
                "部分本轮 AALC/Limbus 进程已先行退出或无法按 owned 规则关闭；将由退出屏障复查",
                {
                    "ended_pids": ended,
                    "exited_pids": exited,
                    "unknown_pids": unknown,
                    "task_stop_attempted": task_stop_attempted,
                    "task_stop_ok": task_stop_ok,
                },
            )
        return StepResult(
            True,
            CODE_OK,
            "清理完成",
            {
                "ended_pids": ended,
                "exited_pids": exited,
                "task_stop_attempted": task_stop_attempted,
                "task_stop_ok": task_stop_ok,
            },
        )

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #

    def _take_ownership_of_game(self, rec, game_path: str) -> str | None:
        """接管单个本轮 Limbus PID；成功返回 None，失败返回诊断原因。

        基线中的原有实例不接管也不失败（非本轮归属）；已归属（owned）或
        fallback 直接启动的 PID 跳过；监控器缺接口、缺可信原生标识或接管
        被拒一律失败关闭（T-008 #2 / R4）。
        """
        if rec.pid in self._baseline_game_pids:
            # R1：基线是用户原有实例，等待期间出现在快照里也不得交给
            # take_ownership。
            return None
        if rec.pid in self._owned_game_pids or rec.pid == self._started_game_pid:
            return None
        take_ownership = getattr(self._pm, "take_ownership", None)
        if not callable(take_ownership):
            return "monitor_unsupported"
        if rec.native_create_time <= 0:
            return "missing_native_identity"
        claimed = take_ownership(
            rec.pid,
            expected_path=game_path,
            expected_native_create_time=rec.native_create_time,
        )
        if claimed is None:
            return "take_ownership_rejected"
        self._owned_game_pids.add(rec.pid)
        return None

    def _reset_process_tracking(self) -> None:
        """统一清空本轮进程跟踪（T-008 #9）。

        仅在确认双方退出后返回 ok 之前，或 cleanup 收尾时调用；超时、停止、
        查询失败、仅一方退出时不得提前调用（诊断先于清空保留）。
        """
        self._started_helper_pid = None
        self._started_game_pid = None
        self._owned_game_pids.clear()
        self._baseline_game_pids.clear()
        self._scheduled_helper_task_started = False


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


__all__ = ["AalcAdapter"]
