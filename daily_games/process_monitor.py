"""进程监控：仅使用 Python 标准库。

能力：
- find_by_executable(name)：按可执行文件名查找现有进程（规范化大小写与完整路径）。
- launch(command, cwd=None)：启动进程，保留 PID、启动时间与归属(owned=True)。
- wait_for_process / wait_for_window / wait_for_exit：尊重超时与停止事件，不永久阻塞。
- terminate_if_owned(pid)：只结束本程序启动或明确接管的挂机工具。

进程与窗口检测通过 Windows ToolHelp32 快照与 user32 EnumWindows 完成，
皆以 ctypes 调用，不引入第三方依赖。在非 Windows 平台上这些功能会以可诊断的方式
在不支持的平台会明确返回降级结果，而不是抛出含义不清的错误。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import locale
import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- #
# 平台常数                                                                     #
# --------------------------------------------------------------------------- #

IS_WINDOWS = sys.platform.startswith("win")

# ToolHelp32
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# --------------------------------------------------------------------------- #
# 结构定义（ctypes）                                                           #
# --------------------------------------------------------------------------- #

if IS_WINDOWS:
    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_char * 260),
        ]


# --------------------------------------------------------------------------- #
# 进程记录类型                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProcessRecord:
    """单一进程的记录。owned=True 表示由本程序启动或明确接管。"""

    pid: int
    executable: str
    full_path: str
    owned: bool
    create_time: float  # unix 时间戳（秒）；无法获取时为 0.0
    extra: Mapping[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 辅助：可执行文件比较规范化                                                        #
# --------------------------------------------------------------------------- #


def _norm_exe(name: str) -> str:
    """规范化可执行文件名称：取文件名部分、小写。"""
    return Path(name).name.lower()


def _exe_matches(record_exe: str, target: str) -> bool:
    """比较可执行文件，规范化大小写与完整路径，避免只靠目录名称判断。"""
    target_norm = _norm_exe(target)
    rec_norm = _norm_exe(record_exe)
    if rec_norm == target_norm:
        return True
    # 也比较完整路径的文件名部分
    return Path(record_exe).name.lower() == target_norm


# --------------------------------------------------------------------------- #
# Windows 进程列举（ToolHelp32）                                            #
# --------------------------------------------------------------------------- #


def _snapshot_processes() -> list[dict[str, object]]:
    """以 ToolHelp32 列举进程。返回 [{pid, exe, path}]。非 Windows 返回空。"""
    if not IS_WINDOWS:
        return []
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return []
        out: list[dict[str, object]] = []
        while True:
            pid = int(entry.th32ProcessID)
            exe = entry.szExeFile.decode("mbcs", errors="replace")
            full_path = _query_full_path(pid) or exe
            out.append({"pid": pid, "exe": exe, "path": full_path})
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
        return out
    finally:
        kernel32.CloseHandle(snapshot)


def _query_full_path(pid: int) -> str:
    """通过 QueryFullProcessImageNameW 获取完整路径；失败返回空字符串。"""
    if not IS_WINDOWS:
        return ""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(1024)
        # QueryFullProcessImageNameW(handle, 0, buf, byref(size))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if ok:
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _get_create_time(pid: int) -> float:
    """获取进程创建时间（unix 秒）。无法获取回 0.0。"""
    if not IS_WINDOWS:
        return 0.0
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0.0
    try:
        ctime = ctypes.c_uint64()
        etime = ctypes.c_uint64()
        ktime = ctypes.c_uint64()
        utime = ctypes.c_uint64()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(ctime), ctypes.byref(etime),
            ctypes.byref(ktime), ctypes.byref(utime),
        )
        if not ok:
            return 0.0
        # FILETIME 是 100ns 间隔，自 1601-01-01
        # 转成 unix 秒：减去 11644473600（秒）
        return ctime.value / 10_000_000.0 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 窗口列举（EnumWindows）                                                      #
# --------------------------------------------------------------------------- #


def _enum_windows_for_pid(pid: int) -> list[dict[str, object]]:
    """列举属于 pid 的顶层窗口。返回 [{hwnd, title, class}]。非 Windows 返回空。"""
    if not IS_WINDOWS:
        return []
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    results: list[dict[str, object]] = []

    # EnumWindows 回调签名：BOOL callback(HWND, LPARAM)
    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined]
    def enum_proc(hwnd: int, lparam: int) -> int:
        # 获取窗口所属 PID
        wpid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if int(wpid.value) != pid:
            return 1  # 继续列举
        # 只考虑可见且有标题的窗口
        if not user32.IsWindowVisible(hwnd):
            return 1
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value
        if title or cls:
            results.append({"hwnd": int(hwnd), "title": title, "class": cls})
        return 1

    user32.EnumWindows(enum_proc, 0)
    return results


# --------------------------------------------------------------------------- #
# ProcessMonitor                                                               #
# --------------------------------------------------------------------------- #


class ProcessMonitor:
    """进程查找、启动、等待与终止。

    所有 wait_* 方法接受 ``stop_event``（threading.Event 或具 ``is_set()`` 的对象）
    与 ``timeout_s``，轮询间隔默认 1 秒。不可永久阻塞。
    """

    def __init__(self, poll_interval_s: float = 1.0):
        self._poll_interval = max(0.05, float(poll_interval_s))
        self._owned: dict[int, ProcessRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 查找现有进程                                                    #
    # ------------------------------------------------------------------ #

    def find_by_executable(self, name: str) -> list[ProcessRecord]:
        """按可执行文件名查找现有进程，并返回包含 owned 标记的混合列表。

        记录的 ``owned`` 区分本程序启动者与现有者。
        """
        target = _norm_exe(name)
        with self._lock:
            owned_copy = dict(self._owned)
        results: list[ProcessRecord] = []
        seen_pids: set[int] = set()

        # 先纳入 owned 且符合者
        for pid, rec in owned_copy.items():
            if _exe_matches(rec.executable, target) or _exe_matches(rec.full_path, target):
                results.append(rec)
                seen_pids.add(pid)

        # 再从系统快照补充现有（非 owned）者
        for p in _snapshot_processes():
            pid = int(p["pid"])
            exe = str(p["exe"])
            path = str(p["path"])
            if pid in seen_pids:
                continue
            if _exe_matches(exe, target) or _exe_matches(path, target):
                results.append(ProcessRecord(
                    pid=pid, executable=exe, full_path=path, owned=False,
                    create_time=_get_create_time(pid),
                ))
        return results

    # ------------------------------------------------------------------ #
    # 启动进程                                                        #
    # ------------------------------------------------------------------ #

    def launch(self, command: Sequence[str] | str, cwd: str | os.PathLike[str] | None = None) -> ProcessRecord:
        """启动进程。

        ``command`` 可为 list[str]（推荐，含空格路径安全）或字符串。
        字符串形式会以 shell=False 通过 subprocess 处理；不手工拼接 shell 命令。
        成功后记录 PID、启动时间，并标记 owned=True。
        """
        if isinstance(command, str):
            args: list[str] | str = command
            shell = False
        else:
            args = list(command)
            shell = False

        creationflags = 0
        if IS_WINDOWS:
            # 不创建新窗口抢焦；与被启动的 GUI 程序无冲突
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if False else 0
            # 注意：MAA/MaaEnd/AALC 是 GUI 程序，不要用 CREATE_NO_WINDOW 否则可能影响其窗口
            # 故保留 0，让它们正常显示。

        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd is not None else None,
            shell=shell,
            stdout=subprocess.DEVNULL if False else None,
            stderr=subprocess.DEVNULL if False else None,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        pid = int(proc.pid)
        # 尝试获取完整路径与创建时间
        time.sleep(0.05)  # 给操作系统一点时间登录进程
        full_path = _query_full_path(pid) or _exe_from_command(command)
        create_time = _get_create_time(pid) or time.time()
        rec = ProcessRecord(
            pid=pid,
            executable=Path(full_path).name,
            full_path=full_path,
            owned=True,
            create_time=create_time,
            extra={"returncode_attr": None, "argv": list(command) if not isinstance(command, str) else command},
        )
        with self._lock:
            self._owned[pid] = rec
        return rec

    def scheduled_task_matches(
        self,
        name: str,
        executable: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        arguments: str = "",
        expected_action_count: int = 1,
        expected_wrapper_target: str = "",
    ) -> bool:
        """只读核对计划任务动作及最高权限级别。

        助手任务必须只有一个精确匹配程序、目录与参数的动作。游戏任务也必须
        只有一个系统 Windows PowerShell 短 ``-File`` 动作；包装脚本路径、
        SHA-256 与版本化元数据必须精确匹配启动器、目录、参数和目标游戏进程。
        旧版双动作或长 ``-EncodedCommand`` 定义都会被拒绝。
        """
        if not IS_WINDOWS or not name:
            return False
        if expected_action_count != 1:
            return False
        result = _run_schtasks(["/Query", "/TN", name, "/XML"], capture_output=True)
        if result.returncode != 0:
            return False
        try:
            xml_text = _decode_schtasks_xml(result.stdout)
            # schtasks 的 XML 声明固定写成 UTF-16，但重定向后的实际字节可能
            # 使用 UTF-8 或当前 Windows 代码页；先按实际字节解码，再修正声明。
            xml_text = xml_text.replace('encoding="UTF-16"', 'encoding="UTF-8"', 1)
            root = ET.fromstring(xml_text)
            actions = root.findall(".//{*}Actions/{*}Exec")
            if len(actions) != expected_action_count:
                return False
            action = actions[0]
            command = action.findtext("{*}Command", default="")
            working_directory = action.findtext(
                "{*}WorkingDirectory", default=""
            )
            actual_arguments = action.findtext("{*}Arguments", default="")
            run_level = root.findtext(".//{*}RunLevel", default="")
        except (UnicodeError, ET.ParseError):
            return False
        if run_level.strip().lower() != "highestavailable":
            return False
        if not expected_wrapper_target:
            return (
                _same_path(command, executable)
                and _same_path(working_directory, cwd)
                and actual_arguments.strip() == arguments.strip()
            )

        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        expected_powershell = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if (
            not _same_path(command, expected_powershell)
            or not _same_path(working_directory, cwd)
        ):
            return False
        contract = _launcher_worker_contract(expected_wrapper_target)
        if contract is None:
            return False
        worker_path, expected_worker_arguments = contract
        if actual_arguments.strip() != expected_worker_arguments:
            return False
        metadata = _read_launcher_worker_metadata(worker_path)
        if metadata is None:
            return False
        return (
            metadata.get("version") == 3
            and _same_path(str(metadata.get("launcher_executable", "")), executable)
            and _same_path(str(metadata.get("working_directory", "")), cwd)
            and str(metadata.get("arguments", "")).strip() == arguments.strip()
            and str(metadata.get("target_process", "")).casefold()
            == expected_wrapper_target.casefold()
        )

    def start_scheduled_task(self, name: str) -> bool:
        """启动已预注册的计划任务；不会创建或修改任务。"""
        if not IS_WINDOWS or not name:
            return False
        return _run_schtasks(["/Run", "/TN", name]).returncode == 0

    def stop_scheduled_task(self, name: str) -> bool:
        """停止已预注册的计划任务；不会影响其他计划任务。"""
        if not IS_WINDOWS or not name:
            return False
        return _run_schtasks(["/End", "/TN", name]).returncode == 0

    def take_ownership(self, pid: int) -> ProcessRecord | None:
        """明确接管一个现有进程，使其可被 terminate_if_owned 结束。

        返回新的 owned 记录；若 PID 不存在则返回 None。
        """
        for p in _snapshot_processes():
            if int(p["pid"]) == pid:
                rec = ProcessRecord(
                    pid=pid,
                    executable=str(p["exe"]),
                    full_path=str(p["path"]),
                    owned=True,
                    create_time=_get_create_time(pid) or time.time(),
                )
                with self._lock:
                    self._owned[pid] = rec
                return rec
        return None

    # ------------------------------------------------------------------ #
    # 等待                                                                #
    # ------------------------------------------------------------------ #

    def wait_for_process(
        self, name: str, timeout_s: float, stop_event: object | None = None,
    ) -> list[ProcessRecord]:
        """等待指定可执行文件的进程出现。超时或停止时返回当前结果（可能为空）。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        last: list[ProcessRecord] = []
        while True:
            last = self.find_by_executable(name)
            if last:
                return last
            if _stop_requested(stop_event):
                return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(self._poll_interval)

    def wait_for_window(
        self, pid: int, timeout_s: float, stop_event: object | None = None,
        title_regex: str | None = None,
    ) -> list[dict[str, object]]:
        """等待 pid 的主窗口出现。

        ``title_regex`` 若提供，则进一步以规范化比较窗口标题（大小写不敏感，子字符串）。
        超时或停止时返回当前符合结果（可能为空）。
        """
        import re

        deadline = time.monotonic() + max(0.0, timeout_s)
        pattern = re.compile(title_regex, re.IGNORECASE) if title_regex else None
        while True:
            wins = _enum_windows_for_pid(pid)
            if pattern is not None:
                wins = [w for w in wins if pattern.search(str(w.get("title", "")))]
            if wins:
                return wins
            if _stop_requested(stop_event):
                return wins
            if time.monotonic() >= deadline:
                return wins
            time.sleep(self._poll_interval)

    def wait_for_exit(
        self, pid: int, timeout_s: float, stop_event: object | None = None,
    ) -> bool:
        """等待 pid 结束。成功结束回 True；超时或停止回 False。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if not self._is_alive(pid):
                # 结束后从 owned 移除
                with self._lock:
                    self._owned.pop(pid, None)
                return True
            if _stop_requested(stop_event):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_interval)

    # ------------------------------------------------------------------ #
    # 终止（仅 owned）                                                    #
    # ------------------------------------------------------------------ #

    def terminate_if_owned(self, pid: int, grace_s: float = 5.0) -> bool:
        """只结束 owned 进程。非 owned 回 False 且不执行任何动作。

        先尝试优雅关闭（WM_CLOSE / TerminateProcess）， grace_s 后仍在则强制结束。
        """
        with self._lock:
            rec = self._owned.get(pid)
        if rec is None or not rec.owned:
            return False
        ok = _terminate_pid(pid, grace_s)
        if ok:
            with self._lock:
                self._owned.pop(pid, None)
        return ok

    def owned_pids(self) -> list[int]:
        with self._lock:
            return list(self._owned.keys())

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #

    def _is_alive(self, pid: int) -> bool:
        if not IS_WINDOWS:
            # 降级：以 os.kill(pid, 0) 探测
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            # STILL_ACTIVE = 259
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 模块级辅助函数                                                               #
# --------------------------------------------------------------------------- #


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


def _exe_from_command(command: Sequence[str] | str) -> str:
    """从命令推测可执行文件路径（用于 launch 后立刻补 full_path）。"""
    if isinstance(command, str):
        return command
    if command:
        return str(command[0])
    return ""


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        left_text = os.fspath(left)
        right_text = os.fspath(right)
        if not left_text.strip() or not right_text.strip():
            return False
        return os.path.normcase(os.path.abspath(left_text)) == os.path.normcase(
            os.path.abspath(right_text)
        )
    except (OSError, TypeError, ValueError):
        return False


def _launcher_worker_contract(target_process: str) -> tuple[Path, str] | None:
    """返回目标游戏对应的根目录包装脚本及精确任务参数。"""
    target_name = Path(target_process).name.casefold()
    worker_names = {
        "arknights.exe": "Arknights",
        "endfield.exe": "Endfield",
    }
    process_stem = worker_names.get(target_name)
    if process_stem is None:
        return None
    worker_path = (
        Path(__file__).resolve().parent.parent
        / ".generated"
        / f"launcher_automation_{process_stem}.ps1"
    )
    try:
        worker_sha256 = hashlib.sha256(worker_path.read_bytes()).hexdigest().upper()
    except OSError:
        return None
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
        worker_sha256,
    ])
    return worker_path, task_arguments


def _read_launcher_worker_metadata(
    worker_path: Path,
) -> dict[str, object] | None:
    """读取根目录包装脚本中唯一的版本化脱敏元数据。"""
    try:
        source = worker_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None

    marker_prefix = "# DAILY_GAMES_LAUNCHER_WORKER_V3="
    markers = [
        line[len(marker_prefix):]
        for line in source.splitlines()
        if line.startswith(marker_prefix)
    ]
    if len(markers) != 1 or not markers[0]:
        return None
    try:
        metadata_payload = base64.b64decode(markers[0], validate=True)
        metadata = json.loads(metadata_payload.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or set(metadata) != {
        "version",
        "launcher_executable",
        "working_directory",
        "arguments",
        "target_process",
    }:
        return None
    return metadata


def _decode_schtasks_xml(payload: bytes) -> str:
    """按 schtasks 实际输出编码解码 XML，兼容含中文参数的任务。"""
    encodings = ("utf-8-sig", locale.getpreferredencoding(False), "mbcs")
    attempted: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return payload.decode(encoding, errors="strict")
        except (LookupError, UnicodeError):
            continue
    raise UnicodeError("无法解码 schtasks XML 输出")


def _run_schtasks(
    arguments: Sequence[str], *, capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    command = ["schtasks.exe", *[str(value) for value in arguments]]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        creationflags=creationflags,
        check=False,
    )


def _terminate_pid(pid: int, grace_s: float) -> bool:
    """向 pid 发送正常关闭信号；经过 grace_s 后仍在运行则调用 TerminateProcess。"""
    if not IS_WINDOWS:
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
            return True
        except (OSError, ProcessLookupError):
            return False

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    # 1. 对所有属于 pid 的可见窗口送 WM_CLOSE
    WM_CLOSE = 0x0010

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined]
    def close_proc(hwnd: int, lparam: int) -> int:
        wpid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if int(wpid.value) == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return 1

    user32.EnumWindows(close_proc, 0)

    # 2. 等待 grace_s
    deadline = time.monotonic() + max(0.0, grace_s)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    while time.monotonic() < deadline:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True  # 已结束
        exit_code = ctypes.c_uint32()
        got = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if got and exit_code.value != 259:
            return True
        time.sleep(0.2)

    # 3. 强制结束
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return True  # 已结束或无权限，视为已不在
    try:
        ok = bool(kernel32.TerminateProcess(handle, 1))
        return ok
    finally:
        kernel32.CloseHandle(handle)


__all__ = [
    "ProcessMonitor",
    "ProcessRecord",
    "IS_WINDOWS",
]
