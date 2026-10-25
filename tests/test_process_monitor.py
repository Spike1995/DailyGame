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
import types
import unittest
from unittest import mock

import daily_games.process_monitor as process_monitor_module
from daily_games.process_monitor import (
    IS_WINDOWS,
    STILL_ACTIVE,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ProcessMonitor,
    ProcessRecord,
)


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
        record = ProcessRecord(
            pid=424242,
            executable="x.exe",
            full_path="C:\\x.exe",
            owned=True,
            create_time=1.0,
            native_create_time=99,
        )
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
            # 拒绝访问：无法证明身份，失败关闭且不终止。
            self.assertFalse(
                process_monitor_module._terminate_owned_record(record, 0.0)
            )

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
            # PID 无效即原进程已退出：放弃破坏动作并报告已退出。
            self.assertTrue(
                process_monitor_module._terminate_owned_record(record, 0.0)
            )

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

    def test_take_ownership_rechecks_path_and_native_identity_anchor(self):
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
                    expected_native_create_time=rec.native_create_time + 1,
                )
            )

            taken = pm.take_ownership(
                rec.pid,
                expected_path=rec.full_path,
                # float 只是兼容参数，不再作为身份等价依据（T-008a #7）：
                # 故意传偏差 -10s 的旧值，接管身份由原生标识精确相等保证。
                expected_create_time=rec.create_time - 10.0,
                expected_native_create_time=rec.native_create_time,
            )
            self.assertIsNotNone(taken)
            self.assertEqual(taken.full_path.casefold(), rec.full_path.casefold())
            self.assertEqual(taken.native_create_time, rec.native_create_time)
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

    def test_launch_records_native_creation_identity(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        rec = pm.launch(_sleeper_command(5))
        try:
            self.assertGreater(rec.native_create_time, 0)
            self.assertGreater(rec.create_time, 0.0)
            # 同一进程的原生标识在任何时刻重新查询都必须逐位相等。
            native_now, _seconds = (
                process_monitor_module._query_process_times_by_pid(rec.pid)
            )
            self.assertEqual(native_now, rec.native_create_time)
        finally:
            pm.terminate_if_owned(rec.pid)

    def test_launch_does_not_fabricate_identity_with_time_time(self):
        pm = ProcessMonitor()
        fake_proc = mock.Mock(pid=4711)
        with (
            mock.patch.object(
                process_monitor_module.subprocess, "Popen", return_value=fake_proc
            ),
            mock.patch.object(
                process_monitor_module,
                "_query_full_path",
                return_value=r"C:\Tools\MAA.exe",
            ),
            mock.patch.object(
                process_monitor_module,
                "_query_process_times_by_pid",
                return_value=(0, 0.0),
            ),
            mock.patch.object(
                process_monitor_module.time,
                "time",
                side_effect=AssertionError("禁止用 time.time 人工补身份"),
            ),
        ):
            rec = pm.launch([sys.executable])

        self.assertEqual(rec.native_create_time, 0)
        self.assertEqual(rec.create_time, 0.0)

    def test_find_by_executable_snapshot_records_carry_native_identity(self):
        pm = ProcessMonitor(poll_interval_s=0.1)
        source = ProcessMonitor(poll_interval_s=0.1)
        rec = source.launch(_sleeper_command(5))
        try:
            exe_name = os.path.basename(sys.executable)
            found = pm.find_by_executable(exe_name, expected_path=rec.full_path)
            match = next((r for r in found if r.pid == rec.pid), None)
            self.assertIsNotNone(match)
            self.assertFalse(match.owned)
            # 快照记录与 launch 注册使用同一原生标识语义（T-008a #7）。
            self.assertEqual(match.native_create_time, rec.native_create_time)
        finally:
            source.terminate_if_owned(rec.pid)


FAKE_HANDLE = 0xDE00


def _make_kernel32(
    *,
    full_path: str,
    native_create_time: int,
    exit_codes: list[int] | None = None,
    query_path_ok: bool = True,
    times_ok: bool = True,
    exit_ok: bool = True,
    terminate_ok: bool = True,
    wait_results: list[int] | None = None,
    open_last_error: int = 0,
) -> mock.Mock:
    """构造带受控行为的 kernel32 替身。

    ``exit_codes``/``wait_results`` 依调用顺序消费，末位值对其后所有调用
    保持不变。byref 传递的 ctypes 结构经 ``_obj`` 回写。
    """
    kernel32 = mock.Mock()
    kernel32.OpenProcess.return_value = FAKE_HANDLE
    kernel32.OpenProcess.side_effect = None

    exit_timeline = list(exit_codes) if exit_codes is not None else [STILL_ACTIVE]
    wait_timeline = list(wait_results) if wait_results is not None else [WAIT_TIMEOUT]

    def _query_full_image(handle, flags, buffer, size_ref):
        if not query_path_ok:
            return 0
        buffer.value = full_path
        size_ref._obj.value = len(full_path)
        return 1

    def _get_process_times(handle, create_ref, _exit_ref, _kernel_ref, _user_ref):
        if not times_ok:
            return 0
        create = create_ref._obj
        create.dwLowDateTime = native_create_time & 0xFFFFFFFF
        create.dwHighDateTime = (native_create_time >> 32) & 0xFFFFFFFF
        return 1

    def _get_exit_code(handle, code_ref):
        if not exit_ok or not exit_timeline:
            return 0
        code_ref._obj.value = (
            exit_timeline.pop(0) if len(exit_timeline) > 1 else exit_timeline[0]
        )
        return 1

    def _wait(handle, milliseconds):
        # 终止后的等待固定用 TERMINATE_WAIT_MS；按参数区分，避免宽限分段
        # 的瞬时 mock 返回提前拿到「已退出」信号。
        if milliseconds >= process_monitor_module.TERMINATE_WAIT_MS:
            return WAIT_OBJECT_0
        return (
            wait_timeline.pop(0)
            if len(wait_timeline) > 1
            else wait_timeline[0]
        )

    kernel32.QueryFullProcessImageNameW.side_effect = _query_full_image
    kernel32.GetProcessTimes.side_effect = _get_process_times
    kernel32.GetExitCodeProcess.side_effect = _get_exit_code
    kernel32.TerminateProcess.return_value = terminate_ok
    kernel32.WaitForSingleObject.side_effect = _wait
    return kernel32


def _make_user32(pid: int, *, visible: bool = True, window_pids=None) -> mock.Mock:
    user32 = mock.Mock()
    user32.IsWindowVisible.return_value = 1 if visible else 0

    def _get_thread_pid(hwnd, ref):
        ref._obj.value = (window_pids or {}).get(hwnd, pid)
        return 1

    user32.GetWindowThreadProcessId.side_effect = _get_thread_pid
    user32.PostMessageW.return_value = 1
    return user32


@unittest.skipUnless(IS_WINDOWS, "句柄绑定终止仅在 Windows 上模拟完整分支")
class TestTerminateOwnedRecordIdentityBinding(unittest.TestCase):
    TARGET_PATH = r"C:\Tools\MAA.exe"
    NATIVE = (1234 << 32) | 5678

    def make_rec(self, *, native: int | None = None, path: str | None = None):
        return ProcessRecord(
            pid=4242,
            executable="MAA.exe",
            full_path=path or self.TARGET_PATH,
            owned=True,
            create_time=1.0,
            native_create_time=self.NATIVE if native is None else native,
        )

    def run_terminate(self, rec, grace_s, kernel32, user32=None, windows=None):
        with (
            mock.patch.object(
                process_monitor_module,
                "_kernel32_process_api",
                return_value=kernel32,
            ),
            mock.patch.object(
                process_monitor_module, "_user32_window_api", return_value=user32
            ),
            mock.patch.object(
                process_monitor_module,
                "_enum_windows_for_pid",
                return_value=windows if windows is not None else [],
            ),
        ):
            return process_monitor_module._terminate_owned_record(rec, grace_s)

    def test_identity_match_full_flow_binds_single_handle(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
            wait_results=[WAIT_TIMEOUT],
        )
        user32 = _make_user32(4242)
        windows = [
            {"hwnd": 11, "title": "MAA", "class": "w"},
            {"hwnd": 12, "title": "MAA", "class": "w"},
        ]

        ok = self.run_terminate(self.make_rec(), 0.05, kernel32, user32, windows)

        self.assertTrue(ok)
        # 全流程只 OpenProcess 一次、CloseHandle 恰好一次。
        self.assertEqual(kernel32.OpenProcess.call_count, 1)
        open_args = kernel32.OpenProcess.call_args.args
        self.assertEqual(
            open_args[0],
            process_monitor_module.PROCESS_QUERY_LIMITED_INFORMATION
            | process_monitor_module.PROCESS_TERMINATE
            | process_monitor_module.SYNCHRONIZE,
        )
        self.assertEqual(open_args[2], 4242)
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)
        # WM_CLOSE 逐 HWND 发送；宽限到期只对原句柄 TerminateProcess。
        self.assertEqual(
            [call.args[0] for call in user32.PostMessageW.call_args_list],
            [11, 12],
        )
        for call in user32.PostMessageW.call_args_list:
            self.assertEqual(call.args[1], process_monitor_module.WM_CLOSE)
        kernel32.TerminateProcess.assert_called_once_with(FAKE_HANDLE, 1)
        for call in kernel32.WaitForSingleObject.call_args_list:
            self.assertEqual(call.args[0], FAKE_HANDLE)

    def test_path_mismatch_after_open_aborts_without_destroy_action(self):
        kernel32 = _make_kernel32(
            full_path=r"C:\Other\MaaEnd.exe",
            native_create_time=self.NATIVE,
        )

        ok = self.run_terminate(self.make_rec(), 0.0, kernel32)

        self.assertFalse(ok)
        kernel32.TerminateProcess.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_native_identity_mismatch_even_under_10ms_aborts(self):
        # 相差 50_000 个 FILETIME 单位 = 5ms；float create_time 无法区分。
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE + 50_000,
        )

        ok = self.run_terminate(self.make_rec(), 0.0, kernel32)

        self.assertFalse(ok)
        kernel32.TerminateProcess.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_missing_native_identity_fails_closed(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=0,
        )

        ok = self.run_terminate(self.make_rec(native=0), 0.0, kernel32)

        self.assertFalse(ok)
        kernel32.TerminateProcess.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_exit_after_precheck_sends_nothing_and_terminates_nothing(self):
        # 原进程在预检后已退出（或 PID 已被复用为新实例，但身份仍核验原
        # 句柄）：零 PostMessage、零 TerminateProcess。
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
            exit_codes=[0],
        )
        user32 = _make_user32(4242)
        windows = [{"hwnd": 11, "title": "MAA", "class": "w"}]

        ok = self.run_terminate(self.make_rec(), 1.0, kernel32, user32, windows)

        self.assertTrue(ok)
        user32.PostMessageW.assert_not_called()
        kernel32.TerminateProcess.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_exit_during_hwnd_gating_stops_sending_remaining_messages(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
            exit_codes=[STILL_ACTIVE, STILL_ACTIVE, 0],
        )
        user32 = _make_user32(4242)
        windows = [
            {"hwnd": 11, "title": "MAA", "class": "w"},
            {"hwnd": 12, "title": "MAA", "class": "w"},
        ]

        ok = self.run_terminate(self.make_rec(), 1.0, kernel32, user32, windows)

        self.assertTrue(ok)
        self.assertEqual(user32.PostMessageW.call_count, 1)
        kernel32.TerminateProcess.assert_not_called()

    def test_invisible_or_foreign_window_stops_sending(self):
        window = [{"hwnd": 11, "title": "MAA", "class": "w"}]
        cases = [
            ("invisible", {"visible": False, "window_pids": None}, 0),
            ("foreign_pid", {"visible": True, "window_pids": {11: 9999}}, 0),
        ]
        for label, kwargs, expected_posts in cases:
            with self.subTest(case=label):
                kernel32 = _make_kernel32(
                    full_path=self.TARGET_PATH,
                    native_create_time=self.NATIVE,
                    wait_results=[WAIT_TIMEOUT, WAIT_OBJECT_0],
                )
                user32 = _make_user32(4242, **kwargs)
                ok = self.run_terminate(
                    self.make_rec(), 0.05, kernel32, user32, window
                )
                self.assertTrue(ok)
                self.assertEqual(user32.PostMessageW.call_count, expected_posts)

    def test_exit_during_grace_does_not_reopen_or_terminate_new_instance(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
            exit_codes=[STILL_ACTIVE, STILL_ACTIVE, 0],
            wait_results=[WAIT_TIMEOUT],
        )

        ok = self.run_terminate(self.make_rec(), 5.0, kernel32)

        self.assertTrue(ok)
        # 宽限期间退出：零重开、零新实例破坏。
        self.assertEqual(kernel32.OpenProcess.call_count, 1)
        kernel32.TerminateProcess.assert_not_called()
        for call in kernel32.WaitForSingleObject.call_args_list:
            self.assertEqual(call.args[0], FAKE_HANDLE)
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_grace_expiry_terminates_only_original_handle(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
            wait_results=[WAIT_TIMEOUT],
        )

        ok = self.run_terminate(self.make_rec(), 0.05, kernel32)

        self.assertTrue(ok)
        kernel32.TerminateProcess.assert_called_once_with(FAKE_HANDLE, 1)
        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_terminate_failure_rechecks_exit_on_same_handle(self):
        cases = [
            ("still_running", [STILL_ACTIVE], False),
            # 宽限边界自行退出：TerminateProcess 回报失败，但同句柄复查
            # 已退出 → True。
            ("exited_at_boundary", [STILL_ACTIVE, STILL_ACTIVE, 0], True),
        ]
        for label, exit_codes, expected in cases:
            with self.subTest(case=label):
                kernel32 = _make_kernel32(
                    full_path=self.TARGET_PATH,
                    native_create_time=self.NATIVE,
                    exit_codes=exit_codes,
                    terminate_ok=False,
                )
                ok = self.run_terminate(self.make_rec(), 0.0, kernel32)
                self.assertEqual(ok, expected)
                kernel32.TerminateProcess.assert_called_once_with(FAKE_HANDLE, 1)
                kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_query_failures_abort_and_close_handle(self):
        cases = [
            ("path_query_failed", {"query_path_ok": False}),
            ("times_query_failed", {"times_ok": False}),
            ("exit_query_failed", {"exit_ok": False}),
        ]
        for label, kwargs in cases:
            with self.subTest(case=label):
                kernel32 = _make_kernel32(
                    full_path=self.TARGET_PATH,
                    native_create_time=self.NATIVE,
                    **kwargs,
                )
                ok = self.run_terminate(self.make_rec(), 0.0, kernel32)
                self.assertFalse(ok)
                kernel32.TerminateProcess.assert_not_called()
                kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)

    def test_close_handle_released_on_unexpected_exception(self):
        kernel32 = _make_kernel32(
            full_path=self.TARGET_PATH,
            native_create_time=self.NATIVE,
        )
        kernel32.QueryFullProcessImageNameW.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.run_terminate(self.make_rec(), 0.0, kernel32)

        kernel32.CloseHandle.assert_called_once_with(FAKE_HANDLE)


