"""适配器单元测试。

使用假的 process_monitor 与临时配置，验证三个适配器的正常路径、未验证配置、
工具提前退出与完成信号不足等情况。不启动真实游戏或挂机工具。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock

from daily_games.adapters import build_adapters
from daily_games.adapters.aalc import AalcAdapter
from daily_games.adapters.base import (
    CODE_COMPLETION_SIGNAL_UNSET,
    CODE_ENV_NOT_READY,
    CODE_HELPER_EXITED_EARLY,
    CODE_MANUAL_CONFIGURATION_REQUIRED,
    CODE_OK,
    CODE_START_FAILED,
    CODE_STOP_REQUESTED,
    CODE_WAIT_TIMEOUT,
    GameAdapter,
    StepResult,
)
from daily_games.adapters.maa import MaaAdapter
from daily_games.adapters.maaend import MaaEndAdapter
from daily_games.process_monitor import ProcessRecord


class FakeProcessMonitor:
    """可控制的假监控器，便于测试各种情境。"""

    def __init__(self):
        self.launch_exc: Exception | None = None
        self.find_results: dict[str, list[ProcessRecord]] = {}
        self.find_sequences: dict[str, list[list[ProcessRecord]]] = {}
        self.wait_process_results: dict[str, list[ProcessRecord]] = {}
        self.wait_window_results: list[dict[str, object]] = []
        self.wait_exit_results: dict[int, bool] = {}
        self.wait_exit_calls: list[int] = []
        self.find_calls: list[tuple[str, str | None]] = []
        self.wait_process_calls: list[tuple[str, str | None]] = []
        self.terminated: list[int] = []
        self.launch_calls: list[tuple[list[str], str | None]] = []
        self.claimed: list[int] = []
        self.claim_calls: list[tuple[int, str | None, float | None]] = []
        self.native_claim_calls: list[tuple[int, str | None, int | None]] = []
        self.take_ownership_result = True
        self.scheduled_task_matches_result = True
        self.start_scheduled_task_result = True
        self.stop_scheduled_task_result = True
        self.scheduled_task_match_calls = []
        self.started_scheduled_tasks: list[str] = []
        self.stopped_scheduled_tasks: list[str] = []
        self._owned: set[int] = set()
        self._next_pid = 10000

    def set_find(self, name: str, recs: list[ProcessRecord]):
        self.find_results[name.lower()] = recs

    def set_find_sequence(self, name: str, values: list[list[ProcessRecord]]):
        self.find_sequences[name.lower()] = [list(value) for value in values]

    def set_wait_process(self, name: str, recs: list[ProcessRecord]):
        self.wait_process_results[name.lower()] = recs

    def set_wait_exit(self, pid: int, exited: bool):
        self.wait_exit_results[pid] = exited

    def find_by_executable(
        self, name: str, *, expected_path: str | None = None,
    ) -> list[ProcessRecord]:
        self.find_calls.append((name, expected_path))
        key = name.lower()
        sequence = self.find_sequences.get(key)
        if sequence:
            value = sequence[0]
            if len(sequence) > 1:
                sequence.pop(0)
            return list(value)
        return list(self.find_results.get(key, []))

    def launch(self, command, cwd=None) -> ProcessRecord:
        if self.launch_exc:
            raise self.launch_exc
        pid = self._next_pid
        self._next_pid += 1
        self._owned.add(pid)
        exe = command[0] if isinstance(command, list) else str(command)
        self.launch_calls.append((list(command), cwd))
        rec = ProcessRecord(pid=pid, executable=Path(exe).name, full_path=exe,
                            owned=True, create_time=0.0)
        return rec

    def take_ownership(
        self,
        pid: int,
        *,
        expected_path: str | None = None,
        expected_create_time: float | None = None,
        expected_native_create_time: int | None = None,
    ) -> ProcessRecord | None:
        self.claim_calls.append((pid, expected_path, expected_create_time))
        self.native_claim_calls.append(
            (pid, expected_path, expected_native_create_time)
        )
        if not self.take_ownership_result:
            return None
        records = [
            record
            for values in [
                *self.find_results.values(),
                *self.wait_process_results.values(),
                *(value for sequence in self.find_sequences.values() for value in sequence),
            ]
            for record in values
            if record.pid == pid
        ]
        if not records:
            return None
        source = records[0]
        rec = ProcessRecord(
            pid=source.pid,
            executable=source.executable,
            full_path=source.full_path,
            owned=True,
            create_time=source.create_time,
            native_create_time=source.native_create_time,
        )
        self._owned.add(pid)
        self.claimed.append(pid)
        return rec

    def wait_for_process(
        self, name, timeout_s, stop_event=None, *, expected_path=None,
    ):
        self.wait_process_calls.append((name, expected_path))
        return list(self.wait_process_results.get(name.lower(), []))

    def wait_for_window(self, pid, timeout_s, stop_event=None, title_regex=None):
        return list(self.wait_window_results)

    def scheduled_task_matches(
        self,
        name,
        executable,
        cwd,
        arguments="",
        expected_action_count=1,
        expected_wrapper_target="",
        expected_wrapper_executable="",
    ):
        self.scheduled_task_match_calls.append(
            (
                name,
                str(executable),
                str(cwd),
                arguments,
                expected_action_count,
                expected_wrapper_target,
                expected_wrapper_executable,
            )
        )
        return self.scheduled_task_matches_result

    def start_scheduled_task(self, name):
        self.started_scheduled_tasks.append(name)
        return self.start_scheduled_task_result

    def stop_scheduled_task(self, name):
        self.stopped_scheduled_tasks.append(name)
        return self.stop_scheduled_task_result

    def wait_for_exit(self, pid, timeout_s, stop_event=None) -> bool:
        self.wait_exit_calls.append(pid)
        exited = self.wait_exit_results.get(pid, True)
        if exited:
            self._owned.discard(pid)
        return exited

    def terminate_if_owned(self, pid, grace_s=5.0) -> bool:
        if pid in self._owned:
            self._owned.discard(pid)
            self.terminated.append(pid)
            return True
        return False

    def owned_pids(self) -> list[int]:
        return list(self._owned)


def _make_exe(path: Path):
    path.write_text("dummy")
    return str(path)


def _arknights_cfg(
    tmp: Path,
    *,
    run_directly: bool = False,
    post_actions: str = "ExitArknights, ExitSelf",
    **overrides,
) -> dict:
    launcher = _make_exe(tmp / "Launcher.exe")
    game = _make_exe(tmp / "Arknights.exe")
    maa = _make_exe(tmp / "MAA.exe")
    cfg_dir = tmp / "maa_config"
    cfg_dir.mkdir(exist_ok=True)
    gui_new = cfg_dir / "gui.new.json"
    gui_new.write_text(json.dumps({
        "Current": "Default",
        "Configurations": {
            "Default": {
                "Gui": {
                    "StartUpSettings": {"RunDirectly": run_directly},
                    "PostActions": post_actions,
                },
            },
        },
    }), encoding="utf-8")
    cfg = {
        "display_name": "明日方舟",
        "launcher": {"executable": launcher, "args": ["--game=arknights"], "verified": False},
        "game_executable": game,
        "game_process_name": "Arknights.exe",
        "helper": {
            "id": "maa", "executable": maa, "process_name": "MAA.exe",
            "reuse_existing": False,
            "config_dir": str(cfg_dir), "config_file": "gui.new.json",
            "use_coordinate_click": False,
        },
    }
    cfg.update(overrides)
    return cfg


def _endfield_cfg(
    tmp: Path,
    *,
    auto_run_on_launch: bool = True,
    controller_name: str = "Win32-Front",
    **overrides,
) -> dict:
    launcher = _make_exe(tmp / "Launcher.exe")
    game = _make_exe(tmp / "Endfield.exe")
    maaend = _make_exe(tmp / "MaaEnd.exe")
    cfg_dir = tmp / "maaend_config"
    cfg_dir.mkdir(exist_ok=True)
    mxu = cfg_dir / "mxu-MaaEnd.json"
    mxu.write_text(json.dumps({
        "instances": [{
            "id": "inst1",
            "name": "日常",
            "controllerName": controller_name,
            "tasks": [{"taskName": "VisitFriends", "enabled": True}],
        }],
        "settings": {"autoRunOnLaunch": auto_run_on_launch, "hotkeys": {"startTasks": "F10"}},
        "lastActiveInstanceId": "inst1",
    }), encoding="utf-8")
    cfg = {
        "display_name": "终末地",
        "launcher": {"executable": launcher, "args": ["--game=endfield"], "verified": False},
        "game_executable": game,
        "game_process_name": "Endfield.exe",
        "game_window_title_regex": "Endfield",
        "game_window_title_verified": True,
        "helper": {
            "id": "maaend", "executable": maaend, "process_name": "MaaEnd.exe",
            "reuse_existing": False,
            "config_dir": str(cfg_dir), "config_file": "mxu-MaaEnd.json",
            "instance_id": "inst1", "instance_name": "日常",
            "cli_autostart_verified": True,
            "quit_after_run_verified": True,
            "close_owned_game_after_tasks": True,
            "web_server_port": 12701, "web_server_port_do_not_use": True,
        },
    }
    cfg.update(overrides)
    return cfg


def _write_maaend_task_log(
    tmp: Path,
    pid: int,
    events: list[tuple[str, int, str]],
    *,
    abort: bool = False,
) -> Path:
    debug_dir = tmp / "debug"
    debug_dir.mkdir(exist_ok=True)
    lines: list[str] = []
    for index, (event, task_id, entry) in enumerate(events):
        details = json.dumps(
            {"entry": entry, "task_id": task_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        lines.append(
            f"[2026-08-10 13:00:{index:02d}.000][INF][Px{pid}][Tx1] "
            f"[msg=Tasker.Task.{event}] [details={details}]"
        )
    if abort:
        lines.append(
            f"[2026-08-10 13:00:59.000][INF][Px{pid}][Tx1] "
            "[msg=Node.Action.Succeeded] [details={\"name\":\"__SceneCheckAbortPipeline\"}]"
        )
    log_path = debug_dir / "maafw.log"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def _limbus_cfg(tmp: Path, **overrides) -> dict:
    game = _make_exe(tmp / "LimbusCompany.exe")
    aalc = _make_exe(tmp / "AALC.exe")
    cfg = {
        "display_name": "边狱巴士",
        "launch": {
            "method": "steam_protocol", "steam_uri": "steam://run/1973530",
            "fallback_method": "direct_executable",
            "fallback_executable": game, "fallback_verified": False,
        },
        "game_executable": game,
        "game_process_name": "LimbusCompany.exe",
        "language": {
            "steam_manifest_language": "koreana",
            "supported_by_helper": ["zh_cn", "en_us"],
            "actual_language": "zh_cn", "user_confirmed_hanization": True,
            "verified": True,
        },
        "helper": {
            "id": "aalc", "executable": aalc, "process_name": "AALC.exe",
            "reuse_existing": False, "config_file": str(tmp / "aalc.yaml"),
            "cli_args": ["start", "--exit", "6"],
            "cli_start_verified": True,
            "cli_exit_both_verified": True,
            "game_path_key": "game_path",
            "game_path_current": "C:\\old\\LimbusCompany.exe",
            "game_path_correct": str(game),
            "game_path_verified": False,
        },
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# build_adapters                                                              #
# --------------------------------------------------------------------------- #


class TestBuildAdapters(unittest.TestCase):
    def test_returns_three_fixed_keys(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = {"games": {
                "arknights": _arknights_cfg(tmp),
                "endfield": _endfield_cfg(tmp),
                "limbus": _limbus_cfg(tmp),
            }}
            pm = FakeProcessMonitor()
            adapters = build_adapters(cfg, pm)
            self.assertEqual(set(adapters.keys()), {"arknights", "endfield", "limbus"})
            for a in adapters.values():
                self.assertIsInstance(a, GameAdapter)

    def test_invalid_games_raises(self):
        with self.assertRaises(ValueError):
            build_adapters({"games": "不是字典"}, FakeProcessMonitor())


# --------------------------------------------------------------------------- #
# MaaAdapter                                                                  #
# --------------------------------------------------------------------------- #


class TestMaaAdapter(unittest.TestCase):
    def test_check_environment_requires_actual_run_directly(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = MaaAdapter(game_cfg=_arknights_cfg(tmp), process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertFalse(r.details["run_directly"])

    def test_check_environment_requires_both_post_actions(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True, post_actions="ExitSelf")
            r = MaaAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_COMPLETION_SIGNAL_UNSET)
            self.assertFalse(r.details["exit_arknights"])

    def test_check_environment_rejects_exit_emulator_for_pc_mode(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(
                tmp,
                run_directly=True,
                post_actions="ExitEmulator, ExitSelf",
            )
            r = MaaAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_COMPLETION_SIGNAL_UNSET)
            self.assertFalse(r.details["exit_arknights"])

    def test_check_environment_accepts_actual_dual_exit_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            r = MaaAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()
            self.assertTrue(r.ok)

    def test_check_environment_requires_matching_admin_task_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MAA-Admin",
            })
            pm = FakeProcessMonitor()
            pm.scheduled_task_matches_result = False

            result = MaaAdapter(game_cfg=cfg, process_monitor=pm).check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertIn("计划任务", result.message)

    def test_check_environment_requires_single_wrapper_game_task_for_button_automation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Arknights-Admin",
                "start_button_automation": {"enabled": True, "verified": True},
            })
            pm = FakeProcessMonitor()

            result = MaaAdapter(game_cfg=cfg, process_monitor=pm).check_environment()

            self.assertTrue(result.ok)
            self.assertEqual(pm.scheduled_task_match_calls[0][4], 1)
            self.assertEqual(
                pm.scheduled_task_match_calls[0][5], "Arknights.exe"
            )
            self.assertEqual(
                pm.scheduled_task_match_calls[0][6],
                str(cfg["game_executable"]),
            )

    def test_check_environment_rejects_unverified_button_automation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Arknights-Admin",
                "start_button_automation": {"enabled": True, "verified": False},
            })

            result = MaaAdapter(
                game_cfg=cfg,
                process_monitor=FakeProcessMonitor(),
            ).check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertIn("按钮自动化未验证", result.message)

    def test_check_environment_missing_maa(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp)
            Path(cfg["helper"]["executable"]).unlink()
            a = MaaAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_ENV_NOT_READY)

    def test_start_helper_disabled_autorun_does_not_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            a = MaaAdapter(game_cfg=_arknights_cfg(tmp), process_monitor=pm)
            r = a.start_helper()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertEqual(pm.launch_calls, [])

    def test_start_helper_rejects_existing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            rec = ProcessRecord(pid=1234, executable="MAA.exe",
                                full_path="C:\\MAA.exe", owned=False, create_time=0.0)
            pm.set_find("MAA.exe", [rec])
            a = MaaAdapter(
                game_cfg=_arknights_cfg(tmp, run_directly=True),
                process_monitor=pm,
            )
            r = a.start_helper()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertEqual(r.details.get("reused_pid"), 1234)

    def test_start_helper_uses_pre_registered_admin_task_without_direct_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MAA-Admin",
                "scheduled_task_start_timeout_s": 3,
            })
            pm = FakeProcessMonitor()
            elevated = ProcessRecord(
                pid=811, executable="MAA.exe",
                full_path="C:\\MAA.exe", owned=False, create_time=0.0,
            )
            pm.set_wait_process("MAA.exe", [elevated])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.start_helper()
            cleanup = adapter.cleanup()

            self.assertTrue(result.ok)
            self.assertEqual(pm.started_scheduled_tasks, ["DailyGames-MAA-Admin"])
            self.assertEqual(pm.launch_calls, [])
            self.assertEqual(pm.stopped_scheduled_tasks, ["DailyGames-MAA-Admin"])
            self.assertIn(
                ("MAA.exe", cfg["helper"]["executable"]),
                pm.wait_process_calls,
            )
            self.assertTrue(cleanup.ok)

    def test_wait_game_ready_requires_configured_game_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.set_wait_process("Arknights.exe", [ProcessRecord(
                pid=810,
                executable="Arknights.exe",
                full_path=cfg["game_executable"],
                owned=False,
                create_time=0.0,
                native_create_time=(1234 << 32) | 7,
            )])

            result = MaaAdapter(
                game_cfg=cfg,
                process_monitor=pm,
            ).wait_game_ready(1)

            self.assertTrue(result.ok)
            self.assertEqual(
                pm.wait_process_calls,
                [("Arknights.exe", cfg["game_executable"])],
            )
            # 接管同样使用配置的精确路径（T-008 #1 / T-010 原生标识语义）。
            self.assertEqual(
                pm.native_claim_calls,
                [(810, cfg["game_executable"], (1234 << 32) | 7)],
            )

    def test_wait_completion_disabled_autorun_returns_manual_required(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = MaaAdapter(game_cfg=_arknights_cfg(tmp), process_monitor=FakeProcessMonitor())
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)

    def test_wait_completion_rejects_only_helper_or_only_game(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            for present_name, pid in (("MAA.exe", 345), ("Arknights.exe", 346)):
                pm = FakeProcessMonitor()
                rec = ProcessRecord(
                    pid=pid,
                    executable=present_name,
                    full_path=f"C:\\{present_name}",
                    owned=False,
                    create_time=0.0,
                )
                pm.set_find(present_name, [rec])
                r = MaaAdapter(game_cfg=cfg, process_monitor=pm).wait_completion(timeout_s=1)
                self.assertFalse(r.ok)
                self.assertEqual(r.code, CODE_HELPER_EXITED_EARLY)
                self.assertEqual(pm.wait_exit_calls, [])

    def test_wait_completion_waits_for_every_game_and_helper_pid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            games = [
                ProcessRecord(pid=301 + i, executable="Arknights.exe",
                              full_path="C:\\Arknights.exe", owned=False, create_time=0.0)
                for i in range(2)
            ]
            helpers = [
                ProcessRecord(pid=401 + i, executable="MAA.exe",
                              full_path="C:\\MAA.exe", owned=False, create_time=0.0)
                for i in range(2)
            ]
            pm.set_find("Arknights.exe", games)
            pm.set_find("MAA.exe", helpers)
            r = MaaAdapter(
                game_cfg=_arknights_cfg(tmp, run_directly=True),
                process_monitor=pm,
            ).wait_completion(timeout_s=5)
            self.assertTrue(r.ok)
            self.assertEqual(pm.wait_exit_calls, [301, 302, 401, 402])

    def test_wait_completion_tracks_and_claims_admin_restart_pid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            adapter = MaaAdapter(
                game_cfg=_arknights_cfg(tmp, run_directly=True),
                process_monitor=pm,
            )
            started = adapter.start_helper()
            self.assertTrue(started.ok)
            stub_pid = started.details["pid"]
            game = ProcessRecord(
                pid=301, executable="Arknights.exe",
                full_path="C:\\Arknights.exe", owned=False, create_time=1.0,
            )
            stub = ProcessRecord(
                pid=stub_pid, executable="MAA.exe",
                full_path="C:\\MAA.exe", owned=True, create_time=1.0,
            )
            elevated = ProcessRecord(
                pid=401, executable="MAA.exe",
                full_path="C:\\MAA.exe", owned=False, create_time=2.0,
            )
            pm.set_find("Arknights.exe", [game])
            pm.set_find_sequence("MAA.exe", [[stub], [elevated], [elevated]])

            result = adapter.wait_completion(timeout_s=1)

            self.assertTrue(result.ok)
            self.assertEqual(pm.claimed, [401])
            self.assertEqual(result.details["helper_pids"], [401, stub_pid])
            self.assertIn(401, pm.wait_exit_calls)

    def test_timeout_cleanup_terminates_claimed_admin_restart_pid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            adapter = MaaAdapter(
                game_cfg=_arknights_cfg(tmp, run_directly=True),
                process_monitor=pm,
            )
            started = adapter.start_helper()
            stub_pid = started.details["pid"]
            game = ProcessRecord(
                pid=301, executable="Arknights.exe",
                full_path="C:\\Arknights.exe", owned=False, create_time=1.0,
            )
            stub = ProcessRecord(
                pid=stub_pid, executable="MAA.exe",
                full_path="C:\\MAA.exe", owned=True, create_time=1.0,
            )
            elevated = ProcessRecord(
                pid=402, executable="MAA.exe",
                full_path="C:\\MAA.exe", owned=False, create_time=2.0,
            )
            pm.set_find("Arknights.exe", [game])
            pm.set_find_sequence("MAA.exe", [[stub], [stub], [elevated]])
            pm.set_wait_exit(402, False)

            result = adapter.wait_completion(timeout_s=0)
            cleanup = adapter.cleanup()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_WAIT_TIMEOUT)
            self.assertEqual(pm.claimed, [402])
            self.assertIn(402, pm.terminated)
            self.assertFalse(cleanup.ok)
            self.assertEqual(cleanup.code, "not_owned")

    def test_wait_completion_timeout_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            game = ProcessRecord(pid=301, executable="Arknights.exe",
                                 full_path="C:\\Arknights.exe", owned=False, create_time=0.0)
            helper = ProcessRecord(pid=401, executable="MAA.exe",
                                   full_path="C:\\MAA.exe", owned=False, create_time=0.0)
            pm.set_find("Arknights.exe", [game])
            pm.set_find("MAA.exe", [helper])
            pm.set_wait_exit(301, False)
            r = MaaAdapter(
                game_cfg=_arknights_cfg(tmp, run_directly=True),
                process_monitor=pm,
            ).wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_WAIT_TIMEOUT)

    def test_start_game_reports_unverified_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            a = MaaAdapter(game_cfg=_arknights_cfg(tmp), process_monitor=pm)
            r = a.start_game()
            self.assertTrue(r.ok)
            self.assertFalse(r.details.get("launcher_verified"))

    def test_start_game_uses_pre_registered_admin_task_without_direct_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Arknights-Admin",
            })
            pm = FakeProcessMonitor()
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.start_game()
            cleanup = adapter.cleanup()

            self.assertTrue(result.ok)
            self.assertEqual(
                pm.started_scheduled_tasks,
                ["DailyGames-Arknights-Admin"],
            )
            self.assertEqual(pm.launch_calls, [])
            self.assertEqual(
                pm.stopped_scheduled_tasks,
                ["DailyGames-Arknights-Admin"],
            )
            self.assertTrue(cleanup.ok)

    def test_start_game_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            pm.launch_exc = OSError("boom")
            a = MaaAdapter(game_cfg=_arknights_cfg(tmp), process_monitor=pm)
            r = a.start_game()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_START_FAILED)


# --------------------------------------------------------------------------- #
# MaaAdapter 游戏归属与极快完成（T-008 #1、#6-MAA）                            #
# --------------------------------------------------------------------------- #


class TestMaaGameOwnership(unittest.TestCase):
    def _game_rec(self, pid=301, native=(1234 << 32) | 99,
                  full_path="C:\\Arknights.exe") -> ProcessRecord:
        return ProcessRecord(
            pid=pid, executable="Arknights.exe", full_path=full_path,
            owned=False, create_time=1.0, native_create_time=native,
        )

    def test_start_game_records_baseline_and_takeover_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.set_find("Arknights.exe", [self._game_rec(pid=300)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)

            self.assertTrue(adapter.start_game().ok)
            self.assertEqual(adapter._baseline_game_pids, {300})
            # 启动器拉起后：新 PID 301 出现（300 仍在，属于基线）。
            pm.set_wait_process("Arknights.exe", [
                self._game_rec(pid=300), self._game_rec(pid=301),
            ])
            ready = adapter.wait_game_ready(1)

            self.assertTrue(ready.ok)
            self.assertEqual(pm.claimed, [301])
            self.assertEqual(
                pm.native_claim_calls,
                [(301, cfg["game_executable"], (1234 << 32) | 99)],
            )
            self.assertEqual(adapter._owned_game_pids, {301})
            # cleanup 关闭本轮接管的 Arknights，不触碰基线中的原有实例。
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertIn(301, pm.terminated)
            self.assertNotIn(300, pm.terminated)

    def test_takeover_failure_blocks_helper_and_partial_ownership_goes_to_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            pm.set_wait_process("Arknights.exe", [
                self._game_rec(pid=301),
                self._game_rec(pid=302, native=(1234 << 32) | 200),
            ])
            real_take = pm.take_ownership

            def _reject_second(pid, **kwargs):
                if pid == 302:
                    return None
                return real_take(pid, **kwargs)

            pm.take_ownership = _reject_second

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(ready.details["pid"], 302)
            # 已接管的部分交给 cleanup 并成功关闭；被拒绝的 PID 绝不终止。
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertNotIn(302, pm.terminated)
            cleanup = adapter.cleanup()
            self.assertIn(301, pm.terminated)
            self.assertTrue(cleanup.ok)

    def test_takeover_rejection_fails_closed_without_ownership(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.take_ownership_result = False
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=301)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(adapter._owned_game_pids, set())
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])

    def test_missing_native_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=301, native=0)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(pm.claimed, [])
            self.assertEqual(adapter._owned_game_pids, set())

    def test_baseline_only_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.set_find("Arknights.exe", [self._game_rec(pid=300)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            # 重启场景：快照里只有基线中的原有实例，没有本轮新 PID。
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=300)])

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(pm.claimed, [])
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])

    def test_helper_launch_failure_cleanup_still_closes_owned_game(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MAA-Admin",
            })
            pm = FakeProcessMonitor()
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=301)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.wait_game_ready(1).ok)
            # 助手失败场景一：/Run 成功但始终没有 MAA 进程出现。
            self.assertFalse(adapter.start_helper().ok)

            cleanup = adapter.cleanup()

            # 已接管的游戏仍要关闭；本轮已触发的任务 /End 恰好一次。
            self.assertEqual(pm.terminated, [301])
            self.assertEqual(pm.stopped_scheduled_tasks, ["DailyGames-MAA-Admin"])
            self.assertTrue(cleanup.ok)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MAA-Admin",
            })
            pm = FakeProcessMonitor()
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=302)])
            pm.set_wait_process("MAA.exe", [ProcessRecord(
                pid=830, executable="MAA.exe", full_path="C:\\MAA.exe",
                owned=False, create_time=1.0,
            )])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.wait_game_ready(1).ok)
            self.assertTrue(adapter.start_helper().ok)
            # 助手失败场景二：MAA 停止失败（如先行退出/无法按 owned 关闭）。
            pm.stop_scheduled_task_result = False
            pm.wait_exit_results[adapter._started_helper_pid] = False

            cleanup = adapter.cleanup()

            # 即便 MAA 停止失败，本轮已接管的游戏仍被关闭并暴露 not_owned。
            self.assertEqual(pm.terminated, [302])
            self.assertFalse(cleanup.ok)
            self.assertEqual(cleanup.code, "not_owned")

    def test_empty_process_result_is_environment_not_ready(self):
        """R2：wait_for_process 返回空列表同属无本轮新 PID 的失败契约。

        与「快照全属基线」一致返回 environment_not_ready（非 wait_timeout），
        保留超时/无进程诊断，零接管；停止优先不被改变。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            pm.set_find("Arknights.exe", [self._game_rec(pid=300)])
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            pm.set_wait_process("Arknights.exe", [])

            ready = adapter.wait_game_ready(0)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(ready.details["timeout_s"], 0)
            self.assertEqual(pm.claimed, [])
            self.assertEqual(adapter._owned_game_pids, set())
            # 停止优先不被改变：预先置位的停止事件仍返回 stop_requested。
            stop = threading.Event()
            stop.set()
            ready = adapter.wait_game_ready(0, stop_event=stop)
            self.assertEqual(ready.code, CODE_STOP_REQUESTED)

    def test_monitor_without_take_ownership_is_rejected(self):
        """配置完好但监控器不支持接管时，check_environment 必须拒绝。"""

        class _NoTakeoverMonitor:
            def scheduled_task_matches(self, *args, **kwargs):
                return True

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=_NoTakeoverMonitor())

            result = adapter.check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertIn(
                "进程监控器不支持安全接管本轮启动的 Arknights",
                result.message,
            )

    def test_normal_completion_resets_tracking_and_cleanup_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _arknights_cfg(tmp, run_directly=True)
            pm = FakeProcessMonitor()
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            adapter.start_game()
            pm.set_find("Arknights.exe", [])
            pm.set_wait_process("Arknights.exe", [self._game_rec(pid=301)])
            self.assertTrue(adapter.wait_game_ready(1).ok)
            self.assertTrue(adapter.start_helper().ok)
            pm.set_find("Arknights.exe", [self._game_rec(pid=301)])
            pm.set_find("MAA.exe", [ProcessRecord(
                pid=adapter._started_helper_pid,
                executable="MAA.exe",
                full_path="C:\\MAA.exe",
                owned=True,
                create_time=1.0,
            )])

            result = adapter.wait_completion(timeout_s=1)

            self.assertTrue(result.ok)
            # 正常双退出后跟踪已清空：cleanup 无 terminate、无 /End。
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])
            self.assertEqual(pm.stopped_scheduled_tasks, [])


