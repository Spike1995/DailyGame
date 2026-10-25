"""进程监控：仅使用 Python 标准库。

能力：
- find_by_executable(name, expected_path=None)：按文件名查找，并可要求精确完整路径。
- launch(command, cwd=None)：仅以绝对路径参数序列启动进程，保留 PID、启动时间与归属(owned=True)。
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
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- #
# 平台常数                                                                     #
# --------------------------------------------------------------------------- #

IS_WINDOWS = sys.platform.startswith("win")

# ToolHelp32
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
OPEN_EXISTING = 3
ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
SCHTASKS_TIMEOUT_S = 15
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
TERMINATE_WAIT_MS = 5_000
WM_CLOSE = 0x0010

_FILE_MUTATION_RIGHTS = (
    0x00000002,  # FILE_WRITE_DATA
    0x00000004,  # FILE_APPEND_DATA
    0x00000010,  # FILE_WRITE_EA
    0x00000100,  # FILE_WRITE_ATTRIBUTES
    0x00010000,  # DELETE
    0x00040000,  # WRITE_DAC
    0x00080000,  # WRITE_OWNER
)
_DIRECTORY_MUTATION_RIGHTS = (
    0x00000002,  # FILE_ADD_FILE
    0x00000004,  # FILE_ADD_SUBDIRECTORY
    0x00000040,  # FILE_DELETE_CHILD
    0x00010000,  # DELETE
    0x00040000,  # WRITE_DAC
    0x00080000,  # WRITE_OWNER
)


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
            ("szExeFile", ctypes.c_wchar * 260),
        ]


    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_uint32),
            ("dwHighDateTime", ctypes.c_uint32),
        ]


    _WindowEnumProc = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ssize_t,
    )


@lru_cache(maxsize=1)
def _kernel32_process_api():
    """载入并声明进程相关 Win32 API，避免 64 位 HANDLE 被截断。"""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessEntry32),
        ]
        kernel32.Process32FirstW.restype = ctypes.c_int
        kernel32.Process32NextW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessEntry32),
        ]
        kernel32.Process32NextW.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        return kernel32
    except (AttributeError, OSError, TypeError):
        return None


@lru_cache(maxsize=1)
def _user32_window_api():
    """载入并声明窗口相关 Win32 API。"""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumWindows.argtypes = [_WindowEnumProc, ctypes.c_ssize_t]
        user32.EnumWindows.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_int
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.PostMessageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.PostMessageW.restype = ctypes.c_int
        return user32
    except (AttributeError, OSError, TypeError):
        return None


def _windows_api_error(operation: str) -> OSError:
    error_code = int(ctypes.get_last_error())
    return OSError(
        error_code or 1,
        f"{operation} 失败（Windows 错误 {error_code or '未知'}）",
    )


@lru_cache(maxsize=1)
def _version_api():
    """载入并声明文件版本 API（version.dll），供只读版本诊断使用。"""
    if not IS_WINDOWS:
        return None
    try:
        version_dll = ctypes.WinDLL("version", use_last_error=True)
        version_dll.GetFileVersionInfoSizeW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        version_dll.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
        version_dll.GetFileVersionInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        version_dll.GetFileVersionInfoW.restype = ctypes.c_int
        version_dll.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        version_dll.VerQueryValueW.restype = ctypes.c_int
        return version_dll
    except (AttributeError, OSError, TypeError):
        return None


UNKNOWN_VERSION = "unknown"


def read_file_version(exe_path: str | os.PathLike[str]) -> str:
    """只读返回 exe 的 FileVersion 字符串；无法读取时返回 ``unknown``。

    仅供启动器身份漂移等只读诊断（T-006 S2）使用：只调用
    GetFileVersionInfoW/VerQueryValueW 读取版本资源，不启动进程、
    不调用 PowerShell、不验证签名；授权仍以启动 worker 的
    签名/ACL/哈希门控为准。任何一步失败都返回 ``unknown``，调用方
    必须失败关闭，不得把读不到当成版本一致。
    """
    if not IS_WINDOWS:
        return UNKNOWN_VERSION
    version_dll = _version_api()
    if version_dll is None:
        return UNKNOWN_VERSION
    path = os.fspath(exe_path)
    try:
        size = int(version_dll.GetFileVersionInfoSizeW(path, None))
        if size <= 0:
            return UNKNOWN_VERSION
        block = ctypes.create_string_buffer(size)
        block_pointer = ctypes.cast(block, ctypes.c_void_p)
        if not version_dll.GetFileVersionInfoW(path, 0, size, block_pointer):
            return UNKNOWN_VERSION

        value_pointer = ctypes.c_void_p()
        value_length = ctypes.c_uint32()
        if (
            not version_dll.VerQueryValueW(
                block_pointer,
                "\\VarFileInfo\\Translation",
                ctypes.byref(value_pointer),
                ctypes.byref(value_length),
            )
            or value_length.value < 4
            or not value_pointer.value
        ):
            return UNKNOWN_VERSION
        translation = ctypes.cast(value_pointer, ctypes.POINTER(ctypes.c_uint16))
        subblock = (
            f"\\StringFileInfo\\{translation[0]:04X}{translation[1]:04X}"
            "\\FileVersion"
        )

        value_pointer = ctypes.c_void_p()
        value_length = ctypes.c_uint32()
        if (
            not version_dll.VerQueryValueW(
                block_pointer,
                subblock,
                ctypes.byref(value_pointer),
                ctypes.byref(value_length),
            )
            or value_length.value == 0
            or not value_pointer.value
        ):
            return UNKNOWN_VERSION
        # VerQueryValueW 返回的长度含结尾 NUL；空串同样视为不可核验。
        text = ctypes.wstring_at(
            value_pointer.value, max(0, value_length.value - 1),
        ).strip()
        return text or UNKNOWN_VERSION
    except (OSError, ValueError, IndexError, ctypes.ArgumentError):
        return UNKNOWN_VERSION


# --------------------------------------------------------------------------- #
# 进程记录类型                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProcessRecord:
    """单一进程的记录。owned=True 表示由本程序启动或明确接管。

    ``native_create_time`` 是 GetProcessTimes 的 64 位 FILETIME 原值
    （100ns 单位，``(``high<<32)|low``，int），为 owned 身份的唯一依据：
    接管与终止都必须与之精确相等；取不到时为 0，一律失败关闭。
    ``create_time``（unix 秒 float）仅作显示与兼容，不得再作为身份等价
    依据（T-008a #7）。
    """

    pid: int
    executable: str
    full_path: str
    owned: bool
    create_time: float  # unix 时间戳（秒）；无法获取时为 0.0
    native_create_time: int = 0  # 64 位 FILETIME 原值；0 表示不可信/未知
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


def _process_identity_matches(
    record_exe: str,
    record_path: str,
    target_name: str,
    expected_path: str | os.PathLike[str] | None,
) -> bool:
    """先核对文件名；指定路径时，路径缺失或非绝对路径均拒绝。"""
    if not (
        _exe_matches(record_exe, target_name)
        or _exe_matches(record_path, target_name)
    ):
        return False
    if expected_path is None:
        return True
    try:
        record_text = os.fspath(record_path)
        expected_text = os.fspath(expected_path)
        if not os.path.isabs(record_text) or not os.path.isabs(expected_text):
            return False
    except (TypeError, ValueError):
        return False
    return _same_path(record_text, expected_text)


# --------------------------------------------------------------------------- #
# Windows 进程列举（ToolHelp32）                                            #
# --------------------------------------------------------------------------- #


def _snapshot_processes() -> list[dict[str, object]]:
    """以 ToolHelp32 列举进程。返回 [{pid, exe, path}]。非 Windows 返回空。"""
    if not IS_WINDOWS:
        return []
    kernel32 = _kernel32_process_api()
    if kernel32 is None:
        raise OSError("无法载入 Windows 进程列举 API")
    ctypes.set_last_error(0)
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise _windows_api_error("CreateToolhelp32Snapshot")
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ctypes.set_last_error(0)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            if ctypes.get_last_error() == ERROR_NO_MORE_FILES:
                return []
            raise _windows_api_error("Process32FirstW")
        out: list[dict[str, object]] = []
        while True:
            pid = int(entry.th32ProcessID)
            exe = entry.szExeFile
            full_path = _query_full_path(pid) or exe
            out.append({"pid": pid, "exe": exe, "path": full_path})
            ctypes.set_last_error(0)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error_code = int(ctypes.get_last_error())
                if error_code not in {0, ERROR_NO_MORE_FILES}:
                    raise _windows_api_error("Process32NextW")
                break
        return out
    finally:
        kernel32.CloseHandle(snapshot)


def _query_full_path(pid: int) -> str:
    """通过 QueryFullProcessImageNameW 获取完整路径；失败返回空字符串。"""
    if not IS_WINDOWS:
        return ""
    kernel32 = _kernel32_process_api()
    if kernel32 is None:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buf))
        # QueryFullProcessImageNameW(handle, 0, buf, byref(size))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if ok:
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _filetime_to_unix_seconds(file_time: int) -> float:
    """64 位 FILETIME（100ns，1601 纪元）转 unix 秒；仅作显示/兼容。"""
    # FILETIME 是 100ns 间隔，自 1601-01-01；减去 1601→1970 的 11644473600 秒。
    return file_time / 10_000_000.0 - 11644473600.0


def _query_process_times_from_handle(kernel32, handle) -> tuple[int, float]:
    """用已打开的句柄查询 (原生创建 FILETIME int, unix 秒 float)。

    原生标识是 GetProcessTimes 的 64 位 FILETIME 原值 ``(``high<<32)|low``，
    为 owned 身份的唯一依据；查询失败或取不到正值时返回 (0, 0.0)，
    调用方必须失败关闭，不得用 time.time 人工补身份（T-008a #7）。
    """
    ctime = _FileTime()
    etime = _FileTime()
    ktime = _FileTime()
    utime = _FileTime()
    ok = kernel32.GetProcessTimes(
        handle, ctypes.byref(ctime), ctypes.byref(etime),
        ctypes.byref(ktime), ctypes.byref(utime),
    )
    if not ok:
        return 0, 0.0
    native = (int(ctime.dwHighDateTime) << 32) | int(ctime.dwLowDateTime)
    if native <= 0:
        return 0, 0.0
    return native, _filetime_to_unix_seconds(native)


def _query_process_times_by_pid(pid: int) -> tuple[int, float]:
    """打开进程一次并查询 (原生创建 FILETIME int, unix 秒 float)。

    失败返回 (0, 0.0)；原生标识为 0 时接管/终止一律失败关闭。
    """
    if not IS_WINDOWS:
        return 0, 0.0
    kernel32 = _kernel32_process_api()
    if kernel32 is None:
        return 0, 0.0
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0, 0.0
    try:
        return _query_process_times_from_handle(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _get_create_time(pid: int) -> float:
    """获取进程创建时间（unix 秒，显示/兼容用）。无法获取回 0.0。"""
    _native, seconds = _query_process_times_by_pid(pid)
    return seconds


# --------------------------------------------------------------------------- #
# 窗口列举（EnumWindows）                                                      #
# --------------------------------------------------------------------------- #


def _enum_windows_for_pid(pid: int) -> list[dict[str, object]]:
    """列举属于 pid 的顶层窗口。返回 [{hwnd, title, class}]。非 Windows 返回空。"""
    if not IS_WINDOWS:
        return []
    user32 = _user32_window_api()
    if user32 is None:
        raise OSError("无法载入 Windows 窗口列举 API")
    results: list[dict[str, object]] = []

    # EnumWindows 回调签名：BOOL callback(HWND, LPARAM)
    @_WindowEnumProc
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
            results.append({
                "hwnd": int(hwnd or 0),
                "title": title,
                "class": cls,
            })
        return 1

    ctypes.set_last_error(0)
    if not user32.EnumWindows(enum_proc, 0):
        raise _windows_api_error("EnumWindows")
    return results


# --------------------------------------------------------------------------- #
# ProcessMonitor                                                               #
# --------------------------------------------------------------------------- #


class ProcessMonitor:
    """进程查找、启动、等待与终止。

    所有 wait_* 方法接受 ``stop_event``（threading.Event 或具 ``is_set()`` 的对象）
    与 ``timeout_s``，轮询间隔默认 1 秒。不可永久阻塞。

    为避免轮询循环里多次 ``find_by_executable`` 各做一次完整 ToolHelp32 扫描，
    本类缓存最近一次系统进程快照，TTL 默认 50ms（小于最小轮询间隔）。
    ``launch`` / ``terminate_if_owned`` 等改变进程状态的操作会让缓存立即失效。
    """

    # 进程快照缓存 TTL。适配器在单次 wait_completion 循环里可能调用 6-7 次
    # find_by_executable；缓存让它们共享同一份系统快照，把每轮系统调用数
    # 从几十次降到一次。TTL 远小于最小轮询间隔，不会让等待逻辑产生可观察延迟。
    _SNAPSHOT_CACHE_TTL_S = 0.05

    def __init__(self, poll_interval_s: float = 1.0):
        self._poll_interval = max(0.05, float(poll_interval_s))
        self._owned: dict[int, ProcessRecord] = {}
        self._children: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._snapshot_cache: list[dict[str, object]] | None = None
        self._snapshot_cache_time: float = 0.0
        self._snapshot_cache_lock = threading.Lock()

    def _cached_snapshot_processes(self) -> list[dict[str, object]]:
        """带 TTL 的进程快照；同一次轮询循环内多次调用共享一份系统快照。"""
        now = time.monotonic()
        with self._snapshot_cache_lock:
            if (
                self._snapshot_cache is not None
                and now - self._snapshot_cache_time < self._SNAPSHOT_CACHE_TTL_S
            ):
                return self._snapshot_cache
        fresh = _snapshot_processes()
        with self._snapshot_cache_lock:
            self._snapshot_cache = fresh
            self._snapshot_cache_time = now
        return fresh

    def _invalidate_snapshot_cache(self) -> None:
        """本进程刚启动或终止了进程，让下一次查找重新枚举。"""
        with self._snapshot_cache_lock:
            self._snapshot_cache = None

    # ------------------------------------------------------------------ #
    # 查找现有进程                                                    #
    # ------------------------------------------------------------------ #

    def find_by_executable(
        self,
        name: str,
        *,
        expected_path: str | os.PathLike[str] | None = None,
    ) -> list[ProcessRecord]:
        """按文件名查找进程，并可进一步要求完整路径精确匹配。

        记录的 ``owned`` 区分本程序启动者与现有者。指定 ``expected_path``
        后，无法取得绝对进程路径的记录也会拒绝，避免同名程序成为启动或完成证据。
        """
        target = _norm_exe(name)
        self._prune_exited_owned()
        with self._lock:
            owned_copy = dict(self._owned)
        results: list[ProcessRecord] = []
        seen_pids: set[int] = set()

        # 先纳入 owned 且符合者
        for pid, rec in owned_copy.items():
            if _process_identity_matches(
                rec.executable,
                rec.full_path,
                target,
                expected_path,
            ):
                results.append(rec)
                seen_pids.add(pid)

        # 再从系统快照补充现有（非 owned）者
        for p in self._cached_snapshot_processes():
            pid = int(p["pid"])
            exe = str(p["exe"])
            path = str(p["path"])
            if pid in seen_pids:
                continue
            if _process_identity_matches(exe, path, target, expected_path):
                native_create_time, create_time = _query_process_times_by_pid(pid)
                results.append(ProcessRecord(
                    pid=pid, executable=exe, full_path=path, owned=False,
                    create_time=create_time,
                    native_create_time=native_create_time,
                ))
        return results

    # ------------------------------------------------------------------ #
    # 启动进程                                                        #
    # ------------------------------------------------------------------ #

    def launch(
        self,
        command: Sequence[str | os.PathLike[str]],
        cwd: str | os.PathLike[str] | None = None,
    ) -> ProcessRecord:
        """启动进程。

        ``command`` 必须是非空参数序列，且首项为绝对可执行文件路径；不接受
        字符串命令列或 PATH 搜索，避免 Windows 对含空格命令的模糊解析。成功后
        记录 PID、启动时间，并标记 owned=True。
        """
        if isinstance(command, (str, bytes, os.PathLike)):
            raise TypeError("command 必须是参数序列，不能是字符串或单一路径")
        try:
            args = [os.fspath(value) for value in command]
        except (TypeError, ValueError) as exc:
            raise TypeError("command 只能包含字符串或路径") from exc
        if any(not isinstance(value, str) for value in args):
            raise TypeError("command 只能包含文字路径或参数")
        if not args or not args[0].strip() or not os.path.isabs(args[0]):
            raise ValueError("command 的首项必须是绝对可执行文件路径")
        if any("\0" in value for value in args):
            raise ValueError("command 不得包含 NUL 字符")
        if cwd is not None:
            cwd_text = os.fspath(cwd)
            if (
                not isinstance(cwd_text, str)
                or not cwd_text.strip()
                or not os.path.isabs(cwd_text)
            ):
                raise ValueError("cwd 必须是绝对路径")
        else:
            cwd_text = None

        proc = subprocess.Popen(
            args,
            cwd=cwd_text,
            shell=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        pid = int(proc.pid)
        # 尝试获取完整路径与原生创建标识/创建时间
        time.sleep(0.05)  # 给操作系统一点时间登录进程
        full_path = _query_full_path(pid) or _exe_from_command(args)
        # 原生创建标识是接管/终止的唯一身份依据；取不到时保持 0（后续
        # 终止失败关闭），绝不用 time.time 人工补身份（T-008a #7）。
        native_create_time, create_time = _query_process_times_by_pid(pid)
        rec = ProcessRecord(
            pid=pid,
            executable=Path(full_path).name,
            full_path=full_path,
            owned=True,
            create_time=create_time,
            native_create_time=native_create_time,
            extra={"returncode_attr": None, "argv": list(args)},
        )
        with self._lock:
            self._owned[pid] = rec
            self._children[pid] = proc
        self._invalidate_snapshot_cache()
        return rec

    def scheduled_task_matches(
        self,
        name: str,
        executable: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        arguments: str = "",
        expected_action_count: int = 1,
        expected_wrapper_target: str = "",
        expected_wrapper_executable: str = "",
    ) -> bool:
        """只读核对计划任务动作及最高权限级别。

        助手任务必须只有一个精确匹配程序、目录与参数的动作。游戏任务也必须
        只有一个系统 Windows PowerShell 短 ``-File`` 动作；包装脚本必须位于
        受保护的 ProgramData 目录，且路径、SHA-256 与版本化元数据必须精确
        匹配启动器、目录、参数和目标游戏进程。
        旧版双动作、长 ``-EncodedCommand`` 定义与 V4 及更早的包装脚本都会
        被拒绝；仅 V5（点击前窗口就绪门控）受支持。
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
            principal_user_id = root.findtext(".//{*}UserId", default="")
            logon_type = root.findtext(".//{*}LogonType", default="")
            run_level = root.findtext(".//{*}RunLevel", default="")
            multiple_instances = root.findtext(
                ".//{*}MultipleInstancesPolicy", default=""
            )
            execution_time_limit = root.findtext(
                ".//{*}ExecutionTimeLimit", default=""
            )
            disallow_on_battery = root.findtext(
                ".//{*}DisallowStartIfOnBatteries", default=""
            )
            stop_on_battery = root.findtext(
                ".//{*}StopIfGoingOnBatteries", default=""
            )
            allow_demand = root.findtext(
                ".//{*}AllowStartOnDemand", default="true"
            )
            enabled = root.findtext(".//{*}Enabled", default="true")
            triggers = root.findall(".//{*}Triggers/*")
        except (UnicodeError, ET.ParseError):
            return False
        current_user_sid = _current_user_sid()
        if (
            not current_user_sid
            or principal_user_id.strip().casefold() != current_user_sid.casefold()
            or logon_type.strip().casefold() != "interactivetoken"
            or run_level.strip().casefold() != "highestavailable"
            or multiple_instances.strip().casefold() != "ignorenew"
            or execution_time_limit.strip().upper() != "PT12H"
            or disallow_on_battery.strip().casefold() != "false"
            or stop_on_battery.strip().casefold() != "false"
            or allow_demand.strip().casefold() != "true"
            or enabled.strip().casefold() != "true"
            or len(triggers) != 0
        ):
            return False
        if not expected_wrapper_target:
            return (
                _same_path(command, executable)
                and _same_path(working_directory, cwd)
                and actual_arguments.strip() == arguments.strip()
            )
        if not expected_wrapper_executable:
            return False

        expected_powershell = _windows_system_file(
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        if (
            expected_powershell is None
            or not _same_path(command, expected_powershell)
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
            metadata.get("version") == 5
            and _same_path(str(metadata.get("launcher_executable", "")), executable)
            and _same_path(str(metadata.get("working_directory", "")), cwd)
            and str(metadata.get("arguments", "")).strip() == arguments.strip()
            and str(metadata.get("target_process", "")).casefold()
            == expected_wrapper_target.casefold()
            and _same_path(
                str(metadata.get("target_executable", "")),
                expected_wrapper_executable,
            )
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

    def take_ownership(
        self,
        pid: int,
        *,
        expected_path: str | os.PathLike[str] | None = None,
        expected_create_time: float | None = None,
        expected_native_create_time: int | None = None,
    ) -> ProcessRecord | None:
        """明确接管一个现有进程，使其可被 terminate_if_owned 结束。

        身份核验（T-008a #7）：
        - ``expected_path`` 提供时路径必须精确匹配；
        - 原生 64 位 FILETIME 创建标识在快照枚举后与再次打开后各查询一次，
          两次必须逐位相等（防御枚举与登记之间的 PID 复用）；
        - ``expected_native_create_time`` 提供时还须与之精确相等。
        ``expected_create_time``（float）仅为兼容旧调用保留，**不再作为
        身份等价依据**（相差 <10ms 的不同实例可能得到相同 float）；调用方
        应改传 ``expected_native_create_time``（快照记录现已携带
        ``native_create_time``）。任一原生标识取不到或不相等一律拒绝，
        绝不以 time.time 补齐。
        """
        for p in _snapshot_processes():
            if int(p["pid"]) != pid:
                continue
            executable = str(p["exe"])
            full_path = str(p["path"])
            if expected_path is not None and not _process_identity_matches(
                executable,
                full_path,
                Path(os.fspath(expected_path)).name,
                expected_path,
            ):
                return None
            native_first, _seconds_first = _query_process_times_by_pid(pid)
            if native_first == 0:
                return None
            native_create_time, create_time = _query_process_times_by_pid(pid)
            if native_create_time == 0 or native_create_time != native_first:
                return None
            if (
                expected_native_create_time is not None
                and native_create_time != expected_native_create_time
            ):
                return None
            rec = ProcessRecord(
                pid=pid,
                executable=executable,
                full_path=full_path,
                owned=True,
                create_time=create_time,
                native_create_time=native_create_time,
            )
            with self._lock:
                self._owned[pid] = rec
            return rec
        return None

    # ------------------------------------------------------------------ #
    # 等待                                                                #
    # ------------------------------------------------------------------ #

    def wait_for_process(
        self,
        name: str,
        timeout_s: float,
        stop_event: object | None = None,
        *,
        expected_path: str | os.PathLike[str] | None = None,
    ) -> list[ProcessRecord]:
        """等待指定程序出现；可要求完整路径，超时或停止时返回当前结果。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        last: list[ProcessRecord] = []
        while True:
            last = self.find_by_executable(name, expected_path=expected_path)
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
            if self._owned_process_has_exited(pid):
                self._forget_owned(pid, reap_timeout_s=0.5)
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

        Windows 上只 OpenProcess 一次：身份核验（路径/原生创建标识/存活）、
        逐窗口 WM_CLOSE、宽限等待与强制结束都复用同一内核对象句柄，每个
        分支在 ``finally`` 中恰好关闭一次；身份不明或原进程已退出时放弃
        破坏动作（T-008a #7）。非 Windows 保留 SIGTERM 降级路径。
        """
        with self._lock:
            rec = self._owned.get(pid)
        if rec is None or not rec.owned:
            return False
        if IS_WINDOWS:
            if rec.native_create_time == 0:
                # 缺可信原生标识：绝不终止；若进程已退出则仅回收记录。
                if self._owned_process_has_exited(pid):
                    return self._forget_owned(pid, reap_timeout_s=0.5)
                return False
            ok = _terminate_owned_record(rec, grace_s)
        else:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                ok = True
            except (OSError, ProcessLookupError):
                ok = False
        if ok:
            return self._forget_owned(
                pid,
                reap_timeout_s=max(1.0, min(5.0, float(grace_s))),
            )
        # 进程可能恰好在宽限期边界自行退出，使终止回报失败。
        # 对本程序启动的子进程再做一次有界 wait，确认退出后仍应回收句柄。
        with self._lock:
            child = self._children.get(pid)
        if child is not None:
            return self._forget_owned(pid, reap_timeout_s=0.5)
        return False

    def owned_pids(self) -> list[int]:
        self._prune_exited_owned()
        with self._lock:
            return list(self._owned.keys())

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #

    def _owned_process_has_exited(self, pid: int) -> bool:
        with self._lock:
            child = self._children.get(pid)
        if child is not None:
            return child.poll() is not None
        return not self._is_alive(pid)

    def _forget_owned(self, pid: int, *, reap_timeout_s: float) -> bool:
        """回收本程序启动的 Popen，再移除 owned 记录。"""
        with self._lock:
            child = self._children.get(pid)
        if child is not None:
            try:
                child.wait(timeout=max(0.0, reap_timeout_s))
            except subprocess.TimeoutExpired:
                return False
        with self._lock:
            self._owned.pop(pid, None)
            self._children.pop(pid, None)
        self._invalidate_snapshot_cache()
        return True

    def _prune_exited_owned(self) -> None:
        with self._lock:
            pids = tuple(self._owned)
        for pid in pids:
            if self._owned_process_has_exited(pid):
                self._forget_owned(pid, reap_timeout_s=0.0)

    def _is_alive(self, pid: int) -> bool:
        if not IS_WINDOWS:
            # 降级：以 os.kill(pid, 0) 探测
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        kernel32 = _kernel32_process_api()
        if kernel32 is None:
            return True
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            # PID 不存在时 OpenProcess 回 ERROR_INVALID_PARAMETER。访问被拒或
            # 其他未知错误不能证明进程已退出，必须保守视为仍在运行。
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == STILL_ACTIVE
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


def _exe_from_command(command: Sequence[str]) -> str:
    """从命令推测可执行文件路径（用于 launch 后立刻补 full_path）。"""
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


def _protected_launcher_worker_root() -> Path | None:
    """返回系统 ProgramData 下受保护的启动器 worker 目录。"""
    if not IS_WINDOWS:
        return None
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        get_folder_path = shell32.SHGetFolderPathW
        get_folder_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
        ]
        get_folder_path.restype = ctypes.c_int32
        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_COMMON_APPDATA = 0x23，与 PowerShell CommonApplicationData 一致。
        if get_folder_path(None, 0x23, None, 0, buffer) != 0:
            return None
        common_data = buffer.value.strip()
        if not common_data:
            return None
        return Path(common_data) / "DailyGames" / "workers"
    except (AttributeError, OSError, ValueError):
        return None


def _path_grants_access(
    path: Path, desired_access: int, *, directory: bool,
) -> bool | None:
    """只读探测当前 token 是否取得指定存取权；未知错误回 ``None``。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = create_file(
            os.fspath(path),
            desired_access,
            FILE_SHARE_ALL,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS if directory else 0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return False if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
        if handle is None:
            return None
        close_handle(handle)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _launcher_worker_security_is_valid(worker_path: Path) -> bool:
    """拒绝重解析点及当前普通 token 可修改的 worker 路径。"""
    if not IS_WINDOWS:
        return False
    worker_root = _protected_launcher_worker_root()
    if worker_root is None:
        return False
    try:
        expected_root = Path(os.path.abspath(worker_root))
        candidate = Path(os.path.abspath(worker_path))
        if candidate.parent != expected_root:
            return False
        if candidate.name not in {
            "launcher_automation_Arknights.ps1",
            "launcher_automation_Endfield.ps1",
        }:
            return False
        product_root = expected_root.parent
        log_root = product_root / "logs"
        for item in (product_root, expected_root, log_root, candidate):
            stat_result = os.lstat(item)
            if (
                getattr(stat_result, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                return False
        probes = (
            (product_root, _DIRECTORY_MUTATION_RIGHTS, True),
            (expected_root, _DIRECTORY_MUTATION_RIGHTS, True),
            (log_root, _DIRECTORY_MUTATION_RIGHTS, True),
            (candidate, _FILE_MUTATION_RIGHTS, False),
        )
        for path, access_rights, is_directory in probes:
            for access in access_rights:
                granted = _path_grants_access(
                    path, access, directory=is_directory,
                )
                if granted is not False:
                    return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def _launcher_worker_contract(target_process: str) -> tuple[Path, str] | None:
    """返回目标游戏对应的受保护包装脚本及精确任务参数。"""
    target_name = Path(target_process).name.casefold()
    worker_names = {
        "arknights.exe": "Arknights",
        "endfield.exe": "Endfield",
    }
    process_stem = worker_names.get(target_name)
    if process_stem is None:
        return None
    worker_root = _protected_launcher_worker_root()
    if worker_root is None:
        return None
    worker_path = worker_root / f"launcher_automation_{process_stem}.ps1"
    if not _launcher_worker_security_is_valid(worker_path):
        return None
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
    """读取受保护包装脚本中唯一的版本化脱敏元数据。"""
    try:
        source = worker_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None

    marker_prefix = "# DAILY_GAMES_LAUNCHER_WORKER_V5="
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
        "target_executable",
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


@lru_cache(maxsize=1)
def _windows_system_directory() -> Path | None:
    """通过 Win32 API 取得系统目录，不信任可被继承修改的环境变量。"""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_system_directory = kernel32.GetSystemDirectoryW
        get_system_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        get_system_directory.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(get_system_directory(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            return None
        directory = Path(buffer.value)
        return directory if directory.is_dir() else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_system_file(*parts: str) -> Path | None:
    """取得系统目录下的既存文件；任何异常都失败关闭。"""
    root = _windows_system_directory()
    if root is None or not parts:
        return None
    try:
        if any(
            not part
            or Path(part).is_absolute()
            or any(piece in {"", ".", ".."} for piece in Path(part).parts)
            for part in parts
        ):
            return None
        candidate = root.joinpath(*parts)
        return candidate if candidate.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


def _native_current_user_sid() -> str | None:
    """直接从当前 Windows token 读取 SID，不启动 PATH 中的外部程序。"""
    if not IS_WINDOWS:
        return None
    token = ctypes.c_void_p()
    sid_text = ctypes.c_void_p()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("Sid", ctypes.c_void_p),
                ("Attributes", ctypes.c_uint32),
            ]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("User", _SidAndAttributes)]

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.OpenProcessToken.restype = ctypes.c_int
        advapi32.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.GetTokenInformation.restype = ctypes.c_int
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = ctypes.c_int

        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,  # TOKEN_QUERY
            ctypes.byref(token),
        ):
            return None
        required = ctypes.c_uint32()
        advapi32.GetTokenInformation(
            token,
            1,  # TokenUser
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            return None
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            return None
        token_user = ctypes.cast(
            buffer,
            ctypes.POINTER(_TokenUser),
        ).contents
        if not token_user.User.Sid or not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid,
            ctypes.byref(sid_text),
        ):
            return None
        value = ctypes.wstring_at(sid_text.value)
        return value if re.fullmatch(r"S-\d+(?:-\d+)+", value) else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    finally:
        if sid_text.value:
            try:
                kernel32.LocalFree(sid_text)
            except (AttributeError, OSError):
                pass
        if token.value:
            try:
                kernel32.CloseHandle(token)
            except (AttributeError, OSError):
                pass


@lru_cache(maxsize=1)
def _current_user_sid() -> str | None:
    """读取当前 token 的 SID；无法明确取得时失败关闭。"""
    return _native_current_user_sid()


def _run_schtasks(
    arguments: Sequence[str], *, capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    executable = _windows_system_file("schtasks.exe")
    command = [
        str(executable) if executable is not None else "<schtasks-unavailable>",
        *[str(value) for value in arguments],
    ]
    if executable is None:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=b"",
            stderr=b"Windows system schtasks.exe is unavailable",
        )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            creationflags=creationflags,
            check=False,
            timeout=SCHTASKS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=b"",
            stderr=str(exc).encode("utf-8", errors="replace"),
        )


def _terminate_owned_record(rec: ProcessRecord, grace_s: float) -> bool:
    """终止一个已通过 owned 记录绑定的进程（仅 Windows）。

    只 OpenProcess 一次（查询/终止/同步权限），随后路径、原生创建标识、
    存活查询、逐窗口 WM_CLOSE 门控、宽限等待与强制结束全部复用这一内核
    对象句柄，``finally`` 中恰好关闭一次；绝不按 PID 第二次打开目标。
    路径与原生标识须与 owned 记录精确相等且原进程仍存活，否则立即放弃，
    不发 WM_CLOSE、不 TerminateProcess，因此 PID 复用后的新实例永远不会
    被选中（T-008a #7）。返回 True 表示已确认退出或已强制结束。
    """
    if not IS_WINDOWS:
        return False
    kernel32 = _kernel32_process_api()
    if kernel32 is None:
        return False

    def _exit_code() -> int | None:
        """同一句柄查询退出码；查询失败返回 None（调用方失败关闭）。"""
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return int(exit_code.value)

    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
        False,
        rec.pid,
    )
    if not handle:
        # PID 无效说明原进程已退出；其他错误（如拒绝访问）失败关闭。
        return ctypes.get_last_error() == ERROR_INVALID_PARAMETER
    try:
        path_buffer = ctypes.create_unicode_buffer(32768)
        path_size = ctypes.c_uint32(len(path_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, path_buffer, ctypes.byref(path_size),
        ):
            return False
        if not _same_path(path_buffer.value, rec.full_path):
            return False
        native_create_time, _seconds = _query_process_times_from_handle(
            kernel32, handle,
        )
        if native_create_time == 0 or native_create_time != rec.native_create_time:
            return False

        exit_code_value = _exit_code()
        if exit_code_value is None:
            return False
        if exit_code_value != STILL_ACTIVE:
            # 原进程已退出：没有需要终止的对象。
            return True

        # 优雅关闭：只向逐个重新核验过的具体 HWND 发送，不做 PID 广播；
        # 任一门控失败或原进程已退出就停止发送，转入同句柄等待。
        user32 = _user32_window_api()
        if user32 is not None:
            try:
                windows = _enum_windows_for_pid(rec.pid)
            except OSError:
                windows = []
            for window in windows:
                hwnd = window["hwnd"]
                if not user32.IsWindowVisible(hwnd):
                    break
                window_pid = ctypes.c_uint32()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
                if int(window_pid.value) != rec.pid:
                    break
                exit_code_value = _exit_code()
                if exit_code_value is None or exit_code_value != STILL_ACTIVE:
                    break
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        # 宽限期：同一句柄分段等待；原进程退出即返回，不触碰复用 PID 的新实例。
        deadline = time.monotonic() + max(0.0, grace_s)
        while True:
            exit_code_value = _exit_code()
            if exit_code_value is None:
                return False
            if exit_code_value != STILL_ACTIVE:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            wait_ms = int(min(0.2, remaining) * 1000)
            if kernel32.WaitForSingleObject(handle, wait_ms) == WAIT_OBJECT_0:
                return True

        # 宽限到期：只对最初已核验的句柄强制结束，并用同一句柄等待退出。
        ctypes.set_last_error(0)
        terminated = bool(kernel32.TerminateProcess(handle, 1))
        if not terminated:
            # 可能恰好在宽限期边界自行退出；查询失败仍失败关闭。
            exit_code_value = _exit_code()
            return (
                exit_code_value is not None
                and exit_code_value != STILL_ACTIVE
            )
        return kernel32.WaitForSingleObject(handle, TERMINATE_WAIT_MS) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


__all__ = [
    "ProcessMonitor",
    "ProcessRecord",
    "IS_WINDOWS",
    "UNKNOWN_VERSION",
    "read_file_version",
]