@unittest.skipUnless(
    IS_WINDOWS,
    "R1 平台隔离：缺标识用例会写入假 owned 记录，非 Windows 下 terminate_if_owned "
    "会走真实 SIGTERM 分支向未启动的 PID 发信号，整组仅限 Windows 运行",
)
class TestOwnedIdentityRegistration(unittest.TestCase):
    """take_ownership / launch 的原生标识语义（快照替身，可跨平台运行）。"""

    SNAPSHOT = [{"pid": 4242, "exe": "MAA.exe", "path": r"C:\Tools\MAA.exe"}]
    NATIVE = (1234 << 32) | 5678

    def run_take_ownership(self, pm, times_results, **kwargs):
        with (
            mock.patch.object(
                process_monitor_module,
                "_snapshot_processes",
                return_value=self.SNAPSHOT,
            ),
            mock.patch.object(
                process_monitor_module,
                "_query_process_times_by_pid",
                side_effect=times_results,
            ),
        ):
            return pm.take_ownership(4242, **kwargs)

    def test_take_ownership_requires_trustworthy_native_identity(self):
        pm = ProcessMonitor()
        cases = [
            ("first_query_missing", [(0, 0.0)], {}),
            ("second_query_missing", [(self.NATIVE, 5.0), (0, 0.0)], {}),
            ("identity_changed_between_queries",
             [(self.NATIVE, 5.0), (self.NATIVE + 50_000, 5.0)], {}),
            ("expected_native_mismatch_under_10ms",
             [(self.NATIVE, 5.0), (self.NATIVE + 50_000, 5.0)],
             {"expected_native_create_time": self.NATIVE}),
        ]
        for label, times_results, kwargs in cases:
            with self.subTest(case=label):
                self.assertIsNone(
                    self.run_take_ownership(pm, times_results, **kwargs)
                )
        self.assertEqual(pm.owned_pids(), [])

    def test_take_ownership_registers_exact_native_identity(self):
        pm = ProcessMonitor()
        rec = self.run_take_ownership(
            pm,
            [(self.NATIVE, 5.0), (self.NATIVE, 5.0)],
            expected_path=r"C:\Tools\MAA.exe",
            expected_create_time=4.9,
            expected_native_create_time=self.NATIVE,
        )
        self.assertIsNotNone(rec)
        self.assertTrue(rec.owned)
        self.assertEqual(rec.native_create_time, self.NATIVE)
        self.assertIn(4242, pm._owned)

    def test_terminate_if_owned_missing_native_identity_fails_closed(self):
        pm = ProcessMonitor()
        rec = ProcessRecord(
            pid=99,
            executable="x.exe",
            full_path=r"C:\Tools\x.exe",
            owned=True,
            create_time=1.0,
            native_create_time=0,
        )
        pm._owned[99] = rec
        with mock.patch.object(
            process_monitor_module, "_terminate_owned_record"
        ) as terminate:
            with mock.patch.object(
                pm, "_owned_process_has_exited", return_value=False
            ):
                self.assertFalse(pm.terminate_if_owned(99))
            terminate.assert_not_called()
            # 已退出则仅回收记录，仍不终止。
            with mock.patch.object(
                pm, "_owned_process_has_exited", return_value=True
            ):
                self.assertTrue(pm.terminate_if_owned(99))
            terminate.assert_not_called()
        self.assertNotIn(99, pm._owned)


