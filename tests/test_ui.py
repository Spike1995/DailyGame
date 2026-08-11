"""前端单元测试。

使用假的编排器（FakeOrchestrator）注入，不覆盖也不依赖 GPT Luna 的
orchestrator.py；除最后的 Tk 冒烟测试外，所有案例都不需要显示环境。
"""
from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily_games.ui_components import (  # noqa: E402
    CLOSE_CANCEL,
    CLOSE_KEEP,
    CLOSE_STOP_THEN_EXIT,
    GAME_ORDER,
    STATE_LABELS,
    UiController,
    control_states,
    format_event,
    selectable_change_ids,
    state_label,
    validate_change_selection,
)
from daily_games.ui import (  # noqa: E402
    AUTO_EXIT_SUCCESS_DELAY_MS,
    DailyGamesApp,
    STOP_EXIT_TIMEOUT_MS,
    _enable_dark_title_bar,
    _enable_dpi_awareness,
    resolve_log_directories,
)

ALL_STATES = [
    "idle", "checking", "starting_game", "waiting_game", "starting_helper",
    "running", "cleaning", "completed", "failed", "paused", "skipped",
    "stopping", "stopped",
]


def make_event(game_id="arknights", state="running", message="执行中",
               error_code=None, timestamp="2026-08-09T06:00:00+00:00"):
    return SimpleNamespace(
        game_id=game_id, state=state, message=message,
        error_code=error_code, timestamp=timestamp,
    )


def make_change(change_id, verified):
    return SimpleNamespace(
        change_id=change_id, tool="MAA", field="autostart",
        current_display="关闭", proposed_display="开启",
        verified=verified, warning="",
    )


class LogDirectoryTests(unittest.TestCase):
    def test_configured_and_protected_log_directories_are_both_exposed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config" / "user.json"
            config_path.parent.mkdir()
            configured_logs = root / "custom-logs"
            config_path.write_text(
                json.dumps({"paths": {"logs_dir": str(configured_logs)}}),
                encoding="utf-8",
            )
            program_data = root / "program-data"
            protected_logs = program_data / "DailyGames" / "logs"
            protected_logs.mkdir(parents=True)

            directories = resolve_log_directories(
                config_path,
                program_data=program_data,
            )

            self.assertEqual(directories, (configured_logs, protected_logs))

    def test_invalid_config_falls_back_to_project_logs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config" / "user.json"
            config_path.parent.mkdir()
            config_path.write_text("{", encoding="utf-8")

            directories = resolve_log_directories(
                config_path,
                program_data=root / "missing-program-data",
            )

            self.assertEqual(directories, (root / "logs",))


class NativeUiTests(unittest.TestCase):
    def test_native_ui_calls_declare_types_and_preserve_window_handle(self):
        set_dpi = mock.Mock(return_value=0)
        shcore = mock.Mock()
        shcore.SetProcessDpiAwareness = set_dpi
        get_parent = mock.Mock(return_value=0x1234567887654321)
        user32 = mock.Mock()
        user32.GetParent = get_parent
        set_window_attribute = mock.Mock(return_value=0)
        dwmapi = mock.Mock()
        dwmapi.DwmSetWindowAttribute = set_window_attribute

        def load_library(name, **_kwargs):
            return {
                "shcore": shcore,
                "user32": user32,
                "dwmapi": dwmapi,
            }[name]

        window = mock.Mock()
        window.winfo_id.return_value = 0x1111222233334444
        with mock.patch.object(ctypes, "WinDLL", side_effect=load_library):
            _enable_dpi_awareness()
            _enable_dark_title_bar(window)

        self.assertEqual(set_dpi.argtypes, [ctypes.c_int])
        self.assertIs(set_dpi.restype, ctypes.c_long)
        self.assertEqual(set_dpi.call_args.args, (2,))
        self.assertEqual(get_parent.argtypes, [ctypes.c_void_p])
        self.assertIs(get_parent.restype, ctypes.c_void_p)
        self.assertEqual(
            get_parent.call_args.args[0].value,
            0x1111222233334444,
        )
        self.assertEqual(
            set_window_attribute.argtypes,
            [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ],
        )
        self.assertIs(set_window_attribute.restype, ctypes.c_long)
        self.assertEqual(
            set_window_attribute.call_args.args[0].value,
            0x1234567887654321,
        )