class _DelayedStopEvent:
    """前 ``calls_before_stop`` 次 is_set() 返回 False，之后恒 True。

    模拟在 wait_completion 执行期间（首次快照之后、关键出口之前）到达的
    停止请求：真实场景由其他线程在两次检查之间 set()，此处用时序替身
    确定性复现（T-011 R1）。
    """

    def __init__(self, calls_before_stop: int):
        self._remaining = calls_before_stop
        self._event = threading.Event()

    def is_set(self):
        if self._remaining > 0:
            self._remaining -= 1
            return False
        self._event.set()
        return True

    def set(self):
        self._event.set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)


class TestMaaFastCompletion(unittest.TestCase):
    """T-008 #6（MAA）：双方极快退出按完成处理。"""

    def _ready_adapter(self, tmp: Path):
        cfg = _arknights_cfg(tmp, run_directly=True)
        pm = FakeProcessMonitor()
        adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
        adapter.start_game()
        pm.set_find("Arknights.exe", [])
        pm.set_wait_process("Arknights.exe", [ProcessRecord(
            pid=301, executable="Arknights.exe", full_path="C:\\Arknights.exe",
            owned=False, create_time=1.0, native_create_time=(1234 << 32) | 99,
        )])
        self.assertTrue(adapter.wait_game_ready(1).ok)
        self.assertTrue(adapter.start_helper().ok)
        # 双方从快照中消失（极快退出）。
        pm.set_find("Arknights.exe", [])
        pm.set_find("MAA.exe", [])
        return adapter, pm

    def test_both_sides_exited_immediately_is_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            helper_pid = adapter._started_helper_pid

            result = adapter.wait_completion(timeout_s=1)

            self.assertTrue(result.ok)
            self.assertEqual(result.details["game_pids"], [301])
            self.assertIn(301, pm.wait_exit_calls)
            self.assertIn(helper_pid, pm.wait_exit_calls)
            # 成功重置后 cleanup 干净：无 terminate、无 /End。
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])
            self.assertEqual(pm.stopped_scheduled_tasks, [])

    def test_unknown_game_pid_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            adapter._owned_game_pids.clear()

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(pm.terminated, [])

    def test_unknown_helper_pid_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            adapter._started_helper_pid = None
            adapter._owned_helper_pids.clear()

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)

    def test_unconfirmed_exit_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            pm.set_wait_exit(301, False)

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(result.details["unconfirmed_pid"], 301)

    def test_one_side_missing_still_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            pm.set_find("Arknights.exe", [ProcessRecord(
                pid=301, executable="Arknights.exe", full_path="C:\\Arknights.exe",
                owned=False, create_time=1.0,
            )])

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(result.details["missing"], "helper")

    def test_stop_takes_priority_in_fast_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            pm.set_wait_exit(301, False)
            stop = threading.Event()
            stop.set()

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)

    def test_stop_after_snapshot_with_missing_game_pid_takes_priority(self):
        """R1 时序①：首次快照之后、缺 PID 出口之前到达的停止请求优先。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            adapter._owned_game_pids.clear()
            stop = _DelayedStopEvent(calls_before_stop=1)

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)

    def test_stop_after_exit_confirmation_returns_stop_without_success_reset(self):
        """R1 时序②：全部确认退出后到达的停止请求优先，不走成功重置。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm = self._ready_adapter(tmp)
            helper_pid = adapter._started_helper_pid
            stop = _DelayedStopEvent(calls_before_stop=1)

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            # 停止路径不走成功重置：跟踪保留，cleanup 仍尝试关闭双方
            # （替身中已确认退出的 PID 不再可 terminate → not_owned）。
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertEqual(adapter._started_helper_pid, helper_pid)
            cleanup = adapter.cleanup()
            self.assertFalse(cleanup.ok)
            self.assertEqual(cleanup.code, "not_owned")
            self.assertEqual(
                sorted(cleanup.details["failed_pids"]),
                sorted([301, helper_pid]),
            )