class TestPlatformIsolation(unittest.TestCase):
    """R1 平台隔离回归：Windows 原生身份测试不得在非 Windows 触碰真实信号。

    生产代码的非 Windows SIGTERM 降级路径保留不变（T-008a #7）；本组验证
    ①按非 Windows 重新求值本文件测试声明时，原生身份/句柄绑定两组用例
    整体跳过，不存在能向未启动 PID 发信号的用例；②非 Windows 降级的唯一
    信号通道是 ``os.kill``（此处 mock），为上一条的隔离要求提供行为依据。
    """

    WINDOWS_ONLY_CLASSES = (
        "TestTerminateOwnedRecordIdentityBinding",
        "TestOwnedIdentityRegistration",
    )

    def _load_non_windows_declarations(self):
        """按非 Windows 重新求值本文件测试声明，返回探针模块。

        用浅拷贝替身替换 ``sys.modules`` 中的 process_monitor（IS_WINDOWS=False），
        使源码里的 ``skipUnless`` 装饰器按非 Windows 求值。
        """
        proxy = types.ModuleType(process_monitor_module.__name__)
        proxy.__dict__.update(process_monitor_module.__dict__)
        proxy.IS_WINDOWS = False
        saved = sys.modules.get(process_monitor_module.__name__)
        sys.modules[process_monitor_module.__name__] = proxy
        try:
            probe = types.ModuleType("test_process_monitor_non_windows_probe")
            probe.__file__ = __file__
            source = Path(__file__).read_text(encoding="utf-8")
            exec(compile(source, __file__, "exec"), probe.__dict__)
        finally:
            if saved is None:
                sys.modules.pop(process_monitor_module.__name__, None)
            else:
                sys.modules[process_monitor_module.__name__] = saved
        return probe

    def test_windows_only_test_groups_skip_entirely_on_non_windows(self):
        probe = self._load_non_windows_declarations()
        for class_name in self.WINDOWS_ONLY_CLASSES:
            with self.subTest(group=class_name):
                test_class = getattr(probe, class_name)
                self.assertTrue(issubclass(test_class, unittest.TestCase))
                names = unittest.TestLoader().getTestCaseNames(test_class)
                self.assertTrue(names)
                for name in names:
                    method = getattr(test_class, name)
                    skipped = (
                        getattr(test_class, "__unittest_skip__", False)
                        or getattr(method, "__unittest_skip__", False)
                    )
                    self.assertTrue(
                        skipped,
                        msg=f"{class_name}.{name} 未在非 Windows 跳过",
                    )

    @unittest.skipUnless(
        IS_WINDOWS,
        "对照组断言只在 Windows 上有意义：真实类必须可运行",
    )
    def test_real_windows_only_groups_remain_runnable(self):
        for class_name in self.WINDOWS_ONLY_CLASSES:
            with self.subTest(group=class_name):
                test_class = globals()[class_name]
                names = unittest.TestLoader().getTestCaseNames(test_class)
                self.assertTrue(names)
                for name in names:
                    method = getattr(test_class, name)
                    self.assertFalse(
                        getattr(method, "__unittest_skip__", False),
                        msg=f"{class_name}.{name} 在 Windows 被意外跳过",
                    )

    def test_non_windows_degradation_only_signals_via_os_kill(self):
        """钉住非 Windows 降级行为：唯一信号通道是 os.kill（此处 mock）。

        这正是缺标识用例必须平台隔离的行为依据：不 mock 时该分支会向
        未启动的真实 PID 发送 SIGTERM（R1 复现路径）。
        """
        pm = ProcessMonitor()
        rec = ProcessRecord(
            pid=99,
            executable="x.exe",
            full_path=r"C:\Tools\x.exe",
            owned=True,
            create_time=1.0,
            native_create_time=0,
        )
        pm._owned[99] = rec
        with (
            mock.patch.object(process_monitor_module, "IS_WINDOWS", False),
            mock.patch.object(process_monitor_module.os, "kill") as kill,
        ):
            self.assertTrue(pm.terminate_if_owned(99))
        kill.assert_called_once_with(99, mock.ANY)
        self.assertNotIn(99, pm._owned)


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