class FakeOrchestrator:
    """只实现冻结的公开接口，并记录调用。"""

    def __init__(self):
        self.is_running = False
        self.current_game = None
        self.current_state = "idle"
        self.calls = []
        self.listeners = []
        self.backups = [
            SimpleNamespace(
                backup_id="b1", created_at="2026-08-09T05:00:00+00:00",
                manifest_path=r"D:\Daily\backups\b1\manifest.json",
                source_count=3,
            )
        ]

    def add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def dry_run(self):
        self.calls.append("dry_run")
        return []

    def run_all(self):
        self.calls.append("run_all")
        return SimpleNamespace(ok=True, stopped=False, results=[])

    def run_one(self, game_id):
        self.calls.append(("run_one", game_id))
        return SimpleNamespace(ok=True, stopped=False, results=[])

    def retry_current(self):
        self.calls.append("retry_current")

    def skip_current(self):
        self.calls.append("skip_current")

    def request_stop(self):
        self.calls.append("request_stop")

    def check_configuration(self):
        self.calls.append("check_configuration")
        return [
            SimpleNamespace(name="paths", ok=True, code="OK",
                            message="路径存在", severity="info", details="")
        ]

    def plan_configuration_changes(self):
        self.calls.append("plan_configuration_changes")
        return [make_change("c1", True), make_change("c2", False)]

    def apply_configuration_changes(self, change_ids):
        self.calls.append(("apply_configuration_changes", list(change_ids)))
        return []

    def list_backups(self):
        self.calls.append("list_backups")
        return list(self.backups)

    def restore_backup(self, backup_id):
        self.calls.append(("restore_backup", backup_id))
        return []


def sync_bg(fn, on_done):
    """测试用同步背景替身：立即执行并触发回调。"""
    result = fn()
    if on_done is not None:
        on_done(result)
    return result


class FakeTimerRoot:
    """不创建 Tk 窗口的 close-dialog 计时器替身。"""

    def __init__(self):
        self.after_calls = []
        self.destroy_calls = 0

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        return len(self.after_calls)

    def destroy(self):
        self.destroy_calls += 1


class FailingTimerRoot(FakeTimerRoot):
    def after(self, delay, callback):
        raise RuntimeError("计时器不可用")


class FakeCloseWidget:
    def __init__(self, alive=True):
        self.alive = alive
        self.config_calls = []
        self.state_calls = []

    def winfo_exists(self):
        return self.alive

    def config(self, **kwargs):
        self.config_calls.append(kwargs)

    def state(self, state):
        self.state_calls.append(list(state))


class FakeCloseButtons(FakeCloseWidget):
    def __init__(self, children, alive=True):
        super().__init__(alive=alive)
        self.children = children

    def winfo_children(self):
        return list(self.children)


class StateDisplayTests(unittest.TestCase):
    def test_all_states_have_chinese_label(self):
        for state in ALL_STATES:
            label = state_label(state)
            self.assertTrue(label, state)
            self.assertNotIn(state, label, f"{state} 不应直接返回英文状态码")

    def test_label_table_covers_frozen_states(self):
        self.assertEqual(set(STATE_LABELS), set(ALL_STATES))

    def test_unknown_state_is_explicit(self):
        self.assertIn("未知状态", state_label("some_new_state"))

    def test_format_event_includes_error_code(self):
        line = format_event(make_event(state="failed", message="超时",
                                       error_code="TIMEOUT"))
        self.assertIn("明日方舟", line)
        self.assertIn("失败", line)
        self.assertIn("TIMEOUT", line)


