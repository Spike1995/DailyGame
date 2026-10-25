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
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from daily_games.adapters import build_adapters
from daily_games.config_manager import CheckResult
from daily_games.orchestrator import (
    GAME_ORDER,
    DailyOrchestrator,
    GameResult,
    RunReport,
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


class StoppedBarrierMonitor:
    """启动屏障阶段一律视为无进程；退出屏障阶段按剧本演绎指定结论。

    配合 ``StoppedRunAdapter`` 在 cleanup 时切入 ``exit_phase``，确保两道
    屏障查询到的是同一张内存进程表，全程不触碰真实进程。
    """

    def __init__(self, exit_outcome):
        self.exit_outcome = exit_outcome
        self.exit_phase = False
        self.wait_calls: list[int] = []
        self.unsafe_calls: list[str] = []

    def find_by_executable(self, name):
        if not self.exit_phase:
            return []
        if self.exit_outcome == "query_failed":
            raise RuntimeError("枚举失败")
        if self.exit_outcome == "ok":
            return []
        return [ProcessRecord(
            pid=4100,
            executable=str(name),
            full_path=f"C:\\fake\\{name}",
            owned=False,
            create_time=0.0,
        )]

    def wait_for_exit(self, pid, timeout_s, stop_event=None):
        self.wait_calls.append(pid)
        if self.exit_outcome == "wait_failed":
            raise RuntimeError("等待失败")
        return False

    def launch(self, *args, **kwargs):
        return self._unsafe("launch")

    def take_ownership(self, *args, **kwargs):
        return self._unsafe("take_ownership")

    def terminate_if_owned(self, *args, **kwargs):
        return self._unsafe("terminate_if_owned")

    def _unsafe(self, name):
        self.unsafe_calls.append(name)
        raise AssertionError(f"停止诊断用例不得调用真实进程操作 {name}")


class StoppedRunAdapter(FakeAdapter):
    """wait_completion 阻塞到停止请求；cleanup 前切入退出屏障剧本。"""

    def __init__(self, monitor, cleanup_outcome=None):
        outcomes = {"cleanup": [cleanup_outcome]} if cleanup_outcome is not None else None
        super().__init__(outcomes)
        self.monitor = monitor
        self.entered = threading.Event()

    def wait_completion(self, timeout_s, stop_event):
        self.calls.append("wait_completion")
        self.wait_completion_args.append((timeout_s, stop_event))
        self.entered.set()
        stop_event.wait(2)
        return Step(False, "stop_requested", "已收到停止请求")

    def cleanup(self):
        self.monitor.exit_phase = True
        return super().cleanup()


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


def stopped_diagnostic_config():
    """停止诊断用例的配置：带全部进程名，退出屏障窗口很短。"""
    config = dry_run_config()
    config["timeouts"] = {
        "game_window_s": 1,
        "completion_s": 1,
        "cleanup_s": 0.2,
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

    def test_log_evidence_redacts_structured_and_text_credentials(self):
        logger = FakeLogger()
        orchestrator = make_orchestrator(logger=logger)
        orchestrator._log_step_evidence(
            "arknights",
            "start_helper",
            Step(details={
                "pid": 42,
                "command": [
                    "tool.exe",
                    "--token",
                    "SENTINEL_SPLIT",
                    "--api-key=SENTINEL_EQUALS",
                    "--header",
                    "Authorization: Bearer SENTINEL_BEARER",
                    {"password": "SENTINEL_MAPPING", "mode": "safe"},
                ],
            }),
        )
        orchestrator._log(
            "warning",
            (
                "--secret SENTINEL_TEXT "
                "Authorization: Bearer SENTINEL_DIRECT "
                "/token SENTINEL_SLASH "
                '--password "SENTINEL QUOTED SPACE"'
            ),
        )

        messages = "\n".join(message for _, message in logger.entries)
        for sentinel in (
            "SENTINEL_SPLIT",
            "SENTINEL_EQUALS",
            "SENTINEL_BEARER",
            "SENTINEL_MAPPING",
            "SENTINEL_TEXT",
            "SENTINEL_DIRECT",
            "SENTINEL_SLASH",
            "SENTINEL QUOTED SPACE",
        ):
            self.assertNotIn(sentinel, messages)
        self.assertIn("<已脱敏>", messages)
        self.assertIn("tool.exe", messages)
        self.assertIn("safe", messages)

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

    def test_per_game_completion_timeout_overrides_global_value(self):
        blocking = BlockingWaitAdapter()
        config = {
            "timeouts": {
                "game_process_s": 3,
                "game_window_s": 7,
                "completion_s": 13,
            },
            "games": {
                "limbus": {"timeouts": {"completion_s": 10800}},
            },
        }
        orchestrator = make_orchestrator(
            config=config,
            adapters=make_adapters(limbus=blocking),
        )
        thread, box = run_in_thread(lambda: orchestrator.run_one("limbus"))
        self.assertTrue(blocking.entered.wait(2))
        self.assertEqual(blocking.wait_game_args[0][0], 7.0)
        self.assertEqual(blocking.wait_completion_args[0][0], 10800.0)

        orchestrator.request_stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        self.assertTrue(box["result"].stopped)

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

    def test_stopped_game_result_combines_cleanup_and_exit_barrier_conclusions(self):
        ok_cleanup = None
        not_owned_cleanup = Step(False, "not_owned", "清理时进程已不归属本轮")
        failed_cleanup = Step(False, "cleanup_failed", "无法结束挂机进程")
        cases = [
            # (子场景, cleanup 结果, 屏障剧本, 期望 code)
            ("cleanup_ok_barrier_ok", ok_cleanup, "ok", "stop_requested"),
            ("not_owned_barrier_ok", not_owned_cleanup, "ok", "not_owned"),
            ("cleanup_failed_barrier_ok", failed_cleanup, "ok", "cleanup_failed"),
            ("cleanup_ok_barrier_still_running", ok_cleanup, "still_running", "managed_process_still_running"),
            ("cleanup_ok_barrier_query_failed", ok_cleanup, "query_failed", "managed_process_query_failed"),
            ("cleanup_ok_barrier_wait_failed", ok_cleanup, "wait_failed", "managed_process_wait_failed"),
            ("not_owned_barrier_still_running", not_owned_cleanup, "still_running", "managed_process_still_running"),
            ("cleanup_failed_barrier_query_failed", failed_cleanup, "query_failed", "managed_process_query_failed"),
        ]
        for scenario, cleanup_outcome, barrier_outcome, expected_code in cases:
            with self.subTest(scenario=scenario):
                monitor = StoppedBarrierMonitor(barrier_outcome)
                stopping = StoppedRunAdapter(monitor, cleanup_outcome)
                adapters = make_adapters(arknights=stopping)
                orchestrator = DailyOrchestrator(
                    Path("test-root") / "config" / "user.json",
                    config_manager=FakeConfigManager(stopped_diagnostic_config()),
                    process_monitor=monitor,
                    adapters=adapters,
                    logger=FakeLogger(),
                )
                events = []
                orchestrator.add_listener(events.append)

                thread, box = run_in_thread(orchestrator.run_all)
                self.assertTrue(stopping.entered.wait(2))
                orchestrator.request_stop()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", box)

                report = box["result"]
                self.assertTrue(report.stopped)
                self.assertFalse(report.ok)
                self.assertEqual(len(report.results), 1)
                result = report.results[0]
                self.assertEqual(result.game_id, "arknights")
                self.assertEqual(result.state, STATE_STOPPED)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, expected_code)

                message = result.message
                if barrier_outcome == "ok":
                    if cleanup_outcome is None:
                        self.assertIn("调度已停止", message)
                        self.assertIn("均已退出", message)
                    else:
                        self.assertIn(f"清理返回 {cleanup_outcome.code}", message)
                        self.assertIn("屏障确认全部退出", message)
                        self.assertNotIn("残留", message)
                elif barrier_outcome == "still_running":
                    self.assertIn("仍有进程未退出", message)
                    self.assertIn("本轮游戏或助手尚未退出", message)
                else:
                    self.assertIn("无法确认是否全部退出", message)
                    self.assertNotIn("仍有进程未退出", message)
                    self.assertNotIn("均已退出", message)
                if barrier_outcome != "ok" and cleanup_outcome is not None:
                    self.assertIn(f"清理返回 {cleanup_outcome.code}", message)
                    self.assertIn(expected_code, message)

                # stopped 事件携带与 GameResult 相同的 code/message。
                game_stopped_events = [
                    event for event in events
                    if event.state == STATE_STOPPED and event.game_id == "arknights"
                ]
                self.assertEqual(len(game_stopped_events), 1)
                self.assertEqual(game_stopped_events[0].error_code, result.code)
                self.assertEqual(game_stopped_events[0].message, result.message)

                # 停止分支不进入失败暂停，也不启动下一款游戏。
                self.assertNotIn(STATE_PAUSED, [event.state for event in events])
                self.assertEqual(adapters["endfield"].calls, [])
                self.assertEqual(adapters["limbus"].calls, [])
                # 停止分支只做诊断，不触发任何真实启动或进程操作入口。
                self.assertEqual(monitor.unsafe_calls, [])

    def test_game_result_has_no_stopped_field_but_run_report_keeps_it(self):
        self.assertNotIn("stopped", {item.name for item in fields(GameResult)})
        self.assertIn("stopped", {item.name for item in fields(RunReport)})

    def test_stopped_branch_keeps_earlier_stage_failure_priority(self):
        for barrier_outcome in ("ok", "still_running", "query_failed"):
            with self.subTest(barrier=barrier_outcome):
                monitor = StoppedBarrierMonitor(barrier_outcome)
                holder: dict[str, object] = {}

                def cleanup_during_stop():
                    monitor.exit_phase = True
                    holder["orchestrator"].request_stop()
                    return Step(False, "cleanup_failed", "无法清理")

                adapter = FakeAdapter({
                    "start_game": [Step(False, "launch_failed", "无法启动")],
                    "cleanup": [cleanup_during_stop],
                })
                orchestrator = DailyOrchestrator(
                    Path("test-root") / "config" / "user.json",
                    config_manager=FakeConfigManager(stopped_diagnostic_config()),
                    process_monitor=monitor,
                    adapters=make_adapters(arknights=adapter),
                    logger=FakeLogger(),
                )
                holder["orchestrator"] = orchestrator

                report = orchestrator.run_one("arknights")

                self.assertTrue(report.stopped)
                result = report.results[0]
                self.assertEqual(result.state, STATE_FAILED)
                self.assertEqual(result.code, "launch_failed")
                self.assertEqual(result.message, "无法启动")
                self.assertEqual(monitor.unsafe_calls, [])


class LimbusCloseoutMonitor:
    """limbus 收尾用例的进程表：启动屏障无进程，cleanup 后按剧本作答。"""

    def __init__(self, outcome: str, *, game_pid: int = 4100, helper_pid: int = 4101):
        self.outcome = outcome
        self.exit_phase = False
        self.game_pid = game_pid
        self.helper_pid = helper_pid
        self.queries: list[str] = []

    def _record(self, name: str, pid: int) -> ProcessRecord:
        return ProcessRecord(
            pid=pid,
            executable=str(name),
            full_path=f"C:\\fake\\{name}",
            owned=False,
            create_time=0.0,
        )

    def find_by_executable(self, name):
        self.queries.append(str(name))
        if not self.exit_phase:
            return []
        if self.outcome == "query_failed":
            raise RuntimeError("枚举失败")
        if self.outcome == "exited":
            return []
        lowered = str(name).casefold()
        if lowered == "aalc.exe":
            return [self._record(name, self.helper_pid)]
        if lowered == "limbuscompany.exe":
            return [self._record(name, self.game_pid)]
        return []

    def wait_for_exit(self, pid, timeout_s, stop_event=None):
        return False


class LimbusFailAdapter(FakeAdapter):
    """wait_completion 直接触发 limbus 超时；cleanup 时切入退出屏障剧本。"""

    def __init__(
        self,
        monitor: LimbusCloseoutMonitor,
        *,
        code: str = "wait_timeout",
        message: str = "已连续 45 分钟未观察到任务进展",
        details: dict[str, object] | None = None,
        cleanup_outcome: Any = None,
    ):
        outcomes = {"cleanup": [cleanup_outcome]} if cleanup_outcome is not None else None
        super().__init__(outcomes)
        self.monitor = monitor
        self.failure = Step(False, code, message, dict(details or {}))

    def wait_completion(self, timeout_s, stop_event):
        self.calls.append("wait_completion")
        self.wait_completion_args.append((timeout_s, stop_event))
        return self.failure

    def cleanup(self):
        self.monitor.exit_phase = True
        return super().cleanup()


def limbus_failure_config(*, paused_s: float = 0.0) -> dict:
    """带全部进程名的 limbus 失败配置；暂停截止由用例显式控制。"""
    config = stopped_diagnostic_config()
    config["timeouts"]["paused_s"] = paused_s
    return config


def make_limbus_orchestrator(
    monitor: LimbusCloseoutMonitor,
    adapter: LimbusFailAdapter,
    *,
    config: dict | None = None,
    logger: FakeLogger | None = None,
) -> DailyOrchestrator:
    return DailyOrchestrator(
        Path("test-root") / "config" / "user.json",
        config_manager=FakeConfigManager(config or limbus_failure_config()),
        process_monitor=monitor,
        adapters=make_adapters(limbus=adapter),
        logger=logger or FakeLogger(),
    )


class LimbusCloseoutTests(unittest.TestCase):
    def _run_and_stop(self, orchestrator, adapter, *, wait_paused=2.0):
        events = []
        paused = threading.Event()

        def listener(event):
            events.append(event)
            if event.state == STATE_PAUSED:
                paused.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(lambda: orchestrator.run_one("limbus"))
        self.assertTrue(paused.wait(wait_paused))
        orchestrator.request_stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        return box["result"], events

    def test_timeout_stays_primary_error_with_cleanup_conclusion(self):
        monitor = LimbusCloseoutMonitor("resident")
        adapter = LimbusFailAdapter(
            monitor, cleanup_outcome=Step(False, "not_owned", "已无法按 ownership 结束")
        )
        orchestrator = make_limbus_orchestrator(monitor, adapter)

        report, events = self._run_and_stop(orchestrator, adapter)

        result = report.results[0]
        self.assertEqual(result.state, STATE_FAILED)
        self.assertEqual(result.code, "wait_timeout")
        self.assertIn("已连续 45 分钟未观察到任务进展", result.message)
        self.assertIn("清理返回 not_owned", result.message)
        self.assertIn("AALC（PID 4101）", result.message)
        self.assertIn("游戏（PID 4100）", result.message)
        self.assertIn("仍在运行且无法按当前所有权规则结束", result.message)
        paused_event = next(event for event in events if event.state == STATE_PAUSED)
        self.assertIn("仍在运行且无法按当前所有权规则结束", paused_event.message)
        self.assertIn("请手动退出 AALC 和游戏后重试或跳过", paused_event.message)
        self.assertEqual(paused_event.error_code, "wait_timeout")

    def test_barrier_confirmed_exit_is_reported_as_exited(self):
        monitor = LimbusCloseoutMonitor("exited")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(monitor, adapter)

        report, events = self._run_and_stop(orchestrator, adapter)

        result = report.results[0]
        self.assertEqual(result.code, "wait_timeout")
        self.assertIn("退出屏障已确认进程均已退出", result.message)
        paused_event = next(event for event in events if event.state == STATE_PAUSED)
        self.assertIn("退出屏障已确认进程均已退出", paused_event.message)
        self.assertIn("可直接重试或跳过", paused_event.message)
        self.assertNotIn("仍在运行", paused_event.message)

    def test_query_failure_is_reported_as_unknown(self):
        monitor = LimbusCloseoutMonitor("query_failed")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(monitor, adapter)

        report, events = self._run_and_stop(orchestrator, adapter)

        result = report.results[0]
        self.assertEqual(result.code, "wait_timeout")
        self.assertIn("无法确认 AALC 与游戏是否退出", result.message)
        self.assertNotIn("仍在运行", result.message)
        self.assertNotIn("均已退出", result.message)
        paused_event = next(event for event in events if event.state == STATE_PAUSED)
        self.assertIn("无法确认 AALC 与游戏是否退出", paused_event.message)
        self.assertIn("请先确认 AALC 和游戏是否仍在运行", paused_event.message)


    def test_residual_process_blocks_retry_and_skip_even_for_last_game(self):
        monitor = LimbusCloseoutMonitor("resident")
        adapter = LimbusFailAdapter(monitor)
        # 关闭自动暂停超时，确保队列只因用户操作结束。
        orchestrator = make_limbus_orchestrator(
            monitor, adapter, config=limbus_failure_config(paused_s=0.0)
        )
        events = []
        paused = threading.Event()

        def listener(event):
            events.append(event)
            if event.state == STATE_PAUSED:
                paused.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(lambda: orchestrator.run_one("limbus"))
        self.assertTrue(paused.wait(2))

        orchestrator.retry_current()
        self.assertTrue(self._wait_for_paused_count(events, 2))
        self.assertEqual(orchestrator.current_state, STATE_PAUSED)
        self.assertIn("已拒绝重试", events[-1].message)

        orchestrator.skip_current()
        self.assertTrue(self._wait_for_paused_count(events, 3))
        self.assertEqual(orchestrator.current_state, STATE_PAUSED)
        self.assertIn("已拒绝跳过", events[-1].message)

        orchestrator.request_stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        report = box["result"]
        self.assertTrue(report.stopped)
        self.assertEqual(adapter.calls.count("wait_completion"), 1)
        self.assertEqual(adapter.calls.count("cleanup"), 1)
        self.assertEqual(report.results[0].state, STATE_FAILED)
        self.assertEqual(report.results[0].code, "wait_timeout")

    @staticmethod
    def _wait_for_paused_count(events, count, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sum(1 for event in events if event.state == STATE_PAUSED) >= count:
                time.sleep(0.05)
                return True
            time.sleep(0.01)
        return False

    def test_rejected_retry_does_not_refresh_pause_deadline(self):
        monitor = LimbusCloseoutMonitor("resident")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(
            monitor, adapter, config=limbus_failure_config(paused_s=1.0)
        )
        paused = threading.Event()
        orchestrator.add_listener(
            lambda event: paused.set() if event.state == STATE_PAUSED else None
        )

        thread, box = run_in_thread(lambda: orchestrator.run_one("limbus"))
        self.assertTrue(paused.wait(2))
        paused_at = time.monotonic()
        time.sleep(0.6)
        orchestrator.retry_current()
        thread.join(4)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        elapsed = time.monotonic() - paused_at
        # 若拒绝时刷新截止时间，停止会推到 ~1.6s 之后。
        self.assertLess(elapsed, 1.4)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertTrue(box["result"].stopped)
        self.assertEqual(adapter.calls.count("wait_completion"), 1)

    def test_pause_timeout_stops_queue_without_rerunning_game(self):
        monitor = LimbusCloseoutMonitor("resident")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(
            monitor, adapter, config=limbus_failure_config(paused_s=0.3)
        )
        events = []
        orchestrator.add_listener(events.append)

        report = orchestrator.run_one("limbus")

        self.assertTrue(report.stopped)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].state, STATE_FAILED)
        self.assertEqual(adapter.calls.count("wait_completion"), 1)
        self.assertEqual(
            [event.state for event in events].count(STATE_FAILED), 1
        )
        final = events[-1]
        self.assertEqual(final.state, STATE_STOPPED)
        self.assertEqual(final.message, "队列调度已停止；第三方进程可能仍在运行，请手动检查。")

    def test_stop_message_never_claims_third_party_closed(self):
        monitor = LimbusCloseoutMonitor("resident")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(monitor, adapter)

        _report, events = self._run_and_stop(orchestrator, adapter)

        final = events[-1]
        self.assertEqual(final.state, STATE_STOPPED)
        self.assertIn("队列调度已停止", final.message)
        self.assertNotIn("安全停止", final.message)
        self.assertIn("第三方进程可能仍在运行，请手动检查", final.message)
        # 只有队列/游戏的停止结果不得暗示第三方已退出；停止请求的
        # "收到安全停止请求" 只是控制回执。
        stopped_results = [
            event
            for event in events
            if event.state in {STATE_STOPPED, STATE_COMPLETED}
        ]
        self.assertTrue(stopped_results)
        for event in stopped_results:
            self.assertNotIn("安全停止", event.message)

    def test_exited_closeout_stop_message_reports_processes_gone(self):
        monitor = LimbusCloseoutMonitor("exited")
        adapter = LimbusFailAdapter(monitor)
        orchestrator = make_limbus_orchestrator(monitor, adapter)

        _report, events = self._run_and_stop(orchestrator, adapter)

        final = events[-1]
        self.assertEqual(final.state, STATE_STOPPED)
        self.assertEqual(final.message, "队列调度已停止；本轮游戏与助手进程均已退出。")
        self.assertNotIn("可能仍在运行", final.message)

    def test_non_limbus_failure_keeps_generic_pause_message(self):
        failing = FakeAdapter({
            "wait_completion": [Step(False, "wait_timeout", "日常任务超时")],
        })
        orchestrator = make_orchestrator(adapters=make_adapters(arknights=failing))
        paused_event = []
        paused = threading.Event()

        def listener(event):
            if event.state == STATE_PAUSED:
                paused_event.append(event)
                paused.set()

        orchestrator.add_listener(listener)
        thread, box = run_in_thread(lambda: orchestrator.run_one("arknights"))
        self.assertTrue(paused.wait(2))
        orchestrator.request_stop()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", box)
        self.assertEqual(
            paused_event[0].message, "任务失败，等待重试、跳过或停止。"
        )
        self.assertEqual(box["result"].results[0].message, "日常任务超时")

    def test_step_evidence_logs_only_controlled_diagnostics(self):
        logger = FakeLogger()
        orchestrator = make_orchestrator(logger=logger)
        result = Step(
            False,
            "wait_timeout",
            "已连续 45 分钟未观察到任务进展",
            {
                "pid": 4321,
                "timeout_kind": "idle",
                "timeout_policy": "activity",
                "completion_idle_s": 2700,
                "completion_hard_s": 21600,
                "elapsed_s": 2900.5,
                "idle_age_s": 2700.0,
                "last_progress_source": "mirror",
                "last_progress_category": "任务结束",
                "log_path": "logs/debugLog.log",
                "read_error": "permission_denied",
                "helper_pids": [1, 2],
                "details": {"api_key": "sk-should-not-appear"},
                "api_key": "sk-should-not-appear",
            },
        )

        orchestrator._log_step_evidence("limbus", "wait_completion", result)

        line = logger.entries[-1][1]
        for key in (
            "timeout_kind",
            "timeout_policy",
            "completion_idle_s",
            "completion_hard_s",
            "elapsed_s",
            "idle_age_s",
            "last_progress_source",
            "last_progress_category",
            "log_path",
            "read_error",
            "pid",
        ):
            self.assertIn(key, line)
        self.assertIn("'idle'", line)
        self.assertIn("'activity'", line)
        self.assertNotIn("sk-should-not-appear", line)
        self.assertNotIn("helper_pids", line)
        self.assertNotIn("api_key", line)


if __name__ == "__main__":
    unittest.main()