# --------------------------------------------------------------------------- #
# MaaEndAdapter                                                               #
# --------------------------------------------------------------------------- #


class TestMaaEndAdapter(unittest.TestCase):
    def test_check_environment_win32_does_not_require_android_close_game(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = MaaEndAdapter(game_cfg=_endfield_cfg(tmp),
                              process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertTrue(r.ok)
            self.assertTrue(r.details["win32_controller"])
            self.assertEqual(r.details["enabled_task_count"], 1)

    def test_check_environment_rejects_active_instance_without_enabled_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["tasks"][0]["enabled"] = False
            mxu_path.write_text(json.dumps(data), encoding="utf-8")

            result = MaaEndAdapter(
                game_cfg=cfg, process_monitor=FakeProcessMonitor()
            ).check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertEqual(result.details["enabled_task_count"], 0)

    def test_check_environment_validates_both_admin_task_definitions(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Endfield-Admin",
                "start_button_automation": {"enabled": True, "verified": True},
            })
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MaaEnd-Admin",
                "cli_args": [
                    "--autostart", "--instance", "日常", "--quit-after-run",
                ],
            })
            pm = FakeProcessMonitor()

            result = MaaEndAdapter(
                game_cfg=cfg,
                process_monitor=pm,
            ).check_environment()

            self.assertTrue(result.ok)
            self.assertEqual(
                [call[0] for call in pm.scheduled_task_match_calls],
                ["DailyGames-Endfield-Admin", "DailyGames-MaaEnd-Admin"],
            )
            self.assertEqual(
                pm.scheduled_task_match_calls[0][3],
                "--game=endfield",
            )
            self.assertEqual(
                pm.scheduled_task_match_calls[1][3],
                "--autostart --instance 日常 --quit-after-run",
            )
            self.assertEqual(pm.scheduled_task_match_calls[0][4], 1)
            self.assertEqual(
                pm.scheduled_task_match_calls[0][5], "Endfield.exe"
            )
            self.assertEqual(
                pm.scheduled_task_match_calls[0][6],
                str(cfg["game_executable"]),
            )
            self.assertEqual(pm.scheduled_task_match_calls[1][4], 1)
            self.assertEqual(pm.scheduled_task_match_calls[1][5], "")
            self.assertEqual(pm.scheduled_task_match_calls[1][6], "")

    def test_check_environment_rejects_unverified_button_automation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Endfield-Admin",
                "start_button_automation": {"enabled": True, "verified": False},
            })

            result = MaaEndAdapter(
                game_cfg=cfg,
                process_monitor=FakeProcessMonitor(),
            ).check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertIn("按钮自动化未验证", result.message)

    def test_check_environment_rejects_non_win32_controller(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = MaaEndAdapter(game_cfg=_endfield_cfg(tmp, controller_name="ADB"),
                              process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_ENV_NOT_READY)

    def test_start_helper_rejects_existing_unqualified_process(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            rec = ProcessRecord(pid=555, executable="MaaEnd.exe",
                                full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [rec])
            a = MaaEndAdapter(game_cfg=_endfield_cfg(tmp), process_monitor=pm)
            r = a.start_helper()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertEqual(r.details.get("reused_pid"), 555)

    def test_start_helper_uses_official_one_shot_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            cfg = _endfield_cfg(tmp)
            r = MaaEndAdapter(game_cfg=cfg, process_monitor=pm).start_helper()
            self.assertTrue(r.ok)
            self.assertEqual(
                pm.launch_calls[0][0][1:],
                ["--autostart", "--instance", "日常", "--quit-after-run"],
            )
            self.assertEqual(pm.launch_calls[0][1], str(tmp))

    def test_start_game_and_helper_use_scoped_admin_tasks_without_direct_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            cfg["launcher"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-Endfield-Admin",
            })
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MaaEnd-Admin",
                "scheduled_task_start_timeout_s": 3,
            })
            pm = FakeProcessMonitor()
            elevated = ProcessRecord(
                pid=812,
                executable="MaaEnd.exe",
                full_path="C:\\MaaEnd.exe",
                owned=False,
                create_time=0.0,
            )
            pm.set_wait_process("MaaEnd.exe", [elevated])
            adapter = MaaEndAdapter(game_cfg=cfg, process_monitor=pm)

            game_result = adapter.start_game()
            helper_result = adapter.start_helper()
            cleanup = adapter.cleanup()

            self.assertTrue(game_result.ok)
            self.assertTrue(helper_result.ok)
            self.assertEqual(
                pm.started_scheduled_tasks,
                ["DailyGames-Endfield-Admin", "DailyGames-MaaEnd-Admin"],
            )
            self.assertEqual(pm.launch_calls, [])
            self.assertEqual(
                pm.stopped_scheduled_tasks,
                ["DailyGames-MaaEnd-Admin", "DailyGames-Endfield-Admin"],
            )
            self.assertIn(
                ("MaaEnd.exe", cfg["helper"]["executable"]),
                pm.wait_process_calls,
            )
            self.assertTrue(cleanup.ok)

    def test_wait_completion_uses_maaend_self_exit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=900, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            _write_maaend_task_log(
                tmp,
                900,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
            )
            r = MaaEndAdapter(game_cfg=_endfield_cfg(tmp), process_monitor=pm).wait_completion(1)
            self.assertTrue(r.ok)
            self.assertEqual(pm.wait_exit_calls, [900])
            self.assertEqual(r.details["started_task_count"], 1)
            self.assertEqual(r.details["succeeded_task_count"], 1)

    def test_wait_completion_uses_started_pid_after_fast_exit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            _write_maaend_task_log(
                tmp,
                905,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
            )
            adapter = MaaEndAdapter(
                game_cfg=_endfield_cfg(tmp), process_monitor=pm
            )
            adapter._started_helper_pid = 905

            result = adapter.wait_completion(1)

            self.assertTrue(result.ok)
            self.assertEqual(pm.wait_exit_calls, [905])

    def test_wait_completion_ignores_events_before_start_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=906, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False,
                                   create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            log_path = _write_maaend_task_log(
                tmp,
                906,
                [
                    ("Starting", 100000001, "__SceneCheckAbortPipeline"),
                    ("Succeeded", 100000001, "__SceneCheckAbortPipeline"),
                ],
                abort=True,
            )
            adapter = MaaEndAdapter(
                game_cfg=_endfield_cfg(tmp), process_monitor=pm
            )
            adapter._capture_task_evidence_baseline()
            with log_path.open("a", encoding="utf-8") as fh:
                for event in ("Starting", "Succeeded"):
                    payload = json.dumps({
                        "entry": "VisitFriendsMain",
                        "task_id": 200000001,
                    }, ensure_ascii=False)
                    fh.write(
                        f"[2026-08-10 14:00:00.000][INF][Px906][Tx1]"
                        f"[Event][L1][notify] [msg=Tasker.Task.{event}] "
                        f"[details={payload}]\n"
                    )

            result = adapter.wait_completion(1)

            self.assertTrue(result.ok)
            self.assertFalse(result.details["abort_detected"])
            self.assertGreater(result.details["task_log_start_offset"], 0)

    def test_wait_completion_reads_events_across_log_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["tasks"].append(
                {"taskName": "DailyRewards", "enabled": True}
            )
            mxu_path.write_text(json.dumps(data), encoding="utf-8")
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=909, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False,
                                   create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            log_path = _write_maaend_task_log(
                tmp,
                909,
                [
                    ("Starting", 100000001, "__SceneCheckAbortPipeline"),
                    ("Succeeded", 100000001, "__SceneCheckAbortPipeline"),
                ],
                abort=True,
            )
            adapter = MaaEndAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._capture_task_evidence_baseline()
            with log_path.open("a", encoding="utf-8") as fh:
                for event in ("Starting", "Succeeded"):
                    details = json.dumps({
                        "entry": "VisitFriendsMain",
                        "task_id": 200000001,
                    }, separators=(",", ":"))
                    fh.write(
                        f"\n[2026-08-10 14:01:00.000][INF][Px909][Tx1] "
                        f"[msg=Tasker.Task.{event}] [details={details}]"
                    )
            log_path.rename(
                tmp / "debug" / "maafw.bak.2026.08.10-14.01.01.000.log"
            )
            _write_maaend_task_log(
                tmp,
                909,
                [
                    ("Starting", 200000002, "DailyRewardStart"),
                    ("Succeeded", 200000002, "DailyRewardStart"),
                ],
            )

            result = adapter.wait_completion(1)

            self.assertTrue(result.ok)
            self.assertFalse(result.details["abort_detected"])
            self.assertEqual(result.details["started_task_count"], 2)
            self.assertEqual(len(result.details["task_log_files"]), 2)

    def test_wait_completion_rejects_abort_disguised_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=901, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            _write_maaend_task_log(
                tmp,
                901,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
                abort=True,
            )

            result = MaaEndAdapter(
                game_cfg=_endfield_cfg(tmp), process_monitor=pm
            ).wait_completion(1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, "task_aborted")
            self.assertTrue(result.details["abort_detected"])

    def test_wait_completion_ignores_abort_node_resource_definition(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=908, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False,
                                   create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            log_path = _write_maaend_task_log(
                tmp,
                908,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
            )
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    "\n[2026-08-10 14:00:00.000][DBG][Px908][Tx1]"
                    "[PipelineParser.cpp][L188][parse_node] "
                    "[name=__SceneCheckAbortPipeline]\n"
                )

            result = MaaEndAdapter(
                game_cfg=_endfield_cfg(tmp), process_monitor=pm
            ).wait_completion(1)

            self.assertTrue(result.ok)
            self.assertFalse(result.details["abort_detected"])

    def test_wait_completion_rejects_partially_started_queue(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["tasks"].append(
                {"taskName": "DailyRewards", "enabled": True}
            )
            mxu_path.write_text(json.dumps(data), encoding="utf-8")
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=902, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            _write_maaend_task_log(
                tmp,
                902,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
            )

            result = MaaEndAdapter(game_cfg=cfg, process_monitor=pm).wait_completion(1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, "task_incomplete")
            self.assertEqual(result.details["expected_task_count"], 2)
            self.assertEqual(result.details["started_task_count"], 1)

    def test_wait_completion_requires_terminal_event_for_each_started_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["tasks"].append(
                {"taskName": "DailyRewards", "enabled": True}
            )
            mxu_path.write_text(json.dumps(data), encoding="utf-8")
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=907, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False,
                                   create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            _write_maaend_task_log(
                tmp,
                907,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DailyRewardStart"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000003, "UnrelatedTerminal"),
                ],
            )

            result = MaaEndAdapter(
                game_cfg=cfg, process_monitor=pm
            ).wait_completion(1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, "task_incomplete")
            self.assertEqual(
                result.details["missing_terminal_task_ids"], ["200000002"]
            )

    def test_wait_completion_accepts_all_attempted_with_partial_failures(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["tasks"].append(
                {"taskName": "DailyRewards", "enabled": True}
            )
            mxu_path.write_text(json.dumps(data), encoding="utf-8")
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=903, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            _write_maaend_task_log(
                tmp,
                903,
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Failed", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DailyRewardStart"),
                    ("Succeeded", 200000002, "DailyRewardStart"),
                ],
            )

            result = MaaEndAdapter(game_cfg=cfg, process_monitor=pm).wait_completion(1)

            self.assertTrue(result.ok)
            self.assertEqual(result.code, "task_completed_with_failures")
            self.assertEqual(result.details["started_task_count"], 2)
            self.assertEqual(result.details["failed_entries"], ["VisitFriendsMain"])

    def test_wait_completion_rejects_missing_task_log(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=904, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])

            result = MaaEndAdapter(
                game_cfg=_endfield_cfg(tmp), process_monitor=pm
            ).wait_completion(1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, "task_evidence_missing")

    def test_wait_completion_missing_helper_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            a = MaaEndAdapter(game_cfg=_endfield_cfg(tmp), process_monitor=pm)
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_HELPER_EXITED_EARLY)

    def test_wait_completion_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            helper = ProcessRecord(pid=900, executable="MaaEnd.exe",
                                   full_path="C:\\MaaEnd.exe", owned=False, create_time=0.0)
            pm.set_find("MaaEnd.exe", [helper])
            pm.set_wait_exit(900, False)  # 超时未结束
            a = MaaEndAdapter(game_cfg=_endfield_cfg(tmp),
                              process_monitor=pm)
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_WAIT_TIMEOUT)

    def test_check_environment_rejects_unverified_cli_or_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            cfg = _endfield_cfg(tmp)
            cfg["helper"]["quit_after_run_verified"] = False
            a = MaaEndAdapter(game_cfg=cfg, process_monitor=pm)
            r = a.check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)

    def test_check_environment_uses_actual_active_instance_not_helper_instance_id(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            mxu_path = Path(cfg["helper"]["config_dir"]) / cfg["helper"]["config_file"]
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"].append({
                "id": "active-without-close",
                "name": "other",
                "controllerName": "ADB",
                "tasks": [{"taskName": "VisitFriends", "enabled": True}],
            })
            data["lastActiveInstanceId"] = "active-without-close"
            mxu_path.write_text(json.dumps(data), encoding="utf-8")

            r = MaaEndAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()

            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_ENV_NOT_READY)
            self.assertFalse(r.details["win32_controller"])

    def test_check_environment_does_not_require_auto_run_setting(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp, auto_run_on_launch=False)
            r = MaaEndAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()
            self.assertTrue(r.ok)

    def test_wait_game_ready_claims_only_new_game_and_cleanup_closes_it(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _endfield_cfg(tmp)
            pm = FakeProcessMonitor()
            game = ProcessRecord(
                pid=777,
                executable="Endfield.exe",
                full_path="C:\\Endfield.exe",
                owned=False,
                create_time=0.0,
            )
            adapter = MaaEndAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            pm.set_wait_process("Endfield.exe", [game])
            pm.wait_window_results = [{"hwnd": 1, "title": "Endfield"}]
            self.assertTrue(adapter.wait_game_ready(1).ok)
            self.assertEqual(
                pm.wait_process_calls,
                [("Endfield.exe", cfg["game_executable"])],
            )
            self.assertEqual(pm.claimed, [777])
            self.assertEqual(
                pm.claim_calls,
                [(777, cfg["game_executable"], None)],
            )
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [777])


