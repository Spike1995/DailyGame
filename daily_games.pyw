"""一键日常桌面程序的最小应用入口。

导入本模块不会执行 GUI、进程或配置操作。GUI 路径由 Windows 命名互斥锁保护；
只读的 ``--dry-run`` 路径保持独立，可在已有实例运行时继续诊断。
``--repair-paths`` 供一键启动器在注册管理员排程前执行受限的软件位址自查。
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "user.json"
_MUTEX_NAME = r"Local\DailyGames.OneClickDaily.Gui.v1"
_ERROR_ALREADY_EXISTS = 183
_MODE_GUI = "gui"
_MODE_DRY_RUN = "dry_run"
_MODE_REPAIR_PATHS = "repair_paths"


class WindowsGuiInstanceLock:
    """使用 Windows 原子命名互斥锁实现的进程级 GUI 锁。

    ``CreateMutexW`` 可跨进程原子执行。互斥锁句柄在 GUI 生命周期内保持打开；
    如果进程异常退出，Windows 内核会自动释放它。本类不获取互斥锁所有权；
    进程结束时关闭唯一的本地句柄即可释放命名对象。
    """

    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self._name = name
        self._handle: object | None = None
        self._kernel32: Any | None = None

    def acquire(self) -> bool:
        """另一个 GUI 实例已持有互斥锁时返回 False。"""
        if self._handle is not None:
            return True
        if os.name != "nt":
            raise RuntimeError("当前平台不支持 Windows GUI 单实例锁")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool

        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateMutexW 调用失败")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False

        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        """关闭本进程的互斥锁句柄；可以重复调用。"""
        handle = self._handle
        self._handle = None
        kernel32 = self._kernel32
        self._kernel32 = None
        if handle is not None and kernel32 is not None:
            kernel32.CloseHandle(handle)


def _default_orchestrator_factory(config_path: Path):
    from daily_games.orchestrator import DailyOrchestrator

    return DailyOrchestrator(config_path)


def _default_gui_runner(config_path: Path) -> None:
    from daily_games.ui import run_gui

    # 一键入口在配置检查全部通过后直接执行完整队列，不再要求用户点击按钮。
    run_gui(config_path=config_path, auto_start=True)


def _default_path_repairer(config_path: Path):
    """執行一鍵啟動前的軟體位址自查，不啟動 GUI 或外部軟體。"""
    from daily_games.config_manager import ConfigManager

    project_root = config_path.resolve().parent.parent
    manager = ConfigManager(config_path, project_root / "backups")
    return manager.reconcile_software_paths(apply=True)


def _best_effort_utf8_stream(stream: TextIO | None) -> TextIO | None:
    """尽量让默认命令行流使用 UTF-8，同时保持 pythonw 的容错能力。"""
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 某些 IDE、重定向流或 pythonw 流不支持重新配置。
            pass
    return stream


def _write_line(message: str, stream: TextIO | None) -> None:
    """控制台流存在时写入诊断信息，且不得影响 GUI 启动。"""
    if stream is None:
        return
    try:
        print(message, file=stream)
    except Exception:
        pass


def _format_check_result(result: object) -> str:
    ok = bool(getattr(result, "ok", False))
    status = "通过" if ok else "失败"
    name = getattr(result, "name", "未知")
    severity = getattr(result, "severity", "未知")
    code = getattr(result, "code", "未知")
    message = getattr(result, "message", "")
    return f"[{status}] {name} ({severity}/{code}): {message}"


def _default_gui_error_reporter(message: str) -> None:
    """尽量显示由 pythonw 启动 GUI 时发生的错误。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "一键日常", 0x00000010)
    except Exception:
        # 消息框不可用时，stderr 仍会收到诊断信息。
        pass


def _parse_args(argv: Sequence[str], error_output: TextIO | None) -> str | None:
    """解析單一執行模式；參數無效時輸出簡短用法。"""
    arguments = list(argv)
    if not arguments:
        return _MODE_GUI
    if arguments == ["--dry-run"]:
        return _MODE_DRY_RUN
    if arguments == ["--repair-paths"]:
        return _MODE_REPAIR_PATHS
    _write_line(
        "用法：daily_games.pyw [--dry-run | --repair-paths]",
        error_output,
    )
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    orchestrator_factory: Callable[[Path], Any] | None = None,
    gui_runner: Callable[[Path], None] | None = None,
    path_repairer: Callable[[Path], Any] | None = None,
    lock_factory: Callable[[], Any] = WindowsGuiInstanceLock,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
    gui_error_reporter: Callable[[str], None] | None = None,
) -> int:
    """執行 GUI、唯讀診斷或啟動前軟體位址自查，並傳回退出碼。"""
    # 仅重新配置解释器的默认流。显式注入的流通常由测试或宿主所有，
    # 必须保留自身语义。
    output = output if output is not None else _best_effort_utf8_stream(sys.stdout)
    error_output = (
        error_output
        if error_output is not None
        else _best_effort_utf8_stream(sys.stderr)
    )
    mode = _parse_args(sys.argv[1:] if argv is None else argv, error_output)
    if mode is None:
        return 2

    if mode == _MODE_DRY_RUN:
        factory = orchestrator_factory or _default_orchestrator_factory
        try:
            results = list(factory(CONFIG_PATH).dry_run())
        except Exception as exc:  # 无需 GUI，也要让 dry-run 错误可以诊断。
            _write_line(f"dry-run 失败：{type(exc).__name__}：{exc}", error_output)
            return 2
        for result in results:
            _write_line(_format_check_result(result), output)
        return 0 if all(bool(getattr(result, "ok", False)) for result in results) else 1

    if mode == _MODE_REPAIR_PATHS:
        repairer = path_repairer or _default_path_repairer
        try:
            results = list(repairer(CONFIG_PATH))
        except Exception as exc:
            _write_line(
                f"軟體位址自查失敗：{type(exc).__name__}：{exc}",
                error_output,
            )
            return 2
        for result in results:
            _write_line(_format_check_result(result), output)
        return 0 if all(
            bool(getattr(result, "ok", False)) for result in results
        ) else 1

    reporter = gui_error_reporter or _default_gui_error_reporter
    try:
        lock = lock_factory()
        acquired = bool(lock.acquire())
    except Exception as exc:
        message = f"无法建立 GUI 单实例锁：{type(exc).__name__}：{exc}"
        _write_line(message, error_output)
        reporter(message)
        return 2

    if not acquired:
        message = "另一个“一键日常”实例正在运行。"
        _write_line(message, error_output)
        reporter(message)
        return 1

    exit_code = 0
    try:
        runner = gui_runner or _default_gui_runner
        runner(CONFIG_PATH)
    except Exception as exc:
        exit_code = 2
        message = f"GUI 启动失败：{type(exc).__name__}：{exc}"
        _write_line(message, error_output)
        reporter(message)
    finally:
        try:
            lock.release()
        except Exception as exc:
            if exit_code == 0:
                exit_code = 2
            message = f"GUI 单实例锁释放失败：{type(exc).__name__}：{exc}"
            _write_line(message, error_output)
            reporter(message)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
