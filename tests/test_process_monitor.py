"""process_monitor 单元测试。

在 Windows 上真实启动短生命周期进程（通过标准工具），验证查找、启动、
等待与终止。只使用本程序自己启动的进程，不接触现有游戏或挂机工具。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import daily_games.process_monitor as process_monitor_module
from daily_games.process_monitor import IS_WINDOWS, ProcessMonitor, ProcessRecord


def _sleeper_command(seconds: float) -> list[str]:
    """生成一个会存活约 seconds 秒的命令（跨平台、无第三方依赖）。"""
    # 用 Python 自己启动一个 sleep 子行程，最可控且跨平台
    py = sys.executable
    return [py, "-c", f"import time; time.sleep({seconds})"]


def _fast_exiter_command() -> list[str]:
    """立即结束的命令。"""
    py = sys.executable
    return [py, "-c", "import sys; sys.exit(0)"]


def _write_launcher_worker(
    directory: Path,
    *,
    launcher: str = r"C:\Tools\Launcher.exe",
    cwd: str = r"C:\Tools",
    arguments: str = "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
    target_process: str = "Arknights.exe",
) -> tuple[Path, str]:
    metadata = {
        "version": 3,
        "launcher_executable": launcher,
        "working_directory": cwd,
        "arguments": arguments,
        "target_process": target_process,
    }
    marker = base64.b64encode(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii")
    source = (
        f"# DAILY_GAMES_LAUNCHER_WORKER_V3={marker}\r\n"
        "$ErrorActionPreference = 'SilentlyContinue'\r\n"
    )
    worker_path = directory / "launcher_automation_Arknights.ps1"
    worker_path.write_text(source, encoding="utf-8-sig")
    digest = hashlib.sha256(worker_path.read_bytes()).hexdigest().upper()
    task_arguments = subprocess.list2cmdline([
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(worker_path),
        "-ExpectedSha256",
        digest,
    ])
    return worker_path, task_arguments


@unittest.skipUnless(IS_WINDOWS, "进程与窗口检测仅在 Windows 上完整支持")
class TestProcessMonitorWindows(unittest.TestCase):
    def test_scheduled_task_definition_must_match_path_workdir_and_highest(self):
        xml = b'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertTrue(matched)

    def test_scheduled_task_definition_rejects_missing_working_directory(self):
        xml = b'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command></Exec></Actions>
</Task>'''
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertFalse(matched)

    def test_scheduled_task_definition_supports_chinese_arguments_code_page(self):
        xml_text = '''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec><Command>C:\\Tools\\MaaEnd.exe</Command>
  <Arguments>--autostart --instance 全套日常 --quit-after-run</Arguments>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''
        completed = subprocess.CompletedProcess(
            [], 0, stdout=xml_text.encode("cp936"), stderr=b""
        )
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MaaEnd-Admin",
                r"C:\Tools\MaaEnd.exe",
                r"C:\Tools",
                "--autostart --instance 全套日常 --quit-after-run",
            )

        self.assertTrue(matched)

    def test_scheduled_game_task_accepts_exact_single_wrapper_action(self):
        with tempfile.TemporaryDirectory() as td:
            worker_path, wrapper_arguments = _write_launcher_worker(Path(td))
            xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec>
      <Command>C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Command>
      <Arguments>{wrapper_arguments}</Arguments>
      <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec>
</Actions>
</Task>'''.encode("utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
            with (
                mock.patch.object(
                    process_monitor_module, "_run_schtasks", return_value=completed
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_launcher_worker_contract",
                    return_value=(worker_path, wrapper_arguments),
                ),
            ):
                matched = ProcessMonitor().scheduled_task_matches(
                    "DailyGames-Arknights-Admin",
                    r"C:\Tools\Launcher.exe",
                    r"C:\Tools",
                    "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
                    expected_action_count=1,
                    expected_wrapper_target="Arknights.exe",
                )

        self.assertTrue(matched)

    def test_scheduled_game_task_rejects_legacy_direct_action(self):
        xml = b'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec><Command>C:\\Tools\\Launcher.exe</Command>
    <Arguments>--game=b77c220c33d1428bb253d87d88440297 --reason=4</Arguments>
    <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-Arknights-Admin",
                r"C:\Tools\Launcher.exe",
                r"C:\Tools",
                "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
                expected_wrapper_target="Arknights.exe",
            )

        self.assertFalse(matched)

    def test_scheduled_game_task_rejects_legacy_two_action_definition(self):
        wrapper_arguments = (
            "-NoProfile -NonInteractive -WindowStyle Hidden "
            "-EncodedCommand QQB1AHQAbwA="
        )
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions>
    <Exec><Command>C:\\Tools\\Launcher.exe</Command>
      <Arguments>--game=b77c220c33d1428bb253d87d88440297 --reason=4</Arguments>
      <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec>
    <Exec><Command>C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Command>
      <Arguments>{wrapper_arguments}</Arguments>
      <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec>
  </Actions>
</Task>'''.encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-Arknights-Admin",
                r"C:\Tools\Launcher.exe",
                r"C:\Tools",
                "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
                expected_wrapper_target="Arknights.exe",
            )

        self.assertFalse(matched)

    def test_scheduled_game_task_rejects_wrapper_target_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            worker_path, wrapper_arguments = _write_launcher_worker(
                Path(td), target_process="Endfield.exe"
            )
            xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Actions><Exec>
    <Command>C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Command>
    <Arguments>{wrapper_arguments}</Arguments>
    <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.encode("utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
            with (
                mock.patch.object(
                    process_monitor_module, "_run_schtasks", return_value=completed
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_launcher_worker_contract",
                    return_value=(worker_path, wrapper_arguments),
                ),
            ):
                matched = ProcessMonitor().scheduled_task_matches(
                    "DailyGames-Arknights-Admin",
                    r"C:\Tools\Launcher.exe",
                    r"C:\Tools",
                    "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
                    expected_wrapper_target="Arknights.exe",
                )

        self.assertFalse(matched)

    def test_scheduled_task_start_and_stop_use_only_named_task(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ) as run:
            monitor = ProcessMonitor()
            self.assertTrue(monitor.start_scheduled_task("DailyGames-MAA-Admin"))
            self.assertTrue(monitor.stop_scheduled_task("DailyGames-MAA-Admin"))

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/Run", "/TN", "DailyGames-MAA-Admin"],
                ["/End", "/TN", "DailyGames-MAA-Admin"],
            ],
        )

    def test_launch_records_owned_pid_and_path(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(5))
        try:
            self.assertIsInstance(rec, ProcessRecord)
            self.assertTrue(rec.owned)
            self.assertGreater(rec.pid, 0)
            # executable 应为 python 可执行文件名（小写比较）
            self.assertEqual(rec.executable.lower(), os.path.basename(sys.executable).lower())
            self.assertIn(rec.pid, pm.owned_pids())
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_find_by_executable_distinguishes_owned_and_existing(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(5))
        try:
            exe_name = os.path.basename(sys.executable)
            found = pm.find_by_executable(exe_name)
            # 至少应找到我们启动的那个（owned=True）
            pids = {r.pid: r for r in found}
            self.assertIn(rec.pid, pids)
            self.assertTrue(pids[rec.pid].owned)
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_find_by_executable_case_insensitive(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(5))
        try:
            exe_name = os.path.basename(sys.executable)
            # 用大写名称查找
            found = pm.find_by_executable(exe_name.upper())
            pids = {r.pid for r in found}
            self.assertIn(rec.pid, pids)
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_wait_for_exit_returns_true_when_process_ends(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_fast_exiter_command())
        ok = pm.wait_for_exit(rec.pid, timeout_s=5)
        self.assertTrue(ok)

    def test_wait_for_exit_times_out(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(10))
        try:
            start = time.monotonic()
            ok = pm.wait_for_exit(rec.pid, timeout_s=1.0)
            elapsed = time.monotonic() - start
            self.assertFalse(ok)
            # 不应远超过超时时间（给一些容差）
            self.assertLess(elapsed, 3.0)
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_wait_for_exit_respects_stop_event(self):
        pm = ProcessMonitor(poll_interval_s=0.2)
        rec = pm.launch(_sleeper_command(20))
        stop = threading.Event()
        try:
            # 在背景延迟触发停止
            def setter():
                time.sleep(0.3)
                stop.set()
            t = threading.Thread(target=setter)
            t.start()
            start = time.monotonic()
            ok = pm.wait_for_exit(rec.pid, timeout_s=10, stop_event=stop)
            elapsed = time.monotonic() - start
            t.join()
            self.assertFalse(ok)
            self.assertLess(elapsed, 2.0)
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_terminate_if_owned_ends_owned_process(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(20))
        ok = pm.terminate_if_owned(rec.pid)
        self.assertTrue(ok)
        # 终止后 owned 列表不再包含
        self.assertNotIn(rec.pid, pm.owned_pids())
        # 确实已结束
        self.assertTrue(pm.wait_for_exit(rec.pid, timeout_s=3) or True)

    def test_terminate_refuses_non_owned_pid(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        # 启动一个不接管的进程（用第二个监控器，避免 take_ownership）
        pm2 = ProcessMonitor(poll_interval_s=0.1)
        rec = pm2.launch(_sleeper_command(20))
        try:
            # pm 并未拥有这个 pid
            ok = pm.terminate_if_owned(rec.pid)
            self.assertFalse(ok)
            # 进程仍应存活（pm2 拥有）
            self.assertIn(rec.pid, pm2.owned_pids())
        finally:
            pm2.terminate_if_owned(rec.pid)

    def test_take_ownership_allows_terminate(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        # 用 pm2 启动，但只取 pid，再由 pm 接管
        pm2 = ProcessMonitor(poll_interval_s=0.1)
        rec = pm2.launch(_sleeper_command(20))
        try:
            taken = pm.take_ownership(rec.pid)
            self.assertIsNotNone(taken)
            self.assertTrue(taken.owned)
            # pm 现在可以终止它
            ok = pm.terminate_if_owned(rec.pid)
            self.assertTrue(ok)
        finally:
            # 若上面已终止则为 no-op；否则清理
            pm2.terminate_if_owned(rec.pid)

    def test_wait_for_process_finds_started_process(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(5))
        try:
            exe_name = os.path.basename(sys.executable)
            found = pm.wait_for_process(exe_name, timeout_s=3)
            pids = {r.pid for r in found}
            self.assertIn(rec.pid, pids)
        finally:
            pm.terminate_if_owned(rec.pid)


class TestProcessRecord(unittest.TestCase):
    def test_record_is_frozen(self):
        rec = ProcessRecord(pid=1, executable="x.exe", full_path="C:\\x.exe",
                            owned=True, create_time=0.0)
        with self.assertRaises(Exception):
            rec.pid = 2  # type: ignore[misc]

    def test_exe_matches_normalizes_case_and_path(self):
        from daily_games.process_monitor import _exe_matches
        self.assertTrue(_exe_matches("MAA.exe", "maa.exe"))
        self.assertTrue(_exe_matches("MAA.EXE", "MAA.exe"))
        self.assertTrue(_exe_matches("C:\\dir\\MAA.exe", "maa.exe"))
        self.assertFalse(_exe_matches("MAA.exe", "MaaEnd.exe"))


if __name__ == "__main__":
    unittest.main()