# --------------------------------------------------------------------------- #
# AalcAdapter                                                                 #
# --------------------------------------------------------------------------- #


class TestAalcAdapter(unittest.TestCase):
    def setUp(self):
        # R6：拦截 Steam/系统启动入口（本组虽不调 start_game，仍统一设防）。
        self._startfile_mock, self._popen_mock = _install_system_launch_guard(self)

    def test_check_environment_ok_with_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertTrue(r.ok)
            # game_path 不符应出现在 warnings
            self.assertTrue(any("game_path" in str(w) for w in r.details.get("warnings", [])))

    def test_check_environment_missing_aalc(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            Path(cfg["helper"]["executable"]).unlink()
            a = AalcAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor())
            r = a.check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_ENV_NOT_READY)

    def test_check_environment_requires_matching_admin_task_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-AALC-Admin",
            })
            pm = FakeProcessMonitor()
            pm.scheduled_task_matches_result = False

            r = AalcAdapter(game_cfg=cfg, process_monitor=pm).check_environment()

            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_ENV_NOT_READY)
            self.assertEqual(
                pm.scheduled_task_match_calls,
                [(
                    "DailyGames-AALC-Admin",
                    cfg["helper"]["executable"],
                    str(Path(cfg["helper"]["executable"]).parent),
                    "start --exit 6",
                    1,
                    "",
                    "",
                )],
            )

    def test_check_environment_wrong_one_shot_command_blocks_start(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"]["cli_args"] = ["start", "--exit", "5"]
            r = AalcAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).check_environment()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_COMPLETION_SIGNAL_UNSET)

    def test_start_helper_launches_when_not_existing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            cfg = _limbus_cfg(tmp)
            a = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            r = a.start_helper()
            self.assertTrue(r.ok)
            self.assertIn("pid", r.details)
            self.assertEqual(
                pm.launch_calls,
                [(
                    [cfg["helper"]["executable"], "start", "--exit", "6"],
                    str(Path(cfg["helper"]["executable"]).parent),
                )],
            )

    def test_start_helper_uses_admin_task_without_direct_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-AALC-Admin",
                "scheduled_task_start_timeout_s": 7,
            })
            pm = FakeProcessMonitor()
            record = ProcessRecord(
                pid=812,
                executable="AALC.exe",
                full_path=cfg["helper"]["executable"],
                owned=False,
                create_time=0.0,
            )
            pm.set_wait_process("AALC.exe", [record])
            a = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            r = a.start_helper()

            self.assertTrue(r.ok)
            self.assertEqual(pm.started_scheduled_tasks, ["DailyGames-AALC-Admin"])
            self.assertEqual(pm.launch_calls, [])
            self.assertEqual(r.details["pids"], [812])
            self.assertIn(
                ("AALC.exe", cfg["helper"]["executable"]),
                pm.wait_process_calls,
            )

            cleanup = a.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.stopped_scheduled_tasks, ["DailyGames-AALC-Admin"])
            self.assertIn(812, pm.wait_exit_calls)

    def test_start_helper_admin_task_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-AALC-Admin",
            })
            pm = FakeProcessMonitor()
            pm.start_scheduled_task_result = False

            r = AalcAdapter(game_cfg=cfg, process_monitor=pm).start_helper()

            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_START_FAILED)
            self.assertEqual(pm.started_scheduled_tasks, ["DailyGames-AALC-Admin"])
            self.assertEqual(pm.launch_calls, [])

    def test_start_helper_rejects_existing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            rec = ProcessRecord(pid=777, executable="AALC.exe",
                                full_path="C:\\AALC.exe", owned=False, create_time=0.0)
            pm.set_find("AALC.exe", [rec])
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            r = a.start_helper()
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_MANUAL_CONFIGURATION_REQUIRED)
            self.assertEqual(r.details.get("reused_pid"), 777)

    def test_wait_completion_both_exit_signals_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            game_rec = ProcessRecord(pid=800, executable="LimbusCompany.exe",
                                     full_path="C:\\LimbusCompany.exe", owned=False, create_time=0.0)
            aalc_rec = ProcessRecord(pid=801, executable="AALC.exe",
                                     full_path="C:\\AALC.exe", owned=False, create_time=0.0)
            pm.set_find("LimbusCompany.exe", [game_rec])
            pm.set_find("AALC.exe", [aalc_rec])
            pm.set_wait_exit(800, True)
            pm.set_wait_exit(801, True)
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            # 模拟 wait_game_ready 已完成本轮接管（T-008 #2 后初始快照的
            # 游戏 PID 必须已归属或可信任接管）。
            a._owned_game_pids.update({800})
            r = a.wait_completion(timeout_s=5)
            self.assertTrue(r.ok)

    def test_wait_completion_waits_for_every_captured_game_and_helper_pid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            game_recs = [
                ProcessRecord(pid=800, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe", owned=False, create_time=0.0),
                ProcessRecord(pid=802, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe", owned=False, create_time=0.0),
            ]
            helper_recs = [
                ProcessRecord(pid=801, executable="AALC.exe", full_path="C:\\AALC.exe", owned=False, create_time=0.0),
                ProcessRecord(pid=803, executable="AALC.exe", full_path="C:\\AALC.exe", owned=False, create_time=0.0),
            ]
            pm.set_find("LimbusCompany.exe", game_recs)
            pm.set_find("AALC.exe", helper_recs)
            for rec in [*game_recs, *helper_recs]:
                pm.set_wait_exit(rec.pid, True)

            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            # 模拟 wait_game_ready 已完成本轮接管。
            a._owned_game_pids.update({800, 802})
            r = a.wait_completion(timeout_s=5)

            self.assertTrue(r.ok)
            self.assertEqual(pm.wait_exit_calls, [800, 802, 801, 803])

    def test_wait_completion_game_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            game_rec = ProcessRecord(pid=800, executable="LimbusCompany.exe",
                                     full_path="C:\\LimbusCompany.exe", owned=False, create_time=0.0)
            aalc_rec = ProcessRecord(pid=801, executable="AALC.exe",
                                     full_path="C:\\AALC.exe", owned=False, create_time=0.0)
            pm.set_find("LimbusCompany.exe", [game_rec])
            pm.set_find("AALC.exe", [aalc_rec])
            pm.set_wait_exit(800, False)  # 游戏未结束 -> 超时
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            # 模拟 wait_game_ready 已完成本轮接管。
            a._owned_game_pids.update({800})
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_WAIT_TIMEOUT)

    def test_wait_completion_cli_exit_both_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            cfg = _limbus_cfg(tmp)
            cfg["helper"]["cli_exit_both_verified"] = False
            a = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_COMPLETION_SIGNAL_UNSET)

    def test_wait_completion_wrong_exit_index_returns_unset(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"]["cli_args"] = ["start", "--exit", "5"]
            r = AalcAdapter(game_cfg=cfg, process_monitor=FakeProcessMonitor()).wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_COMPLETION_SIGNAL_UNSET)

    def test_wait_completion_no_processes_found(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            # 不设 find 结果 -> 两者皆未找到
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            r = a.wait_completion(timeout_s=1)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, "helper_exited_early")

    def test_wait_completion_rejects_only_game_or_only_helper(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cases = {
                "only_game": ("LimbusCompany.exe", ProcessRecord(
                    pid=800, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe", owned=False, create_time=0.0,
                ), "helper"),
                "only_helper": ("AALC.exe", ProcessRecord(
                    pid=801, executable="AALC.exe", full_path="C:\\AALC.exe", owned=False, create_time=0.0,
                ), "game"),
            }
            for case, (name, rec, expected_missing) in cases.items():
                with self.subTest(case=case):
                    pm = FakeProcessMonitor()
                    pm.set_find(name, [rec])
                    r = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm).wait_completion(timeout_s=1)
                    self.assertFalse(r.ok)
                    self.assertEqual(r.code, CODE_HELPER_EXITED_EARLY)
                    self.assertEqual(r.details["missing"], expected_missing)

    def test_cleanup_terminates_started_helper(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            a.start_helper()
            r = a.cleanup()
            self.assertTrue(r.ok)
            self.assertEqual(len(pm.terminated), 1)

    def test_stop_event_during_wait_game_ready(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = FakeProcessMonitor()
            stop = threading.Event()
            stop.set()  # 预先触发
            cfg = _limbus_cfg(tmp)
            a = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            r = a.wait_game_ready(timeout_s=1, stop_event=stop)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_STOP_REQUESTED)
            self.assertEqual(
                pm.wait_process_calls,
                [("LimbusCompany.exe", cfg["game_executable"])],
            )


# --------------------------------------------------------------------------- #
# 计划任务归属时机（T-008 #4）                                                 #
# --------------------------------------------------------------------------- #


class _OrderTrackingMonitor(FakeProcessMonitor):
    """记录 /Run、wait_for_process 调用顺序及 wait 时刻的归属标志值。"""

    def __init__(self, flag_attr: str, holder: dict, order: list):
        super().__init__()
        self._flag_attr = flag_attr
        self._holder = holder
        self.order = order

    def start_scheduled_task(self, name):
        self.order.append(("run", None))
        return super().start_scheduled_task(name)

    def wait_for_process(self, name, timeout_s, stop_event=None, *, expected_path=None):
        adapter = self._holder.get("adapter")
        flag_value = getattr(adapter, self._flag_attr, None) if adapter else None
        self.order.append(("wait", flag_value))
        return super().wait_for_process(name, timeout_s, stop_event, expected_path=expected_path)


class TestScheduledTaskOwnershipTiming(unittest.TestCase):
    """T-008 #4：/Run 成功即登记计划任务归属。

    覆盖：/Run 成功但等待无进程 → cleanup 恰好一次 /End；等待抛异常 →
    同样恰好一次；/Run 失败 → 零 /End 且零标记；成功启动后清理 /End 不
    重复；并以上升到 wait_for_process 时的标志值证明「标记先于等待」。
    """

    def _make(self, name: str, tmp: Path):
        pm_holder: dict = {}
        flag_attrs = {
            "maa": "_scheduled_task_started",
            "maaend": "_scheduled_helper_task_started",
            "aalc": "_scheduled_helper_task_started",
        }
        order: list = []
        pm = _OrderTrackingMonitor(flag_attrs[name], pm_holder, order)
        if name == "maa":
            cfg = _arknights_cfg(tmp, run_directly=True)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MAA-Admin",
                "scheduled_task_start_timeout_s": 3,
            })
            adapter = MaaAdapter(game_cfg=cfg, process_monitor=pm)
            helper_name, task_name = "MAA.exe", "DailyGames-MAA-Admin"
        elif name == "maaend":
            cfg = _endfield_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-MaaEnd-Admin",
                "scheduled_task_start_timeout_s": 3,
            })
            adapter = MaaEndAdapter(game_cfg=cfg, process_monitor=pm)
            helper_name, task_name = "MaaEnd.exe", "DailyGames-MaaEnd-Admin"
        else:
            cfg = _limbus_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-AALC-Admin",
                "scheduled_task_start_timeout_s": 3,
            })
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            helper_name, task_name = "AALC.exe", "DailyGames-AALC-Admin"
        pm_holder["adapter"] = adapter
        return adapter, pm, helper_name, task_name, flag_attrs[name], order

    def test_run_success_then_wait_timeout_cleanup_ends_task_exactly_once(self):
        for name in ("maa", "maaend", "aalc"):
            with self.subTest(adapter=name):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    adapter, pm, helper_name, task_name, flag_attr, order = (
                        self._make(name, tmp)
                    )
                    # 等待超时：计划任务已触发，但始终没有进程出现。
                    result = adapter.start_helper()

                    self.assertFalse(result.ok)
                    self.assertEqual(result.code, CODE_START_FAILED)
                    self.assertTrue(getattr(adapter, flag_attr))
                    self.assertEqual(pm.started_scheduled_tasks, [task_name])

                    cleanup = adapter.cleanup()

                    self.assertTrue(cleanup.ok)
                    self.assertEqual(pm.stopped_scheduled_tasks, [task_name])

    def test_wait_process_exception_still_ends_task_exactly_once(self):
        for name in ("maa", "maaend", "aalc"):
            with self.subTest(adapter=name):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    adapter, pm, helper_name, task_name, flag_attr, order = (
                        self._make(name, tmp)
                    )

                    def _explode(*args, **kwargs):
                        raise RuntimeError("wait_for_process exploded")

                    pm.wait_for_process = _explode

                    with self.assertRaises(RuntimeError):
                        adapter.start_helper()

                    # 标志在 /Run 之后、等待之前已登记：异常路径下 cleanup
                    # 仍能精确 /End 本轮已触发的任务。
                    self.assertTrue(getattr(adapter, flag_attr))
                    cleanup = adapter.cleanup()
                    self.assertTrue(cleanup.ok)
                    self.assertEqual(pm.stopped_scheduled_tasks, [task_name])

    def test_run_failure_never_marks_and_never_ends_task(self):
        for name in ("maa", "maaend", "aalc"):
            with self.subTest(adapter=name):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    adapter, pm, helper_name, task_name, flag_attr, order = (
                        self._make(name, tmp)
                    )
                    pm.start_scheduled_task_result = False

                    result = adapter.start_helper()

                    self.assertFalse(result.ok)
                    self.assertEqual(result.code, CODE_START_FAILED)
                    self.assertFalse(getattr(adapter, flag_attr))
                    self.assertEqual(pm.started_scheduled_tasks, [task_name])
                    cleanup = adapter.cleanup()
                    self.assertTrue(cleanup.ok)
                    self.assertEqual(pm.stopped_scheduled_tasks, [])
                    self.assertEqual(pm.terminated, [])

    def test_successful_start_cleanup_ends_task_exactly_once(self):
        for name in ("maa", "maaend", "aalc"):
            with self.subTest(adapter=name):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    adapter, pm, helper_name, task_name, flag_attr, order = (
                        self._make(name, tmp)
                    )
                    pm.set_wait_process(helper_name, [ProcessRecord(
                        pid=820,
                        executable=helper_name,
                        full_path="C:\\elevated.exe",
                        owned=False,
                        create_time=0.0,
                    )])

                    result = adapter.start_helper()

                    self.assertTrue(result.ok)
                    self.assertTrue(getattr(adapter, flag_attr))
                    cleanup = adapter.cleanup()
                    self.assertTrue(cleanup.ok)
                    self.assertEqual(pm.stopped_scheduled_tasks, [task_name])

    def test_task_started_flag_is_registered_before_wait_for_process(self):
        for name in ("maa", "maaend", "aalc"):
            with self.subTest(adapter=name):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    adapter, pm, helper_name, task_name, flag_attr, order = (
                        self._make(name, tmp)
                    )
                    pm.set_wait_process(helper_name, [ProcessRecord(
                        pid=821,
                        executable=helper_name,
                        full_path="C:\\elevated.exe",
                        owned=False,
                        create_time=0.0,
                    )])

                    result = adapter.start_helper()

                    self.assertTrue(result.ok)
                    # wait_for_process 入口处读到的标志已为 True：标记发生在
                    # /Run 之后、等待之前。
                    self.assertEqual(order, [("run", None), ("wait", True)])


