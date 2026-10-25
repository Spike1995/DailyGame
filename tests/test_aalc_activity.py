"""AALC 日志活动探针（aalc_activity）单元测试。

覆盖 T-003 §6「进展识别」「日志边界」两行：受支持来源与前缀/行号变体、
连续同名战斗、噪声不计数、mtime 单独变化不计数；基线前历史不计、
轮转无漏读/重复、新文件、截断、身份变化、UTF-8 跨块/半行/坏编码、
超长行与读取超预算。日志一律造在临时目录，不读取真实 D:\\mobile\\AALC。

另固化 T-009 复审 R1-R5 的反例：重解析点/非普通文件/目录拒绝、基线失败
不落地、seek/read 异常进诊断、跨分片预算不回放、半行与 64 KiB 规则在普通
和溢出两条路径上一致。
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

from daily_games.adapters.aalc_activity import (
    MAX_LINE_BUFFER_BYTES,
    ActivityLogPathError,
    MAX_POLL_READ_BYTES,
    ProbeResult,
    RealFilesystem,
    _AalcActivityProbe,
    match_progress_task,
)


def progress_line(
    source: str = r"..\..\tasks\mirror\mirror.py",
    lineno: int = 109,
    task: str = "镜牢寻路",
    ts: str = "2026-10-25 03:28:52,590",
) -> str:
    return f"[INFO] {ts} [AALC] {source}:{lineno}: 结束执行 {task}\n"


def debug_noise_line(source: str = r"..\..\tasks\mirror\mirror.py") -> str:
    return f"[DEBUG] 2026-10-25 03:28:52,590 [AALC] {source}:12: 模板匹配 0.98\n"


class MatchProgressTaskTests(unittest.TestCase):
    def test_supported_sources_with_prefix_and_lineno_variants(self):
        cases = [
            (r"..\..\tasks\mirror\mirror.py:109: 结束执行 镜牢寻路", "镜牢寻路"),
            ("../../tasks/base/script_task_scheme.py:7: 结束执行 日常收取", "日常收取"),
            (r"tasks\base\make_enkephalin_module.py:33: 结束执行 制币", "制币"),
        ]
        for source_part, expected in cases:
            with self.subTest(source=source_part):
                line = f"[INFO] 2026-10-25 03:28:52,590 [AALC] {source_part}"
                self.assertEqual(match_progress_task(line), expected)

    def test_noise_is_not_progress(self):
        noise_lines = [
            debug_noise_line(),  # DEBUG 扫描
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\base\\retry.py:50: 已卡死 30 秒，重试",  # 重试噪声
            "[ERROR] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:1: 找不到控件",  # 错误
            "[WARNING] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:2: 结束执行 战斗",  # 级别不对
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\ui\\main_window.py:9: 结束执行 战斗",  # 来源不符
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:3: 开始执行 战斗",  # 正文不符
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:4: 结束执行 ",  # 任务名空
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:5: 窗口位置变化",  # GUI 噪声
            "这不是格式化日志行",
            "",
        ]
        for line in noise_lines:
            with self.subTest(line=line[:40]):
                self.assertIsNone(match_progress_task(line))

    def test_consecutive_same_name_battles_both_count(self):
        line = progress_line(task="一次战斗")
        self.assertEqual(match_progress_task(line.strip()), "一次战斗")


class ProbeTestCase(unittest.TestCase):
    """公共脚手架：每个用例一个临时日志目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)
        self.main = self.log_dir / "debugLog.log"

    def make_probe(self, fs=None) -> _AalcActivityProbe:
        return _AalcActivityProbe(self.log_dir, fs=fs)

    def write(self, data: bytes) -> None:
        self.main.write_bytes(data)

    def append(self, data: bytes) -> None:
        with self.main.open("ab") as fh:
            fh.write(data)


class BaselineTests(ProbeTestCase):
    def test_poll_before_baseline_raises(self):
        probe = self.make_probe()
        with self.assertRaises(RuntimeError):
            probe.poll()

    def test_baseline_history_not_counted(self):
        self.write(
            progress_line(task="历史任务").encode("utf-8") * 2
            + debug_noise_line().encode("utf-8"),
        )
        shard = self.log_dir / "debugLog.log.1"
        shard.write_bytes(progress_line(task="分片历史").encode("utf-8"))
        probe = self.make_probe()
        probe.capture_baseline()
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.last_task_name)

    def test_missing_shards_are_not_errors(self):
        probe = self.make_probe()
        probe.capture_baseline()
        result = probe.poll()
        self.assertIsNone(result.read_error)
        self.assertEqual(result.progress_count, 0)


