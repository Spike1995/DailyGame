"""AALC 日志活动探针（T-003 §2 的独立读取部件，私有）。

只负责「本轮新增日志字节 → 进展事件」的解析，不维护任何计时；时钟、
空闲/硬截止判定与 activity_monitor_unavailable 语义属于后续集成卡
（T-009 只交付本部件与单测，不接线到 AalcAdapter）。

读取边界（严格按 T-003 §2）：

- 只枚举 log_dir 下主文件（默认名 debugLog.log，不含盘符，由调用方传入
  目录）与数字后缀 .1-.10 的分片，不递归、不观察锁文件。
- 路径安全：log_dir 与每个候选分片都必须是普通目录/普通文件，且不是
  Windows 重解析点（符号链接、联接点、云端占位）。真实文件系统用
  os.lstat 取身份、打开后再用 os.fstat 复核；不满足条件的条目不读，原因
  写入 ProbeResult.rejected_entries。
- capture_baseline() 记录现有分片的 (st_dev, st_ino) -> EOF；基线前已存在
  的内容永不计入本轮。基线可能失败：stat/打开/复核出错时清空状态、保持
  未就绪并抛出异常（不吞错），由调用方重试。基线捕获时若文件末尾不是
  换行，说明基线落在半行中间，该身份先跳过到下一个换行，避免后续字节
  伪装成完整记录。
- 按「身份 + 偏移」跟踪轮转：改名沿用偏移，新身份从 0 读，同身份截断重置
  偏移；每次打开后复核身份，读取完立即关闭句柄。
- 二进制偏移 + 按换行字节切割后再严格 UTF-8 解码（换行字节 0x0A 不会出现
  在多字节序列内部，按它切割不破坏编码边界）；未完成行缓冲上限 64 KiB，
  超长行/非法编码整行丢弃并计诊断，不作为活动。任何被丢弃的半行都会把该
  身份标记为「跳过到换行」，其续写不会被当成新记录。
- 单次 poll() 读取总预算 1 MiB：先固定本轮所有候选新增区，再从最新分片向
  最旧分片分配预算（保住尾部）。某分片本次新增超出其预算时只分析尾部完整
  行；完全没分到预算的区间整段丢弃、偏移推到 EOF 并标记跳过到换行，所以
  没有新写入的下一次 poll() 不会回放同一批积压。
- 进展判定：完整行匹配 [级别] 日期时间 [AALC] 来源:行号: 正文、级别为
  INFO、来源把反斜杠规范化为斜杠后以三个受支持任务脚本之一结尾、正文以
  「结束执行 」开头且任务名非空。同名任务重复出现照常计数。
"""

from __future__ import annotations

import os
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_MAIN_FILENAME = "debugLog.log"
MAX_LINE_BUFFER_BYTES = 64 * 1024
MAX_POLL_READ_BYTES = 1024 * 1024
MAX_SHARD_NUMBER = 10
PROGRESS_BODY_PREFIX = "结束执行 "
PROGRESS_LEVEL = "INFO"

#: Windows FILE_ATTRIBUTE_REPARSE_POINT：符号链接/联接点/云端占位文件。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

REJECT_REPARSE_POINT = "reparse_point"
REJECT_NOT_REGULAR = "not_regular_file"
REJECT_NOT_DIRECTORY = "not_directory"

#: 受支持的任务脚本规范化后缀（不绑定打包相对前缀层数或行号）。
PROGRESS_SOURCE_SUFFIXES = (
    "tasks/base/script_task_scheme.py",
    "tasks/mirror/mirror.py",
    "tasks/base/make_enkephalin_module.py",
)

_PROGRESS_LINE_RE = re.compile(
    r"^\[(\w+)\] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \[AALC\] "
    r"(.+?):(\d+): (.*)$"
)


class ActivityLogPathError(OSError):
    """候选日志路径不可安全读取（重解析点/非普通文件/身份不可核验）。"""

    def __init__(self, path: str | os.PathLike[str], reason: str) -> None:
        super().__init__(f"{path} 不能用于活动读取：{reason}")
        self.path = path
        self.reason = reason