# --------------------------------------------------------------------------- #
# AalcAdapter 游戏归属、极快完成与跟踪重置（T-008 #2、#6-AALC、#9）            #
# --------------------------------------------------------------------------- #


def _install_system_launch_guard(testcase):
    """R6：拦截 AALC 测试中的 Steam/系统协议启动入口。

    真实 ``os.startfile``（Steam 协议）与 ``subprocess.Popen``（非 Windows
    降级）在守卫期间不可达；Steam 路径的被测语义（method/uri/details）仍可
    经注入边界断言。返回 (startfile_mock, popen_mock)。
    """
    startfile_patch = mock.patch.object(os, "startfile", create=True)
    popen_patch = mock.patch.object(subprocess, "Popen")
    startfile_mock = startfile_patch.start()
    popen_mock = popen_patch.start()
    testcase.addCleanup(startfile_patch.stop)
    testcase.addCleanup(popen_patch.stop)
    return startfile_mock, popen_mock


class TestAalcGameOwnership(unittest.TestCase):
    def setUp(self):
        # R6：AALC start_game 的 Steam 分支会调用真实系统入口，一律拦截。
        self._startfile_mock, self._popen_mock = _install_system_launch_guard(self)

    def _game_rec(self, pid=301, native=(2345 << 32) | 77,
                  full_path="C:\\LimbusCompany.exe") -> ProcessRecord:
        return ProcessRecord(
            pid=pid, executable="LimbusCompany.exe", full_path=full_path,
            owned=False, create_time=1.0, native_create_time=native,
        )

    def test_wait_game_ready_takeover_succeeds_with_path_and_native(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [self._game_rec(pid=300)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            self.assertEqual(adapter._baseline_game_pids, {300})
            pm.set_wait_process("LimbusCompany.exe", [
                self._game_rec(pid=300), self._game_rec(pid=301),
            ])

            ready = adapter.wait_game_ready(1)

            self.assertTrue(ready.ok)
            self.assertEqual(pm.claimed, [301])
            self.assertEqual(
                pm.native_claim_calls,
                [(301, cfg["game_executable"], (2345 << 32) | 77)],
            )
            self.assertEqual(adapter._owned_game_pids, {301})
            # cleanup 终止本轮归属的 301（终止不可能时才降级为确认退出）；
            # 基线 300 从不触碰。
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertIn(301, pm.terminated)
            self.assertNotIn(300, pm.terminated)
            self.assertNotIn(300, cleanup.details["ended_pids"])

    def test_wait_game_ready_takeover_rejection_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.take_ownership_result = False
            pm.set_wait_process("LimbusCompany.exe", [self._game_rec(pid=301)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(ready.details["pid"], 301)
            self.assertEqual(adapter._owned_game_pids, set())
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])

    def test_wait_game_ready_missing_native_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_wait_process("LimbusCompany.exe", [self._game_rec(pid=301, native=0)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(pm.claimed, [])
            self.assertEqual(adapter._owned_game_pids, set())

    def test_wait_game_ready_empty_result_is_environment_not_ready(self):
        """R2 教训（T-011）：空结果同属无本轮新 PID，返回 environment_not_ready。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [self._game_rec(pid=300)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            pm.set_wait_process("LimbusCompany.exe", [])

            ready = adapter.wait_game_ready(0)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(ready.details["timeout_s"], 0)
            self.assertEqual(pm.claimed, [])
            # 停止优先不被改变：预先置位的停止事件仍返回 stop_requested。
            stop = threading.Event()
            stop.set()
            ready = adapter.wait_game_ready(0, stop_event=stop)
            self.assertEqual(ready.code, CODE_STOP_REQUESTED)

    def test_wait_game_ready_partial_takeover_failure_goes_to_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_wait_process("LimbusCompany.exe", [
                self._game_rec(pid=301),
                self._game_rec(pid=302, native=(2345 << 32) | 500),
            ])
            real_take = pm.take_ownership

            def _reject_second(pid, **kwargs):
                if pid == 302:
                    return None
                return real_take(pid, **kwargs)

            pm.take_ownership = _reject_second
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(ready.details["pid"], 302)
            self.assertEqual(adapter._owned_game_pids, {301})
            # 已接管的部分交给 cleanup：301 被终止收尾；302 未归属不触碰。
            cleanup = adapter.cleanup()
            self.assertIn(301, pm.terminated)
            self.assertNotIn(302, pm.terminated)
            self.assertTrue(cleanup.ok)

    def test_fallback_launch_is_owned_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["launch"]["method"] = "direct_executable"
            pm = FakeProcessMonitor()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.start_game()

            self.assertTrue(result.ok)
            self.assertEqual(result.details["method"], "direct_executable")
            pid = result.details["pid"]
            # T-008 #2：fallback 直接启动的 PID 同样登记为本轮归属。
            self.assertIn(pid, adapter._owned_game_pids)
            pm.set_wait_process("LimbusCompany.exe", [ProcessRecord(
                pid=pid, executable="LimbusCompany.exe",
                full_path=cfg["launch"]["fallback_executable"],
                owned=True, create_time=0.0,
            )])
            ready = adapter.wait_game_ready(1)
            self.assertTrue(ready.ok)
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [pid])
            self.assertEqual(cleanup.details["ended_pids"], [pid])

    def test_retry_refreshes_baseline_and_does_not_reuse_old_pids(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [self._game_rec(pid=300)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            self.assertEqual(adapter._baseline_game_pids, {300})
            # 重试：重新记录基线（替换而非合并），旧 PID 不被沿用。
            pm.set_find("LimbusCompany.exe", [self._game_rec(pid=400)])
            self.assertTrue(adapter.start_game().ok)
            self.assertEqual(adapter._baseline_game_pids, {400})

    def test_wait_game_ready_baseline_only_is_not_success(self):
        """R1：快照只有基线中的原有实例（无 fallback 归属）时不算就绪。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [self._game_rec(pid=300)])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_game().ok)
            pm.set_wait_process("LimbusCompany.exe", [self._game_rec(pid=300)])

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertEqual(pm.claimed, [])
            self.assertEqual(adapter._owned_game_pids, set())
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])

    def test_wait_completion_protects_baseline_pids_in_both_takeover_spots(self):
        """R1：初始与循环两处接管都不得把基线实例交给 take_ownership。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [
                self._game_rec(pid=300),
                self._game_rec(pid=301, native=(2345 << 32) | 77),
            ])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._baseline_game_pids = {300}
            adapter._owned_game_pids = {301}
            adapter._started_helper_pid = 830
            # 与适配器视角一致：301 已被接管（可终止），830/300 未接管。
            pm._owned.add(301)
            # 循环内重新快照仍同时包含基线 300 与本轮归属的 301。
            pm.set_find_sequence("LimbusCompany.exe", [
                [self._game_rec(pid=300), self._game_rec(pid=301)],
                [self._game_rec(pid=300), self._game_rec(pid=301)],
            ])
            pm.set_wait_exit(300, False)
            pm.set_wait_exit(301, False)
            pm.set_wait_exit(830, False)

            result = adapter.wait_completion(timeout_s=0)

            self.assertEqual(result.code, CODE_WAIT_TIMEOUT)
            # 两处接管都保护基线：300 从未被交给 take_ownership。
            self.assertEqual(
                [pid for pid, _path, _native in pm.native_claim_calls],
                [],
            )
            self.assertEqual(pm.claimed, [])
            cleanup = adapter.cleanup()
            # cleanup 只关闭本轮归属的 301，不触碰基线 300。
            self.assertIn(301, pm.terminated)
            self.assertNotIn(300, pm.terminated)

    def test_wait_game_ready_missing_interface_fails_closed(self):
        """R4：ready 处缺接管接口按 environment_not_ready 失败关闭。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_wait_process("LimbusCompany.exe", [self._game_rec(pid=301)])
            pm.take_ownership = None  # 缺接口
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            ready = adapter.wait_game_ready(1)

            self.assertFalse(ready.ok)
            self.assertEqual(ready.code, CODE_ENV_NOT_READY)
            self.assertIn("不支持安全接管", ready.message)
            self.assertEqual(pm.claimed, [])

    def test_cleanup_details_record_task_stop_result(self):
        """R5：任务停止的尝试与结果进入 details，/End 成败可区分。"""
        for stop_ok in (True, False):
            with self.subTest(stop_ok=stop_ok):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    cfg = _limbus_cfg(tmp)
                    cfg["helper"].update({
                        "requires_administrator": True,
                        "scheduled_task_name": "DailyGames-AALC-Admin",
                    })
                    pm = FakeProcessMonitor()
                    pm.set_wait_process("AALC.exe", [ProcessRecord(
                        pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                        owned=False, create_time=1.0,
                    )])
                    pm.stop_scheduled_task_result = stop_ok
                    pm.set_wait_exit(830, True)
                    adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
                    self.assertTrue(adapter.start_helper().ok)

                    cleanup = adapter.cleanup()

                    self.assertTrue(cleanup.details["task_stop_attempted"])
                    self.assertEqual(
                        cleanup.details["task_stop_ok"], stop_ok,
                    )
                    self.assertEqual(cleanup.details["exited_pids"], [830])

    def test_cleanup_details_separate_exited_from_unknown(self):
        """R5：确认已退出与身份/退出未知分列，不得混成同一列表。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            cfg["helper"].update({
                "requires_administrator": True,
                "scheduled_task_name": "DailyGames-AALC-Admin",
            })
            pm = FakeProcessMonitor()
            pm.set_wait_process("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            self.assertTrue(adapter.start_helper().ok)
            # 助手已确认退出；已归属游戏 PID 终止失败且未确认退出
            # （身份/退出未知），二者分开记录。
            pm.set_wait_exit(830, True)
            pm.set_wait_exit(301, False)
            adapter._owned_game_pids = {301}
            pm._owned.discard(301)

            cleanup = adapter.cleanup()

            self.assertFalse(cleanup.ok)
            self.assertEqual(cleanup.code, "not_owned")
            self.assertEqual(cleanup.details["exited_pids"], [830])
            self.assertEqual(cleanup.details["unknown_pids"], [301])
            self.assertNotIn(301, cleanup.details["exited_pids"])

    def test_monitor_without_take_ownership_is_rejected(self):
        class _NoTakeoverMonitor:
            def scheduled_task_matches(self, *args, **kwargs):
                return True

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter = AalcAdapter(
                game_cfg=_limbus_cfg(tmp), process_monitor=_NoTakeoverMonitor(),
            )

            result = adapter.check_environment()

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertIn(
                "进程监控器不支持安全接管本轮启动的 Limbus",
                result.message,
            )


class TestAalcFastCompletion(unittest.TestCase):
    """T-008 #6-AALC 与 #9：极快完成与跟踪重置。"""

    def setUp(self):
        # R6：拦截 Steam/系统启动入口，防止 start_game 真实拉起游戏。
        self._startfile_mock, self._popen_mock = _install_system_launch_guard(self)


    def _ready_adapter(self, tmp: Path):
        cfg = _limbus_cfg(tmp)
        pm = FakeProcessMonitor()
        pm.set_find("LimbusCompany.exe", [ProcessRecord(
            pid=300, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
            owned=False, create_time=1.0,
        )])
        adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
        adapter.start_game()
        pm.set_find("LimbusCompany.exe", [])
        pm.set_wait_process("LimbusCompany.exe", [ProcessRecord(
            pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
            owned=False, create_time=1.0, native_create_time=(2345 << 32) | 77,
        )])
        self.assertTrue(adapter.wait_game_ready(1).ok)
        self.assertTrue(adapter.start_helper().ok)
        helper_pid = adapter._started_helper_pid
        # 双方从快照中消失（极快退出）。
        pm.set_find("LimbusCompany.exe", [])
        pm.set_find("AALC.exe", [])
        return adapter, pm, helper_pid

    def test_both_sides_exited_immediately_is_success_and_resets(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, helper_pid = self._ready_adapter(tmp)

            result = adapter.wait_completion(timeout_s=1)

            self.assertTrue(result.ok)
            self.assertEqual(result.details["game_pids"], [301])
            self.assertIn(301, pm.wait_exit_calls)
            self.assertIn(helper_pid, pm.wait_exit_calls)
            # T-008 #9：成功重置后 cleanup 干净：无 terminate、无 /End。
            self.assertEqual(adapter._owned_game_pids, set())
            self.assertIsNone(adapter._started_game_pid)
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [])
            self.assertEqual(pm.stopped_scheduled_tasks, [])

    def test_unknown_game_pid_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            adapter._owned_game_pids.clear()

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(pm.terminated, [])

    def test_unknown_helper_pid_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            adapter._started_helper_pid = None

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)

    def test_unconfirmed_exit_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            pm.set_wait_exit(301, False)

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(result.details["unconfirmed_pid"], 301)

    def test_one_side_missing_still_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
                owned=False, create_time=1.0,
            )])

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(result.details["missing"], "helper")

    def test_preset_stop_takes_priority_in_fast_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            pm.set_wait_exit(301, False)
            stop = threading.Event()
            stop.set()

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)

    def test_stop_after_snapshot_with_missing_game_pid_takes_priority(self):
        """R1 教训（T-011）时序①：缺 PID 出口之前的停止请求优先。

        入口检查时未停止，在首次助手快照返回前置位。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, _helper_pid = self._ready_adapter(tmp)
            adapter._owned_game_pids.clear()
            stop = threading.Event()
            original_find = pm.find_by_executable

            def find(name, **kwargs):
                records = original_find(name, **kwargs)
                if name == "AALC.exe":
                    stop.set()
                return records

            pm.find_by_executable = find

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)

    def test_stop_after_exit_confirmation_returns_stop_without_success_reset(self):
        """R1 教训（T-011）时序②：确认退出后到达的停止优先，不走成功重置。

        在最后一个已知 PID 确认退出时置位，不依赖 is_set 调用次数。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, helper_pid = self._ready_adapter(tmp)
            stop = threading.Event()
            original_wait = pm.wait_for_exit

            def wait(pid, timeout_s, stop_event=None):
                exited = original_wait(pid, timeout_s, stop_event)
                if pid == helper_pid:
                    stop.set()
                return exited

            pm.wait_for_exit = wait

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            self.assertEqual(pm.wait_exit_calls, [301, helper_pid])
            # 停止路径不走成功重置：跟踪保留，cleanup 仍能看到并收尾双方
            # （若已被错误重置，cleanup 将没有任何目标可记录）。
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertEqual(adapter._started_helper_pid, helper_pid)
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(
                sorted(cleanup.details["exited_pids"]),
                sorted([301, helper_pid]),
            )

    def test_timeout_keeps_tracking_and_cleanup_reports_not_owned(self):
        """T-008 #9：超时不得提前清空跟踪；cleanup 仍尝试 owned 清理。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapter, pm, helper_pid = self._ready_adapter(tmp)
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
                owned=False, create_time=1.0,
            )])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=helper_pid, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            pm.set_wait_exit(301, False)

            result = adapter.wait_completion(timeout_s=0)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_WAIT_TIMEOUT)
            # 超时不清空：cleanup 仍对 owned 游戏执行终止。
            self.assertEqual(adapter._owned_game_pids, {301})
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertIn(301, pm.terminated)
            self.assertIn(helper_pid, cleanup.details["exited_pids"])

    def test_preset_stop_preempts_initial_takeover_with_zero_new_claims(self):
        """R2：预置停止优先于单边缺失与初始接管——零新接管。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
                owned=False, create_time=1.0, native_create_time=0,
            )])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            stop = threading.Event()
            stop.set()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            # 不返回 helper_exited_early / environment_not_ready，而是停止；
            # 零接管（缺标识的 301 未被交给 take_ownership）。
            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            self.assertEqual(pm.claimed, [])
            self.assertEqual(pm.terminated, [])

    def test_preset_stop_preempts_one_sided_missing(self):
        """R2：预置停止优先于单边缺失判断。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
                owned=False, create_time=1.0,
            )])
            stop = threading.Event()
            stop.set()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.wait_completion(timeout_s=1, stop_event=stop)

            self.assertEqual(result.code, CODE_STOP_REQUESTED)

    def test_stop_during_normal_loop_snapshot_returns_stop_without_reset(self):
        """R2：普通循环第二次快照期间置位的停止优先，不走成功重置。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe", full_path="C:\\LimbusCompany.exe",
                owned=False, create_time=1.0, native_create_time=(2345 << 32) | 77,
            )])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._owned_game_pids = {301}
            adapter._started_helper_pid = 830
            pm.set_wait_exit(301, False)
            pm.set_wait_exit(830, False)
            stop = threading.Event()
            original_find = pm.find_by_executable

            def find(name, **kwargs):
                records = original_find(name, **kwargs)
                if name == "AALC.exe" and len(pm.find_calls) == 4:
                    stop.set()
                return records

            pm.find_by_executable = find

            result = adapter.wait_completion(timeout_s=5, stop_event=stop)

            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            self.assertTrue(stop.is_set())
            self.assertEqual(len(pm.find_calls), 4)
            # 不走成功重置：跟踪保留，cleanup 仍尝试关闭双方。
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertEqual(adapter._started_helper_pid, 830)
            cleanup = adapter.cleanup()
            self.assertEqual(cleanup.code, "not_owned")
            self.assertEqual(
                sorted(cleanup.details["unknown_pids"]),
                [301, 830],
            )

    def test_stop_during_initial_single_side_snapshot_preserves_tracking(self):
        """R2-a：入口未停止，首次快照返回一侧缺失时停止已经到达。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = _limbus_cfg(Path(td))
            pm = FakeProcessMonitor()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._started_game_pid = 301
            adapter._owned_game_pids = {301}
            adapter._started_helper_pid = 830
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path=cfg["helper"]["executable"],
                owned=False, create_time=1.0,
            )])
            stop = threading.Event()
            original_find = pm.find_by_executable

            def find(name, **kwargs):
                self.assertFalse(stop.is_set())
                records = original_find(name, **kwargs)
                if name == "AALC.exe":
                    stop.set()
                return records

            pm.find_by_executable = find

            result = adapter.wait_completion(timeout_s=0, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            self.assertTrue(stop.is_set())
            self.assertEqual(pm.find_calls, [
                ("LimbusCompany.exe", cfg["game_executable"]),
                ("AALC.exe", cfg["helper"]["executable"]),
            ])
            self.assertEqual(pm.wait_exit_calls, [])
            self.assertEqual(pm.claimed, [])
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertEqual(adapter._started_game_pid, 301)
            self.assertEqual(adapter._started_helper_pid, 830)
            self._startfile_mock.assert_not_called()
            self._popen_mock.assert_not_called()

    def test_stop_during_final_normal_exit_confirmation_preserves_tracking(self):
        """R2-b：普通循环的最后一次退出核验内到达停止，不得成功重置。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = _limbus_cfg(Path(td))
            pm = FakeProcessMonitor()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._started_game_pid = 301
            adapter._owned_game_pids = {301}
            adapter._baseline_game_pids = {300}
            adapter._started_helper_pid = 830
            adapter._scheduled_helper_task_started = True
            pm._owned.add(301)
            for name, pid, path in (
                ("LimbusCompany.exe", 301, cfg["game_executable"]),
                ("AALC.exe", 830, cfg["helper"]["executable"]),
            ):
                pm.set_find_sequence(name, [[ProcessRecord(
                    pid=pid, executable=name, full_path=path,
                    owned=False, create_time=1.0,
                )], []])
            stop = threading.Event()
            original_wait = pm.wait_for_exit

            def wait(pid, timeout_s, stop_event=None):
                self.assertFalse(stop.is_set())
                exited = original_wait(pid, timeout_s, stop_event)
                if pid == 830:
                    stop.set()
                return exited

            pm.wait_for_exit = wait

            result = adapter.wait_completion(timeout_s=0, stop_event=stop)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_STOP_REQUESTED)
            self.assertTrue(stop.is_set())
            self.assertEqual(len(pm.find_calls), 4)
            self.assertEqual(pm.wait_exit_calls, [301, 830])
            self.assertEqual(adapter._owned_game_pids, {301})
            self.assertEqual(adapter._started_game_pid, 301)
            self.assertEqual(adapter._baseline_game_pids, {300})
            self.assertEqual(adapter._started_helper_pid, 830)
            self.assertTrue(adapter._scheduled_helper_task_started)
            self._startfile_mock.assert_not_called()
            self._popen_mock.assert_not_called()

    def test_every_observed_helper_must_be_confirmed_exited(self):
        """R3-a：额外助手快照消失但退出未知时，保留跟踪与任务清理标记。"""
        for include_baseline in (False, True):
            with self.subTest(include_baseline=include_baseline), tempfile.TemporaryDirectory() as td:
                cfg = _limbus_cfg(Path(td))
                pm = FakeProcessMonitor()
                adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
                adapter._started_game_pid = 301
                adapter._owned_game_pids = {301}
                adapter._baseline_game_pids = {300}
                adapter._started_helper_pid = 830
                adapter._scheduled_helper_task_name = "owned-helper"
                adapter._scheduled_helper_task_started = True
                pm._owned.add(301)
                games = [ProcessRecord(
                    pid=pid, executable="LimbusCompany.exe", full_path=cfg["game_executable"],
                    owned=False, create_time=1.0,
                ) for pid in ([300, 301] if include_baseline else [301])]
                helpers = [ProcessRecord(
                    pid=pid, executable="AALC.exe", full_path=cfg["helper"]["executable"],
                    owned=False, create_time=1.0,
                ) for pid in (830, 831)]
                pm.set_find_sequence("LimbusCompany.exe", [games, []])
                pm.set_find_sequence("AALC.exe", [helpers, []])
                pm.set_wait_exit(300, False)
                pm.set_wait_exit(301, True)
                pm.set_wait_exit(830, True)
                pm.set_wait_exit(831, False)

                result = adapter.wait_completion(timeout_s=0)

                self.assertFalse(result.ok)
                self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
                self.assertEqual(result.details["unconfirmed_pids"], [831])
                self.assertEqual(len(pm.find_calls), 4)
                self.assertEqual(pm.wait_exit_calls, [301, 830, 831])
                self.assertEqual(pm.claimed, [])
                self.assertEqual(adapter._owned_game_pids, {301})
                self.assertEqual(adapter._started_game_pid, 301)
                self.assertEqual(adapter._baseline_game_pids, {300})
                self.assertEqual(adapter._started_helper_pid, 830)
                self.assertTrue(adapter._scheduled_helper_task_started)
                self.assertEqual(pm.stopped_scheduled_tasks, [])
                self._startfile_mock.assert_not_called()
                self._popen_mock.assert_not_called()

    def test_snapshot_vanished_but_known_helper_unconfirmed_is_not_success(self):
        """R3：快照为空但本轮已知助手未确认退出时，不得按完成重置。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            pm.set_wait_process("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe",
                full_path="C:\\LimbusCompany.exe", owned=False,
                create_time=1.0, native_create_time=(2345 << 32) | 77,
            )])
            self.assertTrue(adapter.wait_game_ready(1).ok)
            # 先完成助手启动（此时 AALC 快照为空，不触发排重拒绝），
            # 再让精确路径快照进入「首查存在、复查消失」的序列。
            self.assertTrue(adapter.start_helper().ok)
            helper_pid = adapter._started_helper_pid
            flag_before = adapter._scheduled_helper_task_started
            pm.set_find_sequence("LimbusCompany.exe", [
                [ProcessRecord(
                    pid=301, executable="LimbusCompany.exe",
                    full_path="C:\\LimbusCompany.exe", owned=False,
                    create_time=1.0, native_create_time=(2345 << 32) | 77,
                )],
                [],
            ])
            pm.set_find_sequence("AALC.exe", [
                [ProcessRecord(
                    pid=helper_pid, executable="AALC.exe", full_path="C:\\AALC.exe",
                    owned=False, create_time=1.0,
                )],
                [],
            ])
            pm.set_wait_exit(301, True)
            pm.set_wait_exit(helper_pid, False)

            result = adapter.wait_completion(timeout_s=5)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_HELPER_EXITED_EARLY)
            self.assertEqual(result.details["unconfirmed_pids"], [helper_pid])
            # 跟踪保留：started/标志不被成功重置清空。
            self.assertEqual(adapter._started_helper_pid, helper_pid)
            self.assertEqual(adapter._scheduled_helper_task_started, flag_before)

    def test_wait_completion_initial_takeover_failure_matrix(self):
        """R4：初始接管 None/缺原生标识 → environment_not_ready。"""
        cases = [
            ("takeover_rejected", dict(take_ownership_result=False),
             (2345 << 32) | 77),
            ("missing_native", {}, 0),
        ]
        for label, pm_kwargs, native in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    cfg = _limbus_cfg(tmp)
                    pm = FakeProcessMonitor()
                    for key, value in pm_kwargs.items():
                        setattr(pm, key, value)
                    pm.set_find("LimbusCompany.exe", [ProcessRecord(
                        pid=301, executable="LimbusCompany.exe",
                        full_path="C:\\LimbusCompany.exe", owned=False,
                        create_time=1.0, native_create_time=native,
                    )])
                    pm.set_find("AALC.exe", [ProcessRecord(
                        pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                        owned=False, create_time=1.0,
                    )])
                    adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

                    result = adapter.wait_completion(timeout_s=1)

                    self.assertFalse(result.ok)
                    self.assertEqual(result.code, CODE_ENV_NOT_READY)

    def test_wait_completion_missing_interface_fails_closed(self):
        """R4：初始接管缺接口按 environment_not_ready 失败关闭。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find("LimbusCompany.exe", [ProcessRecord(
                pid=301, executable="LimbusCompany.exe",
                full_path="C:\\LimbusCompany.exe", owned=False,
                create_time=1.0, native_create_time=(2345 << 32) | 77,
            )])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            pm.take_ownership = None  # 缺接口
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)

            result = adapter.wait_completion(timeout_s=1)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertEqual(result.details["reason"], "monitor_unsupported")

    def test_wait_completion_running_restart_pid_failure_matrix(self):
        """R4：循环内重启 PID 接管失败立即返回 environment_not_ready。"""
        cases = [
            ("takeover_rejected", dict(take_ownership_result=False),
             (2345 << 32) | 500),
            ("missing_native", {}, 0),
        ]
        for label, pm_kwargs, native in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    cfg = _limbus_cfg(tmp)
                    pm = FakeProcessMonitor()
                    for key, value in pm_kwargs.items():
                        setattr(pm, key, value)
                    pm.set_find_sequence("LimbusCompany.exe", [
                        [ProcessRecord(
                            pid=301, executable="LimbusCompany.exe",
                            full_path="C:\\LimbusCompany.exe", owned=False,
                            create_time=1.0, native_create_time=(2345 << 32) | 77,
                        )],
                        [ProcessRecord(
                            pid=401, executable="LimbusCompany.exe",
                            full_path="C:\\LimbusCompany.exe", owned=False,
                            create_time=2.0, native_create_time=native,
                        )],
                    ])
                    pm.set_find("AALC.exe", [ProcessRecord(
                        pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                        owned=False, create_time=1.0,
                    )])
                    pm.set_wait_exit(301, True)
                    pm.set_wait_exit(830, True)
                    adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
                    # 模拟 wait_game_ready 已归属初始实例。
                    adapter._owned_game_pids = {301}
                    adapter._started_helper_pid = 830

                    result = adapter.wait_completion(timeout_s=5)

                    self.assertFalse(result.ok)
                    self.assertEqual(result.code, CODE_ENV_NOT_READY)
                    self.assertEqual(result.details["pid"], 401)

    def test_wait_completion_loop_missing_interface_fails_closed(self):
        """R4：循环内新 PID 缺接口按 environment_not_ready 失败关闭。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _limbus_cfg(tmp)
            pm = FakeProcessMonitor()
            pm.set_find_sequence("LimbusCompany.exe", [
                [ProcessRecord(
                    pid=301, executable="LimbusCompany.exe",
                    full_path="C:\\LimbusCompany.exe", owned=False,
                    create_time=1.0, native_create_time=(2345 << 32) | 77,
                )],
                [ProcessRecord(
                    pid=401, executable="LimbusCompany.exe",
                    full_path="C:\\LimbusCompany.exe", owned=False,
                    create_time=2.0, native_create_time=(2345 << 32) | 500,
                )],
            ])
            pm.set_find("AALC.exe", [ProcessRecord(
                pid=830, executable="AALC.exe", full_path="C:\\AALC.exe",
                owned=False, create_time=1.0,
            )])
            pm.set_wait_exit(301, True)
            pm.set_wait_exit(830, True)
            pm.take_ownership = None  # 缺接口
            adapter = AalcAdapter(game_cfg=cfg, process_monitor=pm)
            adapter._owned_game_pids = {301}
            adapter._started_helper_pid = 830

            result = adapter.wait_completion(timeout_s=5)

            self.assertFalse(result.ok)
            self.assertEqual(result.code, CODE_ENV_NOT_READY)
            self.assertEqual(result.details["reason"], "monitor_unsupported")


class TestAalcLaunchIsolation(unittest.TestCase):
    """T-012 R6：AALC 测试禁止真实 Steam/系统协议启动。"""

    def test_steam_start_only_reaches_injected_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            startfile_mock, popen_mock = _install_system_launch_guard(self)
            pm = FakeProcessMonitor()
            adapter = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)

            result = adapter.start_game()

            # Steam 路径被测语义保留：返回 ok 并携带协议与 URI。
            self.assertTrue(result.ok)
            self.assertEqual(result.details["method"], "steam_protocol")
            # Steam URI 只到达注入边界；真实 os.startfile / Popen 零调用。
            startfile_mock.assert_called_once_with("steam://run/1973530")
            popen_mock.assert_not_called()
            # 边界一旦拒绝调用，Steam 路径即无法继续：真实入口不可达。
            startfile_mock.side_effect = AssertionError(
                "测试试图绕过系统启动边界"
            )
            with self.assertRaises(AssertionError):
                adapter.start_game()
            self.assertEqual(startfile_mock.call_count, 2)
            popen_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# StepResult 不可变                                                            #
# --------------------------------------------------------------------------- #


class TestStepResult(unittest.TestCase):
    def test_stepresult_is_frozen(self):
        r = StepResult(ok=True, code=CODE_OK, message="x", details={})
        with self.assertRaises(Exception):
            r.ok = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
