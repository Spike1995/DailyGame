"""Luna 负责的顺序编排器单元测试。

所有协作者均为内存替身。这些测试不会启动游戏、挂机工具或外部进程，
也不会修改用户的真实配置。
"""

from __future__ import annotations

import json
import threading
import time
import tempfile
import unittest
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from daily_games.adapters import build_adapters
from daily_games.config_manager import CheckResult
from daily_games.orchestrator import (
    GAME_ORDER,
    DailyOrchestrator,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_PAUSED,
    STATE_SKIPPED,
    STATE_STOPPED,
)
from daily_games.process_monitor import ProcessRecord


@dataclass(frozen=True)
class Step:
    ok: bool = True
    code: str = "ok"
    message: str = "ok"
    details: dict[str, object] = field(default_factory=dict)


OK = Step()


class FakeConfigManager:
    def __init__(self, config: dict[str, object] | None = None):
        self.config = config or {
            "timeouts": {
                "game_process_s": 11,
                "game_window_s": 17,
                "completion_s": 29,
            },
            "polling": {"interval_s": 0.01},
        }
        self.calls: list[object] = []
        self.preflight_result = []
        self.preflight_error: Exception | None = None
        self.change_plan = []
        self.plan_error: Exception | None = None
        self.apply_result = ["apply"]
        self.backups = ["backup"]
        self.restore_result = ["restore"]

    def load(self):
        self.calls.append("load")
        return self.config

    def preflight(self):
        self.calls.append("preflight")
        if self.preflight_error is not None:
            raise self.preflight_error
        return self.preflight_result

    def plan_required_changes(self):
        self.calls.append("plan")
        if self.plan_error is not None:
            raise self.plan_error
        return self.change_plan

    def apply_required_changes(self, change_ids):
        self.calls.append(("apply", list(change_ids)))
        return self.apply_result

    def list_backups(self):
        self.calls.append("list_backups")
        return self.backups

    def restore_backup(self, backup_id):
        self.calls.append(("restore_backup", backup_id))
        return self.restore_result


class FakeLogger:
    def __init__(self):
        self.entries: list[tuple[str, str]] = []

    def info(self, message):
        self.entries.append(("info", str(message)))

    def warning(self, message):
        self.entries.append(("warning", str(message)))

    def error(self, message):
        self.entries.append(("error", str(message)))


class FakeAdapter:
    """可为每个方法提供结果队列的 GameAdapter 替身。"""

    def __init__(self, outcomes: dict[str, list[Any]] | None = None):
        self.calls: list[str] = []
        self.wait_game_args: list[tuple[float, object]] = []
        self.wait_completion_args: list[tuple[float, object]] = []
        self._outcomes = {
            name: deque(values) for name, values in (outcomes or {}).items()
        }

    def _next(self, method: str, *args):
        self.calls.append(method)
        choices = self._outcomes.get(method)
        if choices:
            outcome = choices.popleft()
            if callable(outcome):
                return outcome(*args)
            return outcome
        return OK

    def check_environment(self):
        return self._next("check_environment")

    def start_game(self):
        return self._next("start_game")

    def wait_game_ready(self, timeout_s, stop_event):
        self.wait_game_args.append((timeout_s, stop_event))
        return self._next("wait_game_ready", timeout_s, stop_event)

    def start_helper(self):
        return self._next("start_helper")

    def wait_completion(self, timeout_s, stop_event):
        self.wait_completion_args.append((timeout_s, stop_event))
        return self._next("wait_completion", timeout_s, stop_event)

    def cleanup(self):
        return self._next("cleanup")


class BlockingWaitAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def wait_completion(self, timeout_s, stop_event):
        self.wait_completion_args.append((timeout_s, stop_event))
        self.calls.append("wait_completion")
        self.entered.set()
        stop_event.wait(2)
        return Step(False, "stop_requested", "已收到停止请求")


@dataclass(frozen=True)
class FakeProcess:
    pid: int


class SequenceProcessMonitor:
    """以内存进程表精确验证跨游戏退出屏障。"""

    def __init__(self, *, trace=None, refuse_exit_names=None, preexisting=None):
        self.trace = trace if trace is not None else []
        self.refuse_exit_names = {str(name).lower() for name in (refuse_exit_names or ())}
        self.running: dict[str, list[ProcessRecord]] = {}
        self._next_pid = 2000
        for name in preexisting or ():
            self.add(name)

    def add(self, name: str) -> ProcessRecord:
        record = ProcessRecord(
            pid=self._next_pid,
            executable=name,
            full_path=f"C:\\fake\\{name}",
            owned=True,
            create_time=0.0,
        )
        self._next_pid += 1
        self.running.setdefault(name.lower(), []).append(record)
        return record

    def find_by_executable(self, name):
        return list(self.running.get(str(name).lower(), []))

    def wait_for_exit(self, pid, timeout_s, stop_event=None):
        for name, records in self.running.items():
            if any(record.pid == pid for record in records):
                self.trace.append(f"exit:{name}:{pid}")
                if name in self.refuse_exit_names:
                    return False
                self.running[name] = [record for record in records if record.pid != pid]
                return True
        return True


