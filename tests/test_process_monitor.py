"""process_monitor 单元测试。

在 Windows 上真实启动短生命周期进程（通过标准工具），验证查找、启动、
等待与终止。只使用本程序自己启动的进程，不接触现有游戏或挂机工具。
"""

from __future__ import annotations

import base64
import ctypes
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
    target_executable: str = r"C:\Games\Arknights.exe",
) -> tuple[Path, str]:
    metadata = {
        "version": 5,
        "launcher_executable": launcher,
        "working_directory": cwd,
        "arguments": arguments,
        "target_process": target_process,
        "target_executable": target_executable,
    }
    marker = base64.b64encode(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii")
    source = (
        f"# DAILY_GAMES_LAUNCHER_WORKER_V5={marker}\r\n"
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


class TestLauncherWorkerSecurityContract(unittest.TestCase):
    def test_contract_uses_protected_root_and_v5_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            worker_root = Path(td) / "DailyGames" / "workers"
            worker_root.mkdir(parents=True)
            worker_path, expected_arguments = _write_launcher_worker(worker_root)
            with (
                mock.patch.object(
                    process_monitor_module,
                    "_protected_launcher_worker_root",
                    return_value=worker_root,
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_launcher_worker_security_is_valid",
                    return_value=True,
                ) as security_check,
            ):
                contract = process_monitor_module._launcher_worker_contract(
                    "Arknights.exe"
                )

            self.assertEqual(contract, (worker_path, expected_arguments))
            security_check.assert_called_once_with(worker_path)
            metadata = process_monitor_module._read_launcher_worker_metadata(
                worker_path
            )
            self.assertEqual(metadata["version"], 5)
            self.assertEqual(
                metadata["target_executable"],
                r"C:\Games\Arknights.exe",
            )

    def test_contract_rejects_worker_when_security_cannot_be_proven(self):
        with tempfile.TemporaryDirectory() as td:
            worker_root = Path(td) / "DailyGames" / "workers"
            worker_root.mkdir(parents=True)
            _write_launcher_worker(worker_root)
            with (
                mock.patch.object(
                    process_monitor_module,
                    "_protected_launcher_worker_root",
                    return_value=worker_root,
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_launcher_worker_security_is_valid",
                    return_value=False,
                ),
            ):
                contract = process_monitor_module._launcher_worker_contract(
                    "Arknights.exe"
                )

            self.assertIsNone(contract)

    def test_v4_worker_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            worker_path, _ = _write_launcher_worker(Path(td))
            source = worker_path.read_text(encoding="utf-8-sig").replace(
                "DAILY_GAMES_LAUNCHER_WORKER_V5",
                "DAILY_GAMES_LAUNCHER_WORKER_V4",
            )
            worker_path.write_text(source, encoding="utf-8-sig")

            self.assertIsNone(
                process_monitor_module._read_launcher_worker_metadata(worker_path)
            )

    def test_effective_mutation_access_fails_worker_security_closed(self):
        with tempfile.TemporaryDirectory() as td:
            worker_root = Path(td) / "DailyGames" / "workers"
            worker_path = worker_root / "launcher_automation_Arknights.ps1"
            stat_result = mock.Mock(st_file_attributes=0)
            with (
                mock.patch.object(process_monitor_module, "IS_WINDOWS", True),
                mock.patch.object(
                    process_monitor_module,
                    "_protected_launcher_worker_root",
                    return_value=worker_root,
                ),
                mock.patch.object(
                    process_monitor_module.os,
                    "lstat",
                    return_value=stat_result,
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_path_grants_access",
                    return_value=True,
                ),
            ):
                self.assertFalse(
                    process_monitor_module._launcher_worker_security_is_valid(
                        worker_path
                    )
                )

    def test_protected_non_reparse_tree_passes_worker_security(self):
        with tempfile.TemporaryDirectory() as td:
            worker_root = Path(td) / "DailyGames" / "workers"
            worker_path = worker_root / "launcher_automation_Endfield.ps1"
            stat_result = mock.Mock(st_file_attributes=0)
            with (
                mock.patch.object(process_monitor_module, "IS_WINDOWS", True),
                mock.patch.object(
                    process_monitor_module,
                    "_protected_launcher_worker_root",
                    return_value=worker_root,
                ),
                mock.patch.object(
                    process_monitor_module.os,
                    "lstat",
                    return_value=stat_result,
                ),
                mock.patch.object(
                    process_monitor_module,
                    "_path_grants_access",
                    return_value=False,
                ),
            ):
                self.assertTrue(
                    process_monitor_module._launcher_worker_security_is_valid(
                        worker_path
                    )
                )


TEST_USER_SID = "S-1-5-21-1000-1000-1000-1001"
TASK_SECURITY_XML = f"""
  <Principals><Principal>
    <UserId>{TEST_USER_SID}</UserId>
    <LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
  </Settings>
"""


@unittest.skipUnless(IS_WINDOWS, "进程与窗口检测仅在 Windows 上完整支持")
class TestProcessMonitorWindows(unittest.TestCase):
    def setUp(self):
        sid_patch = mock.patch.object(
            process_monitor_module,
            "_current_user_sid",
            return_value=TEST_USER_SID,
        )
        sid_patch.start()
        self.addCleanup(sid_patch.stop)

    def test_scheduled_task_definition_must_match_path_workdir_and_highest(self):
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertTrue(matched)

    def test_scheduled_task_definition_rejects_other_principal(self):
        security_xml = TASK_SECURITY_XML.replace(TEST_USER_SID, "S-1-5-18")
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {security_xml}
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertFalse(matched)

    def test_scheduled_task_definition_rejects_changed_settings(self):
        security_xml = TASK_SECURITY_XML.replace("PT12H", "PT72H")
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {security_xml}
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertFalse(matched)

    def test_scheduled_task_definition_rejects_any_trigger(self):
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.encode("utf-8")
        completed = subprocess.CompletedProcess([], 0, stdout=xml, stderr=b"")
        with mock.patch.object(
            process_monitor_module, "_run_schtasks", return_value=completed
        ):
            matched = ProcessMonitor().scheduled_task_matches(
                "DailyGames-MAA-Admin", r"C:\Tools\MAA.exe", r"C:\Tools"
            )

        self.assertFalse(matched)

    def test_scheduled_task_definition_rejects_missing_working_directory(self):
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
  <Actions><Exec><Command>C:\\Tools\\MAA.exe</Command></Exec></Actions>
</Task>'''.encode("utf-8")
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
  {task_security}
  <Actions><Exec><Command>C:\\Tools\\MaaEnd.exe</Command>
  <Arguments>--autostart --instance 全套日常 --quit-after-run</Arguments>
  <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
</Task>'''.format(task_security=TASK_SECURITY_XML)
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
  {TASK_SECURITY_XML}
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
                    expected_wrapper_executable=r"C:\Games\Arknights.exe",
                )
                wrong_target = ProcessMonitor().scheduled_task_matches(
                    "DailyGames-Arknights-Admin",
                    r"C:\Tools\Launcher.exe",
                    r"C:\Tools",
                    "--game=b77c220c33d1428bb253d87d88440297 --reason=4",
                    expected_action_count=1,
                    expected_wrapper_target="Arknights.exe",
                    expected_wrapper_executable=r"C:\Games\Wrong.exe",
                )

        self.assertTrue(matched)
        self.assertFalse(wrong_target)

    def test_scheduled_game_task_rejects_legacy_direct_action(self):
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
  <Actions><Exec><Command>C:\\Tools\\Launcher.exe</Command>
    <Arguments>--game=b77c220c33d1428bb253d87d88440297 --reason=4</Arguments>
    <WorkingDirectory>C:\\Tools</WorkingDirectory></Exec></Actions>
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
                expected_wrapper_executable=r"C:\Games\Arknights.exe",
            )

        self.assertFalse(matched)

    def test_scheduled_game_task_rejects_legacy_two_action_definition(self):
        wrapper_arguments = (
            "-NoProfile -NonInteractive -WindowStyle Hidden "
            "-EncodedCommand QQB1AHQAbwA="
        )
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
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
                expected_wrapper_executable=r"C:\Games\Arknights.exe",
            )

        self.assertFalse(matched)

    def test_scheduled_game_task_rejects_wrapper_target_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            worker_path, wrapper_arguments = _write_launcher_worker(
                Path(td), target_process="Endfield.exe"
            )
            xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {TASK_SECURITY_XML}
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
                    expected_wrapper_executable=r"C:\Games\Arknights.exe",
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

    def test_schtasks_timeout_fails_closed(self):
        timeout = subprocess.TimeoutExpired(["schtasks.exe"], 15)
        with mock.patch.object(
            process_monitor_module.subprocess,
            "run",
            side_effect=timeout,
        ) as run:
            monitor = ProcessMonitor()
            self.assertFalse(
                monitor.start_scheduled_task("DailyGames-MAA-Admin")
            )
            self.assertFalse(
                monitor.stop_scheduled_task("DailyGames-MAA-Admin")
            )

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(
                call.kwargs["timeout"],
                process_monitor_module.SCHTASKS_TIMEOUT_S,
            )
            executable = Path(call.args[0][0])
            self.assertTrue(executable.is_absolute())
            self.assertEqual(executable.name.casefold(), "schtasks.exe")

    def test_system_tool_resolution_and_native_sid_fail_closed(self):
        system_directory = process_monitor_module._windows_system_directory()
        schtasks = process_monitor_module._windows_system_file("schtasks.exe")
        powershell = process_monitor_module._windows_system_file(
            "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        sid = process_monitor_module._native_current_user_sid()

        self.assertIsNotNone(system_directory)
        self.assertTrue(system_directory.is_absolute())
        self.assertIsNotNone(schtasks)
        self.assertTrue(schtasks.is_file())
        self.assertIsNotNone(powershell)
        self.assertTrue(powershell.is_file())
        self.assertRegex(sid or "", r"^S-\d+(?:-\d+)+$")

        with (
            mock.patch.object(
                process_monitor_module,
                "_windows_system_file",
                return_value=None,
            ),
            mock.patch.object(
                process_monitor_module.subprocess,
                "run",
            ) as run,
        ):
            unavailable = process_monitor_module._run_schtasks(["/Query"])

        self.assertNotEqual(unavailable.returncode, 0)
        run.assert_not_called()

    def test_native_process_api_uses_pointer_sized_handles_and_current_identity(self):
        kernel32 = process_monitor_module._kernel32_process_api()

        self.assertIsNotNone(kernel32)
        self.assertIs(kernel32.OpenProcess.restype, ctypes.c_void_p)
        self.assertIs(
            kernel32.CreateToolhelp32Snapshot.restype,
            ctypes.c_void_p,
        )
        current_path = process_monitor_module._query_full_path(os.getpid())
        self.assertTrue(
            process_monitor_module._same_path(current_path, sys.executable)
        )
        current = next(
            (
                row
                for row in process_monitor_module._snapshot_processes()
                if int(row["pid"]) == os.getpid()
            ),
            None,
        )
        self.assertIsNotNone(current)
        self.assertTrue(
            process_monitor_module._same_path(
                str(current["path"]),
                sys.executable,
            )
        )
        create_time = process_monitor_module._get_create_time(os.getpid())
        self.assertGreater(create_time, 0.0)
        self.assertLessEqual(create_time, time.time())
        self.assertTrue(ProcessMonitor()._is_alive(os.getpid()))

    def test_process_query_and_termination_fail_closed_on_access_denied(self):
        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = None
        monitor = ProcessMonitor()
        with (
            mock.patch.object(
                process_monitor_module,
                "_kernel32_process_api",
                return_value=kernel32,
            ),
            mock.patch.object(
                process_monitor_module,
                "_user32_window_api",
                return_value=None,
            ),
            mock.patch.object(
                process_monitor_module.ctypes,
                "get_last_error",
                return_value=process_monitor_module.ERROR_ACCESS_DENIED,
            ),
        ):
            self.assertTrue(monitor._is_alive(424242))
            self.assertFalse(process_monitor_module._terminate_pid(424242, 0.0))

        with (
            mock.patch.object(
                process_monitor_module,
                "_kernel32_process_api",
                return_value=kernel32,
            ),
            mock.patch.object(
                process_monitor_module,
                "_user32_window_api",
                return_value=None,
            ),
            mock.patch.object(
                process_monitor_module.ctypes,
                "get_last_error",
                return_value=process_monitor_module.ERROR_INVALID_PARAMETER,
            ),
        ):
            self.assertFalse(monitor._is_alive(424242))
            self.assertTrue(process_monitor_module._terminate_pid(424242, 0.0))

    def test_process_snapshot_failure_is_not_reported_as_an_empty_system(self):
        kernel32 = mock.Mock()
        kernel32.CreateToolhelp32Snapshot.return_value = (
            process_monitor_module.INVALID_HANDLE_VALUE
        )
        with (
            mock.patch.object(
                process_monitor_module,
                "_kernel32_process_api",
                return_value=kernel32,
            ),
            mock.patch.object(
                process_monitor_module.ctypes,
                "get_last_error",
                return_value=process_monitor_module.ERROR_ACCESS_DENIED,
            ),
        ):
            with self.assertRaises(OSError):
                process_monitor_module._snapshot_processes()

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

    def test_launch_rejects_ambiguous_or_relative_commands_before_popen(self):
        pm = ProcessMonitor()
        with mock.patch.object(process_monitor_module.subprocess, "Popen") as popen:
            with self.assertRaises(TypeError):
                pm.launch(sys.executable)
            with self.assertRaises(ValueError):
                pm.launch([])
            with self.assertRaises(ValueError):
                pm.launch([Path(sys.executable).name])
            with self.assertRaises(ValueError):
                pm.launch([sys.executable], cwd="relative-directory")
            with self.assertRaises(ValueError):
                pm.launch([sys.executable, "bad\0argument"])

        popen.assert_not_called()

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

    def test_find_and_wait_reject_same_name_from_wrong_full_path(self):
        pm = ProcessMonitor(poll_interval_s=0.05)
        rec = pm.launch(_sleeper_command(5))
        try:
            exe_name = os.path.basename(sys.executable)
            expected = os.path.abspath(rec.full_path)
            wrong = str(Path(expected).parent / "wrong-location" / exe_name)

            exact = pm.find_by_executable(
                exe_name,
                expected_path=expected,
            )
            wrong_path = pm.find_by_executable(
                exe_name,
                expected_path=wrong,
            )
            waited_wrong = pm.wait_for_process(
                exe_name,
                timeout_s=0.1,
                expected_path=wrong,
            )

            self.assertIn(rec.pid, {item.pid for item in exact})
            self.assertNotIn(rec.pid, {item.pid for item in wrong_path})
            self.assertNotIn(rec.pid, {item.pid for item in waited_wrong})
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_wait_for_exit_returns_true_when_process_ends(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_fast_exiter_command())
        ok = pm.wait_for_exit(rec.pid, timeout_s=5)
        self.assertTrue(ok)
        self.assertNotIn(rec.pid, pm._children)

    def test_owned_pids_prunes_and_reaps_fast_exiter(self):
        pm = ProcessMonitor(poll_interval_s=0.05)
        rec = pm.launch(_fast_exiter_command())
        deadline = time.monotonic() + 5
        while rec.pid in pm.owned_pids() and time.monotonic() < deadline:
            time.sleep(0.05)

        self.assertNotIn(rec.pid, pm.owned_pids())
        self.assertNotIn(rec.pid, pm._children)

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
        self.assertNotIn(rec.pid, pm._children)
        # 终止后 owned 列表不再包含
        self.assertNotIn(rec.pid, pm.owned_pids())
        # 确实已结束
        self.assertTrue(pm.wait_for_exit(rec.pid, timeout_s=3))

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

    def test_take_ownership_rechecks_path_and_create_time(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        source = ProcessMonitor(poll_interval_s=0.1)
        rec = source.launch(_sleeper_command(20))
        try:
            wrong_path = str(
                Path(rec.full_path).parent
                / "wrong-location"
                / Path(rec.full_path).name
            )
            self.assertIsNone(
                pm.take_ownership(
                    rec.pid,
                    expected_path=wrong_path,
                    expected_create_time=rec.create_time,
                )
            )
            self.assertIsNone(
                pm.take_ownership(
                    rec.pid,
                    expected_path=rec.full_path,
                    expected_create_time=rec.create_time - 10.0,
                )
            )

            taken = pm.take_ownership(
                rec.pid,
                expected_path=rec.full_path,
                expected_create_time=rec.create_time,
            )
            self.assertIsNotNone(taken)
            self.assertEqual(taken.full_path.casefold(), rec.full_path.casefold())
            self.assertTrue(pm.terminate_if_owned(rec.pid))
        finally:
            source.terminate_if_owned(rec.pid)

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

    def test_process_identity_requires_absolute_exact_path_when_requested(self):
        from daily_games.process_monitor import _process_identity_matches

        current = Path(__file__).resolve()
        wrong = current.parent / "wrong-location" / current.name
        self.assertTrue(
            _process_identity_matches(
                current.name,
                str(current),
                current.name,
                str(current),
            )
        )
        self.assertFalse(
            _process_identity_matches(
                current.name,
                str(current),
                current.name,
                str(wrong),
            )
        )
        self.assertFalse(
            _process_identity_matches(
                current.name,
                current.name,
                current.name,
                str(current),
            )
        )


if __name__ == "__main__":
    unittest.main()