class ProgressRecognitionTests(ProbeTestCase):
    def test_counts_progress_and_reports_last_task(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(
            (progress_line(task="一次战斗")
             + progress_line(task="镜牢商店")).encode("utf-8"),
        )
        result = probe.poll()
        self.assertEqual(result.progress_count, 2)
        self.assertEqual(result.last_task_name, "镜牢商店")

    def test_consecutive_same_name_battles_count(self):
        probe = self.make_probe()
        probe.capture_baseline()
        line = progress_line(task="一次战斗")
        self.write((line + line).encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 2)
        self.assertEqual(result.last_task_name, "一次战斗")

    def test_noise_not_counted(self):
        probe = self.make_probe()
        probe.capture_baseline()
        noise = "".join([
            debug_noise_line(),
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\base\\retry.py:50: 已卡死 30 秒，重试\n",
            "[ERROR] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:1: 找不到控件\n",
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\ui\\main_window.py:9: 结束执行 战斗\n",
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:3: 开始执行 战斗\n",
            "[INFO] 2026-10-25 03:28:52,590 [AALC] ..\\..\\tasks\\mirror\\mirror.py:4: 结束执行 \n",
        ])
        self.write(noise.encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.last_task_name)
        self.assertEqual(result.dropped_lines, 0)

    def test_mtime_only_change_is_not_progress(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(progress_line(task="基线前").encode("utf-8"))
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)
        stat = self.main.stat()
        os.utime(self.main, (stat.st_atime, stat.st_mtime + 120))
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.read_error)


