"""MaaEnd 失败项名称展示（T-014 / T-006 A·S6）单元测试。

只验证 `_validate_task_evidence` 的消息拼装：列出失败 entry 名称、去重、
限长，且不改任务成败判定。全部使用临时目录与假进程监控器，不启动真实
MaaEnd / 鹰角启动器 / 游戏。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daily_games.adapters.maaend import MaaEndAdapter
from daily_games.process_monitor import ProcessRecord

CODE_COMPLETED_WITH_FAILURES = "task_completed_with_failures"
CODE_TASK_FAILED = "task_failed"
CODE_TASK_ABORTED = "task_aborted"
CODE_TASK_INCOMPLETE = "task_incomplete"


def _make_exe(path: Path) -> str:
    path.write_text("dummy")
    return str(path)


class _FakeProcessMonitor:
    """只服务于 wait_completion 的最小假监控器。"""

    def __init__(self, helper_pid: int = 901):
        self._records = [
            ProcessRecord(
                pid=helper_pid,
                executable="MaaEnd.exe",
                full_path="C:\\MaaEnd.exe",
                owned=False,
                create_time=0.0,
            )
        ]
        self.find_calls: list[tuple[str, str | None]] = []
        self.wait_exit_calls: list[int] = []

    def find_by_executable(self, name, *, expected_path=None):
        self.find_calls.append((name, expected_path))
        return list(self._records)

    def wait_for_exit(self, pid, timeout, stop_event=None):
        self.wait_exit_calls.append(pid)
        return True


def _endfield_cfg(tmp: Path, *, enabled_tasks: int = 1) -> dict:
    launcher = _make_exe(tmp / "Launcher.exe")
    game = _make_exe(tmp / "Endfield.exe")
    maaend = _make_exe(tmp / "MaaEnd.exe")
    cfg_dir = tmp / "maaend_config"
    cfg_dir.mkdir(exist_ok=True)
    mxu = cfg_dir / "mxu-MaaEnd.json"
    mxu.write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "id": "inst1",
                        "name": "日常",
                        "controllerName": "Win32-Front",
                        "tasks": [
                            {"taskName": f"Task{index}", "enabled": True}
                            for index in range(enabled_tasks)
                        ],
                    }
                ],
                "lastActiveInstanceId": "inst1",
            }
        ),
        encoding="utf-8",
    )
    return {
        "display_name": "终末地",
        "launcher": {"executable": launcher, "args": ["--game=endfield"], "verified": False},
        "game_executable": game,
        "game_process_name": "Endfield.exe",
        "game_window_title_regex": "Endfield",
        "helper": {
            "id": "maaend",
            "executable": maaend,
            "process_name": "MaaEnd.exe",
            "reuse_existing": False,
            "config_dir": str(cfg_dir),
            "config_file": "mxu-MaaEnd.json",
            "instance_id": "inst1",
            "instance_name": "日常",
            "cli_autostart_verified": True,
            "quit_after_run_verified": True,
            "close_owned_game_after_tasks": True,
            "web_server_port": 12701,
        },
    }


def _write_task_log(
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
            f"[2026-10-25 00:29:{index:02d}.000][INF][Px{pid}][Tx1] "
            f"[msg=Tasker.Task.{event}] [details={details}]"
        )
    if abort:
        lines.append(
            f"[2026-10-25 00:29:59.000][INF][Px{pid}][Tx1] "
            "[msg=Node.Action.Succeeded] [details={\"name\":\"__SceneCheckAbortPipeline\"}]"
        )
    log_path = debug_dir / "maafw.log"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


class TestMaaEndFailureSummary(unittest.TestCase):
    def setUp(self):
        # 测试隔离：真实启动入口一律替换为替身。
        startfile_patch = mock.patch.object(os, "startfile", create=True)
        popen_patch = mock.patch.object(subprocess, "Popen")
        self.startfile_mock = startfile_patch.start()
        self.popen_mock = popen_patch.start()
        self.addCleanup(startfile_patch.stop)
        self.addCleanup(popen_patch.stop)
        self.addCleanup(self._assert_no_real_launch)

    def _assert_no_real_launch(self):
        self.startfile_mock.assert_not_called()
        self.popen_mock.assert_not_called()

    def _run(
        self,
        tmp: Path,
        events: list[tuple[str, int, str]],
        *,
        enabled_tasks: int,
        abort: bool = False,
        pid: int = 901,
    ):
        pm = _FakeProcessMonitor(pid)
        _write_task_log(tmp, pid, events, abort=abort)
        adapter = MaaEndAdapter(
            game_cfg=_endfield_cfg(tmp, enabled_tasks=enabled_tasks),
            process_monitor=pm,
        )
        return adapter.wait_completion(1)

    def test_single_failed_entry_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DeliveryJobsMain"),
                    ("Failed", 200000002, "DeliveryJobsMain"),
                ],
                enabled_tasks=2,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, CODE_COMPLETED_WITH_FAILURES)
        self.assertEqual(
            result.message,
            "MaaEnd 已执行全部启用任务，其中 1 项失败：DeliveryJobsMain",
        )
        self.assertEqual(result.details["failed_entries"], ["DeliveryJobsMain"])

    def test_two_failed_entries_are_listed_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DeliveryJobsMain"),
                    ("Failed", 200000002, "DeliveryJobsMain"),
                    ("Starting", 200000003, "AutoEssenceSchedule"),
                    ("Failed", 200000003, "AutoEssenceSchedule"),
                ],
                enabled_tasks=3,
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.message,
            "MaaEnd 已执行全部启用任务，其中 2 项失败："
            "DeliveryJobsMain、AutoEssenceSchedule",
        )

    def test_more_than_three_failures_show_first_three_and_refer_to_log(self):
        entries = ["FailOne", "FailTwo", "FailThree", "FailFour", "FailFive"]
        events = [
            ("Starting", 200000001, "VisitFriendsMain"),
            ("Succeeded", 200000001, "VisitFriendsMain"),
        ]
        for index, entry in enumerate(entries):
            events.append(("Starting", 200000010 + index, entry))
            events.append(("Failed", 200000010 + index, entry))

        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), events, enabled_tasks=6)

        self.assertTrue(result.ok)
        self.assertTrue(result.message.startswith("MaaEnd 已执行全部启用任务，其中 5 项失败："))
        self.assertIn("FailOne、FailTwo、FailThree", result.message)
        self.assertNotIn("FailFour", result.message)
        self.assertNotIn("FailFive", result.message)
        self.assertIn("等 5 项，其余见日志", result.message)
        # 完整受控列表仍保留在 details 中，供日志排查。
        self.assertEqual(result.details["failed_entries"], entries)

    def test_duplicate_entries_are_listed_once(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DeliveryJobsMain"),
                    ("Failed", 200000002, "DeliveryJobsMain"),
                    ("Starting", 200000003, "DeliveryJobsMain"),
                    ("Failed", 200000003, "DeliveryJobsMain"),
                ],
                enabled_tasks=3,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.message.count("DeliveryJobsMain"), 1)
        self.assertEqual(
            result.message,
            "MaaEnd 已执行全部启用任务，其中 2 项失败：DeliveryJobsMain",
        )
        # 任务数不变：失败任务仍是两条，只有展示去重。
        self.assertEqual(len(result.details["failed_entries"]), 2)

    def test_overlong_entry_name_is_truncated(self):
        long_name = "Very" + "Long" * 20 + "EntryName"
        self.assertGreater(len(long_name), 40)
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, long_name),
                    ("Failed", 200000002, long_name),
                ],
                enabled_tasks=2,
            )

        self.assertTrue(result.ok)
        summary = result.message.rsplit("：", 1)[-1]
        self.assertEqual(len(summary), 40)
        self.assertTrue(summary.endswith("…"))
        self.assertNotIn(long_name, result.message)
        # 原始名称完整保留在受控 details 中。
        self.assertEqual(result.details["failed_entries"], [long_name])

    def test_all_failed_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "DeliveryJobsMain"),
                    ("Failed", 200000001, "DeliveryJobsMain"),
                    ("Starting", 200000002, "AutoEssenceSchedule"),
                    ("Failed", 200000002, "AutoEssenceSchedule"),
                ],
                enabled_tasks=2,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, CODE_TASK_FAILED)
        self.assertNotIn("项失败", result.message)

    def test_abort_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                ],
                enabled_tasks=1,
                abort=True,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, CODE_TASK_ABORTED)
        self.assertNotIn("项失败", result.message)

    def test_incomplete_queue_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                Path(td),
                [
                    ("Starting", 200000001, "VisitFriendsMain"),
                    ("Succeeded", 200000001, "VisitFriendsMain"),
                    ("Starting", 200000002, "DeliveryJobsMain"),
                    ("Failed", 200000002, "DeliveryJobsMain"),
                ],
                enabled_tasks=3,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, CODE_TASK_INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
