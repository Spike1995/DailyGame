"""无副作用桌面入口的测试。

以源码方式加载 .pyw 模块，并注入 GUI、锁、编排器和进程相关协作者。
所有测试均不会启动真实 GUI、游戏、挂机工具或外部进程。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "daily_games.pyw"
CMD_FILE = ROOT / "启动一键日常.cmd"


def load_entrypoint():
    """加载新的 .pyw 模块，同时不触发其 __main__ 代码块。"""
    name = f"daily_games_entrypoint_test_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(ENTRYPOINT))
    spec = spec_from_loader(name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    code: str = "ok"
    message: str = "已检查"
    severity: str = "info"
    details: dict[str, object] = field(default_factory=dict)


class FakeOrchestrator:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = 0

    def dry_run(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.results)


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        return self.acquired

    def release(self):
        self.release_calls += 1


class ReconfigurableStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.reconfigure_calls = []

    def reconfigure(self, **kwargs):
        self.reconfigure_calls.append(kwargs)


class UnavailableReconfigureStream(io.StringIO):
    def reconfigure(self, **kwargs):
        raise OSError("不支持")


class InjectedStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.reconfigure_called = False

    def reconfigure(self, **kwargs):
        self.reconfigure_called = True
        raise AssertionError("不得重新配置注入的流")


class EntrypointTests(unittest.TestCase):
    def test_import_has_no_gui_or_process_side_effect(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            entrypoint = load_entrypoint()

        self.assertTrue(callable(entrypoint.main))
        popen.assert_not_called()

    def test_default_one_click_gui_runner_enables_automatic_full_queue(self):
        entrypoint = load_entrypoint()
        run_gui = mock.Mock()
        fake_ui = type("FakeUiModule", (), {"run_gui": run_gui})()
        config_path = ROOT / "config" / "user.json"

        with mock.patch.dict(sys.modules, {"daily_games.ui": fake_ui}):
            entrypoint._default_gui_runner(config_path)

        run_gui.assert_called_once_with(config_path=config_path, auto_start=True)

    def test_default_streams_are_best_effort_reconfigured_to_utf8_before_dry_run_output(self):
        entrypoint = load_entrypoint()
        stdout = ReconfigurableStream()
        stderr = ReconfigurableStream()
        fake = FakeOrchestrator([Check("中文检查", True)])

        with mock.patch.object(entrypoint.sys, "stdout", stdout), \
             mock.patch.object(entrypoint.sys, "stderr", stderr):
            exit_code = entrypoint.main(
                ["--dry-run"],
                orchestrator_factory=lambda config_path: fake,
                lock_factory=lambda: self.fail("dry-run 不得获取锁"),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.reconfigure_calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.reconfigure_calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertIn("中文检查", stdout.getvalue())

    def test_unavailable_default_streams_are_ignored_and_injected_streams_are_untouched(self):
        entrypoint = load_entrypoint()
        lock = FakeLock()
        gui_calls = []

        with mock.patch.object(entrypoint.sys, "stdout", None), \
             mock.patch.object(entrypoint.sys, "stderr", None):
            exit_code = entrypoint.main(
                [],
                gui_runner=lambda config_path: gui_calls.append(config_path),
                lock_factory=lambda: lock,
                gui_error_reporter=lambda message: self.fail(message),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(gui_calls), 1)

        output = InjectedStream()
        errors = InjectedStream()
        unavailable = UnavailableReconfigureStream()
        self.assertIs(entrypoint._best_effort_utf8_stream(unavailable), unavailable)
        self.assertIsNone(entrypoint._best_effort_utf8_stream(None))
        exit_code = entrypoint.main(
            ["--dry-run"],
            orchestrator_factory=lambda config_path: FakeOrchestrator([Check("check", True)]),
            output=output,
            error_output=errors,
            lock_factory=lambda: self.fail("dry-run 不得获取锁"),
        )
        self.assertEqual(exit_code, 0)
        self.assertFalse(output.reconfigure_called)
        self.assertFalse(errors.reconfigure_called)

    def test_gui_success_uses_root_relative_config_and_releases_lock(self):
        entrypoint = load_entrypoint()
        lock = FakeLock()
        calls = []
        output = io.StringIO()
        errors = io.StringIO()

        exit_code = entrypoint.main(
            [],
            gui_runner=lambda config_path: calls.append(config_path),
            lock_factory=lambda: lock,
            output=output,
            error_output=errors,
            gui_error_reporter=lambda message: self.fail(message),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [entrypoint.CONFIG_PATH])
        self.assertEqual(lock.acquire_calls, 1)
        self.assertEqual(lock.release_calls, 1)
        self.assertEqual(errors.getvalue(), "")

    def test_duplicate_gui_instance_is_rejected_without_calling_gui(self):
        entrypoint = load_entrypoint()
        lock = FakeLock(acquired=False)
        gui_calls = []
        diagnostics = []
        errors = io.StringIO()

        exit_code = entrypoint.main(
            [],
            gui_runner=lambda config_path: gui_calls.append(config_path),
            lock_factory=lambda: lock,
            error_output=errors,
            gui_error_reporter=diagnostics.append,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(gui_calls, [])
        self.assertEqual(lock.acquire_calls, 1)
        self.assertEqual(lock.release_calls, 0)
        self.assertIn("正在运行", errors.getvalue())
        self.assertEqual(len(diagnostics), 1)

    def test_dry_run_all_passing_prints_every_result_and_does_not_take_gui_lock(self):
        entrypoint = load_entrypoint()
        fake = FakeOrchestrator([
            Check("config", True, "ok", "配置有效"),
            Check("paths", True, "ok", "路径有效"),
        ])
        factory_calls = []
        output = io.StringIO()

        exit_code = entrypoint.main(
            ["--dry-run"],
            orchestrator_factory=lambda config_path: (factory_calls.append(config_path) or fake),
            gui_runner=lambda config_path: self.fail("dry-run 不得运行 GUI"),
            lock_factory=lambda: self.fail("dry-run 不得获取 GUI 锁"),
            output=output,
            error_output=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(factory_calls, [entrypoint.CONFIG_PATH])
        self.assertEqual(fake.calls, 1)
        rendered = output.getvalue()
        self.assertIn("config", rendered)
        self.assertIn("paths", rendered)
        self.assertIn("通过", rendered)

    def test_dry_run_failure_is_nonzero_and_prints_all_results(self):
        entrypoint = load_entrypoint()
        fake = FakeOrchestrator([
            Check("config", True),
            Check("games.arknights.game_process_name", True, "process_not_running", "未运行"),
            Check("helper", False, "file_not_found", "缺少挂机工具", "error"),
        ])
        output = io.StringIO()

        exit_code = entrypoint.main(
            ["--dry-run"],
            orchestrator_factory=lambda config_path: fake,
            lock_factory=lambda: self.fail("dry-run 不得获取锁"),
            output=output,
            error_output=io.StringIO(),
        )

        self.assertEqual(exit_code, 1)
        rendered = output.getvalue()
        self.assertIn("config", rendered)
        self.assertIn("games.arknights.game_process_name", rendered)
        self.assertIn("process_not_running", rendered)
        self.assertIn("helper", rendered)
        self.assertIn("失败", rendered)
        self.assertIn("file_not_found", rendered)

    def test_dry_run_exception_is_diagnostic_and_nonzero_without_external_process(self):
        entrypoint = load_entrypoint()
        fake = FakeOrchestrator(error=RuntimeError("配置损坏"))
        errors = io.StringIO()

        with mock.patch.object(subprocess, "Popen") as popen:
            exit_code = entrypoint.main(
                ["--dry-run"],
                orchestrator_factory=lambda config_path: fake,
                lock_factory=lambda: self.fail("dry-run 不得获取锁"),
                output=io.StringIO(),
                error_output=errors,
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("dry-run 失败", errors.getvalue())
        self.assertIn("RuntimeError", errors.getvalue())
        popen.assert_not_called()

    def test_repair_paths_runs_without_gui_lock_and_returns_result_status(self):
        entrypoint = load_entrypoint()
        calls = []
        output = io.StringIO()

        exit_code = entrypoint.main(
            ["--repair-paths"],
            path_repairer=lambda config_path: (
                calls.append(config_path)
                or [Check(
                    "software_paths.maa",
                    True,
                    "software_path_repaired",
                    "已更新",
                )]
            ),
            gui_runner=lambda config_path: self.fail("位址自查不得运行 GUI"),
            lock_factory=lambda: self.fail("位址自查不得获取 GUI 锁"),
            output=output,
            error_output=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [entrypoint.CONFIG_PATH])
        self.assertIn("software_paths.maa", output.getvalue())
        self.assertIn("software_path_repaired", output.getvalue())

    def test_repair_paths_unresolved_candidate_returns_nonzero(self):
        entrypoint = load_entrypoint()
        output = io.StringIO()

        exit_code = entrypoint.main(
            ["--repair-paths"],
            path_repairer=lambda _config_path: [Check(
                "software_paths.maa",
                False,
                "software_path_ambiguous",
                "多个候选",
                "error",
            )],
            lock_factory=lambda: self.fail("位址自查不得获取 GUI 锁"),
            output=output,
            error_output=io.StringIO(),
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("software_path_ambiguous", output.getvalue())

    def test_gui_exception_releases_lock_and_reports_nonzero(self):
        entrypoint = load_entrypoint()
        lock = FakeLock()
        diagnostics = []
        errors = io.StringIO()

        exit_code = entrypoint.main(
            [],
            gui_runner=lambda config_path: (_ for _ in ()).throw(RuntimeError("Tk 不可用")),
            lock_factory=lambda: lock,
            error_output=errors,
            gui_error_reporter=diagnostics.append,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(lock.release_calls, 1)
        self.assertIn("GUI 启动失败", errors.getvalue())
        self.assertEqual(len(diagnostics), 1)

    @unittest.skipUnless(os.name == "nt", "Windows 命名互斥锁行为")
    def test_windows_named_mutex_rejects_second_instance_then_releases(self):
        entrypoint = load_entrypoint()
        name = rf"Local\DailyGames.EntryPointTest.{uuid.uuid4().hex}"
        first = entrypoint.WindowsGuiInstanceLock(name)
        second = entrypoint.WindowsGuiInstanceLock(name)
        third = entrypoint.WindowsGuiInstanceLock(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()

    def test_launcher_script_is_root_relative_and_uses_admin_task_helper(self):
        source = CMD_FILE.read_text(encoding="utf-8")
        raw = CMD_FILE.read_bytes()
        lowered = source.lower()

        self.assertIn("%~dp0", source)
        self.assertIn("daily_games_launcher.ps1", source)
        self.assertIn("powershell.exe", lowered)
        self.assertNotIn("d:\\daily", lowered)
        self.assertTrue(all(byte < 128 for byte in raw))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_admin_task_helper_registers_scoped_programs_and_guarded_ui_automation(self):
        source = (ROOT / "daily_games_launcher.ps1").read_text(encoding="utf-8")
        lowered = source.lower()

        self.assertIn("$psscriptroot", lowered)
        self.assertIn("daily_games.pyw", lowered)
        self.assertIn("pythonw.exe", lowered)
        self.assertIn("dailygames-arknights-admin", lowered)
        self.assertIn("dailygames-maa-admin", lowered)
        self.assertIn("dailygames-endfield-admin", lowered)
        self.assertIn("dailygames-maaend-admin", lowered)
        self.assertIn("dailygames-aalc-admin", lowered)
        self.assertIn("config\\user.json", lowered)
        self.assertIn("register-scheduledtask", lowered)
        self.assertIn("-runlevel highest", lowered)
        self.assertIn("-verb runas", lowered)
        self.assertIn("$repairtasks", lowered)
        self.assertIn("if ($repairtasks)", lowered)
        self.assertIn("invoke-softwarepathselfcheck", lowered)
        self.assertIn("--repair-paths", lowered)
        self.assertIn("& $pythonpath $entrypath '--repair-paths'", lowered)
        self.assertNotIn("$repairarguments", lowered)
        self.assertLess(
            lowered.rfind("invoke-softwarepathselfcheck -pythonwpath"),
            lowered.rfind("$specs = @(get-admintaskspecs)"),
        )
        self.assertNotIn("start-scheduledtask", lowered)
        self.assertNotIn("d:\\daily", lowered)
        self.assertNotIn("sendkeys", lowered)
        self.assertNotIn("mouse_event", lowered)
        self.assertIn("uiautomationclient", lowered)
        self.assertIn("sendinput", lowered)
        self.assertIn("getwindowthreadprocessid", lowered)
        self.assertIn("attachthreadinput", lowered)
        self.assertIn("get-authenticodesignature", lowered)
        self.assertIn("game_selected_by_verified_position", lowered)
        self.assertIn("launcher_button_automation.log", lowered)
        self.assertIn("test-verifiedlauncherwindow", lowered)
        self.assertIn("daily_games_launcher_worker_v3", lowered)
        self.assertIn("launcher_dispatched", lowered)
        self.assertIn("launcher_dispatch_failed", lowered)
        self.assertIn("find-launcherwindowcandidate", lowered)
        self.assertIn("launcher_window_candidate_missing", lowered)
        self.assertIn("native_type_load_failed", lowered)
        self.assertIn("-filepath '__launcher_executable__'", lowered)
        self.assertIn("-erroraction stop", lowered)
        self.assertIn(".generated", lowered)
        self.assertIn("expectedsha256", lowered)
        self.assertIn("get-filehash", lowered)
        self.assertNotIn("-encodedcommand", lowered)
        self.assertIn("if ($actions.count -ne 1)", lowered)
        self.assertNotIn("postaction", lowered)
        self.assertIn("开始游戏", source)

    def test_production_launcher_uses_verified_game_ids_and_debug_logging(self):
        config = json.loads(
            (ROOT / "config" / "user.json").read_text(encoding="utf-8")
        )

        self.assertEqual(config["logging"]["level"], "DEBUG")
        path_check = config["software_path_self_check"]
        self.assertTrue(path_check["enabled"])
        self.assertTrue(path_check["auto_repair_unique_match"])
        self.assertEqual(
            set(path_check["software_ids"]),
            {
                "arknights_game", "maa", "endfield_game",
                "maaend", "limbus_game", "aalc",
            },
        )
        self.assertNotIn("D:\\", path_check["search_roots"])
        self.assertEqual(
            config["games"]["arknights"]["launcher"]["args"],
            ["--game=b77c220c33d1428bb253d87d88440297", "--reason=4"],
        )
        self.assertEqual(
            config["games"]["endfield"]["launcher"]["args"],
            ["--game=24dab91fe61d1e55938b25b74963cbc5", "--reason=4"],
        )
        for game_id in ("arknights", "endfield"):
            automation = config["games"][game_id]["launcher"][
                "start_button_automation"
            ]
            self.assertTrue(automation["enabled"])
            self.assertTrue(automation["verified"])
            self.assertEqual(automation["window_title"], "鹰角启动器")
            self.assertEqual(automation["window_process_name"], "Games")
            self.assertEqual(automation["button_name"], "开始游戏")
            self.assertEqual(automation["window_process_version"], "1.5.0.1504")
            self.assertEqual(
                automation["window_signer_thumbprint"],
                "2B366F397E64A9494A553B70004E8CA1AAE02B9D",
            )
        self.assertEqual(
            config["games"]["arknights"]["launcher"]
            ["start_button_automation"]["game_button_name"],
            "明日方舟",
        )
        self.assertEqual(
            config["games"]["endfield"]["launcher"]
            ["start_button_automation"]["game_button_name"],
            "明日方舟：终末地",
        )
        self.assertNotEqual(
            config["games"]["arknights"]["launcher"]
            ["start_button_automation"]["selector_y_ratio"],
            config["games"]["endfield"]["launcher"]
            ["start_button_automation"]["selector_y_ratio"],
        )
        self.assertEqual(
            config["games"]["arknights"]["launcher"]
            ["start_button_automation"]["selector_y_ratio"],
            0.402,
        )
        self.assertEqual(
            config["games"]["endfield"]["launcher"]
            ["start_button_automation"]["selector_y_ratio"],
            0.495,
        )
        for game_id in ("arknights", "endfield"):
            automation = config["games"][game_id]["launcher"][
                "start_button_automation"
            ]
            self.assertEqual(automation["fallback_x_ratio"], 0.86)
            self.assertEqual(automation["fallback_y_ratio"], 0.835)
        aalc = config["games"]["limbus"]["helper"]
        self.assertTrue(aalc["requires_administrator"])
        self.assertEqual(
            aalc["scheduled_task_name"], "DailyGames-AALC-Admin"
        )
        self.assertEqual(aalc["cli_args"], ["start", "--exit", "6"])
        self.assertTrue(aalc["cli_start_verified"])
        self.assertTrue(aalc["cli_exit_both_verified"])


if __name__ == "__main__":
    unittest.main()