class LogBoundaryTests(ProbeTestCase):
    def test_rotation_without_loss_or_duplicates(self):
        probe = self.make_probe()
        self.write(progress_line(task="开局").encode("utf-8"))
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)

        observed: list[str] = []
        expected = ["战斗一", "战斗二", "战斗三"]
        main_shard = self.log_dir / "debugLog.log.1"
        old_shard = self.log_dir / "debugLog.log.2"
        for task in expected:
            self.append(progress_line(task=task).encode("utf-8"))
            result = probe.poll()
            self.assertEqual(result.progress_count, 1, msg=task)
            self.assertEqual(result.last_task_name, task)
            observed.append(result.last_task_name or "")
            # 轮转：旧主文件改名进分片链（.1→.2，main→.1），新主文件从空开始。
            if main_shard.exists():
                os.replace(main_shard, old_shard)
            os.replace(self.main, main_shard)
            self.main.write_bytes(b"")
        self.assertEqual(observed, expected)

    def test_new_file_after_baseline_is_read_from_zero(self):
        probe = self.make_probe()
        probe.capture_baseline()  # 此时主文件不存在
        self.write(progress_line(task="新文件").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "新文件")

    def test_truncation_resets_offset_and_counts_new_content(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.append(
            (progress_line(task="旧进展一")
             + progress_line(task="旧进展二")).encode("utf-8"),
        )
        self.assertEqual(probe.poll().progress_count, 2)
        # 同一身份就地截断重写为更短内容。
        self.write(progress_line(task="截断后").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "截断后")

    def test_identity_change_reads_from_zero(self):
        probe = self.make_probe()
        self.write(progress_line(task="旧文件").encode("utf-8"))
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)
        # 删除后重建：新 inode（身份变化），新内容应从 0 消费。
        self.main.unlink()
        self.write(progress_line(task="新身份").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "新身份")

    def test_utf8_multibyte_split_across_polls(self):
        probe = self.make_probe()
        probe.capture_baseline()
        line = progress_line(task="镜牢寻路").encode("utf-8")
        # 找一个多字节字符中间的切点（任务名首字符 3 字节，切在第 2 字节后）。
        head = line.index("牢".encode("utf-8")) + 1
        self.write(line[:head])
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(line[head:])
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "镜牢寻路")

    def test_half_line_waited_until_newline(self):
        probe = self.make_probe()
        probe.capture_baseline()
        full = progress_line(task="一次战斗").encode("utf-8")
        self.write(full[: int(len(full) * 0.7)])
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(full[int(len(full) * 0.7):])
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "一次战斗")

    def test_bad_encoding_line_dropped_without_crash(self):
        probe = self.make_probe()
        probe.capture_baseline()
        bad = (
            b"[INFO] 2026-10-25 03:28:52,590 [AALC] "
            b"tasks/mirror/mirror.py:1: \xff\xfe"
            + "坏编码".encode("utf-8")
            + b"\n"
        )
        self.write(bad + progress_line(task="后续").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "后续")
        self.assertGreaterEqual(result.dropped_lines, 1)

    def test_overlong_complete_line_dropped(self):
        probe = self.make_probe()
        probe.capture_baseline()
        overlong = (
            b"[INFO] 2026-10-25 03:28:52,590 [AALC] tasks/mirror/mirror.py:1: "
            + b"x" * (MAX_LINE_BUFFER_BYTES + 1)
            + b"\n"
        )
        self.write(overlong + progress_line(task="后续").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertGreaterEqual(result.dropped_lines, 1)

    def test_incomplete_line_over_buffer_limit_dropped(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(
            "[INFO] 没有换行的超长行 ".encode("utf-8")
            + b"y" * (MAX_LINE_BUFFER_BYTES + 1)
        )
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertGreaterEqual(result.dropped_lines, 1)
        # 超长行的残余部分被跳过；后续正常行照常计数。
        self.append(b"\n" + progress_line(task="恢复").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "恢复")

    def test_read_over_budget_only_tail_complete_lines(self):
        probe = self.make_probe()
        probe.capture_baseline()
        noise = (
            debug_noise_line().encode("utf-8")
            * (MAX_POLL_READ_BYTES // 40 + 5000)
        )
        payload = (
            progress_line(task="头部会被丢弃").encode("utf-8")
            + noise
            + progress_line(task="尾部保留").encode("utf-8")
        )
        self.write(payload)
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "尾部保留")
        self.assertGreater(result.dropped_bytes, 0)
        # 不分多轮回放积压：下一轮没有新增，也不重复计数。
        again = probe.poll()
        self.assertEqual(again.progress_count, 0)


class _FakeStat:
    def __init__(
        self,
        dev: int,
        ino: int,
        size: int,
        *,
        mode: int = stat.S_IFREG | 0o644,
        file_attributes: int = 0,
    ) -> None:
        self.st_dev = dev
        self.st_ino = ino
        self.st_size = size
        self.st_mode = mode
        self.st_file_attributes = file_attributes


class _FakeReader(io.BytesIO):
    """可注入 seek/read 失败的读取替身，句柄关闭由 close() 自然记录。"""

    def __init__(self, data: bytes, fs: "FakeFilesystem") -> None:
        super().__init__(data)
        self._fs = fs

    def seek(self, *args, **kwargs):
        if self._fs.fail_seek is not None:
            raise self._fs.fail_seek
        return super().seek(*args, **kwargs)

    def read(self, *args, **kwargs):
        if self._fs.fail_read is not None:
            raise self._fs.fail_read
        return super().read(*args, **kwargs)


class FakeFilesystem:
    """内存文件系统替身：可注入读取错误、身份变化与重解析点属性。"""

    def __init__(self):
        self.files: dict[Path, tuple[int, int, bytes]] = {}
        self._next_ino = 1
        self.open_error: Exception | None = None
        self.open_identity_override: tuple[int, int] | None = None
        self.open_mode_override: int | None = None
        self.open_file_attributes_override: int | None = None
        self.fail_read: Exception | None = None
        self.fail_seek: Exception | None = None
        # path -> (st_mode, st_file_attributes)，用于模拟目录/重解析点/非普通文件
        self.stat_overrides: dict[Path, tuple[int, int]] = {}
        # path -> 抛出的异常，用于模拟基线/轮询时 stat 不可用
        self.stat_errors: dict[Path, Exception] = {}
        self.handles: list[_FakeReader] = []

    def create(self, path: Path, data: bytes = b"") -> None:
        self.files[path] = (1, self._next_ino, data)
        self._next_ino += 1

    def write(self, path: Path, data: bytes) -> None:
        dev, ino, _old = self.files[path]
        self.files[path] = (dev, ino, data)

    def append(self, path: Path, extra: bytes) -> None:
        dev, ino, current = self.files[path]
        self.files[path] = (dev, ino, current + extra)

    def stat(self, path: Path):
        error = self.stat_errors.get(path)
        if error is not None:
            raise error
        entry = self.files.get(path)
        if entry is None:
            return None
        dev, ino, data = entry
        mode, attributes = self.stat_overrides.get(path, (stat.S_IFREG | 0o644, 0))
        return _FakeStat(
            dev, ino, len(data), mode=mode, file_attributes=attributes,
        )

    def open_for_read(self, path: Path):
        if self.open_error is not None:
            raise self.open_error
        stat_result = self.stat(path)
        if stat_result is None:
            raise FileNotFoundError(str(path))
        if self.open_identity_override is not None:
            dev, ino = self.open_identity_override
            stat_result = _FakeStat(dev, ino, stat_result.st_size)
        if self.open_mode_override is not None:
            stat_result.st_mode = self.open_mode_override
        if self.open_file_attributes_override is not None:
            stat_result.st_file_attributes = self.open_file_attributes_override
        reader = _FakeReader(self.files[path][2], self)
        self.handles.append(reader)
        return reader, stat_result


class InjectedFilesystemTests(ProbeTestCase):
    def make_fake_probe(self) -> tuple[_AalcActivityProbe, FakeFilesystem, Path]:
        fs = FakeFilesystem()
        probe = _AalcActivityProbe(self.log_dir, fs=fs)
        return probe, fs, self.log_dir / "debugLog.log"

    def test_read_error_reported_and_recovered(self):
        probe, fs, main = self.make_fake_probe()
        fs.create(main, progress_line(task="基线").encode("utf-8"))
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)

        fs.append(main, progress_line(task="新进展").encode("utf-8"))
        fs.open_error = PermissionError(13, "拒绝访问")
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertEqual(result.read_error, "PermissionError")

        fs.open_error = None
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "新进展")
        self.assertIsNone(result.read_error)

    def test_identity_change_between_stat_and_open_skips_shard(self):
        probe, fs, main = self.make_fake_probe()
        fs.create(main, b"")
        probe.capture_baseline()
        fs.append(main, progress_line(task="竞态进展").encode("utf-8"))
        fs.open_identity_override = (1, 999_999)
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.read_error)
        # 身份恢复后按原偏移正常消费，无重复。
        fs.open_identity_override = None
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "竞态进展")