class TransientExitSnapshotMonitor:
    """模拟进程退出后 Windows 枚举仍短暂返回同一 PID。"""

    def __init__(self):
        self.record = ProcessRecord(
            pid=2999,
            executable="Arknights.exe",
            full_path=r"C:\fake\Arknights.exe",
            owned=False,
            create_time=0.0,
        )
        self.find_calls = 0
        self.wait_calls = 0

    def find_by_executable(self, name):
        self.find_calls += 1
        # 第一帧已清空，第二帧却短暂重现已退出 PID，随后稳定清空。
        if str(name).lower() == "arknights.exe" and self.find_calls == 2:
            return [self.record]
        return []

    def wait_for_exit(self, pid, timeout_s, stop_event=None):
        self.wait_calls += 1
        return True


class SequenceAdapter(FakeAdapter):
    def __init__(self, game_id, game_name, helper_name, monitor, trace):
        super().__init__()
        self.game_id = game_id
        self.game_name = game_name
        self.helper_name = helper_name
        self.monitor = monitor
        self.trace = trace

    def start_game(self):
        self.calls.append("start_game")
        self.trace.append(f"{self.game_id}:start_game")
        self.monitor.add(self.game_name)
        return OK

    def start_helper(self):
        self.calls.append("start_helper")
        self.trace.append(f"{self.game_id}:start_helper")
        self.monitor.add(self.helper_name)
        return OK

    def wait_completion(self, timeout_s, stop_event):
        self.calls.append("wait_completion")
        self.wait_completion_args.append((timeout_s, stop_event))
        self.trace.append(f"{self.game_id}:wait_completion")
        return OK

    def cleanup(self):
        self.calls.append("cleanup")
        self.trace.append(f"{self.game_id}:cleanup")
        return OK


@dataclass(frozen=True)
class FakeConfigChange:
    change_id: str
    tool: str
    field: str
    verified: bool
    warning: str = ""
    current_display: str = "敏感当前值"
    proposed_display: str = "敏感建议值"


class ReadOnlyProcessMonitor:
    """记录 dry-run 查询，并拒绝所有修改或等待操作。"""

    def __init__(self, running=None, errors=None):
        self.running = dict(running or {})
        self.errors = dict(errors or {})
        self.find_calls: list[str] = []
        self.unsafe_calls: list[str] = []

    def find_by_executable(self, name):
        self.find_calls.append(name)
        error = self.errors.get(name)
        if error is not None:
            raise error
        return list(self.running.get(name, []))

    def _unsafe(self, name):
        self.unsafe_calls.append(name)
        raise AssertionError(f"dry_run 不得调用 {name}")

    def launch(self, *args, **kwargs):
        return self._unsafe("launch")

    def take_ownership(self, *args, **kwargs):
        return self._unsafe("take_ownership")

    def terminate_if_owned(self, *args, **kwargs):
        return self._unsafe("terminate_if_owned")

    def wait_for_process(self, *args, **kwargs):
        return self._unsafe("wait_for_process")

    def wait_for_window(self, *args, **kwargs):
        return self._unsafe("wait_for_window")

    def wait_for_exit(self, *args, **kwargs):
        return self._unsafe("wait_for_exit")


class BridgeProcessMonitor:
    """真实适配器安全桥接使用的监控替身；它绝不会启动进程。"""

    def __init__(self):
        self.launch_calls: list[list[str]] = []
        self.terminate_calls: list[int] = []

    def find_by_executable(self, name):
        return []

    def launch(self, command, cwd=None):
        self.launch_calls.append([str(part) for part in command])
        return ProcessRecord(
            pid=9000 + len(self.launch_calls),
            executable=Path(str(command[0])).name,
            full_path=str(command[0]),
            owned=True,
            create_time=0.0,
        )

    def wait_for_process(self, name, timeout_s, stop_event=None):
        return []

    def wait_for_window(self, pid, timeout_s, stop_event=None, title_regex=None):
        return []

    def wait_for_exit(self, pid, timeout_s, stop_event=None):
        return True

    def terminate_if_owned(self, pid, grace_s=5.0):
        self.terminate_calls.append(pid)
        return True


def make_adapters(arknights=None, endfield=None, limbus=None):
    return {
        "arknights": arknights or FakeAdapter(),
        "endfield": endfield or FakeAdapter(),
        "limbus": limbus or FakeAdapter(),
    }