class ControlStateTests(unittest.TestCase):
    def test_idle_allows_start_only(self):
        states = control_states(False, "idle")
        self.assertTrue(states["start_all"])
        self.assertTrue(states["run_one"])
        self.assertFalse(states["retry"])
        self.assertFalse(states["skip"])
        self.assertFalse(states["stop"])

    def test_running_disables_start_and_enables_stop(self):
        for state in ("checking", "starting_game", "waiting_game",
                      "starting_helper", "running", "cleaning", "stopping"):
            states = control_states(True, state)
            self.assertFalse(states["start_all"], state)
            self.assertFalse(states["run_one"], state)
            self.assertTrue(states["stop"], state)
            self.assertFalse(states["retry"], state)
            self.assertFalse(states["skip"], state)

    def test_attention_states_enable_retry_and_skip(self):
        for state in ("failed", "paused"):
            states = control_states(True, state)
            self.assertTrue(states["retry"], state)
            self.assertTrue(states["skip"], state)
            self.assertTrue(states["stop"], state)
            self.assertFalse(states["start_all"], state)
            self.assertFalse(states["run_one"], state)

    def test_config_tools_disabled_while_running(self):
        self.assertFalse(control_states(True, "running")["check_config"])
        self.assertFalse(control_states(True, "running")["restore_backup"])
        self.assertTrue(control_states(False, "idle")["check_config"])
        self.assertTrue(control_states(False, "idle")["restore_backup"])

    def test_terminal_states_allow_restart_when_not_running(self):
        for state in ("completed", "stopped"):
            states = control_states(False, state)
            self.assertTrue(states["start_all"], state)
            self.assertFalse(states["stop"], state)