class PathSafetyTests(ProbeTestCase):
    """R1：重解析点、非普通文件与目录都不得提供进展。"""

    def test_reparse_point_shard_rejected(self):
        fs = FakeFilesystem()
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.create(self.main, progress_line(task="越界读取").encode("utf-8"))
        fs.stat_overrides[self.main] = (stat.S_IFLNK | 0o777, 0x400)
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.last_task_name)
        self.assertEqual(len(result.rejected_entries), 1)
        self.assertIn("reparse_point", result.rejected_entries[0])

    def test_non_regular_shard_rejected(self):
        fs = FakeFilesystem()
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.create(self.main, progress_line(task="管道").encode("utf-8"))
        fs.stat_overrides[self.main] = (stat.S_IFIFO | 0o644, 0)
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertEqual(len(result.rejected_entries), 1)
        self.assertIn("not_regular_file", result.rejected_entries[0])

    def test_directory_reparse_point_blocks_whole_poll(self):
        fs = FakeFilesystem()
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.create(self.log_dir, b"")
        fs.stat_overrides[self.log_dir] = (stat.S_IFLNK | 0o777, 0x400)
        fs.create(self.main, progress_line(task="越界目录").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertEqual(result.read_error, ActivityLogPathError.__name__)
        self.assertEqual(len(result.rejected_entries), 1)

    def test_directory_reparse_point_fails_baseline(self):
        fs = FakeFilesystem()
        fs.create(self.log_dir, b"")
        fs.stat_overrides[self.log_dir] = (stat.S_IFLNK | 0o777, 0x400)
        probe = self.make_probe(fs=fs)
        with self.assertRaises(ActivityLogPathError):
            probe.capture_baseline()
        with self.assertRaises(RuntimeError):
            probe.poll()

    def test_regular_file_at_log_dir_rejected(self):
        fs = FakeFilesystem()
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.create(self.log_dir, b"")
        fs.create(self.main, progress_line(task="目录被占用").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertEqual(result.read_error, ActivityLogPathError.__name__)
        self.assertIn("not_directory", result.rejected_entries[0])

    def test_replacement_race_into_reparse_point_rejected(self):
        fs = FakeFilesystem()
        fs.create(self.main, b"")
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.append(self.main, progress_line(task="竞态重解析").encode("utf-8"))
        fs.open_mode_override = stat.S_IFLNK | 0o777
        fs.open_file_attributes_override = 0x400
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIn("reparse_point", result.rejected_entries[0])
        # 竞态恢复后按原偏移消费一次，不重复。
        fs.open_mode_override = None
        fs.open_file_attributes_override = None
        again = probe.poll()
        self.assertEqual(again.progress_count, 1)
        self.assertEqual(again.last_task_name, "竞态重解析")

    def test_real_filesystem_directory_named_like_log_is_rejected(self):
        self.main.mkdir()
        probe = self.make_probe()
        with self.assertRaises(ActivityLogPathError) as raised:
            probe.capture_baseline()
        self.assertEqual(raised.exception.reason, "not_regular_file")
        with self.assertRaises(RuntimeError):
            probe.poll()


class BaselineFailureTests(ProbeTestCase):
    """R2：失败的基线不得标成成功，历史内容也不得靠恢复读取续期。"""

    def test_rejected_baseline_shard_stays_unready_after_type_restored(self):
        cases = [
            (stat.S_IFREG | 0o644, 0x400, "reparse_point"),
            (stat.S_IFLNK | 0o777, 0, "reparse_point"),
            (stat.S_IFIFO | 0o644, 0, "not_regular_file"),
            (stat.S_IFDIR | 0o755, 0, "not_regular_file"),
        ]
        for suffix in ("", ".1", ".10"):
            for mode, attributes, reason in cases:
                with self.subTest(suffix=suffix, mode=mode, attributes=attributes):
                    fs = FakeFilesystem()
                    path = self.log_dir / f"debugLog.log{suffix}"
                    fs.create(path, progress_line(task="基线前历史记录").encode("utf-8"))
                    original = fs.files[path]
                    fs.stat_overrides[path] = (mode, attributes)
                    probe = self.make_probe(fs=fs)
                    with self.assertRaises(ActivityLogPathError) as raised:
                        probe.capture_baseline()
                    self.assertEqual(raised.exception.reason, reason)
                    with self.assertRaises(RuntimeError):
                        probe.poll()
                    del fs.stat_overrides[path]
                    self.assertEqual(fs.files[path], original)  # 身份与字节均未变
                    with self.assertRaises(RuntimeError):
                        probe.poll()  # 属性恢复不能把历史当新增，仍须重新捕获

    def test_recapture_after_rejected_shard_counts_only_new_append(self):
        fs = FakeFilesystem()
        fs.create(self.main, progress_line(task="基线前历史记录").encode("utf-8"))
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        fs.stat_overrides[self.main] = (stat.S_IFREG | 0o644, 0x400)
        with self.assertRaises(ActivityLogPathError):
            probe.capture_baseline()  # 失败的重新捕获也必须清除旧就绪状态
        del fs.stat_overrides[self.main]
        with self.assertRaises(RuntimeError):
            probe.poll()
        probe.capture_baseline()
        historical = probe.poll()
        self.assertEqual(historical.progress_count, 0)
        self.assertIsNone(historical.last_task_name)
        fs.append(self.main, progress_line(task="重新捕获后新记录").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "重新捕获后新记录")
        self.assertEqual(probe.poll().progress_count, 0)

    def test_baseline_identity_change_keeps_probe_unready(self):
        fs = FakeFilesystem()
        fs.create(self.main, progress_line(task="基线前历史记录").encode("utf-8"))
        fs.open_identity_override = (1, 999_999)
        probe = self.make_probe(fs=fs)
        with self.assertRaises(ActivityLogPathError) as raised:
            probe.capture_baseline()
        self.assertEqual(raised.exception.reason, "identity_changed")
        fs.open_identity_override = None
        with self.assertRaises(RuntimeError):
            probe.poll()
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)

    def test_failed_baseline_keeps_probe_unready(self):
        fs = FakeFilesystem()
        fs.create(self.main, progress_line(task="历史记录").encode("utf-8"))
        probe = self.make_probe(fs=fs)
        fs.stat_errors[self.main] = PermissionError(13, "拒绝访问")
        with self.assertRaises(OSError):
            probe.capture_baseline()
        with self.assertRaises(RuntimeError):
            probe.poll()
        # 权限恢复后重新建立基线：历史内容仍然不计入本轮。
        del fs.stat_errors[self.main]
        probe.capture_baseline()
        self.assertEqual(probe.poll().progress_count, 0)

    def test_baseline_open_failure_keeps_probe_unready(self):
        fs = FakeFilesystem()
        fs.create(self.main, progress_line(task="历史记录").encode("utf-8"))
        probe = self.make_probe(fs=fs)
        fs.open_error = PermissionError(13, "拒绝访问")
        with self.assertRaises(OSError):
            probe.capture_baseline()
        with self.assertRaises(RuntimeError):
            probe.poll()

    def test_recapture_baseline_clears_previous_buffer(self):
        probe = self.make_probe()
        probe.capture_baseline()
        line = progress_line(task="旧半行").encode("utf-8")
        self.write(line[: len(line) // 2])
        self.assertEqual(probe.poll().progress_count, 0)
        probe.capture_baseline()
        self.append(line[len(line) // 2:])  # 补全基线前那条半行
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(progress_line(task="基线后新记录").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "基线后新记录")


class ReadFailureTests(ProbeTestCase):
    """R3：seek/read 的 I/O 异常进入 ProbeResult，偏移与缓冲不前移。"""

    def _fake_probe(self):
        fs = FakeFilesystem()
        fs.create(self.main, b"")
        probe = self.make_probe(fs=fs)
        probe.capture_baseline()
        return probe, fs

    def test_read_error_reported_and_retried_once(self):
        probe, fs = self._fake_probe()
        fs.append(self.main, progress_line(task="读失败一次").encode("utf-8"))
        fs.fail_read = OSError("injected read failure")
        failed = probe.poll()
        self.assertEqual(failed.read_error, "OSError")
        self.assertEqual(failed.progress_count, 0)
        fs.fail_read = None
        recovered = probe.poll()
        self.assertIsNone(recovered.read_error)
        self.assertEqual(recovered.progress_count, 1)
        self.assertEqual(recovered.last_task_name, "读失败一次")
        self.assertEqual(probe.poll().progress_count, 0)

    def test_seek_error_reported_and_retried_once(self):
        probe, fs = self._fake_probe()
        fs.append(self.main, progress_line(task="寻址失败一次").encode("utf-8"))
        fs.fail_seek = OSError("injected seek failure")
        failed = probe.poll()
        self.assertEqual(failed.read_error, "OSError")
        self.assertEqual(failed.progress_count, 0)
        fs.fail_seek = None
        recovered = probe.poll()
        self.assertEqual(recovered.progress_count, 1)
        self.assertEqual(recovered.last_task_name, "寻址失败一次")

    def test_overflow_read_error_does_not_advance_offset(self):
        probe, fs = self._fake_probe()
        fs.write(
            self.main,
            b"x" * (MAX_POLL_READ_BYTES + 100) + b"\n"
            + progress_line(task="尾部进展").encode("utf-8"),
        )
        fs.fail_read = OSError("injected overflow read failure")
        failed = probe.poll()
        self.assertEqual(failed.read_error, "OSError")
        self.assertEqual(failed.progress_count, 0)
        fs.fail_read = None
        recovered = probe.poll()
        self.assertEqual(recovered.progress_count, 1)
        self.assertEqual(recovered.last_task_name, "尾部进展")
        self.assertEqual(probe.poll().progress_count, 0)

    def test_handle_closed_after_read_failure(self):
        probe, fs = self._fake_probe()
        fs.append(self.main, progress_line(task="句柄回收").encode("utf-8"))
        fs.fail_read = OSError("injected read failure")
        probe.poll()
        self.assertTrue(fs.handles)
        self.assertTrue(all(handle.closed for handle in fs.handles))


class BudgetTests(ProbeTestCase):
    """R4：单次全局预算按最新向最旧分配，积压不得在下一次 poll 回放。"""

    def test_newer_shard_not_starved_by_older_backlog(self):
        probe = self.make_probe()
        probe.capture_baseline()
        shard = self.log_dir / "debugLog.log.1"
        shard.write_bytes(
            b"x" * (MAX_POLL_READ_BYTES + 100) + b"\n"
            + progress_line(task="旧片尾部").encode("utf-8"),
        )
        self.write(progress_line(task="新主文件").encode("utf-8"))
        first = probe.poll()
        self.assertEqual(first.progress_count, 2)
        self.assertEqual(first.last_task_name, "新主文件")
        self.assertGreater(first.dropped_bytes, 0)
        self.assertEqual(probe.poll().progress_count, 0)

    def test_multishard_total_over_budget_is_not_replayed(self):
        probe = self.make_probe()
        probe.capture_baseline()
        noise = 600 * 1024
        (self.log_dir / "debugLog.log.1").write_bytes(
            b"y" * noise + b"\n" + progress_line(task="旧片").encode("utf-8"),
        )
        self.write(b"z" * noise + b"\n" + progress_line(task="新主").encode("utf-8"))
        first = probe.poll()
        self.assertEqual(first.progress_count, 2)
        self.assertEqual(first.last_task_name, "新主")
        self.assertGreater(first.dropped_bytes, 0)
        self.assertEqual(probe.poll().progress_count, 0)

    def test_shard_without_budget_is_dropped_once(self):
        probe = self.make_probe()
        probe.capture_baseline()
        (self.log_dir / "debugLog.log.1").write_bytes(
            progress_line(task="整段丢弃的旧片").encode("utf-8"),
        )
        self.write(
            b"q" * MAX_POLL_READ_BYTES + b"\n"
            + progress_line(task="新主尾部").encode("utf-8"),
        )
        first = probe.poll()
        self.assertEqual(first.progress_count, 1)
        self.assertEqual(first.last_task_name, "新主尾部")
        self.assertGreater(first.dropped_bytes, 0)
        self.assertEqual(probe.poll().progress_count, 0)


class LineBoundaryTests(ProbeTestCase):
    """R5：基线/丢弃半行与 64 KiB 规则在普通和溢出路径上一致。"""

    def test_baseline_in_middle_of_line_does_not_fabricate_record(self):
        self.main.write_bytes(b"old unterminated prefix ")
        probe = self.make_probe()
        probe.capture_baseline()
        self.append(progress_line(task="不是独立物理行").encode("utf-8"))
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(progress_line(task="恢复").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "恢复")

    def test_overflow_trailing_partial_skipped_until_newline(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(b"x" * (MAX_POLL_READ_BYTES + 100) + b"\npartial prefix ")
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(progress_line(task="续写伪装").encode("utf-8"))
        self.assertEqual(probe.poll().progress_count, 0)
        self.append(progress_line(task="后续正常").encode("utf-8"))
        result = probe.poll()
        self.assertEqual(result.progress_count, 1)
        self.assertEqual(result.last_task_name, "后续正常")

    def test_overflow_path_rejects_overlong_progress_line(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(
            b"x" * MAX_POLL_READ_BYTES + b"\n"
            + progress_line(task="q" * (MAX_LINE_BUFFER_BYTES + 10)).encode("utf-8"),
        )
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.last_task_name)
        self.assertGreater(result.dropped_lines, 0)

    def test_normal_path_rejects_overlong_progress_line(self):
        probe = self.make_probe()
        probe.capture_baseline()
        self.write(
            progress_line(task="q" * (MAX_LINE_BUFFER_BYTES + 10)).encode("utf-8"),
        )
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertIsNone(result.last_task_name)
        self.assertGreater(result.dropped_lines, 0)

    def test_overflow_path_rejects_bad_encoding_line(self):
        probe = self.make_probe()
        probe.capture_baseline()
        bad = (
            b"[INFO] 2026-10-25 03:28:52,590 [AALC] tasks/mirror/mirror.py:1: "
            b"\xe7\xbb\x93\xe6\x9d\x9f\xe6\x89\xa7\xe8\xa1\x8c \xff\xfe\n"
        )
        self.write(b"x" * (MAX_POLL_READ_BYTES + 100) + b"\n" + bad)
        result = probe.poll()
        self.assertEqual(result.progress_count, 0)
        self.assertGreater(result.dropped_lines, 0)


class RealFilesystemStatTests(unittest.TestCase):
    def test_stat_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            fs = RealFilesystem()
            self.assertIsNone(fs.stat(Path(td) / "debugLog.log"))


if __name__ == "__main__":
    unittest.main()