def _unusable_reason(stat_result: Any, *, directory: bool = False) -> str | None:
    """返回拒绝原因；可安全读取时返回 None。

    测试替身可能不提供 st_mode/st_file_attributes，缺失时不做类型判断
    （真实 os.lstat/os.fstat 总是提供）。
    """
    if getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return REJECT_REPARSE_POINT
    mode = getattr(stat_result, "st_mode", None)
    if mode is None:
        return None
    if stat_module.S_ISLNK(mode):
        return REJECT_REPARSE_POINT
    if directory:
        return None if stat_module.S_ISDIR(mode) else REJECT_NOT_DIRECTORY
    return None if stat_module.S_ISREG(mode) else REJECT_NOT_REGULAR


def _decode_line(raw: bytes) -> str | None:
    """严格 UTF-8 解码完整行；坏编码返回 None 由调用方计丢弃。"""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_line(raw: bytes) -> tuple[str | None, bool]:
    """返回 (任务名, 是否可分析)；超长行与坏编码行不可分析（整行拒绝）。"""
    if len(raw) > MAX_LINE_BUFFER_BYTES:
        return None, False
    text = _decode_line(raw)
    if text is None:
        return None, False
    return match_progress_task(text), True


def match_progress_task(line: str) -> str | None:
    """返回该行的任务名（进展）；非进展行返回 None。"""
    match = _PROGRESS_LINE_RE.match(line)
    if match is None:
        return None
    level, _timestamp, source, _lineno, body = match.groups()
    if level != PROGRESS_LEVEL:
        return None
    normalized = source.replace("\\", "/")
    if not normalized.endswith(PROGRESS_SOURCE_SUFFIXES):
        return None
    if not body.startswith(PROGRESS_BODY_PREFIX):
        return None
    task_name = body[len(PROGRESS_BODY_PREFIX):].strip()
    return task_name or None


@dataclass(frozen=True)
class ProbeResult:
    """单次 poll() 的结果（只读）。

    read_error 为本轮第一个读取异常的类型名（如 PermissionError）；文件尚未
    生成不算错误。dropped_bytes 是本轮未分析的新增字节（超预算积压、超长行、
    坏编码行、被丢弃的半行），dropped_lines 只统计实际观察到的整行/半行；
    整段未读的积压只进 dropped_bytes。rejected_entries 是本轮因重解析点、
    非普通文件等原因被拒绝的路径诊断（形如 "debugLog.log.1: reparse_point"，
    每轮重复报告，永不提供进展）。
    """

    progress_count: int = 0
    last_task_name: str | None = None
    read_error: str | None = None
    dropped_bytes: int = 0
    dropped_lines: int = 0
    rejected_entries: tuple[str, ...] = ()


@dataclass
class _Candidate:
    """本轮固定下来的一个候选新增区（先快照，再统一分配预算）。"""

    path: Path
    identity: tuple[int, int]
    offset: int
    size: int
    window_start: int = 0
    window_bytes: int = 0

    @property
    def new_bytes(self) -> int:
        return self.size - self.offset


class RealFilesystem:
    """默认文件系统视图：真实磁盘。stat 用 lstat，不跟随链接/重解析点。"""

    def stat(self, path: Path):
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None

    def open_for_read(self, path: Path) -> tuple[BinaryIO, Any]:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            stat_result = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb"), stat_result