def make_orchestrator(*, config=None, adapters=None, logger=None):
    return DailyOrchestrator(
        Path("test-root") / "config" / "user.json",
        config_manager=FakeConfigManager(config),
        process_monitor=object(),
        adapters=adapters or make_adapters(),
        logger=logger or FakeLogger(),
    )


def dry_run_config():
    return {
        "polling": {"interval_s": 0.01},
        "games": {
            "arknights": {
                "game_process_name": "Arknights.exe",
                "helper": {"process_name": "MAA.exe"},
            },
            "endfield": {
                "game_process_name": "Endfield.exe",
                "helper": {"process_name": "MaaEnd.exe"},
            },
            "limbus": {
                "game_process_name": "LimbusCompany.exe",
                "helper": {"process_name": "AALC.exe"},
            },
        },
    }


def sequential_config():
    config = dry_run_config()
    config["timeouts"] = {
        "game_window_s": 1,
        "completion_s": 1,
        "cleanup_s": 1,
    }
    return config


def bridge_arknights_config(
    root: Path,
    *,
    helper_exists: bool,
    run_directly: bool,
    post_actions: str,
) -> dict[str, object]:
    """为真实 ``build_adapters`` 安全门控桥接创建临时配置。"""
    launcher = root / "Launcher.exe"
    game = root / "Arknights.exe"
    helper = root / "MAA.exe"
    launcher.write_text("fake", encoding="utf-8")
    game.write_text("fake", encoding="utf-8")
    if helper_exists:
        helper.write_text("fake", encoding="utf-8")
    config_dir = root / "maa_config"
    config_dir.mkdir()
    (config_dir / "gui.new.json").write_text(json.dumps({
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
    return {
        "timeouts": {"game_window_s": 1, "completion_s": 1},
        "polling": {"interval_s": 0.01},
        "games": {
            "arknights": {
                "display_name": "Arknights",
                "launcher": {
                    "executable": str(launcher),
                    "args": ["--game=arknights"],
                    "verified": True,
                },
                "game_executable": str(game),
                "game_process_name": "Arknights.exe",
                "helper": {
                    "executable": str(helper),
                    "process_name": "MAA.exe",
                    "config_dir": str(config_dir),
                    "config_file": "gui.new.json",
                },
            },
            "endfield": {},
            "limbus": {},
        },
    }


def run_in_thread(callable_):
    box: dict[str, object] = {}

    def target():
        try:
            box["result"] = callable_()
        except BaseException as exc:  # 由测试线程向外传递
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class OrchestratorTests(unittest.TestCase):
    def test_runs_three_games_in_frozen_order_with_full_stage_sequence(self):
        adapters = make_adapters()
        orchestrator = make_orchestrator(adapters=adapters)

        report = orchestrator.run_all()

        self.assertTrue(report.ok)
        self.assertFalse(report.stopped)
        self.assertEqual([result.game_id for result in report.results], list(GAME_ORDER))
        for game_id in GAME_ORDER:
            self.assertEqual(
                adapters[game_id].calls,
                [
                    "check_environment", "start_game", "wait_game_ready",
                    "start_helper", "wait_completion", "cleanup",
                ],
            )
        self.assertEqual(orchestrator.current_state, STATE_COMPLETED)
        self.assertIsNone(orchestrator.current_game)

    def test_managed_logger_writes_debug_serial_queue_plan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = sequential_config()
            config["paths"] = {"logs_dir": tmp_dir}
            config["logging"] = {"level": "DEBUG"}
            orchestrator = DailyOrchestrator(
                Path(tmp_dir) / "config" / "user.json",
                config_manager=FakeConfigManager(config),
                process_monitor=SequenceProcessMonitor(),
                adapters=make_adapters(),
            )

            report = orchestrator.run_all()

            self.assertTrue(report.ok)
            log_files = list(Path(tmp_dir).glob("daily_games_*.log"))
            self.assertEqual(len(log_files), 1)
            log_text = log_files[0].read_text(encoding="utf-8")
            self.assertIn("DEBUG", log_text)
            self.assertIn(
                "串行执行顺序=arknights,endfield,limbus",
                log_text,
            )

    def test_next_game_starts_only_after_previous_game_and_helper_exit(self):
        trace: list[str] = []
        monitor = SequenceProcessMonitor(trace=trace)
        specs = {
            "arknights": ("Arknights.exe", "MAA.exe"),
            "endfield": ("Endfield.exe", "MaaEnd.exe"),
            "limbus": ("LimbusCompany.exe", "AALC.exe"),
        }
        adapters = {
            game_id: SequenceAdapter(game_id, *specs[game_id], monitor, trace)
            for game_id in GAME_ORDER
        }
        orchestrator = DailyOrchestrator(
            Path("test-root") / "config" / "user.json",
            config_manager=FakeConfigManager(sequential_config()),
            process_monitor=monitor,
            adapters=adapters,
            logger=FakeLogger(),
        )

        report = orchestrator.run_all()

        self.assertTrue(report.ok)
        for previous, following in zip(GAME_ORDER, GAME_ORDER[1:]):
            previous_game, previous_helper = specs[previous]
            next_start = trace.index(f"{following}:start_game")
            game_exit = next(
                index for index, item in enumerate(trace)
                if item.startswith(f"exit:{previous_game.lower()}:")
            )
            helper_exit = next(
                index for index, item in enumerate(trace)
                if item.startswith(f"exit:{previous_helper.lower()}:")
            )
            self.assertLess(game_exit, next_start)
            self.assertLess(helper_exit, next_start)

    def test_lingering_game_or_helper_blocks_next_game(self):
        for lingering_name in ("Arknights.exe", "MAA.exe"):
            with self.subTest(lingering=lingering_name):
                trace: list[str] = []
                monitor = SequenceProcessMonitor(
                    trace=trace,
                    refuse_exit_names={lingering_name},
                )
                arknights = SequenceAdapter(
                    "arknights", "Arknights.exe", "MAA.exe", monitor, trace,
                )
                endfield = SequenceAdapter(
                    "endfield", "Endfield.exe", "MaaEnd.exe", monitor, trace,
                )
                adapters = make_adapters(arknights=arknights, endfield=endfield)
                orchestrator = DailyOrchestrator(
                    Path("test-root") / "config" / "user.json",
                    config_manager=FakeConfigManager(sequential_config()),
                    process_monitor=monitor,
                    adapters=adapters,
                    logger=FakeLogger(),
                )
                paused = threading.Event()
                orchestrator.add_listener(
                    lambda event: paused.set() if event.state == STATE_PAUSED else None
                )

                thread, box = run_in_thread(orchestrator.run_all)
                self.assertTrue(paused.wait(2))
                self.assertNotIn("start_game", endfield.calls)
                orchestrator.request_stop()
                thread.join(3)

                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", box)
                self.assertEqual(
                    box["result"].results[0].code,
                    "managed_process_still_running",
                )

    def test_exit_barrier_rechecks_transient_post_exit_snapshot(self):
        monitor = TransientExitSnapshotMonitor()
        orchestrator = make_orchestrator()
        config = {
            "games": {
                "arknights": {
                    "game_process_name": "Arknights.exe",
                    "helper": {},
                },
            },
        }

        result = orchestrator._wait_for_current_processes_exit(
            "arknights",
            config,
            monitor,
            timeout_s=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "managed_processes_exited")
        self.assertGreaterEqual(monitor.find_calls, 4)
        self.assertEqual(monitor.wait_calls, 1)

    def test_preexisting_managed_process_blocks_every_adapter_start(self):
        monitor = SequenceProcessMonitor(preexisting={"Endfield.exe"})
        adapters = make_adapters()
        orchestrator = DailyOrchestrator(
            Path("test-root") / "config" / "user.json",
            config_manager=FakeConfigManager(sequential_config()),
            process_monitor=monitor,
            adapters=adapters,
            logger=FakeLogger(),
        )
        paused = threading.Event()
        orchestrator.add_listener(
            lambda event: paused.set() if event.state == STATE_PAUSED else None
        )

        thread, box = run_in_thread(orchestrator.run_all)
        self.assertTrue(paused.wait(2))
        for adapter in adapters.values():
            self.assertNotIn("start_game", adapter.calls)
        orchestrator.request_stop()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            box["result"].results[0].code,
            "managed_process_already_running",
        )

    def test_run_one_does_not_touch_other_adapters(self):
        adapters = make_adapters()
        orchestrator = make_orchestrator(adapters=adapters)

        report = orchestrator.run_one("endfield")

        self.assertTrue(report.ok)
        self.assertEqual([result.game_id for result in report.results], ["endfield"])
        self.assertEqual(adapters["arknights"].calls, [])
        self.assertEqual(adapters["limbus"].calls, [])
        self.assertEqual(adapters["endfield"].calls[-1], "cleanup")

    def test_rejects_unknown_game_id(self):
        with self.assertRaises(ValueError):
            make_orchestrator().run_one("unknown")

    def test_same_instance_rejects_reentrant_run(self):
        blocking = BlockingWaitAdapter()
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=blocking))
        thread, box = run_in_thread(orchestrator.run_all)
        self.assertTrue(blocking.entered.wait(2))

        with self.assertRaises(RuntimeError):
            orchestrator.run_one("endfield")

        orchestrator.request_stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        self.assertTrue(box["result"].stopped)

    def test_failure_emits_failed_then_paused_and_waits_for_control(self):
        failing = FakeAdapter({"start_game": [Step(False, "launch_failed", "无法启动")]})
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=failing))
        events = []
        paused = threading.Event()

        def listener(event):
            events.append(event)
            if event.state == STATE_PAUSED:
                paused.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(orchestrator.run_all)
        self.assertTrue(paused.wait(2))
        self.assertTrue(orchestrator.is_running)
        self.assertEqual(orchestrator.current_state, STATE_PAUSED)
        self.assertEqual(failing.calls, ["check_environment", "start_game", "cleanup"])

        orchestrator.request_stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        self.assertTrue(box["result"].stopped)
        self.assertEqual(box["result"].results[0].code, "launch_failed")
        states = [event.state for event in events]
        self.assertLess(states.index(STATE_FAILED), states.index(STATE_PAUSED))

    def test_retry_reexecutes_current_game_after_pause(self):
        flaky = FakeAdapter({"start_game": [Step(False, "launch_failed", "首次失败"), OK]})
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=flaky))
        paused = threading.Event()
        orchestrator.add_listener(lambda event: paused.set() if event.state == STATE_PAUSED else None)

        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(paused.wait(2))
        orchestrator.retry_current()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        report = box["result"]
        self.assertTrue(report.ok)
        self.assertEqual(report.results[0].state, STATE_COMPLETED)
        self.assertEqual(flaky.calls.count("start_game"), 2)
        self.assertEqual(flaky.calls.count("cleanup"), 2)

    def test_skip_records_skipped_result_then_continues_queue(self):
        failing = FakeAdapter({"check_environment": [Step(False, "env_bad", "环境异常")]})
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=failing))
        paused = threading.Event()
        orchestrator.add_listener(lambda event: paused.set() if event.state == STATE_PAUSED else None)

        thread, box = run_in_thread(orchestrator.run_all)
        self.assertTrue(paused.wait(2))
        orchestrator.skip_current()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        report = box["result"]
        self.assertFalse(report.ok)
        self.assertFalse(report.stopped)
        self.assertEqual(
            [result.state for result in report.results],
            [STATE_SKIPPED, STATE_COMPLETED, STATE_COMPLETED],
        )
        self.assertEqual(report.results[0].code, "env_bad")

    def test_stop_event_is_passed_to_waits_and_stops_cooperatively(self):
        blocking = BlockingWaitAdapter()
        config = {
            "timeouts": {
                "game_process_s": 3,
                "game_window_s": 7,
                "completion_s": 13,
            }
        }
        orchestrator = make_orchestrator(config=config, adapters=make_adapters(arknights=blocking))
        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(blocking.entered.wait(2))
        self.assertEqual(blocking.wait_game_args[0][0], 7.0)
        self.assertEqual(blocking.wait_completion_args[0][0], 13.0)
        self.assertFalse(blocking.wait_completion_args[0][1].is_set())

        orchestrator.request_stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        report = box["result"]
        self.assertTrue(report.stopped)
        self.assertEqual(report.results[0].state, STATE_STOPPED)
        self.assertEqual(blocking.calls[-1], "cleanup")

    def test_stopped_listener_can_observe_running_until_run_finally_finishes(self):
        blocking = BlockingWaitAdapter()
        orchestrator = make_orchestrator(
            adapters=make_adapters(arknights=blocking)
        )
        observed_running = []
        stopped_event = threading.Event()

        def listener(event):
            if event.state == STATE_STOPPED:
                observed_running.append(orchestrator.is_running)
                stopped_event.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(blocking.entered.wait(2))
        orchestrator.request_stop()
        self.assertTrue(stopped_event.wait(2))
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        self.assertTrue(any(observed_running))
        self.assertFalse(orchestrator.is_running)
        self.assertIsNone(orchestrator.current_game)
        self.assertEqual(orchestrator.current_state, STATE_STOPPED)

    def test_wait_completion_timeout_emits_failed_then_paused_and_preserves_code(self):
        adapter = FakeAdapter({
            "wait_completion": [Step(False, "wait_timeout", "日常任务超时")],
        })
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=adapter))
        states = []
        paused = threading.Event()

        def listener(event):
            states.append(event.state)
            if event.state == STATE_PAUSED:
                paused.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(paused.wait(2))
        orchestrator.request_stop()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        report = box["result"]
        self.assertTrue(report.stopped)
        self.assertEqual(report.results[0].state, STATE_FAILED)
        self.assertEqual(report.results[0].code, "wait_timeout")
        self.assertLess(states.index(STATE_FAILED), states.index(STATE_PAUSED))
        self.assertEqual(adapter.calls[-1], "cleanup")

    def test_listener_cancellation_and_listener_exception_do_not_break_run(self):
        orchestrator = make_orchestrator()
        once = []
        all_events = []

        def once_listener(event):
            once.append(event)
            unsubscribe()

        unsubscribe = orchestrator.add_listener(once_listener)
        orchestrator.add_listener(lambda event: all_events.append(event))
        orchestrator.add_listener(lambda event: (_ for _ in ()).throw(RuntimeError("监听器异常")))

        report = orchestrator.run_one("arknights")

        self.assertTrue(report.ok)
        self.assertEqual(len(once), 1)
        self.assertGreater(len(all_events), 1)
        for event in all_events:
            self.assertIsNotNone(datetime.fromisoformat(event.timestamp).tzinfo)

    def test_configuration_methods_only_delegate_to_config_manager(self):
        config = FakeConfigManager()
        config.change_plan = ["plan"]
        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor=object(),
            adapters=make_adapters(),
            logger=FakeLogger(),
        )

        self.assertIs(orchestrator.check_configuration(), config.preflight_result)
        self.assertIs(orchestrator.plan_configuration_changes(), config.change_plan)
        self.assertIs(orchestrator.apply_configuration_changes(["c1"]), config.apply_result)
        self.assertIs(orchestrator.list_backups(), config.backups)
        self.assertIs(orchestrator.restore_backup("b1"), config.restore_result)
        self.assertEqual(
            config.calls,
            [
                "preflight", "plan", ("apply", ["c1"]),
                "list_backups", ("restore_backup", "b1"),
            ],
        )

    def test_dry_run_preserves_preflight_and_checks_all_six_process_names_read_only(self):
        config = FakeConfigManager(dry_run_config())
        preflight_marker = object()
        config.preflight_result = [preflight_marker]
        config.change_plan = [
            FakeConfigChange("maa.autorun", "MAA", "Start.RunDirectly", False, "需要确认"),
            FakeConfigChange("maaend.autorun", "MaaEnd", "autoRunOnLaunch", True),
            FakeConfigChange("aalc.autostart", "AALC", "autostart", True),
            FakeConfigChange("aalc.game_path", "AALC", "game_path", False, "需要手动修复"),
            FakeConfigChange("aalc.image_resource_sync", "AALC", "image_resource_sync", True),
        ]
        monitor = ReadOnlyProcessMonitor({
            "Arknights.exe": [FakeProcess(101)],
            "AALC.exe": [FakeProcess(602)],
        })
        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor=monitor,
            adapters=make_adapters(),
            logger=FakeLogger(),
        )

        results = orchestrator.dry_run()

        self.assertIs(results[0], preflight_marker)
        changes = results[1:6]
        self.assertEqual(len(changes), 5)
        self.assertEqual(changes[0].name, "configuration.change.maa.autorun")
        self.assertEqual(changes[0].code, "manual_configuration_required")
        self.assertEqual(changes[1].code, "configuration_change_required")
        self.assertTrue(all(not result.ok and result.severity == "warning" for result in changes))
        for result in changes:
            self.assertEqual(set(result.details), {"change_id", "tool", "field", "verified"})
            self.assertNotIn("敏感当前值", result.message)
            self.assertNotIn("敏感建议值", result.message)
        self.assertEqual(
            monitor.find_calls,
            [
                "Arknights.exe", "MAA.exe", "Endfield.exe", "MaaEnd.exe",
                "LimbusCompany.exe", "AALC.exe",
            ],
        )
        self.assertEqual(monitor.unsafe_calls, [])
        self.assertEqual(config.calls, ["preflight", "plan", "load"])
        by_name = {result.name: result for result in results[6:]}
        self.assertEqual(len(by_name), 6)
        self.assertTrue(all(result.ok and result.severity == "info" for result in by_name.values()))
        self.assertEqual(by_name["games.arknights.game_process_name"].code, "process_running")
        self.assertEqual(by_name["games.arknights.game_process_name"].details, {"count": 1, "pids": [101]})
        self.assertEqual(by_name["games.arknights.helper.process_name"].code, "process_not_running")
        self.assertEqual(by_name["games.limbus.helper.process_name"].code, "process_running")

    def test_dry_run_reports_missing_names_and_query_errors_without_losing_other_results(self):
        config_data = dry_run_config()
        config_data["games"]["endfield"]["game_process_name"] = ""
        config = FakeConfigManager(config_data)
        preflight_marker = object()
        config.preflight_result = [preflight_marker]
        monitor = ReadOnlyProcessMonitor(errors={"AALC.exe": RuntimeError("查询失败")})
        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor=monitor,
            adapters=make_adapters(),
            logger=FakeLogger(),
        )

        results = orchestrator.dry_run()

        self.assertIs(results[0], preflight_marker)
        by_name = {result.name: result for result in results[1:]}
        self.assertEqual(len(by_name), 6)
        self.assertEqual(by_name["games.endfield.game_process_name"].code, "process_name_missing")
        self.assertFalse(by_name["games.endfield.game_process_name"].ok)
        self.assertEqual(by_name["games.limbus.helper.process_name"].code, "process_query_failed")
        self.assertFalse(by_name["games.limbus.helper.process_name"].ok)
        self.assertTrue(by_name["games.arknights.game_process_name"].ok)
        self.assertEqual(monitor.unsafe_calls, [])

    def test_dry_run_reports_plan_exception_and_still_queries_processes(self):
        config = FakeConfigManager(dry_run_config())
        preflight_marker = object()
        config.preflight_result = [preflight_marker]
        config.plan_error = RuntimeError("配置规划不可用")
        monitor = ReadOnlyProcessMonitor()
        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor=monitor,
            adapters=make_adapters(),
            logger=FakeLogger(),
        )

        results = orchestrator.dry_run()

        self.assertIs(results[0], preflight_marker)
        self.assertEqual(results[1].name, "configuration.plan")
        self.assertEqual(results[1].code, "configuration_plan_failed")
        self.assertFalse(results[1].ok)
        self.assertEqual(len(monitor.find_calls), 6)
        self.assertEqual(monitor.unsafe_calls, [])
        self.assertEqual(config.calls, ["preflight", "plan", "load"])

    def test_runtime_preflight_failure_short_circuits_plan_and_runtime_build(self):
        config = FakeConfigManager()
        config.preflight_result = [CheckResult(
            name="games.arknights.helper.executable",
            ok=False,
            code="file_not_found",
            message="缺失",
            severity="error",
            details={},
        )]
        monitor_factory_calls = []
        adapter_factory_calls = []
        events = []

        def monitor_factory(*, poll_interval_s):
            monitor_factory_calls.append(poll_interval_s)
            return object()

        def adapter_factory(loaded_config, loaded_monitor, loaded_logger):
            adapter_factory_calls.append((loaded_config, loaded_monitor, loaded_logger))
            return make_adapters()

        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor_factory=monitor_factory,
            adapters_factory=adapter_factory,
            logger=FakeLogger(),
        )
        orchestrator.add_listener(events.append)

        report = orchestrator.run_one("arknights")

        self.assertFalse(report.ok)
        self.assertFalse(report.stopped)
        self.assertEqual(report.results, ())
        self.assertEqual(config.calls, ["preflight"])
        self.assertEqual(monitor_factory_calls, [])
        self.assertEqual(adapter_factory_calls, [])
        self.assertEqual(events[-1].state, STATE_FAILED)
        self.assertEqual(events[-1].error_code, "configuration_not_ready")

    def test_runtime_pending_configuration_blocks_build_and_start(self):
        config = FakeConfigManager()
        config.change_plan = [FakeConfigChange(
            "maaend.autorun", "MaaEnd", "autoRunOnLaunch", True,
        )]
        adapter_factory_calls = []
        events = []

        def adapter_factory(loaded_config, loaded_monitor, loaded_logger):
            adapter_factory_calls.append((loaded_config, loaded_monitor, loaded_logger))
            return make_adapters()

        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor_factory=lambda **kwargs: object(),
            adapters_factory=adapter_factory,
            logger=FakeLogger(),
        )
        orchestrator.add_listener(events.append)

        report = orchestrator.run_all()

        self.assertFalse(report.ok)
        self.assertEqual(report.results, ())
        self.assertEqual(config.calls, ["preflight", "plan"])
        self.assertEqual(adapter_factory_calls, [])
        self.assertEqual(events[-1].error_code, "configuration_confirmation_required")

    def test_runtime_preflight_and_plan_exceptions_fail_closed_before_build(self):
        for phase in ("preflight", "plan"):
            with self.subTest(phase=phase):
                config = FakeConfigManager()
                if phase == "preflight":
                    config.preflight_error = RuntimeError("预检不可用")
                else:
                    config.plan_error = RuntimeError("配置规划不可用")
                adapter_factory_calls = []

                def adapter_factory(loaded_config, loaded_monitor, loaded_logger):
                    adapter_factory_calls.append((loaded_config, loaded_monitor, loaded_logger))
                    return make_adapters()

                orchestrator = DailyOrchestrator(
                    "test-root/config/user.json",
                    config_manager=config,
                    process_monitor_factory=lambda **kwargs: object(),
                    adapters_factory=adapter_factory,
                    logger=FakeLogger(),
                )
                events = []
                orchestrator.add_listener(events.append)

                report = orchestrator.run_one("arknights")

                self.assertFalse(report.ok)
                self.assertEqual(report.results, ())
                self.assertEqual(
                    config.calls,
                    ["preflight"] if phase == "preflight" else ["preflight", "plan"],
                )
                self.assertEqual(adapter_factory_calls, [])
                self.assertEqual(events[-1].error_code, "configuration_not_ready")

    def test_real_build_adapters_preserves_safety_codes_before_start_game(self):
        cases = (
            ("environment_not_ready", False, False, "ExitArknights, ExitSelf"),
            ("manual_configuration_required", True, False, "ExitArknights, ExitSelf"),
            ("completion_signal_unset", True, True, "ExitSelf"),
        )
        for expected_code, helper_exists, run_directly, post_actions in cases:
            with self.subTest(code=expected_code), tempfile.TemporaryDirectory() as td:
                monitor = BridgeProcessMonitor()
                config = FakeConfigManager(bridge_arknights_config(
                    Path(td),
                    helper_exists=helper_exists,
                    run_directly=run_directly,
                    post_actions=post_actions,
                ))
                orchestrator = DailyOrchestrator(
                    Path(td) / "user.json",
                    config_manager=config,
                    process_monitor=monitor,
                    adapters_factory=build_adapters,
                    logger=FakeLogger(),
                )
                events = []
                paused = threading.Event()

                def listener(event):
                    events.append(event)
                    if event.state == STATE_PAUSED:
                        paused.set()

                orchestrator.add_listener(listener)
                thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
                self.assertTrue(paused.wait(2))
                self.assertEqual(monitor.launch_calls, [])
                self.assertEqual(
                    [event.error_code for event in events if event.state == STATE_FAILED],
                    [expected_code],
                )
                self.assertEqual(
                    [event.error_code for event in events if event.state == STATE_PAUSED],
                    [expected_code],
                )

                orchestrator.request_stop()
                thread.join(3)

                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", box)
                self.assertEqual(box["result"].results[0].code, expected_code)

    def test_runtime_constructs_process_monitor_and_adapters_from_frozen_factories(self):
        config = FakeConfigManager({
            "timeouts": {"game_window_s": 5, "completion_s": 9},
            "polling": {"interval_s": 0.25},
        })
        seen: dict[str, object] = {}
        monitor = object()

        def monitor_factory(*, poll_interval_s):
            seen["poll_interval_s"] = poll_interval_s
            return monitor

        def adapter_factory(loaded_config, loaded_monitor, loaded_logger):
            seen["config"] = loaded_config
            seen["monitor"] = loaded_monitor
            seen["logger"] = loaded_logger
            return make_adapters()

        logger = FakeLogger()
        orchestrator = DailyOrchestrator(
            "test-root/config/user.json",
            config_manager=config,
            process_monitor_factory=monitor_factory,
            adapters_factory=adapter_factory,
            logger=logger,
        )

        report = orchestrator.run_one("arknights")

        self.assertTrue(report.ok)
        self.assertEqual(seen["poll_interval_s"], 0.25)
        self.assertIs(seen["config"], config.config)
        self.assertIs(seen["monitor"], monitor)
        self.assertIs(seen["logger"], logger)

    def test_not_owned_cleanup_after_success_keeps_success_with_warning(self):
        logger = FakeLogger()
        adapter = FakeAdapter({"cleanup": [Step(False, "not_owned", "已被复用")]})
        orchestrator = make_orchestrator(
            adapters=make_adapters(arknights=adapter), logger=logger
        )

        report = orchestrator.run_one("arknights")

        self.assertTrue(report.ok)
        self.assertEqual(report.results[0].state, STATE_COMPLETED)
        self.assertTrue(report.results[0].ok)
        self.assertIn("not_owned", report.results[0].message)
        self.assertTrue(any(level == "warning" and "not_owned" in message for level, message in logger.entries))

    def test_other_cleanup_failure_is_failed_and_paused(self):
        adapter = FakeAdapter({"cleanup": [Step(False, "cleanup_failed", "无法清理")]})
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=adapter))
        paused = threading.Event()
        orchestrator.add_listener(lambda event: paused.set() if event.state == STATE_PAUSED else None)

        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(paused.wait(2))
        orchestrator.request_stop()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        result = box["result"].results[0]
        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.code, "cleanup_failed")

    def test_phase_failure_is_not_overwritten_by_cleanup_failure(self):
        adapter = FakeAdapter({
            "start_game": [Step(False, "launch_failed", "无法启动")],
            "cleanup": [Step(False, "cleanup_failed", "无法清理")],
        })
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=adapter))
        paused = threading.Event()
        orchestrator.add_listener(lambda event: paused.set() if event.state == STATE_PAUSED else None)

        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(paused.wait(2))
        orchestrator.request_stop()
        thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        result = box["result"].results[0]
        self.assertEqual(result.code, "launch_failed")
        self.assertEqual(result.state, STATE_FAILED)


if __name__ == "__main__":
    unittest.main()