class StartGuardTests(unittest.TestCase):
    def test_second_start_rejected_while_running(self):
        orch = FakeOrchestrator()
        orch.is_running = True
        controller = UiController(orch, bg=sync_bg)
        self.assertFalse(controller.request_start_all())
        self.assertFalse(controller.request_run_one("arknights"))
        self.assertNotIn("run_all", orch.calls)
        self.assertFalse(any(c == ("run_one", "arknights") for c in orch.calls))

    def test_start_all_invokes_orchestrator_when_idle(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        self.assertTrue(controller.request_start_all())
        self.assertIn("run_all", orch.calls)

    def test_run_one_invokes_orchestrator_when_idle(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        for game_id in GAME_ORDER:
            self.assertTrue(controller.request_run_one(game_id))
            self.assertIn(("run_one", game_id), orch.calls)

    def test_run_one_rejects_unknown_game(self):
        controller = UiController(FakeOrchestrator(), bg=sync_bg)
        with self.assertRaises(ValueError):
            controller.request_run_one("star_rail")


class EventQueueTests(unittest.TestCase):
    def test_listener_from_background_thread_goes_through_queue(self):
        controller = UiController(FakeOrchestrator())
        event = make_event(game_id="endfield", state="waiting_game",
                           message="等待窗口")

        thread = threading.Thread(target=controller.listener, args=(event,))
        thread.start()
        thread.join(timeout=5)

        drained = controller.drain()
        self.assertEqual(drained, [("event", event)])
        controller.apply_event(event)
        card = controller.cards["endfield"]
        self.assertEqual(card.state, "waiting_game")
        self.assertEqual(card.message, "等待窗口")

    def test_system_event_does_not_touch_cards(self):
        controller = UiController(FakeOrchestrator())
        event = make_event(game_id=None, state="running", message="队列开始")
        controller.apply_event(event)
        for card in controller.cards.values():
            self.assertEqual(card.state, "idle")
        self.assertEqual(controller.last_state, "running")

    def test_background_exception_becomes_visible_error_event(self):
        def boom():
            raise RuntimeError("连接失败")

        controller = UiController(FakeOrchestrator())
        thread = controller.run_blocking(boom)
        thread.join(timeout=5)

        drained = controller.drain()
        self.assertEqual(len(drained), 1)
        kind, event = drained[0]
        self.assertEqual(kind, "event")
        self.assertEqual(event.state, "failed")
        self.assertEqual(event.error_code, "FRONTEND_ERROR")
        self.assertIn("连接失败", event.message)


class ExecutionControlTests(unittest.TestCase):
    def test_retry_skip_stop_call_orchestrator_methods(self):
        orch = FakeOrchestrator()
        controller = UiController(orch)
        controller.retry_current()
        controller.skip_current()
        controller.request_stop()
        self.assertEqual(
            orch.calls, ["retry_current", "skip_current", "request_stop"]
        )


class AutomaticStartTests(unittest.TestCase):
    def make_app(self, auto_start=True, pending_changes=()):
        app = DailyGamesApp.__new__(DailyGamesApp)
        app._auto_start_pending = auto_start
        app._initial_setup_in_progress = False
        app._initial_setup_resolved = False
        app._initial_check_results = []
        app.auto_start_logs = []
        app.auto_start_calls = []
        app.wizard_calls = []
        app.plan_calls = []

        class Controller:
            def plan_configuration_changes(self, on_done):
                app.plan_calls.append("plan")
                on_done(list(pending_changes))

        app.controller = Controller()
        app._append_log = app.auto_start_logs.append
        app._on_start_all = (
            lambda *, auto_exit=False:
            app.auto_start_calls.append(("run_all", auto_exit))
        )
        app._show_setup_wizard = app.wizard_calls.append
        return app

    def test_passing_initial_check_starts_full_queue_exactly_once(self):
        app = self.make_app()
        results = [SimpleNamespace(ok=True)]

        app._on_initial_check(results)
        app._on_initial_check(results)

        self.assertEqual(app.auto_start_calls, [("run_all", True)])
        self.assertEqual(len(app.auto_start_logs), 1)
        self.assertIn("自动开始全部任务", app.auto_start_logs[0])
        self.assertEqual(app.wizard_calls, [])
        self.assertEqual(app.plan_calls, ["plan"])

    def test_failed_initial_check_opens_wizard_and_never_auto_starts(self):
        app = self.make_app()
        results = [SimpleNamespace(ok=False)]

        app._on_initial_check(results)

        self.assertEqual(app.auto_start_calls, [])
        self.assertEqual(app.wizard_calls, [results])
        self.assertFalse(app._auto_start_pending)
        self.assertEqual(app.plan_calls, [])

    def test_pending_configuration_change_opens_wizard_before_auto_start(self):
        pending = [SimpleNamespace(change_id="maa.autorun")]
        app = self.make_app(pending_changes=pending)
        results = [SimpleNamespace(ok=True)]

        app._on_initial_check(results)

        self.assertEqual(app.auto_start_calls, [])
        self.assertEqual(app.wizard_calls, [results])
        self.assertFalse(app._auto_start_pending)
        self.assertEqual(app.plan_calls, ["plan"])

    def test_regular_gui_keeps_manual_start_behavior(self):
        app = self.make_app(auto_start=False)

        app._on_initial_check([SimpleNamespace(ok=True)])

        self.assertEqual(app.auto_start_calls, [])
        self.assertEqual(app.plan_calls, ["plan"])


class CloseWindowTests(unittest.TestCase):
    def test_idle_close_needs_no_choice(self):
        orch = FakeOrchestrator()
        controller = UiController(orch)
        self.assertEqual(controller.close_choices(), ())

    def test_running_close_offers_three_branches(self):
        orch = FakeOrchestrator()
        orch.is_running = True
        controller = UiController(orch)
        self.assertEqual(
            controller.close_choices(),
            (CLOSE_KEEP, CLOSE_STOP_THEN_EXIT, CLOSE_CANCEL),
        )

    def test_keep_branch_does_not_touch_processes(self):
        orch = FakeOrchestrator()
        orch.is_running = True
        controller = UiController(orch)
        self.assertEqual(controller.handle_close_choice(CLOSE_KEEP), "exit")
        self.assertEqual(orch.calls, [])
        self.assertFalse(hasattr(orch, "kill") and orch.calls)

    def test_cancel_branch_returns_to_window(self):
        orch = FakeOrchestrator()
        orch.is_running = True
        controller = UiController(orch)
        self.assertEqual(controller.handle_close_choice(CLOSE_CANCEL), "stay")
        self.assertEqual(orch.calls, [])

    def test_stop_branch_only_requests_stop(self):
        orch = FakeOrchestrator()
        orch.is_running = True
        controller = UiController(orch)
        self.assertEqual(
            controller.handle_close_choice(CLOSE_STOP_THEN_EXIT),
            "wait-stopped",
        )
        # 只能调用 request_stop，绝不直接终止进程。
        self.assertEqual(orch.calls, ["request_stop"])

    def test_unknown_choice_rejected(self):
        controller = UiController(FakeOrchestrator())
        with self.assertRaises(ValueError):
            controller.handle_close_choice("kill")


class StopThenExitTests(unittest.TestCase):
    def make_app(self, orchestrator=None):
        app = DailyGamesApp.__new__(DailyGamesApp)
        app.root = FakeTimerRoot()
        app.orchestrator = orchestrator or FakeOrchestrator()
        app.controller = UiController(app.orchestrator)
        app._exit_when_stopped = False
        app._stop_exit_wait_generation = 0
        return app

    def make_dialog_parts(self, alive=True):
        dialog = FakeCloseWidget(alive=alive)
        hint = FakeCloseWidget(alive=alive)
        children = [FakeCloseWidget(alive=alive) for _ in range(3)]
        return dialog, hint, FakeCloseButtons(children, alive=alive)

    def test_stop_finished_accepts_only_the_frozen_three_combinations(self):
        combinations = (
            (False, "running", True),
            (True, "stopped", True),
            (True, "running", False),
        )
        for is_running, current_state, expected in combinations:
            with self.subTest(is_running=is_running, current_state=current_state):
                orchestrator = FakeOrchestrator()
                orchestrator.is_running = is_running
                orchestrator.current_state = current_state
                self.assertEqual(self.make_app(orchestrator)._stop_finished(), expected)

    def test_timeout_restores_all_close_choices_without_process_operations(self):
        orchestrator = FakeOrchestrator()
        orchestrator.is_running = True
        orchestrator.current_state = "stopping"
        app = self.make_app(orchestrator)
        dialog, hint, buttons = self.make_dialog_parts()

        generation = app._start_stop_exit_wait(dialog, hint, buttons)

        self.assertTrue(app._exit_when_stopped)
        self.assertEqual(orchestrator.calls, ["request_stop"])
        self.assertEqual(app.root.after_calls[0][0], STOP_EXIT_TIMEOUT_MS)
        self.assertEqual(
            [child.state_calls[-1] for child in buttons.children],
            [["disabled"], ["disabled"], ["disabled"]],
        )

        app._on_stop_exit_timeout(generation, dialog, hint, buttons)

        self.assertFalse(app._exit_when_stopped)
        self.assertIn("停止请求仍有效", hint.config_calls[-1]["text"])
        self.assertEqual(
            [child.state_calls[-1] for child in buttons.children],
            [["!disabled"], ["!disabled"], ["!disabled"]],
        )
        self.assertEqual(orchestrator.calls, ["request_stop"])
        self.assertEqual(app.root.destroy_calls, 0)

    def test_timer_registration_failure_restores_close_choices(self):
        orchestrator = FakeOrchestrator()
        orchestrator.is_running = True
        orchestrator.current_state = "stopping"
        app = self.make_app(orchestrator)
        app.root = FailingTimerRoot()
        dialog, hint, buttons = self.make_dialog_parts()

        generation = app._start_stop_exit_wait(dialog, hint, buttons)

        self.assertFalse(app._exit_when_stopped)
        self.assertNotEqual(generation, app._stop_exit_wait_generation)
        self.assertIn("无法启动停止等待计时", hint.config_calls[-1]["text"])
        self.assertEqual(
            [child.state_calls[-1] for child in buttons.children],
            [["!disabled"], ["!disabled"], ["!disabled"]],
        )
        self.assertEqual(orchestrator.calls, ["request_stop"])
        self.assertEqual(app.root.destroy_calls, 0)

    def test_timeout_is_noop_when_finished_cancelled_or_dialog_destroyed(self):
        for mode in ("finished", "cancelled", "destroyed"):
            with self.subTest(mode=mode):
                orchestrator = FakeOrchestrator()
                orchestrator.is_running = True
                orchestrator.current_state = "stopping"
                app = self.make_app(orchestrator)
                dialog, hint, buttons = self.make_dialog_parts()
                generation = app._start_stop_exit_wait(dialog, hint, buttons)
                hint.config_calls.clear()
                for child in buttons.children:
                    child.state_calls.clear()

                if mode == "finished":
                    orchestrator.is_running = False
                elif mode == "cancelled":
                    app._cancel_stop_exit_wait()
                else:
                    dialog.alive = False

                app._on_stop_exit_timeout(generation, dialog, hint, buttons)

                self.assertEqual(hint.config_calls, [])
                self.assertEqual(
                    [child.state_calls for child in buttons.children],
                    [[], [], []],
                )
                self.assertEqual(orchestrator.calls, ["request_stop"])

    def test_safe_stop_can_restart_the_wait_with_a_new_timeout_generation(self):
        orchestrator = FakeOrchestrator()
        orchestrator.is_running = True
        orchestrator.current_state = "stopping"
        app = self.make_app(orchestrator)
        dialog, hint, buttons = self.make_dialog_parts()

        first_generation = app._start_stop_exit_wait(dialog, hint, buttons)
        first_callback = app.root.after_calls[-1][1]
        app._on_stop_exit_timeout(first_generation, dialog, hint, buttons)
        second_generation = app._start_stop_exit_wait(dialog, hint, buttons)

        self.assertGreater(second_generation, first_generation)
        self.assertEqual(
            [delay for delay, _ in app.root.after_calls],
            [STOP_EXIT_TIMEOUT_MS, STOP_EXIT_TIMEOUT_MS],
        )
        first_callback()
        self.assertTrue(app._exit_when_stopped)
        self.assertEqual(orchestrator.calls, ["request_stop", "request_stop"])


class AutoExitAfterSuccessTests(unittest.TestCase):
    def make_app(self, *, state="completed", running=False, enabled=True):
        app = DailyGamesApp.__new__(DailyGamesApp)
        app.root = FakeTimerRoot()
        app.orchestrator = FakeOrchestrator()
        app.orchestrator.current_state = state
        app.orchestrator.is_running = running
        app._auto_exit_after_success = enabled
        app._auto_exit_scheduled = False
        app.logged = []
        app._append_log = app.logged.append
        return app

    def test_successful_auto_queue_closes_after_bounded_delay(self):
        app = self.make_app()

        scheduled = app._schedule_auto_exit_if_needed()

        self.assertTrue(scheduled)
        self.assertTrue(app._auto_exit_scheduled)
        self.assertEqual(app.root.after_calls[0][0], AUTO_EXIT_SUCCESS_DELAY_MS)
        self.assertIn("自动关闭", app.logged[-1])
        app.root.after_calls[0][1]()
        self.assertEqual(app.root.destroy_calls, 1)
        self.assertFalse(app._auto_exit_after_success)

    def test_failure_running_or_manual_mode_never_auto_closes(self):
        cases = (
            ("failed", False, True),
            ("completed", True, True),
            ("completed", False, False),
        )
        for state, running, enabled in cases:
            with self.subTest(state=state, running=running, enabled=enabled):
                app = self.make_app(
                    state=state, running=running, enabled=enabled
                )
                self.assertFalse(app._schedule_auto_exit_if_needed())
                self.assertEqual(app.root.after_calls, [])
                self.assertEqual(app.root.destroy_calls, 0)

    def test_delayed_callback_rechecks_before_closing(self):
        app = self.make_app()
        app._schedule_auto_exit_if_needed()
        app.orchestrator.is_running = True
        app.orchestrator.current_state = "running"

        app.root.after_calls[0][1]()

        self.assertEqual(app.root.destroy_calls, 0)
        self.assertFalse(app._auto_exit_after_success)
        self.assertFalse(app._auto_exit_scheduled)


class ConfigAndRestoreTests(unittest.TestCase):
    def test_check_uses_orchestrator_public_api(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        seen = []
        controller.check_configuration(seen.append)
        self.assertIn("check_configuration", orch.calls)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].ok)

    def test_plan_uses_orchestrator_public_api(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        seen = []
        controller.plan_configuration_changes(seen.append)
        self.assertIn("plan_configuration_changes", orch.calls)
        self.assertEqual(len(seen[0]), 2)

    def test_restore_requires_confirmation(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        self.assertIsNone(controller.restore_backup("b1", confirmed=False))
        self.assertNotIn(("restore_backup", "b1"), orch.calls)
        controller.restore_backup("b1", confirmed=True)
        self.assertIn(("restore_backup", "b1"), orch.calls)

    def test_list_backups_uses_orchestrator_public_api(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        seen = []
        controller.list_backups(seen.append)
        self.assertIn("list_backups", orch.calls)
        self.assertEqual(seen[0][0].backup_id, "b1")


class SetupWizardTests(unittest.TestCase):
    def test_only_verified_changes_are_selectable(self):
        changes = [make_change("c1", True), make_change("c2", False)]
        self.assertEqual(selectable_change_ids(changes), {"c1"})

    def test_unverified_selection_is_rejected(self):
        changes = [make_change("c1", True), make_change("c2", False)]
        self.assertEqual(validate_change_selection(changes, ["c2"]), ["c2"])
        self.assertEqual(validate_change_selection(changes, ["c1"]), [])

    def test_apply_refuses_unverified_changes(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        changes = [make_change("c1", True), make_change("c2", False)]
        with self.assertRaises(ValueError):
            controller.apply_selected_changes(changes, ["c1", "c2"])
        self.assertFalse(
            any(isinstance(c, tuple) and c[0] == "apply_configuration_changes"
                for c in orch.calls)
        )

    def test_apply_sends_only_selected_verified_ids(self):
        orch = FakeOrchestrator()
        controller = UiController(orch, bg=sync_bg)
        changes = [make_change("c1", True), make_change("c2", False),
                   make_change("c3", True)]
        controller.apply_selected_changes(changes, ["c1", "c3"])
        self.assertIn(("apply_configuration_changes", ["c1", "c3"]), orch.calls)


class TkSmokeTests(unittest.TestCase):
    """需要显示环境的冒烟测试；无法创建 Tk 时略过。"""

    def test_app_builds_and_processes_event(self):
        try:
            import tkinter as tk

            from daily_games.ui import DailyGamesApp
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"tkinter 不可用：{exc}")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"无显示环境：{exc}")
        try:
            orch = FakeOrchestrator()
            app = DailyGamesApp(root, orch, auto_setup=False)
            self.assertEqual(len(orch.listeners), 1)
            app.controller.listener(make_event(state="running"))
            root.update_idletasks()
            root.update()
            app._poll()
            root.update_idletasks()
            card = app.controller.cards["arknights"]
            self.assertEqual(card.state, "running")
            self.assertIn("执行中", app._card_msg_vars["arknights"].get())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