class _AalcActivityProbe:
    """按身份+偏移消费 AALC 滚动日志，解析本轮新增进展。"""

    def __init__(
        self,
        log_dir: str | os.PathLike[str],
        *,
        main_filename: str = DEFAULT_MAIN_FILENAME,
        fs: RealFilesystem | None = None,
    ) -> None:
        if not main_filename or main_filename != os.path.basename(main_filename):
            raise ValueError("main_filename 必须是纯文件名，不得含目录部分")
        self._log_dir = Path(log_dir)
        self._main_filename = main_filename
        self._fs = fs if fs is not None else RealFilesystem()
        self._baseline_captured = False
        # (st_dev, st_ino) -> 下一次读取偏移
        self._identity_offsets: dict[tuple[int, int], int] = {}
        # 未完成行缓冲与超长/被丢弃半行的跳过标记（按身份）
        self._line_buffers: dict[tuple[int, int], bytearray] = {}
        self._skip_until_newline: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ #
    # 公开接口                                                            #
    # ------------------------------------------------------------------ #

    def capture_baseline(self) -> None:
        """记录现有分片 (st_dev, st_ino) -> EOF；基线前内容不计入本轮。

        失败（路径类型/stat/打开/身份复核）时不吞错：清空全部状态、保持未就绪并抛出
        异常，由调用方重试。重新捕获会丢掉上一轮的偏移、行缓冲与跳过标记，
        因此旧内容不会被恢复读取续期。
        """
        self._clear_state()
        try:
            dir_stat = self._fs.stat(self._log_dir)
            if dir_stat is not None:
                reason = _unusable_reason(dir_stat, directory=True)
                if reason is not None:
                    raise ActivityLogPathError(self._log_dir, reason)
            offsets: dict[tuple[int, int], int] = {}
            skips: set[tuple[int, int]] = set()
            for path in self._shard_paths_oldest_first():
                stat_result = self._fs.stat(path)
                if stat_result is None:
                    continue
                reason = _unusable_reason(stat_result)
                if reason is not None:
                    raise ActivityLogPathError(path, reason)
                identity = (stat_result.st_dev, stat_result.st_ino)
                size = int(stat_result.st_size)
                offsets[identity] = size
                if size > 0 and not self._ends_with_newline(path, size, identity):
                    skips.add(identity)
        except OSError:
            # 身份/内容不能核验：不产生「已就绪」状态，避免历史被当成新增。
            self._clear_state()
            raise
        self._identity_offsets = offsets
        self._skip_until_newline = skips
        self._baseline_captured = True

    def poll(self) -> ProbeResult:
        """读取本轮新增字节，返回本次新增进展与读取诊断。

        本方法不做任何等待；调用方按采样节奏自行调度。只有「未建立基线」
        这类编程契约违规才抛异常，其余读取问题都落在 ProbeResult 上。
        """
        if not self._baseline_captured:
            raise RuntimeError("必须先调用 capture_baseline() 建立基线，再 poll()")

        rejected: list[str] = []
        try:
            dir_stat = self._fs.stat(self._log_dir)
        except OSError as exc:
            return ProbeResult(read_error=type(exc).__name__)
        if dir_stat is not None:
            dir_reason = _unusable_reason(dir_stat, directory=True)
            if dir_reason is not None:
                return ProbeResult(
                    read_error=ActivityLogPathError.__name__,
                    rejected_entries=(f"{self._log_dir}: {dir_reason}",),
                )
        # 目录尚不存在时继续枚举：各分片 stat 均为 None，自然得到空结果。

        # 阶段一：固定本轮所有候选新增区（只 stat，不读正文）。
        candidates: list[_Candidate] = []
        read_error: str | None = None
        for path in self._shard_paths_oldest_first():
            try:
                stat_result = self._fs.stat(path)
            except OSError as exc:
                read_error = read_error or type(exc).__name__
                continue
            if stat_result is None:
                # 尚未生成的日志允许等待；不算错误。
                continue
            reason = _unusable_reason(stat_result)
            if reason is not None:
                rejected.append(f"{path.name}: {reason}")
                continue
            identity = (stat_result.st_dev, stat_result.st_ino)
            size = int(stat_result.st_size)
            offset = self._identity_offsets.get(identity, 0)
            if size < offset:
                # 同身份截断：重置偏移，清空该身份的行缓冲与跳过标记。
                offset = 0
                self._reset_identity_state(identity)
            if size <= offset:
                self._identity_offsets.setdefault(identity, offset)
                continue
            candidates.append(
                _Candidate(path=path, identity=identity, offset=offset, size=size)
            )

        # 阶段二：从最新向最旧分配单次预算，所有区间都要有明确归属。
        _allocate_budget(candidates)

        # 阶段三：按时间顺序（旧→新）读取并解析。
        progress_tasks: list[str] = []
        dropped_bytes = 0
        dropped_lines = 0
        for candidate in candidates:
            if candidate.window_bytes == 0:
                dropped_bytes += candidate.new_bytes
                self._drop_whole_region(candidate.identity, candidate.size)
                continue
            try:
                handle, opened_stat = self._fs.open_for_read(candidate.path)
            except OSError as exc:
                read_error = read_error or type(exc).__name__
                continue
            reason = _unusable_reason(opened_stat)
            if reason is not None:
                handle.close()
                rejected.append(f"{candidate.path.name}: {reason}")
                continue
            if (opened_stat.st_dev, opened_stat.st_ino) != candidate.identity:
                handle.close()
                # 打开前后身份变化（轮转竞态）：本轮跳过，偏移不变。
                continue
            try:
                with handle:
                    handle.seek(candidate.window_start)
                    data = handle.read(candidate.window_bytes)
            except OSError as exc:
                # seek/read 失败：偏移与行缓冲不前移，下一轮重试。
                read_error = read_error or type(exc).__name__
                continue
            actual = len(data)
            if candidate.window_start == candidate.offset:
                self._identity_offsets[candidate.identity] = candidate.offset + actual
                tasks, drop_b, drop_l = self._consume(candidate.identity, data)
            else:
                dropped_head = (
                    candidate.window_start - candidate.offset
                    + candidate.window_bytes - actual
                )
                self._identity_offsets[candidate.identity] = (
                    candidate.window_start + actual
                )
                tasks, drop_b, drop_l = self._consume_window(
                    candidate.identity, data, dropped_head
                )
            progress_tasks.extend(tasks)
            dropped_bytes += drop_b
            dropped_lines += drop_l

        return ProbeResult(
            progress_count=len(progress_tasks),
            last_task_name=progress_tasks[-1] if progress_tasks else None,
            read_error=read_error,
            dropped_bytes=dropped_bytes,
            dropped_lines=dropped_lines,
            rejected_entries=tuple(rejected),
        )

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #

    def _shard_paths_oldest_first(self) -> list[Path]:
        """按时间从旧到新返回 .10 ... .1、主文件。"""
        paths = [
            self._log_dir / f"{self._main_filename}.{number}"
            for number in range(MAX_SHARD_NUMBER, 0, -1)
        ]
        paths.append(self._log_dir / self._main_filename)
        return paths

    def _clear_state(self) -> None:
        self._baseline_captured = False
        self._identity_offsets = {}
        self._line_buffers = {}
        self._skip_until_newline = set()

    def _reset_identity_state(self, identity: tuple[int, int]) -> None:
        self._line_buffers.pop(identity, None)
        self._skip_until_newline.discard(identity)

    def _ends_with_newline(
        self,
        path: Path,
        size: int,
        identity: tuple[int, int],
    ) -> bool:
        """基线处文件是否以换行结尾；读不到就按基线失败上报。"""
        handle, opened_stat = self._fs.open_for_read(path)
        with handle:
            reason = _unusable_reason(opened_stat)
            if reason is not None:
                raise ActivityLogPathError(path, reason)
            if (opened_stat.st_dev, opened_stat.st_ino) != identity:
                raise ActivityLogPathError(path, "identity_changed")
            handle.seek(size - 1)
            tail = handle.read(1)
        return tail == b"\n"

    def _drop_whole_region(self, identity: tuple[int, int], size: int) -> None:
        """整段没分到预算：偏移推到 EOF，并保守地跳过其可能的半行续写。"""
        self._identity_offsets[identity] = size
        self._line_buffers.pop(identity, None)
        self._skip_until_newline.add(identity)

    def _consume(
        self,
        identity: tuple[int, int],
        data: bytes,
    ) -> tuple[list[str], int, int]:
        """整段新增（起点是已确认的行边界）并入行缓冲后分析完整行。"""
        tasks: list[str] = []
        dropped_bytes = 0
        dropped_lines = 0
        buffer = self._line_buffers.setdefault(identity, bytearray())

        if identity in self._skip_until_newline:
            newline = data.find(b"\n")
            if newline < 0:
                dropped_bytes += len(data)
                return tasks, dropped_bytes, dropped_lines
            dropped_bytes += newline + 1
            self._skip_until_newline.discard(identity)
            data = data[newline + 1:]
            buffer.clear()

        buffer.extend(data)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(buffer[:newline])
            del buffer[:newline + 1]
            task, analyzable = _parse_line(raw)
            if not analyzable:
                # 超长行/非法编码：整行丢弃并计诊断，不作为活动。
                dropped_bytes += len(raw) + 1
                dropped_lines += 1
                continue
            if task is not None:
                tasks.append(task)

        if len(buffer) > MAX_LINE_BUFFER_BYTES:
            # 未完成行超限：丢弃已缓冲部分并跳过本行剩余部分。
            dropped_bytes += len(buffer)
            dropped_lines += 1
            buffer.clear()
            self._skip_until_newline.add(identity)
        return tasks, dropped_bytes, dropped_lines

    def _consume_window(
        self,
        identity: tuple[int, int],
        data: bytes,
        dropped_head: int,
    ) -> tuple[list[str], int, int]:
        """窗口读取：起点不是行边界，只分析窗口内可完整核验的物理行。"""
        tasks: list[str] = []
        dropped_bytes = dropped_head
        dropped_lines = 0
        buffered = self._line_buffers.pop(identity, None)
        if buffered:
            # 上一轮遗留的半行已随积压一起丢弃，不可能再补全。
            dropped_bytes += len(buffered)
            dropped_lines += 1
        self._skip_until_newline.discard(identity)
        if not data:
            return tasks, dropped_bytes, dropped_lines

        newline = data.find(b"\n")
        if newline < 0:
            # 窗口内没有完整行：整段丢弃，并跳过该行后续补全。
            dropped_bytes += len(data)
            dropped_lines += 1
            self._skip_until_newline.add(identity)
            return tasks, dropped_bytes, dropped_lines
        if newline:
            # 窗口起点落在半行中间：该物理行无法核验，整行丢弃。
            dropped_lines += 1
        dropped_bytes += newline + 1
        payload = data[newline + 1:]
        if not payload:
            return tasks, dropped_bytes, dropped_lines
        if not payload.endswith(b"\n"):
            cut = payload.rfind(b"\n") + 1
            dropped_bytes += len(payload) - cut
            dropped_lines += 1
            self._skip_until_newline.add(identity)
            payload = payload[:cut]
        for raw in payload.split(b"\n")[:-1]:
            task, analyzable = _parse_line(raw)
            if not analyzable:
                dropped_bytes += len(raw) + 1
                dropped_lines += 1
                continue
            if task is not None:
                tasks.append(task)
        return tasks, dropped_bytes, dropped_lines


def _allocate_budget(candidates: list[_Candidate]) -> None:
    """从最新分片向最旧分片分配单次读取预算；没分到的区间留给丢弃。"""
    total = sum(candidate.new_bytes for candidate in candidates)
    if total <= MAX_POLL_READ_BYTES:
        for candidate in candidates:
            candidate.window_start = candidate.offset
            candidate.window_bytes = candidate.new_bytes
        return
    remaining = MAX_POLL_READ_BYTES
    for candidate in reversed(candidates):
        take = min(candidate.new_bytes, remaining)
        candidate.window_bytes = take
        candidate.window_start = candidate.size - take
        remaining -= take


__all__ = [
    "ActivityLogPathError",
    "ProbeResult",
    "RealFilesystem",
    "_AalcActivityProbe",
    "match_progress_task",
]
