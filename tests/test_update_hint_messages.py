"""更新相关提示文案（T-016 / T-006 C·S5）单元测试。

只验证提示文案与错误码保持不变，不引入任何新检测逻辑。全部使用临时目录
与假进程监控器；真实启动入口（os.startfile / subprocess.Popen）一律替换为
替身并在每个用例后断言未被调用。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daily_games.adapters.maa import MaaAdapter
from daily_games.adapters.maaend import MaaEndAdapter
from daily_games.process_monitor import ProcessRecord

CODE_WAIT_TIMEOUT = "wait_timeout"
CODE_TASK_ABORTED = "task_aborted"
CODE_TASK_FAILED = "task_failed"
CODE_COMPLETED_WITH_FAILURES = "task_completed_with_failures"


def _make_exe(path: Path) -> str:
    path.write_text("dummy")
    return str(path)


class _FakeProcessMonitor:
    """按可执行文件名返回假记录，并让等待永远不结束。"""

    def __init__(self, records: dict[str, ProcessRecord], *, exit_result: bool = True):
        self._records = {name.lower(): [record] for name, record in records.items()}
        self._exit_result = exit_result
        self.wait_exit_calls: list[int] = []

    def find_by_executable(self, name, *, expected_path=None):
        return list(self._records.get(name.lower(), []))

    def wait_for_exit(self, pid, timeout, stop_event=None):
        self.wait_exit_calls.append(pid)
        return self._exit_result


def _arknights_cfg(tmp: Path) -> dict:
    launcher = _make_exe(tmp / "Launcher.exe")
    game = _make_exe(tmp / "Arknights.exe")
    maa = _make_exe(tmp / "MAA.exe")
    cfg_dir = tmp / "maa_config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "gui.new.json").write_text(
        json.dumps(
            {
                "Current": "Default",
                "Configurations": {
                    "Default": {
                        "Gui": {
                            "StartUpSettings": {"RunDirectly": True},
                            "PostActions": "ExitArknights, ExitSelf",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "display_name": "明日方舟",
        "launcher": {"executable": launcher, "args": ["--game=arknights"], "verified": False},
        "game_executable": game,
        "game_process_name": "Arknights.exe",
        "helper": {
            "id": "maa",
            "executable": maa,
            "process_name": "MAA.exe",
            "reuse_existing": False,
            "config_dir": str(cfg_dir),
            "config_file": "gui.new.json",
            "use_coordinate_click": False,
        },
    }


def _endfield_cfg(tmp: Path, *, enabled_tasks: int) -> dict:
    launcher = _make_exe(tmp / "Launcher.exe")
    game = _make_exe(tmp / "Endfield.exe")
    maaend = _make_exe(tmp / "MaaEnd.exe")
    cfg_dir = tmp / "maaend_config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "mxu-MaaEnd.json").write_text(
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


class TestMaaEndSceneAbortMessage(unittest.TestCase):
    def setUp(self):
        self.startfile_mock, self.popen_mock = _install_launch_guard(self)

    def _run_abort(self, tmp: Path):
        pid = 901
        _write_task_log(
            tmp,
            pid,
            [
                ("Starting", 200000001, "VisitFriendsMain"),
                ("Succeeded", 200000001, "VisitFriendsMain"),
            ],
            abort=True,
        )
        pm = _FakeProcessMonitor({
            "MaaEnd.exe": ProcessRecord(
                pid=pid,
                executable="MaaEnd.exe",
                full_path="C:\\MaaEnd.exe",
                owned=False,
                create_time=0.0,
            )
        })
        adapter = MaaEndAdapter(
            game_cfg=_endfield_cfg(tmp, enabled_tasks=1), process_monitor=pm
        )
        return adapter.wait_completion(1)

    def test_abort_message_points_at_scene_and_update_without_claiming_hotfix(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run_abort(Path(td))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, CODE_TASK_ABORTED)
        self.assertTrue(result.details["abort_detected"])
        self.assertIn("无法确认游戏已进入主场景", result.message)
        self.assertIn("游戏内资源更新", result.message)
        self.assertIn("HDR/滤镜", result.message)
        self.assertIn("完成处理后关闭相关程序再重试", result.message)
        # 保留原有关键信息：确实发生了场景检查中止。
        self.assertIn("场景检查中止", result.message)
        # abort 只能证明场景不符，不得断言正在热更新。
        self.assertNotIn("正在热更新", result.message)
        self.assertNotIn("正在下载", result.message)


class TestMaaEndFailureMessagesUnaffected(unittest.TestCase):
    def setUp(self):
        self.startfile_mock, self.popen_mock = _install_launch_guard(self)

    def _run(self, tmp: Path, events, *, enabled_tasks: int):
        pid = 902
        _write_task_log(tmp, pid, events)
        pm = _FakeProcessMonitor({
            "MaaEnd.exe": ProcessRecord(
                pid=pid,
                executable="MaaEnd.exe",
                full_path="C:\\MaaEnd.exe",
                owned=False,
                create_time=0.0,
            )
        })
        adapter = MaaEndAdapter(
            game_cfg=_endfield_cfg(tmp, enabled_tasks=enabled_tasks),
            process_monitor=pm,
        )
        return adapter.wait_completion(1)

    def test_partial_failure_keeps_t014_summary(self):
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
        self.assertNotIn("资源更新", result.message)

    def test_all_failed_message_unchanged(self):
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
        self.assertEqual(result.message, "MaaEnd 已尝试任务，但没有任何启用任务成功")
        self.assertNotIn("资源更新", result.message)


class TestMaaTimeoutHint(unittest.TestCase):
    def setUp(self):
        self.startfile_mock, self.popen_mock = _install_launch_guard(self)

    def test_timeout_message_keeps_helper_timeout_and_adds_update_hint(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pm = _FakeProcessMonitor({
                "Arknights.exe": ProcessRecord(
                    pid=301,
                    executable="Arknights.exe",
                    full_path="C:\\Arknights.exe",
                    owned=False,
                    create_time=0.0,
                ),
                "MAA.exe": ProcessRecord(
                    pid=401,
                    executable="MAA.exe",
                    full_path="C:\\MAA.exe",
                    owned=False,
                    create_time=0.0,
                ),
            }, exit_result=False)
            adapter = MaaAdapter(
                game_cfg=_arknights_cfg(tmp), process_monitor=pm
            )
            result = adapter.wait_completion(timeout_s=1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, CODE_WAIT_TIMEOUT)
        self.assertIn("等待明日方舟与 MAA 全部退出超时", result.message)
        self.assertIn("若游戏正在下载资源更新，请等更新完成后重试", result.message)
        # 超时预算与诊断字段不变。
        self.assertEqual(result.details["timeout_s"], 1)
        self.assertEqual(
            result.details["running_processes"], ["Arknights.exe", "MAA.exe"]
        )


def _install_launch_guard(testcase):
    """测试隔离：真实 os.startfile / subprocess.Popen 在用例内不可达。"""
    startfile_patch = mock.patch.object(os, "startfile", create=True)
    popen_patch = mock.patch.object(subprocess, "Popen")
    startfile_mock = startfile_patch.start()
    popen_mock = popen_patch.start()
    testcase.addCleanup(startfile_patch.stop)
    testcase.addCleanup(popen_patch.stop)
    testcase.addCleanup(startfile_mock.assert_not_called)
    testcase.addCleanup(popen_mock.assert_not_called)
    return startfile_mock, popen_mock


if __name__ == "__main__":
    unittest.main()
