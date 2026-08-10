"""适配器单元测试。

使用假的 process_monitor 与临时配置，验证三个适配器的正常路径、未验证配置、
工具提前退出与完成信号不足等情况。不启动真实游戏或挂机工具。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Mapping

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
        self.terminated: list[int] = []
        self.launch_calls: list[tuple[list[str], str | None]] = []
        self.claimed: list[int] = []
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

    def find_by_executable(self, name: str) -> list[ProcessRecord]:
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

    def take_ownership(self, pid: int) -> ProcessRecord | None:
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
        )
        self._owned.add(pid)
        self.claimed.append(pid)
        return rec

    def wait_for_process(self, name, timeout_s, stop_event=None):
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
    ):
        self.scheduled_task_match_calls.append(
            (
                name,
                str(executable),
                str(cwd),
                arguments,
                expected_action_count,
                expected_wrapper_target,
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
            self.assertTrue(cleanup.ok)

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
            self.assertEqual(pm.scheduled_task_match_calls[1][4], 1)
            self.assertEqual(pm.scheduled_task_match_calls[1][5], "")

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
            self.assertEqual(pm.claimed, [777])
            cleanup = adapter.cleanup()
            self.assertTrue(cleanup.ok)
            self.assertEqual(pm.terminated, [777])


# --------------------------------------------------------------------------- #
# AalcAdapter                                                                 #
# --------------------------------------------------------------------------- #


class TestAalcAdapter(unittest.TestCase):
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

            r = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm).wait_completion(timeout_s=5)

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
            a = AalcAdapter(game_cfg=_limbus_cfg(tmp), process_monitor=pm)
            r = a.wait_game_ready(timeout_s=1, stop_event=stop)
            self.assertFalse(r.ok)
            self.assertEqual(r.code, CODE_STOP_REQUESTED)


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
