"""配置管理：安全、可测试、可恢复的第三方配置处理。

本模块只负责读取 user.json、规划与应用第三方一键执行配置，以及备份和恢复。
所有写入操作前一律先创建备份，写入后重新解析验证，并以稳定错误码报告原因。

设计原则：
- 不导入 PyYAML。YAML 只对唯一、有边界的标量键执行保留格式的替换。
- JSON 只更新指定键并保留未知键；先写临时文件、重新解析验证后再原子替换。
- 恢复前再次备份当前版本，核对清单与哈希，避免部分恢复。
- 尚未验证的配置显示给用户，但不得由 apply_required_changes() 应用。
- 不记录敏感配置内容到日志。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

# --------------------------------------------------------------------------- #
# 稳定错误码与严重等级                                                       #
# --------------------------------------------------------------------------- #

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
_VALID_SEVERITIES = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR)

# 通用错误码（稳定字符串，供 UI 与测试比较）
CODE_OK = "ok"
CODE_FILE_NOT_FOUND = "file_not_found"
CODE_PARSE_FAILED = "parse_failed"
CODE_KEY_MISSING = "key_missing"
CODE_KEY_DUPLICATE = "key_duplicate"
CODE_AMBIGUOUS = "ambiguous"
CODE_TYPE_MISMATCH = "type_mismatch"
CODE_ATOMIC_REPLACE_FAILED = "atomic_replace_failed"
CODE_HASH_MISMATCH = "hash_mismatch"
CODE_BACKUP_FAILED = "backup_failed"
CODE_CHANGE_UNKNOWN = "change_unknown"
CODE_CHANGE_NOT_VERIFIED = "change_not_verified"
CODE_CHANGE_DUPLICATE = "change_duplicate"
CODE_VERIFY_AFTER_WRITE_FAILED = "verify_after_write_failed"
CODE_YAML_MULTILINE_OR_CORRUPT = "yaml_multiline_or_corrupt"
CODE_NOT_IMPLEMENTED = "not_implemented"
CODE_INTERNAL = "internal_error"
CODE_SOFTWARE_PATH_CHANGE_REQUIRED = "software_path_change_required"
CODE_SOFTWARE_PATH_REPAIRED = "software_path_repaired"
CODE_SOFTWARE_PATH_NOT_FOUND = "software_path_not_found"
CODE_SOFTWARE_PATH_AMBIGUOUS = "software_path_ambiguous"
CODE_SOFTWARE_PATH_WRITE_FAILED = "software_path_write_failed"


# --------------------------------------------------------------------------- #
# 公开数据类型（冻结接口）                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    """单一检查或应用动作的结果。"""

    name: str
    ok: bool
    code: str
    message: str
    severity: str
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BackupInfo:
    """一次备份的信息。created_at 为带时区 ISO 8601 字符串。"""

    backup_id: str
    created_at: str
    manifest_path: Path
    source_count: int


@dataclass(frozen=True)
class ConfigChange:
    """一个待应用的配置变更。verified=False 者不可被 apply_required_changes 应用。"""

    change_id: str
    tool: str
    field: str
    current_display: str
    proposed_display: str
    verified: bool
    warning: str


@dataclass(frozen=True)
class _ValidatedBackupEntry:
    index: int
    original_path: Path
    original_name: str
    backup_path: Path
    backup_name: str
    sha256: str
    size: int


@dataclass
class _RestoreStage:
    entry: _ValidatedBackupEntry
    replacement_path: Path
    rollback_path: Path | None
    original_existed: bool
    original_hash: str | None
    committed: bool = False


@dataclass
class _ApplyStage:
    target: Path
    stage_directory: Path
    staged_path: Path
    rollback_path: Path
    baseline_hash: str
    desired_hash: str
    results: list[CheckResult]
    committed: bool = False


class ConfigManagerError(Exception):
    """具有明确错误码和详情的自定义异常基类。"""

    def __init__(self, code: str, message: str, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# 内部辅助函数                                                                  #
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    """带时区的 ISO 8601 时间字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_meta(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "absolute_path": os.path.abspath(os.fspath(path)),
        "sha256": _sha256(path),
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def _coerce_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def _atomic_write_text(target: Path, text: str, encoding: str = "utf-8") -> None:
    """在同目录写临时文件再 os.replace 原子替换。"""
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=target.suffix, dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp_path, target)
    except Exception:
        # 失败时清理临时文件
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _mask_sensitive(value: object) -> str:
    """显示用：对疑似敏感值脱敏。非字符串、短字符串照原样显示。"""
    if not isinstance(value, str):
        return repr(value)
    lowered = value.lower()
    sensitive_keywords = ("token", "secret", "password", "passwd", "cdk", "key", "auth")
    if any(k in lowered for k in sensitive_keywords) and len(value) > 0:
        if len(value) <= 4:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return value


_BACKUP_ID_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)
_BACKUP_DIRECTORY_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6})_(?P<backup_id>[0-9a-f]{8})$",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


def _is_plain_file(path: Path) -> bool:
    """仅接受普通文件，不跟随符号链接或 Windows 重解析点。"""
    try:
        file_stat = os.lstat(path)
    except OSError:
        return False
    return bool(
        stat.S_ISREG(file_stat.st_mode)
        and not stat.S_ISLNK(file_stat.st_mode)
        and not (
            getattr(file_stat, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _is_plain_directory(path: Path) -> bool:
    """仅接受普通目录，不跟随符号链接或 Windows 重解析点。"""
    try:
        directory_stat = os.lstat(path)
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(directory_stat.st_mode)
        and not stat.S_ISLNK(directory_stat.st_mode)
        and not (
            getattr(directory_stat, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _backup_manifest_error(message: str, **details: object) -> ConfigManagerError:
    return ConfigManagerError(CODE_AMBIGUOUS, message, details)


def _validate_backup_manifest(
    manifest_path: Path,
    manifest: object,
    *,
    expected_backup_id: str | None = None,
    verify_payloads: bool,
) -> tuple[BackupInfo, tuple[_ValidatedBackupEntry, ...]]:
    """验证备份清单结构；可选核对每个备份文件的大小与哈希。"""
    if not _is_plain_directory(manifest_path.parent) or not _is_plain_file(
        manifest_path
    ):
        raise _backup_manifest_error("备份目录或清单不是普通文件")
    directory_match = _BACKUP_DIRECTORY_RE.fullmatch(manifest_path.parent.name)
    if directory_match is None:
        raise _backup_manifest_error("备份目录名称格式无效")
    try:
        datetime.strptime(
            directory_match.group("timestamp"),
            "%Y%m%dT%H%M%S",
        )
    except ValueError as exc:
        raise _backup_manifest_error("备份目录时间戳无效") from exc
    if not isinstance(manifest, Mapping):
        raise _backup_manifest_error("备份清单根节点必须是对象")

    backup_id = manifest.get("backup_id")
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
        raise _backup_manifest_error("备份 ID 格式无效")
    normalized_id = backup_id.casefold()
    if directory_match.group("backup_id").casefold() != normalized_id:
        raise _backup_manifest_error("备份目录与清单 ID 不一致")
    if (
        expected_backup_id is not None
        and normalized_id != expected_backup_id.casefold()
    ):
        raise _backup_manifest_error("请求的备份 ID 与清单不一致")

    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise _backup_manifest_error("备份建立时间无效")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise _backup_manifest_error("备份建立时间格式无效") from exc

    source_count = manifest.get("source_count")
    entries_raw = manifest.get("entries")
    if (
        type(source_count) is not int
        or source_count <= 0
        or not isinstance(entries_raw, list)
        or len(entries_raw) != source_count
    ):
        raise _backup_manifest_error("备份文件数量与清单条目不一致")

    validated: list[_ValidatedBackupEntry] = []
    seen_originals: set[str] = set()
    seen_backup_names: set[str] = set()
    for position, raw_entry in enumerate(entries_raw):
        if not isinstance(raw_entry, Mapping):
            raise _backup_manifest_error("备份条目必须是对象", index=position)
        index = raw_entry.get("index")
        original_text = raw_entry.get("original_absolute_path")
        absolute_text = raw_entry.get("absolute_path")
        original_name = raw_entry.get("original_name")
        backup_name = raw_entry.get("backup_name")
        digest = raw_entry.get("sha256")
        size = raw_entry.get("size")
        if type(index) is not int or index != position:
            raise _backup_manifest_error("备份条目索引不连续", index=position)
        if not isinstance(original_text, str) or not original_text.strip():
            raise _backup_manifest_error("备份原始路径无效", index=position)
        original_path = Path(original_text)
        if not original_path.is_absolute():
            raise _backup_manifest_error("备份原始路径必须是绝对路径", index=position)
        if (
            not isinstance(absolute_text, str)
            or _normalized_path(absolute_text) != _normalized_path(original_path)
        ):
            raise _backup_manifest_error("备份绝对路径字段不一致", index=position)
        if (
            not isinstance(original_name, str)
            or not original_name
            or original_name != original_path.name
        ):
            raise _backup_manifest_error("备份原始文件名无效", index=position)
        expected_backup_name = f"{position:03d}_{original_name}"
        if backup_name != expected_backup_name:
            raise _backup_manifest_error("备份副本文件名无效", index=position)
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise _backup_manifest_error("备份 SHA-256 格式无效", index=position)
        if type(size) is not int or size < 0:
            raise _backup_manifest_error("备份文件大小无效", index=position)

        normalized_original = _normalized_path(original_path)
        if (
            normalized_original in seen_originals
            or backup_name in seen_backup_names
        ):
            raise _backup_manifest_error("备份清单含重复路径或副本", index=position)
        seen_originals.add(normalized_original)
        seen_backup_names.add(backup_name)
        backup_path = manifest_path.parent / backup_name
        entry = _ValidatedBackupEntry(
            index=position,
            original_path=original_path,
            original_name=original_name,
            backup_path=backup_path,
            backup_name=backup_name,
            sha256=digest.casefold(),
            size=size,
        )
        if verify_payloads:
            if not _is_plain_file(backup_path):
                raise ConfigManagerError(
                    CODE_FILE_NOT_FOUND,
                    f"备份文件缺失或不是普通文件：{backup_path}",
                    {"backup_name": backup_name},
                )
            payload_size = os.lstat(backup_path).st_size
            if payload_size != size:
                raise ConfigManagerError(
                    CODE_HASH_MISMATCH,
                    f"备份文件大小不符：{backup_path}",
                    {"expected_size": size, "actual_size": payload_size},
                )
            actual_hash = _sha256(backup_path)
            if actual_hash.casefold() != entry.sha256:
                raise ConfigManagerError(
                    CODE_HASH_MISMATCH,
                    f"备份文件哈希不符：{backup_path}",
                    {"expected": entry.sha256, "actual": actual_hash},
                )
        validated.append(entry)

    return (
        BackupInfo(
            backup_id=normalized_id,
            created_at=created_at,
            manifest_path=manifest_path,
            source_count=source_count,
        ),
        tuple(validated),
    )


# --------------------------------------------------------------------------- #
# ConfigManager                                                                #
# --------------------------------------------------------------------------- #


class ConfigManager:
    """读取配置、规划变更、安全应用、备份与恢复。

    所有方法可接受 ``str`` 或 ``pathlib.Path``。
    """

    # 一键执行所需的变更定义表。
    # 每一项描述：tool / field / 找到当前值的方式 / 目标值 / 是否已验证可安全应用 / 警告文本。
    # verified=False 的项目会出现在 plan_required_changes() 供用户查看，
    # 但 apply_required_changes() 会拒绝应用。
    _REQUIRED_CHANGE_SPECS: tuple[dict[str, object], ...] = (
        {
            "change_id": "maa.autorun",
            "tool": "MAA",
            "field": "gui.new.json:Configurations.<current>.Gui.StartUpSettings.RunDirectly",
            "file_key": "maa_gui",
            "json_pointer": (
                "Configurations", "__current__", "Gui", "StartUpSettings", "RunDirectly",
            ),
            "proposed": True,
            "verified": True,
            "warning": "",
        },
        {
            "change_id": "maa.post_actions",
            "tool": "MAA",
            "field": "gui.new.json:Configurations.<current>.Gui.PostActions",
            "file_key": "maa_gui",
            "json_pointer": ("Configurations", "__current__", "Gui", "PostActions"),
            "proposed": "ExitArknights, ExitSelf",
            "verified": True,
            "warning": "",
        },
        {
            "change_id": "aalc.autostart",
            "tool": "AALC",
            "field": "config.yaml:autostart",
            "file_key": "aalc_yaml",
            "yaml_key": "autostart",
            "proposed": "false",
            "verified": True,
            "warning": "关闭旧的配置自启动，避免与官方 start 一次性命令重复触发。",
        },
        {
            "change_id": "aalc.game_path",
            "tool": "AALC",
            "field": "config.yaml:game_path",
            "file_key": "aalc_yaml",
            "yaml_key": "game_path",
            "proposed_config_path": (
                "games", "limbus", "helper", "game_path_correct",
            ),
            "verified": True,
            "yaml_multiline_repair": True,
            "warning": "将先备份，再把唯一的跨行 plain scalar 合并为单行路径。",
        },
        {
            "change_id": "aalc.check_update",
            "tool": "AALC",
            "field": "config.yaml:check_update",
            "file_key": "aalc_yaml",
            "yaml_key": "check_update",
            "proposed": "false",
            "verified": True,
            "warning": "无人值守执行时禁用启动阶段的软件更新提示；更新需在空闲时手动进行。",
        },
        {
            "change_id": "aalc.image_resource_sync",
            "tool": "AALC",
            "field": "config.yaml:image_resource_sync",
            "file_key": "aalc_yaml",
            "yaml_key": "image_resource_sync",
            "proposed": "false",
            "verified": True,
            "warning": "无人值守执行时禁用会阻塞任务启动的图片资源同步提示。",
        },
    )

    # 软件地址自查只处理可由文件名与伴随文件可靠辨识的程序。鹰角启动器
    # 的按钮自动化另有版本与签名冻结条件，不能只凭路径候选自动改址。
    _SOFTWARE_PATH_SPECS: tuple[dict[str, object], ...] = (
        {
            "software_id": "arknights_game",
            "display_name": "明日方舟",
            "filename": "Arknights.exe",
            "kind": "unity_game",
            "executable_path": ("games", "arknights", "game_executable"),
        },
        {
            "software_id": "maa",
            "display_name": "MAA",
            "filename": "MAA.exe",
            "kind": "maa",
            "executable_path": (
                "games", "arknights", "helper", "executable",
            ),
        },
        {
            "software_id": "endfield_game",
            "display_name": "明日方舟：终末地",
            "filename": "Endfield.exe",
            "kind": "unity_game",
            "executable_path": ("games", "endfield", "game_executable"),
        },
        {
            "software_id": "maaend",
            "display_name": "MaaEnd",
            "filename": "MaaEnd.exe",
            "kind": "maaend",
            "executable_path": (
                "games", "endfield", "helper", "executable",
            ),
        },
        {
            "software_id": "limbus_game",
            "display_name": "边狱巴士",
            "filename": "LimbusCompany.exe",
            "kind": "unity_game",
            "executable_path": ("games", "limbus", "game_executable"),
        },
        {
            "software_id": "aalc",
            "display_name": "AALC",
            "filename": "AALC.exe",
            "kind": "aalc",
            "executable_path": ("games", "limbus", "helper", "executable"),
        },
    )

    def __init__(self, config_path: str | os.PathLike[str], backup_root: str | os.PathLike[str]):
        self._config_path = _coerce_path(config_path)
        self._backup_root = _coerce_path(backup_root)
        self._config_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # 配置加载                                                            #
    # ------------------------------------------------------------------ #

    def load(self) -> dict:
        """加载并返回 user.json。缺失或解析失败时抛出 ConfigManagerError。"""
        if self._config_cache is not None:
            return self._config_cache
        if not self._config_path.is_file():
            raise ConfigManagerError(
                CODE_FILE_NOT_FOUND,
                f"配置文件不存在：{self._config_path}",
                {"path": str(self._config_path)},
            )
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            # JSONDecodeError 在不同 Python 版本可能没有 offset；用 getattr 安全获取
            col = getattr(exc, "offset", None)
            if col is None:
                col = getattr(exc, "colno", None)
            raise ConfigManagerError(
                CODE_PARSE_FAILED,
                f"配置文件 JSON 解析失败：{exc}",
                {"path": str(self._config_path), "line": exc.lineno, "col": col},
            ) from exc
        if not isinstance(data, dict):
            raise ConfigManagerError(
                CODE_TYPE_MISMATCH,
                "配置文件根层级必须是对象",
                {"path": str(self._config_path), "actual_type": type(data).__name__},
            )
        self._config_cache = data
        return data

    # ------------------------------------------------------------------ #
    # 软件地址自查                                                        #
    # ------------------------------------------------------------------ #

    def reconcile_software_paths(self, *, apply: bool = False) -> list[CheckResult]:
        """检查并在安全条件成立时修复软件程序地址。

        自查由 ``software_path_self_check.enabled`` 明确开启。现有程序与
        关联字段一致时不扫描、不写入。需要修复时，只在配置的搜索根目录内
        做有限深度搜索，并以软件伴随文件辨识候选。只有完整扫描得到唯一候选
        时才允许自动应用；零候选、多候选或扫描不完整一律保留原配置。

        ``apply=False`` 永远只读，用于 ``preflight`` 与 ``--dry-run``。
        ``apply=True`` 仍受 ``auto_repair_unique_match`` 控制。应用前会备份
        ``user.json``，之后以原子替换写入并重新解析核对全部关联字段。
        """
        cfg = self.load()
        raw_options = cfg.get("software_path_self_check", {})
        if not isinstance(raw_options, Mapping) or not bool(
            raw_options.get("enabled", False)
        ):
            return []

        max_depth = _bounded_int(
            raw_options.get("max_depth", 4), default=4, minimum=1, maximum=6,
        )
        max_directories = _bounded_int(
            raw_options.get("max_directories", 5000),
            default=5000,
            minimum=100,
            maximum=50000,
        )
        auto_repair = bool(raw_options.get("auto_repair_unique_match", False))
        roots, root_issues = self._software_search_roots(raw_options)
        raw_software_ids = raw_options.get("software_ids")
        selected_software_ids = (
            {
                str(value)
                for value in raw_software_ids
                if isinstance(value, str) and value.strip()
            }
            if isinstance(raw_software_ids, (list, tuple))
            else None
        )

        states: list[dict[str, object]] = []
        filenames_to_scan: set[str] = set()
        for spec in self._SOFTWARE_PATH_SPECS:
            if (
                selected_software_ids is not None
                and str(spec["software_id"]) not in selected_software_ids
            ):
                continue
            executable_keys = tuple(spec["executable_path"])  # type: ignore[arg-type]
            configured = _get_nested(cfg, executable_keys)
            current = Path(str(configured)) if isinstance(configured, str) and configured else None
            current_valid = bool(
                current is not None
                and current.is_file()
                and self._software_candidate_is_valid(spec, current)
            )
            current_updates: dict[tuple[str, ...], str] = {}
            if current_valid and current is not None:
                current_updates = self._software_linked_updates(spec, current, cfg)
                needs_repair = self._software_updates_differ(cfg, current_updates)
            else:
                needs_repair = True
            if not needs_repair:
                continue
            state = {
                "spec": spec,
                "configured": str(configured or ""),
                "current": current,
                "current_valid": current_valid,
                "current_updates": current_updates,
            }
            states.append(state)
            if not current_valid:
                filenames_to_scan.add(str(spec["filename"]).casefold())

        if not states:
            return []

        candidate_index: dict[str, list[Path]] = {}
        scan_complete = True
        scan_issues = list(root_issues)
        if filenames_to_scan:
            candidate_index, discovered_issues, scan_complete = (
                self._index_software_candidates(
                    roots,
                    filenames_to_scan,
                    max_depth=max_depth,
                    max_directories=max_directories,
                )
            )
            scan_issues.extend(discovered_issues)

        results: list[CheckResult] = []
        pending: list[dict[str, object]] = []
        for state in states:
            spec = state["spec"]
            assert isinstance(spec, Mapping)
            software_id = str(spec["software_id"])
            display_name = str(spec["display_name"])
            configured = str(state["configured"])
            current = state["current"]
            current_valid = bool(state["current_valid"])

            if current_valid and isinstance(current, Path):
                candidates = [current]
                candidate_scan_complete = True
            else:
                raw_candidates = candidate_index.get(
                    str(spec["filename"]).casefold(), [],
                )
                candidates = [
                    candidate for candidate in raw_candidates
                    if self._software_candidate_is_valid(spec, candidate)
                ]
                candidates = _deduplicate_paths(candidates)
                candidate_scan_complete = scan_complete

            details: dict[str, object] = {
                "software_id": software_id,
                "configured_path": configured,
                "search_roots": [str(root) for root in roots],
                "candidates": [str(candidate) for candidate in candidates],
                "scan_complete": candidate_scan_complete,
            }
            if scan_issues:
                details["scan_issues"] = list(scan_issues)

            if not candidate_scan_complete:
                results.append(CheckResult(
                    name=f"software_paths.{software_id}",
                    ok=False,
                    code=CODE_SOFTWARE_PATH_NOT_FOUND,
                    message=(
                        f"{display_name} 地址需要修复，但搜索未完整完成；"
                        "为避免选错程序，已保留原配置"
                    ),
                    severity=SEVERITY_ERROR,
                    details=details,
                ))
                continue
            if not candidates:
                results.append(CheckResult(
                    name=f"software_paths.{software_id}",
                    ok=False,
                    code=CODE_SOFTWARE_PATH_NOT_FOUND,
                    message=(
                        f"找不到可验证的 {display_name}；请安装软件或把安装目录"
                        "加入 software_path_self_check.search_roots"
                    ),
                    severity=SEVERITY_ERROR,
                    details=details,
                ))
                continue
            if len(candidates) > 1:
                results.append(CheckResult(
                    name=f"software_paths.{software_id}",
                    ok=False,
                    code=CODE_SOFTWARE_PATH_AMBIGUOUS,
                    message=(
                        f"找到 {len(candidates)} 个可验证的 {display_name}，"
                        "无法安全判定应使用哪一个"
                    ),
                    severity=SEVERITY_ERROR,
                    details=details,
                ))
                continue

            candidate = candidates[0]
            updates = self._software_linked_updates(spec, candidate, cfg)
            changed_fields = [
                ".".join(keys)
                for keys, value in updates.items()
                if not _path_values_equal(_get_nested(cfg, keys), value)
            ]
            details.update({
                "candidate": str(candidate),
                "changed_fields": changed_fields,
            })
            if not changed_fields:
                # 这个分支只会出现在扫描与状态判断间文件状态改变时。
                continue
            if not apply or not auto_repair:
                policy_note = (
                    "；自动修复目前未启用"
                    if apply and not auto_repair
                    else ""
                )
                results.append(CheckResult(
                    name=f"software_paths.{software_id}",
                    ok=False,
                    code=CODE_SOFTWARE_PATH_CHANGE_REQUIRED,
                    message=(
                        f"{display_name} 已找到唯一可信地址 {candidate}，"
                        f"需要更新 {len(changed_fields)} 个关联字段{policy_note}"
                    ),
                    severity=SEVERITY_WARNING,
                    details=details,
                ))
                continue
            pending.append({
                "spec": spec,
                "candidate": candidate,
                "updates": updates,
                "details": details,
            })

        if not pending:
            return results

        stale_candidates: list[str] = []
        for item in pending:
            spec = item["spec"]
            candidate = item["candidate"]
            assert isinstance(spec, Mapping)
            assert isinstance(candidate, Path)
            if not self._software_candidate_is_valid(spec, candidate):
                stale_candidates.append(str(candidate))
        if stale_candidates:
            results.append(CheckResult(
                name="software_paths.write",
                ok=False,
                code=CODE_SOFTWARE_PATH_NOT_FOUND,
                message="候选程序在写入前发生变化，已保留原配置",
                severity=SEVERITY_ERROR,
                details={"stale_candidates": stale_candidates},
            ))
            return results

        merged_updates: dict[tuple[str, ...], str] = {}
        conflicts: list[str] = []
        for item in pending:
            updates = item["updates"]
            assert isinstance(updates, Mapping)
            for raw_keys, raw_value in updates.items():
                keys = tuple(raw_keys)  # type: ignore[arg-type]
                value = str(raw_value)
                previous = merged_updates.get(keys)
                if previous is not None and not _path_values_equal(previous, value):
                    conflicts.append(".".join(keys))
                merged_updates[keys] = value
        if conflicts:
            results.append(CheckResult(
                name="software_paths.write",
                ok=False,
                code=CODE_INTERNAL,
                message="软件地址关联字段出现冲突，已拒绝写入",
                severity=SEVERITY_ERROR,
                details={"conflicting_fields": sorted(set(conflicts))},
            ))
            return results

        updated_cfg = copy.deepcopy(cfg)
        invalid_fields: list[str] = []
        for keys, value in merged_updates.items():
            ok, _code, _message = self._set_nested_existing(
                updated_cfg, list(keys), value,
            )
            if not ok:
                invalid_fields.append(".".join(keys))
        if invalid_fields:
            results.append(CheckResult(
                name="software_paths.write",
                ok=False,
                code=CODE_KEY_MISSING,
                message="软件地址关联字段不存在，已拒绝写入",
                severity=SEVERITY_ERROR,
                details={"missing_fields": invalid_fields},
            ))
            return results

        try:
            original_text = self._config_path.read_text(encoding="utf-8")
            backup = self.backup_files([self._config_path])
        except (OSError, ConfigManagerError) as exc:
            code = exc.code if isinstance(exc, ConfigManagerError) else CODE_BACKUP_FAILED
            results.append(CheckResult(
                name="software_paths.backup",
                ok=False,
                code=code,
                message=f"软件地址写入前备份失败，已中止：{exc}",
                severity=SEVERITY_ERROR,
                details={},
            ))
            return results

        serialized = json.dumps(updated_cfg, indent=2, ensure_ascii=False) + "\n"
        try:
            _atomic_write_text(self._config_path, serialized)
            with open(self._config_path, "r", encoding="utf-8") as fh:
                verified_cfg = json.load(fh)
            if not isinstance(verified_cfg, dict):
                raise ValueError("写入后配置根层级不是对象")
            mismatches = [
                ".".join(keys)
                for keys, value in merged_updates.items()
                if not _path_values_equal(_get_nested(verified_cfg, keys), value)
            ]
            if mismatches:
                raise ValueError(f"写入后字段不符：{mismatches}")
        except Exception as exc:
            rollback_error = ""
            try:
                _atomic_write_text(self._config_path, original_text)
                self._config_cache = cfg
            except Exception as rollback_exc:  # pragma: no cover - 極端磁碟故障
                rollback_error = f"；恢复原配置也失败：{rollback_exc}"
                self._config_cache = None
            results.append(CheckResult(
                name="software_paths.write",
                ok=False,
                code=CODE_SOFTWARE_PATH_WRITE_FAILED,
                message=f"软件地址写入或验证失败：{exc}{rollback_error}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup.backup_id},
            ))
            return results

        self._config_cache = verified_cfg
        for item in pending:
            spec = item["spec"]
            details = dict(item["details"])  # type: ignore[arg-type]
            details["backup_id"] = backup.backup_id
            results.append(CheckResult(
                name=f"software_paths.{spec['software_id']}",  # type: ignore[index]
                ok=True,
                code=CODE_SOFTWARE_PATH_REPAIRED,
                message=(
                    f"已自动更新 {spec['display_name']} 地址与关联配置，"  # type: ignore[index]
                    "管理员计划任务将按新配置重新核对"
                ),
                severity=SEVERITY_INFO,
                details=details,
            ))
        return results

    # ------------------------------------------------------------------ #
    # 预检                                                                #
    # ------------------------------------------------------------------ #

    def preflight(self) -> list[CheckResult]:
        """检查环境：路径是否存在、第三方配置文件是否可读、关键键是否在预期状态。

        只读取，不修改任何文件。测试可指向任意临时目录。
        """
        results: list[CheckResult] = []
        cfg = self.load()
        results.extend(self.reconcile_software_paths(apply=False))

        # 执行顺序与稳定 ID
        order = cfg.get("execution", {}).get("order", [])
        expected_ids = {"arknights", "endfield", "limbus"}
        actual_ids = set(order)
        if actual_ids != expected_ids:
            missing = expected_ids - actual_ids
            extra = actual_ids - expected_ids
            results.append(CheckResult(
                name="execution.order",
                ok=False,
                code=CODE_KEY_MISSING if missing else CODE_AMBIGUOUS,
                message=f"执行顺序 ID 不符：缺少 {sorted(missing) or '无'}，多出 {sorted(extra) or '无'}",
                severity=SEVERITY_ERROR,
                details={"order": list(order), "expected": sorted(expected_ids)},
            ))
        else:
            results.append(CheckResult(
                name="execution.order",
                ok=True,
                code=CODE_OK,
                message=f"执行顺序正常：{order}",
                severity=SEVERITY_INFO,
                details={"order": list(order)},
            ))

        # 各游戏的可执行文件与挂机工具可执行文件
        games = cfg.get("games", {})
        for gid in ("arknights", "endfield", "limbus"):
            g = games.get(gid)
            if not isinstance(g, dict):
                results.append(CheckResult(
                    name=f"games.{gid}",
                    ok=False,
                    code=CODE_KEY_MISSING,
                    message=f"缺少游戏配置：{gid}",
                    severity=SEVERITY_ERROR,
                    details={},
                ))
                continue

            # 游戏可执行文件（arknights/endfield 用 game_executable；limbus 同）
            game_exe = g.get("game_executable")
            results.extend(self._check_path_exists(
                f"games.{gid}.game_executable", game_exe, required=True,
            ))

            # 启动方式
            if gid in ("arknights", "endfield"):
                launcher = g.get("launcher", {})
                lex = launcher.get("executable")
                results.extend(self._check_path_exists(
                    f"games.{gid}.launcher.executable", lex, required=True,
                ))
                verified = bool(launcher.get("verified", False))
                results.append(CheckResult(
                    name=f"games.{gid}.launcher.verified",
                    ok=verified,
                    code=CODE_OK if verified else "unverified",
                    message=("启动器参数已验证" if verified else
                             f"启动器参数未验证：{launcher.get('unverified_note', '')}"),
                    severity=SEVERITY_INFO if verified else SEVERITY_WARNING,
                    details={"verified": verified, "args": launcher.get("args", [])},
                ))
            else:  # limbus
                launch = g.get("launch", {})
                fallback = launch.get("fallback_executable")
                results.extend(self._check_path_exists(
                    f"games.{gid}.launch.fallback_executable", fallback, required=True,
                ))

            # 挂机工具
            helper = g.get("helper", {})
            hexe = helper.get("executable")
            results.extend(self._check_path_exists(
                f"games.{gid}.helper.executable", hexe, required=True,
            ))

            # 工具配置文件（若指定）
            # config_file 可能为相对文件名，需与 config_dir 合并；若已是绝对路径则直接用
            cfg_file = helper.get("config_file")
            cfg_dir = helper.get("config_dir")
            if cfg_file:
                file_path = Path(str(cfg_file))
                if not file_path.is_absolute() and cfg_dir:
                    file_path = Path(str(cfg_dir)) / file_path
                results.extend(self._check_path_exists(
                    f"games.{gid}.helper.config_file", str(file_path), required=False,
                ))
            if cfg_dir:
                results.extend(self._check_path_exists(
                    f"games.{gid}.helper.config_dir", cfg_dir, required=False, is_dir=True,
                ))

            # limbus 语言风险（用户已装汉化，仅提示）
            if gid == "limbus":
                lang = g.get("language", {})
                results.append(CheckResult(
                    name=f"games.{gid}.language",
                    ok=True,
                    code=CODE_OK if lang.get("verified") else "unverified",
                    message=(f"Steam 列表语言={lang.get('steam_manifest_language')}，"
                             f"实际={lang.get('actual_language', '未知')}，"
                             f"用户确认汉化={'是' if lang.get('user_confirmed_hanization') else '否'}"),
                    severity=SEVERITY_INFO if lang.get("verified") else SEVERITY_WARNING,
                    details={
                        "steam_manifest_language": lang.get("steam_manifest_language"),
                        "actual_language": lang.get("actual_language"),
                        "user_confirmed_hanization": lang.get("user_confirmed_hanization", False),
                    },
                ))
                cli_args = helper.get("cli_args", [])
                normalized_cli_args = (
                    [str(value) for value in cli_args]
                    if isinstance(cli_args, (list, tuple))
                    else []
                )
                cli_ready = bool(
                    normalized_cli_args == ["start", "--exit", "6"]
                    and helper.get("cli_start_verified")
                    and helper.get("cli_exit_both_verified")
                )
                results.append(CheckResult(
                    name=f"games.{gid}.helper.one_shot_completion",
                    ok=cli_ready,
                    code=CODE_OK if cli_ready else "aalc_one_shot_unverified",
                    message=(
                        "AALC 将使用官方 start --exit 6 一次性命令，自动开始并退出游戏与自身"
                        if cli_ready
                        else "AALC 官方一次性启动与双退出命令尚未验证"
                    ),
                    severity=SEVERITY_INFO if cli_ready else SEVERITY_WARNING,
                    details={
                        "cli_args": normalized_cli_args,
                        "cli_start_verified": bool(
                            helper.get("cli_start_verified", False)
                        ),
                        "cli_exit_both_verified": bool(
                            helper.get("cli_exit_both_verified", False)
                        ),
                    },
                ))

            # endfield 连接端口不得占用 12701
            if gid == "endfield":
                port = helper.get("web_server_port")
                if helper.get("web_server_port_do_not_use") and port:
                    results.append(CheckResult(
                        name=f"games.{gid}.helper.web_server_port",
                        ok=True,
                        code=CODE_OK,
                        message=f"MaaEnd 占用 127.0.0.1:{port}，本程序不使用此连接端口",
                        severity=SEVERITY_INFO,
                        details={"port": port, "do_not_use": True},
                    ))
                instance_ok, instance_details = self._maaend_active_instance_state(helper)
                cli_ok = bool(
                    helper.get("cli_autostart_verified")
                    and helper.get("quit_after_run_verified")
                    and helper.get("close_owned_game_after_tasks")
                )
                ready = instance_ok and cli_ok
                results.append(CheckResult(
                    name=f"games.{gid}.helper.one_shot_completion",
                    ok=ready,
                    code=CODE_OK if ready else "maaend_one_shot_unverified",
                    message=(
                        "MaaEnd 将使用官方一次性运行参数，完成后退出自身；"
                        "随后仅关闭本轮接管的 Win32 游戏"
                        if ready
                        else "MaaEnd 当前活动 Win32 实例或一次性运行/安全关闭策略未验证"
                    ),
                    severity=SEVERITY_INFO if ready else SEVERITY_WARNING,
                    details={**instance_details, "cli_verified": cli_ok},
                ))

        return results

    # ------------------------------------------------------------------ #
    # 变更规划                                                            #
    # ------------------------------------------------------------------ #

    def plan_required_changes(self) -> list[ConfigChange]:
        """规划一键执行所需的配置变更，对敏感值脱敏。

        只返回尚未符合 proposed 值的变更（含 verified=False 供用户查看）。
        current_display 尽量读取实际当前值；无法读取时标记“未读取”。
        """
        cfg = self.load()
        changes: list[ConfigChange] = []
        for spec in self._REQUIRED_CHANGE_SPECS:
            current = self._read_current_value(spec, cfg)
            current_display = _mask_sensitive(current)
            proposed = self._resolve_proposed_value(spec, cfg)
            proposed_display = _mask_sensitive(proposed)
            if current_display == proposed_display:
                continue
            changes.append(ConfigChange(
                change_id=str(spec["change_id"]),
                tool=str(spec["tool"]),
                field=str(spec["field"]),
                current_display=current_display,
                proposed_display=proposed_display,
                verified=bool(spec["verified"]),
                warning=str(spec["warning"]),
            ))
        return changes

    # ------------------------------------------------------------------ #
    # 应用变更                                                            #
    # ------------------------------------------------------------------ #

    def apply_required_changes(self, change_ids: Sequence[str]) -> list[CheckResult]:
        """应用指定变更。

        规则：
        - 拒绝 unknown（不在规划表中的）change_id。
        - 拒绝重复的 change_id。
        - 拒绝 verified=False 的 change_id。
        - 任一请求项目无效时整批拒绝，不应用有效子集。
        - 在任何写入前，统一为所有涉及的目标文件创建一份备份。
        - 先在同目录副本应用并验证全部变更，再逐项原子提交。
        - 提交前复核原档与暂存档；中途失败时只回滚本轮已提交档案。
        """
        ids = list(change_ids)
        # 1. 去重与存在性检查
        seen: set[str] = set()
        specs_by_id = {str(s["change_id"]): s for s in self._REQUIRED_CHANGE_SPECS}
        selected: list[dict[str, object]] = []
        results: list[CheckResult] = []
        for cid in ids:
            if cid in seen:
                results.append(CheckResult(
                    name=f"change.{cid}",
                    ok=False,
                    code=CODE_CHANGE_DUPLICATE,
                    message=f"重复的变更 ID：{cid}",
                    severity=SEVERITY_ERROR,
                    details={"change_id": cid},
                ))
                continue
            seen.add(cid)
            if cid not in specs_by_id:
                results.append(CheckResult(
                    name=f"change.{cid}",
                    ok=False,
                    code=CODE_CHANGE_UNKNOWN,
                    message=f"未知的变更 ID：{cid}",
                    severity=SEVERITY_ERROR,
                    details={"change_id": cid},
                ))
                continue
            spec = specs_by_id[cid]
            if not bool(spec["verified"]):
                results.append(CheckResult(
                    name=f"change.{cid}",
                    ok=False,
                    code=CODE_CHANGE_NOT_VERIFIED,
                    message=f"变更未验证，不可自动应用：{cid}（{spec['warning']}）",
                    severity=SEVERITY_ERROR,
                    details={"change_id": cid, "warning": spec["warning"]},
                ))
                continue
            selected.append(spec)

        # 任一请求项目无效时整批拒绝，不能只应用其中的有效子集。
        if results or not selected:
            return results

        # 2. 任何写入前统一解析目标并备份所有涉及文件。
        try:
            cfg = self.load()
            files_to_specs: dict[Path, list[dict[str, object]]] = {}
            for spec in selected:
                target = self._resolve_target_file(spec, cfg).resolve()
                files_to_specs.setdefault(target, []).append(spec)
            target_files = list(files_to_specs)
            backup = self.backup_files([str(p) for p in target_files])

            # backup_files 已做发布前验证；这里再从已发布清单取得每个目标
            # 的基线哈希与只读副本，后续不得改用备份完成后的现场内容。
            with open(backup.manifest_path, "r", encoding="utf-8") as fh:
                backup_manifest = json.load(fh)
            _backup_info, backup_entries = _validate_backup_manifest(
                backup.manifest_path,
                backup_manifest,
                expected_backup_id=backup.backup_id,
                verify_payloads=True,
            )
            entries_by_target = {
                _normalized_path(entry.original_path): entry
                for entry in backup_entries
            }
            expected_targets = {
                _normalized_path(target) for target in target_files
            }
            if set(entries_by_target) != expected_targets:
                raise ConfigManagerError(
                    CODE_BACKUP_FAILED,
                    "备份清单与本次配置目标不一致",
                )
            results.append(CheckResult(
                name="backup",
                ok=True,
                code=CODE_OK,
                message=f"已创建备份 {backup.backup_id}（{backup.source_count} 个文件）",
                severity=SEVERITY_INFO,
                details={"backup_id": backup.backup_id, "source_count": backup.source_count},
            ))
        except ConfigManagerError as exc:
            results.append(CheckResult(
                name="backup",
                ok=False,
                code=exc.code,
                message=f"备份失败，已中止应用：{exc.message}",
                severity=SEVERITY_ERROR,
                details={"backup_error": exc.details},
            ))
            return results
        except OSError as exc:
            results.append(CheckResult(
                name="backup",
                ok=False,
                code=CODE_BACKUP_FAILED,
                message=f"备份失败，已中止应用：{exc}",
                severity=SEVERITY_ERROR,
                details={"error_type": type(exc).__name__},
            ))
            return results
        except Exception as exc:
            results.append(CheckResult(
                name="apply.prepare",
                ok=False,
                code=CODE_INTERNAL,
                message=f"配置准备异常，已中止应用：{type(exc).__name__}",
                severity=SEVERITY_ERROR,
                details={"error_type": type(exc).__name__},
            ))
            return results

        # 3. 在各目标的同目录副本完成修改与验证，原档保持不变。
        stages: list[_ApplyStage] = []
        stage_directories: set[Path] = set()
        stage_failed = False
        for target, file_specs in files_to_specs.items():
            stage_directory: Path | None = None
            try:
                backup_entry = entries_by_target[_normalized_path(target)]
                if not _is_plain_file(target):
                    raise ConfigManagerError(
                        CODE_FILE_NOT_FOUND,
                        f"目标文件不存在或不是普通文件：{target}",
                    )
                baseline_hash = backup_entry.sha256.casefold()
                if _sha256(target).casefold() != baseline_hash:
                    raise ConfigManagerError(
                        CODE_ATOMIC_REPLACE_FAILED,
                        f"目标配置在备份后已被修改：{target}",
                    )
                stage_directory = Path(tempfile.mkdtemp(
                    prefix=".daily_games_apply_",
                    dir=str(target.parent),
                ))
                stage_directories.add(stage_directory)
                staged_path = stage_directory / target.name
                shutil.copy2(backup_entry.backup_path, staged_path)
                if (
                    not _is_plain_file(staged_path)
                    or _sha256(staged_path).casefold() != baseline_hash
                ):
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"配置暂存副本哈希不符：{target}",
                    )
                staged_results = self._apply_specs_to_one_file(
                    staged_path,
                    file_specs,
                    cfg,
                )
                staged_results = [
                    CheckResult(
                        name=result.name,
                        ok=result.ok,
                        code=result.code,
                        message=result.message.replace(
                            str(staged_path),
                            str(target),
                        ),
                        severity=result.severity,
                        details=result.details,
                    )
                    for result in staged_results
                ]
                if all(result.ok for result in staged_results):
                    desired_hash = _sha256(staged_path).casefold()
                    rollback_path = stage_directory / f".rollback_{target.name}"
                    shutil.copy2(backup_entry.backup_path, rollback_path)
                    if (
                        not _is_plain_file(rollback_path)
                        or _sha256(rollback_path).casefold() != baseline_hash
                    ):
                        raise ConfigManagerError(
                            CODE_HASH_MISMATCH,
                            f"配置回滚副本哈希不符：{target}",
                        )
            except Exception as exc:
                code = exc.code if isinstance(exc, ConfigManagerError) else CODE_INTERNAL
                message = (
                    exc.message
                    if isinstance(exc, ConfigManagerError)
                    else f"{type(exc).__name__}"
                )
                results.append(CheckResult(
                    name=f"apply.{target.name}",
                    ok=False,
                    code=code,
                    message=f"配置暂存失败：{message}",
                    severity=SEVERITY_ERROR,
                    details={"path": str(target)},
                ))
                stage_failed = True
                break
            if any(not result.ok for result in staged_results):
                results.extend(staged_results)
                stage_failed = True
                break
            stages.append(_ApplyStage(
                target=target,
                stage_directory=stage_directory,
                staged_path=staged_path,
                rollback_path=rollback_path,
                baseline_hash=baseline_hash,
                desired_hash=desired_hash,
                results=staged_results,
            ))

        if stage_failed:
            for directory in stage_directories:
                shutil.rmtree(directory, ignore_errors=True)
            results.append(CheckResult(
                name="apply.rollback",
                ok=True,
                code=CODE_OK,
                message="配置暂存失败，本次未提交任何原始文件",
                severity=SEVERITY_INFO,
                details={"backup_id": backup.backup_id},
            ))
            return results

        # 4. 提交前一次性核对所有原档、暂存档与回滚副本。
        drifted: list[Path] = []
        invalid_stages: list[Path] = []
        for stage in stages:
            try:
                if (
                    not _is_plain_file(stage.target)
                    or _sha256(stage.target).casefold() != stage.baseline_hash
                ):
                    drifted.append(stage.target)
            except OSError:
                drifted.append(stage.target)
            try:
                if (
                    not _is_plain_file(stage.staged_path)
                    or _sha256(stage.staged_path).casefold() != stage.desired_hash
                    or not _is_plain_file(stage.rollback_path)
                    or _sha256(stage.rollback_path).casefold() != stage.baseline_hash
                ):
                    invalid_stages.append(stage.target)
            except OSError:
                invalid_stages.append(stage.target)
        if drifted or invalid_stages:
            for directory in stage_directories:
                shutil.rmtree(directory, ignore_errors=True)
            if drifted:
                results.append(CheckResult(
                    name="apply.concurrent_change",
                    ok=False,
                    code=CODE_ATOMIC_REPLACE_FAILED,
                    message="目标配置在备份后被外部修改，已中止整批应用",
                    severity=SEVERITY_ERROR,
                    details={"paths": [str(path) for path in drifted]},
                ))
            if invalid_stages:
                results.append(CheckResult(
                    name="apply.stage_integrity",
                    ok=False,
                    code=CODE_HASH_MISMATCH,
                    message="配置暂存或回滚副本完整性校验失败，已中止整批应用",
                    severity=SEVERITY_ERROR,
                    details={"paths": [str(path) for path in invalid_stages]},
                ))
            results.append(CheckResult(
                name="apply.rollback",
                ok=True,
                code=CODE_OK,
                message="尚未提交任何配置，无需回滚",
                severity=SEVERITY_INFO,
                details={"backup_id": backup.backup_id},
            ))
            return results

        # 5. 同卷原子替换。每项提交前再次复核，缩小并发修改窗口。
        commit_error: Exception | None = None
        for stage in stages:
            replace_attempted = False
            try:
                if (
                    not _is_plain_file(stage.target)
                    or _sha256(stage.target).casefold() != stage.baseline_hash
                ):
                    raise ConfigManagerError(
                        CODE_ATOMIC_REPLACE_FAILED,
                        f"目标配置在提交前已被修改：{stage.target}",
                    )
                if (
                    not _is_plain_file(stage.staged_path)
                    or _sha256(stage.staged_path).casefold() != stage.desired_hash
                ):
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"配置暂存副本在提交前已被修改：{stage.target}",
                    )
                if (
                    not _is_plain_file(stage.rollback_path)
                    or _sha256(stage.rollback_path).casefold() != stage.baseline_hash
                ):
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"配置回滚副本在提交前已被修改：{stage.target}",
                    )
                replace_attempted = True
                os.replace(stage.staged_path, stage.target)
                stage.committed = True
                if _sha256(stage.target).casefold() != stage.desired_hash:
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"配置提交后哈希不符：{stage.target}",
                    )
            except Exception as exc:
                # os.replace 极少数平台错误可能在替换完成后才上报；若目标
                # 已呈现期望哈希，仍视为已提交并纳入回滚范围。
                if replace_attempted and not stage.committed:
                    try:
                        stage.committed = (
                            _is_plain_file(stage.target)
                            and _sha256(stage.target).casefold() == stage.desired_hash
                        )
                    except OSError:
                        pass
                commit_error = exc
                break

        if commit_error is not None:
            commit_message = (
                commit_error.message
                if isinstance(commit_error, ConfigManagerError)
                else type(commit_error).__name__
            )
            results.append(CheckResult(
                name="apply.commit",
                ok=False,
                code=(
                    commit_error.code
                    if isinstance(commit_error, ConfigManagerError)
                    else CODE_ATOMIC_REPLACE_FAILED
                ),
                message=f"配置提交失败：{commit_message}",
                severity=SEVERITY_ERROR,
                details={},
            ))

            committed_count = sum(1 for stage in stages if stage.committed)
            rollback_errors: list[str] = []
            for stage in reversed(stages):
                if not stage.committed:
                    continue
                try:
                    if not _is_plain_file(stage.target):
                        raise OSError("提交后的目标不是普通文件")
                    current_hash = _sha256(stage.target).casefold()
                    if current_hash == stage.baseline_hash:
                        stage.committed = False
                        continue
                    if current_hash != stage.desired_hash:
                        raise OSError("提交后的目标又被外部修改")
                    if (
                        not _is_plain_file(stage.rollback_path)
                        or _sha256(stage.rollback_path).casefold()
                        != stage.baseline_hash
                    ):
                        raise OSError("回滚副本完整性校验失败")
                    os.replace(stage.rollback_path, stage.target)
                    if _sha256(stage.target).casefold() != stage.baseline_hash:
                        raise OSError("回滚后哈希不符")
                    stage.committed = False
                except Exception as exc:
                    # 若 replace 已生效但随后抛错，以现场哈希作为最终判断。
                    try:
                        recovered = (
                            _is_plain_file(stage.target)
                            and _sha256(stage.target).casefold()
                            == stage.baseline_hash
                        )
                    except OSError:
                        recovered = False
                    if recovered:
                        stage.committed = False
                    else:
                        rollback_errors.append(
                            f"{stage.target.name}: {type(exc).__name__}"
                        )

            rollback_ok = not rollback_errors
            results.append(CheckResult(
                name="apply.rollback",
                ok=rollback_ok,
                code=CODE_OK if rollback_ok else CODE_ATOMIC_REPLACE_FAILED,
                message=(
                    "配置应用失败，已恢复本轮已提交档案"
                    if rollback_ok and committed_count
                    else "尚未提交任何配置，无需回滚"
                    if rollback_ok
                    else "配置应用失败，自动回滚未完成"
                ),
                severity=SEVERITY_INFO if rollback_ok else SEVERITY_ERROR,
                details={
                    "backup_id": backup.backup_id,
                    "committed_count": committed_count,
                    "errors": rollback_errors,
                },
            ))
        else:
            for stage in stages:
                results.extend(stage.results)

        for directory in stage_directories:
            shutil.rmtree(directory, ignore_errors=True)

        return results

    # ------------------------------------------------------------------ #
    # 备份                                                                #
    # ------------------------------------------------------------------ #

    def backup_files(self, paths: Sequence[str | os.PathLike[str]]) -> BackupInfo:
        """把指定文件复制到 backup_root/<时间戳>/，并生成列表。

        列表记录原始绝对路径、SHA-256、大小、修改时间。
        任一文件不存在或读取失败则整体失败（不创建部分备份）。
        """
        # abspath 只做词法绝对化，不跟随符号链接或重解析点；随后才能对调用者
        # 实际指定的目录项执行 lstat 安全检查。
        src_paths = [
            Path(os.path.abspath(os.fspath(_coerce_path(p))))
            for p in paths
        ]
        if not src_paths:
            raise ConfigManagerError(
                CODE_AMBIGUOUS,
                "待备份文件列表为空",
            )
        normalized_sources = [_normalized_path(path) for path in src_paths]
        if len(set(normalized_sources)) != len(normalized_sources):
            raise ConfigManagerError(
                CODE_AMBIGUOUS,
                "待备份文件列表含重复路径",
            )
        # 先全部验证存在，并拒绝符号链接或 Windows 重解析点。
        for p in src_paths:
            if not _is_plain_file(p):
                raise ConfigManagerError(
                    CODE_FILE_NOT_FOUND,
                    f"待备份文件不存在或不是普通文件：{p}",
                    {"path": str(p)},
                )

        backup_root = Path(os.path.abspath(os.fspath(self._backup_root)))
        if os.path.lexists(backup_root):
            if not _is_plain_directory(backup_root):
                raise ConfigManagerError(
                    CODE_AMBIGUOUS,
                    f"备份根目录不是普通目录：{backup_root}",
                    {"path": str(backup_root)},
                )
        else:
            backup_root.mkdir(parents=True, exist_ok=False)
            if not _is_plain_directory(backup_root):
                raise ConfigManagerError(
                    CODE_AMBIGUOUS,
                    f"无法建立普通备份根目录：{backup_root}",
                    {"path": str(backup_root)},
                )

        backup_id = secrets.token_hex(4)
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        # 时间戳目录 + 短随机后缀避免同秒冲突
        dest_dir = backup_root / f"{ts}_{backup_id}"
        dest_dir.mkdir(exist_ok=False)
        if (
            not _is_plain_directory(backup_root)
            or not _is_plain_directory(dest_dir)
        ):
            raise ConfigManagerError(
                CODE_AMBIGUOUS,
                "备份目录在建立后未通过普通目录检查",
                {"path": str(dest_dir)},
            )

        manifest_entries: list[dict[str, object]] = []
        try:
            for idx, p in enumerate(src_paths):
                meta = _file_meta(p)
                # 为避免文件名冲突，在原名之前添加序号前缀
                safe_name = f"{idx:03d}_{p.name}"
                dest = dest_dir / safe_name
                shutil.copy2(p, dest)
                # 重新计算哈希确认复制完整
                dest_meta = _file_meta(dest)
                if dest_meta["sha256"] != meta["sha256"]:
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"备份复制后哈希不符：{p}",
                        {"source": meta, "dest": dest_meta},
                    )
                manifest_entries.append({
                    "index": idx,
                    "original_absolute_path": str(p),
                    "original_name": p.name,
                    "backup_name": safe_name,
                    **meta,
                })
            manifest = {
                "backup_id": backup_id,
                "created_at": _now_iso(),
                "source_root_hint": str(backup_root),
                "source_count": len(src_paths),
                "entries": manifest_entries,
            }
            manifest_path = dest_dir / "manifest.json"
            _atomic_write_text(
                manifest_path,
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
            # 回读并验证清单及全部副本，成功后才发布 BackupInfo。
            _validate_backup_manifest(
                manifest_path,
                manifest,
                expected_backup_id=backup_id,
                verify_payloads=True,
            )
        except Exception:
            # 清理已创建的备份目录，避免部分备份
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        return BackupInfo(
            backup_id=backup_id,
            created_at=manifest["created_at"],
            manifest_path=manifest_path,
            source_count=len(src_paths),
        )

    # ------------------------------------------------------------------ #
    # JSON 修补                                                          #
    # ------------------------------------------------------------------ #

    def patch_json(self, path: str | os.PathLike[str], updates: Mapping[str, object]) -> list[CheckResult]:
        """以指定键更新 JSON，保留未知键。

        updates 为 dict 的键路径到值的映射，例如：
            {"settings.autoRunOnLaunch": True}
        点号表示嵌套。只更新已存在的键；键不存在返回 key_missing，不添加。
        先写临时文件、重新解析验证后再原子替换。
        """
        target = _coerce_path(path)
        if not _is_plain_file(target):
            return [CheckResult(
                name=f"patch_json.{target.name}",
                ok=False,
                code=CODE_FILE_NOT_FOUND,
                message=f"文件不存在：{target}",
                severity=SEVERITY_ERROR,
                details={"path": str(target)},
            )]
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            return [CheckResult(
                name=f"patch_json.{target.name}",
                ok=False,
                code=CODE_PARSE_FAILED,
                message=f"JSON 解析失败：{exc}",
                severity=SEVERITY_ERROR,
                details={"line": exc.lineno},
            )]

        results: list[CheckResult] = []
        changed = False
        for dotted, value in updates.items():
            keys = dotted.split(".")
            ok, code, msg = self._set_nested_existing(data, keys, value)
            results.append(CheckResult(
                name=f"patch_json.{target.name}.{dotted}",
                ok=ok,
                code=code,
                message=msg,
                severity=SEVERITY_INFO if ok else SEVERITY_ERROR,
                details={"dotted": dotted, "proposed": _mask_sensitive(value)},
            ))
            if ok:
                changed = True

        # 单次请求具有整批语义；任何键失败时不得写入其余有效键。
        if any(not result.ok for result in results) or not changed:
            return results

        # 串行化、原子替换、重新解析验证
        text = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            _atomic_write_text(target, text)
        except OSError as exc:
            results.append(CheckResult(
                name=f"patch_json.{target.name}.write",
                ok=False,
                code=CODE_ATOMIC_REPLACE_FAILED,
                message=f"原子写入失败：{exc}",
                severity=SEVERITY_ERROR,
                details={},
            ))
            return results

        # 写入后重新解析验证
        verify_ok, verify_msg = self._verify_json_values(target, updates)
        results.append(CheckResult(
            name=f"patch_json.{target.name}.verify",
            ok=verify_ok,
            code=CODE_OK if verify_ok else CODE_VERIFY_AFTER_WRITE_FAILED,
            message=verify_msg,
            severity=SEVERITY_INFO if verify_ok else SEVERITY_ERROR,
            details={},
        ))
        return results

    # ------------------------------------------------------------------ #
    # YAML 修补（无 PyYAML，只处理唯一且有边界的标量键）                   #
    # ------------------------------------------------------------------ #

    def patch_yaml_scalars(
        self,
        path: str | os.PathLike[str],
        updates: Mapping[str, str],
        *,
        _allow_multiline_repair: frozenset[str] = frozenset(),
    ) -> list[CheckResult]:
        """对 YAML 执行保留格式的标量替换。

        规则（符合任一即拒绝写入）：
        - 键缺失。
        - 键重复出现。
        - 当前值的缩进无法确定（例如跨多行的 plain scalar）。
        - 原文件疑似损坏（解析时检测到明显结构异常）。

        updates 的值仅支持字符串；调用者负责格式化（例如 true/false/数字以字符串表示）。
        """
        target = _coerce_path(path)
        if not _is_plain_file(target):
            return [CheckResult(
                name=f"patch_yaml.{target.name}",
                ok=False,
                code=CODE_FILE_NOT_FOUND,
                message=f"文件不存在：{target}",
                severity=SEVERITY_ERROR,
                details={"path": str(target)},
            )]

        # 读取原始行
        try:
            with open(target, "r", encoding="utf-8") as fh:
                original_lines = fh.read().splitlines(keepends=True)
        except OSError as exc:
            return [CheckResult(
                name=f"patch_yaml.{target.name}",
                ok=False,
                code=CODE_INTERNAL,
                message=f"读取失败：{exc}",
                severity=SEVERITY_ERROR,
                details={},
            )]

        results: list[CheckResult] = []
        new_lines = list(original_lines)
        any_applied = False

        for key, new_value in updates.items():
            occurrences = _find_scalar_key_lines(new_lines, key)
            if len(occurrences) == 0:
                results.append(CheckResult(
                    name=f"patch_yaml.{target.name}.{key}",
                    ok=False,
                    code=CODE_KEY_MISSING,
                    message=f"YAML 键不存在：{key}",
                    severity=SEVERITY_ERROR,
                    details={"key": key},
                ))
                continue
            if len(occurrences) > 1:
                results.append(CheckResult(
                    name=f"patch_yaml.{target.name}.{key}",
                    ok=False,
                    code=CODE_KEY_DUPLICATE,
                    message=f"YAML 键重复出现 {len(occurrences)} 次：{key}",
                    severity=SEVERITY_ERROR,
                    details={"key": key, "count": len(occurrences)},
                ))
                continue

            line_idx, indent, value_span, detected_corrupt = occurrences[0]  # type: ignore[misc]
            if detected_corrupt:
                if key in _allow_multiline_repair:
                    repaired = _replace_multiline_plain_scalar(
                        new_lines,
                        line_idx,
                        indent,
                        value_span,
                        new_value,
                    )
                    if repaired is not None:
                        new_lines = repaired
                        any_applied = True
                        results.append(CheckResult(
                            name=f"patch_yaml.{target.name}.{key}",
                            ok=True,
                            code=CODE_OK,
                            message=f"已把 YAML 键 {key} 的唯一跨行标量安全合并为单行",
                            severity=SEVERITY_INFO,
                            details={"key": key, "line": line_idx + 1},
                        ))
                        continue
                results.append(CheckResult(
                    name=f"patch_yaml.{target.name}.{key}",
                    ok=False,
                    code=CODE_YAML_MULTILINE_OR_CORRUPT,
                    message=f"YAML 键 {key} 的当前值跨多行或疑似损坏，拒绝写入",
                    severity=SEVERITY_ERROR,
                    details={"key": key, "line": line_idx + 1},
                ))
                continue

            original_line = new_lines[line_idx]
            prefix = original_line[: value_span[0]]
            suffix = original_line[value_span[1]:]
            replaced_line = f"{prefix}{new_value}{suffix}"
            new_lines[line_idx] = replaced_line
            any_applied = True
            results.append(CheckResult(
                name=f"patch_yaml.{target.name}.{key}",
                ok=True,
                code=CODE_OK,
                message=f"已规划替换 YAML 键 {key}（缩进 {indent}）",
                severity=SEVERITY_INFO,
                details={"key": key, "indent": indent, "line": line_idx + 1},
            ))

        # 单次请求具有整批语义；任何键失败时不得写入其余有效键。
        if any(not result.ok for result in results) or not any_applied:
            return results

        # 原子替换 + 重新解析验证
        new_text = "".join(new_lines)
        try:
            _atomic_write_text(target, new_text)
        except OSError as exc:
            results.append(CheckResult(
                name=f"patch_yaml.{target.name}.write",
                ok=False,
                code=CODE_ATOMIC_REPLACE_FAILED,
                message=f"原子写入失败：{exc}",
                severity=SEVERITY_ERROR,
                details={},
            ))
            return results

        verify_ok, verify_msg = self._verify_yaml_values(target, updates)
        results.append(CheckResult(
            name=f"patch_yaml.{target.name}.verify",
            ok=verify_ok,
            code=CODE_OK if verify_ok else CODE_VERIFY_AFTER_WRITE_FAILED,
            message=verify_msg,
            severity=SEVERITY_INFO if verify_ok else SEVERITY_ERROR,
            details={},
        ))
        return results

    # ------------------------------------------------------------------ #
    # 备份列表与恢复                                                      #
    # ------------------------------------------------------------------ #

    def list_backups(self) -> list[BackupInfo]:
        """列出 backup_root 下的所有备份，并按时间排序。"""
        infos: list[BackupInfo] = []
        if not _is_plain_directory(self._backup_root):
            return infos
        try:
            root_entries = sorted(self._backup_root.iterdir())
        except OSError:
            return infos
        for entry in root_entries:
            if not _is_plain_directory(entry):
                continue
            manifest = entry / "manifest.json"
            if not _is_plain_file(manifest):
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                info, _entries = _validate_backup_manifest(
                    manifest,
                    m,
                    verify_payloads=False,
                )
            except (
                ConfigManagerError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue
            infos.append(info)
        id_counts: dict[str, int] = {}
        for info in infos:
            id_counts[info.backup_id] = id_counts.get(info.backup_id, 0) + 1
        return [info for info in infos if id_counts[info.backup_id] == 1]

    def restore_backup(self, backup_id: str) -> list[CheckResult]:
        """恢复指定备份。

        恢复前先再次备份各文件的当前版本（避免部分恢复后无法回退），
        并在任何目标写入前验证全部副本、准备同目录替换文件与回滚文件。
        提交或写后验证失败时，已提交目标会倒序恢复原状。
        """
        try:
            manifest_path, manifest = self._find_backup_manifest(backup_id)
        except OSError as exc:
            return [CheckResult(
                name="restore.lookup",
                ok=False,
                code=CODE_BACKUP_FAILED,
                message=f"无法读取备份目录：{exc}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_id, "error_type": type(exc).__name__},
            )]
        except Exception as exc:
            return [CheckResult(
                name="restore.lookup",
                ok=False,
                code=CODE_INTERNAL,
                message=f"查找备份时发生异常：{type(exc).__name__}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_id, "error_type": type(exc).__name__},
            )]
        if manifest is None:
            return [CheckResult(
                name="restore",
                ok=False,
                code=CODE_FILE_NOT_FOUND,
                message=f"找不到唯一且有效的备份：{backup_id}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_id},
            )]

        try:
            backup_info, entries = _validate_backup_manifest(
                manifest_path,
                manifest,
                expected_backup_id=backup_id,
                verify_payloads=True,
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, ConfigManagerError)
                else CODE_BACKUP_FAILED
                if isinstance(exc, OSError)
                else CODE_INTERNAL
            )
            message = (
                exc.message
                if isinstance(exc, ConfigManagerError)
                else str(exc)
                if isinstance(exc, OSError)
                else type(exc).__name__
            )
            error_details = (
                exc.details if isinstance(exc, ConfigManagerError) else {}
            )
            return [CheckResult(
                name="restore.validate",
                ok=False,
                code=code,
                message=f"备份验证失败：{message}",
                severity=SEVERITY_ERROR,
                details={
                    "backup_id": backup_id,
                    "error": error_details,
                    "error_type": type(exc).__name__,
                },
            )]

        results: list[CheckResult] = []
        for entry in entries:
            target = entry.original_path
            if os.path.lexists(target) and not _is_plain_file(target):
                return [CheckResult(
                    name=f"restore.{entry.original_name}",
                    ok=False,
                    code=CODE_AMBIGUOUS,
                    message=f"恢复目标不是普通文件：{target}",
                    severity=SEVERITY_ERROR,
                    details={"path": str(target)},
                )]

        # 1. 恢复前把所有既存目标再备份一次。
        current_paths = [
            str(entry.original_path)
            for entry in entries
            if _is_plain_file(entry.original_path)
        ]
        if current_paths:
            try:
                pre_restore_backup = self.backup_files(current_paths)
                results.append(CheckResult(
                    name="restore.pre_backup",
                    ok=True,
                    code=CODE_OK,
                    message=f"恢复前已备份当前版本：{pre_restore_backup.backup_id}",
                    severity=SEVERITY_INFO,
                    details={"pre_backup_id": pre_restore_backup.backup_id},
                ))
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, ConfigManagerError)
                    else CODE_BACKUP_FAILED
                    if isinstance(exc, OSError)
                    else CODE_INTERNAL
                )
                message = (
                    exc.message
                    if isinstance(exc, ConfigManagerError)
                    else str(exc)
                    if isinstance(exc, OSError)
                    else type(exc).__name__
                )
                error_details = (
                    exc.details if isinstance(exc, ConfigManagerError) else {}
                )
                results.append(CheckResult(
                    name="restore.pre_backup",
                    ok=False,
                    code=code,
                    message=f"恢复前备份失败，已中止恢复：{message}",
                    severity=SEVERITY_ERROR,
                    details={
                        "error": error_details,
                        "error_type": type(exc).__name__,
                    },
                ))
                return results

        # 2. 在每个目标的同目录准备替换文件与原状回滚文件，不改动目标。
        stages: list[_RestoreStage] = []
        temporary_paths: set[Path] = set()
        try:
            for entry in entries:
                target = entry.original_path
                target.parent.mkdir(parents=True, exist_ok=True)
                original_existed = _is_plain_file(target)
                original_hash: str | None = None
                rollback_path: Path | None = None
                if original_existed:
                    original_hash = _sha256(target)
                    rollback_fd, rollback_name = tempfile.mkstemp(
                        prefix=".restore_rollback_",
                        suffix=target.suffix,
                        dir=str(target.parent),
                    )
                    os.close(rollback_fd)
                    rollback_path = Path(rollback_name)
                    temporary_paths.add(rollback_path)
                    shutil.copy2(target, rollback_path)
                    if _sha256(rollback_path) != original_hash:
                        raise ConfigManagerError(
                            CODE_HASH_MISMATCH,
                            f"恢复前回滚副本哈希不符：{target}",
                        )

                replacement_fd, replacement_name = tempfile.mkstemp(
                    prefix=".restore_new_",
                    suffix=target.suffix,
                    dir=str(target.parent),
                )
                os.close(replacement_fd)
                replacement_path = Path(replacement_name)
                temporary_paths.add(replacement_path)
                shutil.copy2(entry.backup_path, replacement_path)
                if _sha256(replacement_path).casefold() != entry.sha256:
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"恢复暂存副本哈希不符：{target}",
                    )
                stages.append(_RestoreStage(
                    entry=entry,
                    replacement_path=replacement_path,
                    rollback_path=rollback_path,
                    original_existed=original_existed,
                    original_hash=original_hash,
                ))
        except Exception as exc:
            for temporary in temporary_paths:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            code = (
                exc.code
                if isinstance(exc, ConfigManagerError)
                else CODE_ATOMIC_REPLACE_FAILED
                if isinstance(exc, OSError)
                else CODE_INTERNAL
            )
            message = (
                exc.message
                if isinstance(exc, ConfigManagerError)
                else str(exc)
                if isinstance(exc, OSError)
                else type(exc).__name__
            )
            results.extend([
                CheckResult(
                    name="restore.stage",
                    ok=False,
                    code=code,
                    message=f"恢复暂存失败，目标未修改：{message}",
                    severity=SEVERITY_ERROR,
                    details={"backup_id": backup_info.backup_id},
                ),
                CheckResult(
                    name="restore.summary",
                    ok=False,
                    code=code,
                    message="未恢复任何文件",
                    severity=SEVERITY_ERROR,
                    details={"backup_id": backup_info.backup_id},
                ),
            ])
            return results

        # 3. 再次核对目标未被并发修改，然后逐项原子提交并做写后验证。
        commit_error: Exception | None = None
        try:
            for stage in stages:
                target = stage.entry.original_path
                if stage.original_existed:
                    if (
                        not _is_plain_file(target)
                        or _sha256(target) != stage.original_hash
                    ):
                        raise ConfigManagerError(
                            CODE_ATOMIC_REPLACE_FAILED,
                            f"恢复目标在提交前已被修改：{target}",
                        )
                elif os.path.lexists(target):
                    raise ConfigManagerError(
                        CODE_ATOMIC_REPLACE_FAILED,
                        f"恢复目标在提交前已出现：{target}",
                    )
                os.replace(stage.replacement_path, target)
                temporary_paths.discard(stage.replacement_path)
                stage.committed = True
                actual_hash = _sha256(target)
                if actual_hash.casefold() != stage.entry.sha256:
                    raise ConfigManagerError(
                        CODE_HASH_MISMATCH,
                        f"恢复后哈希不符：{target}",
                    )
        except Exception as exc:
            commit_error = exc

        if commit_error is not None:
            rollback_errors: list[str] = []
            for stage in reversed(stages):
                if not stage.committed:
                    continue
                target = stage.entry.original_path
                try:
                    if stage.original_existed:
                        if stage.rollback_path is None:
                            raise OSError("缺少回滚暂存文件")
                        if not _is_plain_file(target):
                            raise OSError("提交后的目标不是普通文件")
                        current_hash = _sha256(target).casefold()
                        if current_hash == str(stage.original_hash).casefold():
                            stage.committed = False
                            continue
                        if current_hash != stage.entry.sha256:
                            raise OSError("提交后的目标又被外部修改")
                        if (
                            not _is_plain_file(stage.rollback_path)
                            or _sha256(stage.rollback_path).casefold()
                            != str(stage.original_hash).casefold()
                        ):
                            raise OSError("回滚副本完整性校验失败")
                        os.replace(stage.rollback_path, target)
                        temporary_paths.discard(stage.rollback_path)
                        if _sha256(target) != stage.original_hash:
                            raise OSError("回滚后哈希不符")
                        stage.committed = False
                    else:
                        if os.path.lexists(target):
                            if (
                                not _is_plain_file(target)
                                or _sha256(target).casefold()
                                != stage.entry.sha256
                            ):
                                raise OSError("本轮新建目标又被外部修改")
                            target.unlink()
                        if os.path.lexists(target):
                            raise OSError("无法移除本轮新建目标")
                        stage.committed = False
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f"{stage.entry.original_name}: {type(rollback_exc).__name__}"
                    )

            code = (
                commit_error.code
                if isinstance(commit_error, ConfigManagerError)
                else CODE_ATOMIC_REPLACE_FAILED
            )
            message = (
                commit_error.message
                if isinstance(commit_error, ConfigManagerError)
                else str(commit_error)
            )
            results.append(CheckResult(
                name="restore.commit",
                ok=False,
                code=code,
                message=f"恢复提交失败：{message}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_info.backup_id},
            ))
            results.append(CheckResult(
                name="restore.rollback",
                ok=not rollback_errors,
                code=CODE_OK if not rollback_errors else CODE_ATOMIC_REPLACE_FAILED,
                message=(
                    "已恢复提交前状态"
                    if not rollback_errors
                    else "部分目标无法自动回滚"
                ),
                severity=SEVERITY_INFO if not rollback_errors else SEVERITY_ERROR,
                details={"errors": rollback_errors},
            ))
            results.append(CheckResult(
                name="restore.summary",
                ok=False,
                code=code,
                message="恢复未完成",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_info.backup_id},
            ))
        else:
            for stage in stages:
                results.append(CheckResult(
                    name=f"restore.{stage.entry.original_name}",
                    ok=True,
                    code=CODE_OK,
                    message=f"已恢复：{stage.entry.original_path}",
                    severity=SEVERITY_INFO,
                    details={"path": str(stage.entry.original_path)},
                ))
            results.append(CheckResult(
                name="restore.summary",
                ok=True,
                code=CODE_OK,
                message="恢复完成",
                severity=SEVERITY_INFO,
                details={"backup_id": backup_info.backup_id},
            ))

        # 成功或已完成回滚后清理尚存暂存文件；回滚失败的副本会保留供人工恢复。
        preserve = {
            stage.rollback_path
            for stage in stages
            if stage.committed
            and stage.original_existed
            and stage.rollback_path is not None
            and stage.rollback_path.exists()
            and commit_error is not None
        }
        for temporary in temporary_paths - preserve:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return results

    # ================================================================== #
    # 内部辅助                                                            #
    # ================================================================== #

    def _software_search_roots(
        self,
        options: Mapping[str, object],
    ) -> tuple[list[Path], list[str]]:
        """解析受限搜索根目录；拒绝整个磁盘根目录。"""
        raw_roots = options.get("search_roots", [])
        if not isinstance(raw_roots, (list, tuple)):
            return [], ["search_roots 必须是路径数组"]
        roots: list[Path] = []
        issues: list[str] = []
        seen: set[str] = set()
        for raw_root in raw_roots:
            if not isinstance(raw_root, str) or not raw_root.strip():
                issues.append("忽略空白或非字符串搜索根目录")
                continue
            expanded = os.path.expandvars(os.path.expanduser(raw_root.strip()))
            root = Path(expanded)
            if not root.is_absolute():
                issues.append(f"搜索根目录不是绝对路径：{raw_root}")
                continue
            normalized = _normalized_path(root)
            anchor = _normalized_path(Path(root.anchor)) if root.anchor else ""
            if normalized == anchor:
                issues.append(f"拒绝扫描整个磁盘根目录：{root}")
                continue
            if not root.is_dir():
                issues.append(f"搜索根目录不存在：{root}")
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            roots.append(root)
        return roots, issues

    def _index_software_candidates(
        self,
        roots: Sequence[Path],
        filenames: set[str],
        *,
        max_depth: int,
        max_directories: int,
    ) -> tuple[dict[str, list[Path]], list[str], bool]:
        """一次扫描建立候选索引，避免每种软件重复遍历目录。"""
        index: dict[str, list[Path]] = {name: [] for name in filenames}
        issues: list[str] = []
        visited_directories = 0
        scan_complete = True
        skipped_names = {
            ".git", "__pycache__", "$recycle.bin", "system volume information",
            "cache", "logs", "debug", "config_backup", "update_temp",
        }

        def on_error(exc: OSError) -> None:
            nonlocal scan_complete
            scan_complete = False
            issues.append(f"无法读取目录：{getattr(exc, 'filename', '')}（{exc}）")

        stop_scan = False
        for root in roots:
            if stop_scan:
                break
            for current_dir, dir_names, file_names in os.walk(
                root, topdown=True, onerror=on_error, followlinks=False,
            ):
                visited_directories += 1
                if visited_directories > max_directories:
                    scan_complete = False
                    issues.append(
                        f"搜索目录数超过上限 {max_directories}，已停止扫描"
                    )
                    stop_scan = True
                    break
                current_path = Path(current_dir)
                try:
                    depth = len(current_path.relative_to(root).parts)
                except ValueError:
                    scan_complete = False
                    issues.append(f"搜索目录逸出根目录：{current_path}")
                    dir_names[:] = []
                    continue
                dir_names[:] = [
                    name for name in dir_names
                    if name.casefold() not in skipped_names
                    and not (current_path / name).is_symlink()
                ]
                if depth >= max_depth:
                    dir_names[:] = []
                for file_name in file_names:
                    key = file_name.casefold()
                    if key in filenames:
                        index.setdefault(key, []).append(current_path / file_name)

        for key, values in tuple(index.items()):
            index[key] = _deduplicate_paths(values)
        return index, issues, scan_complete

    @staticmethod
    def _software_candidate_is_valid(
        spec: Mapping[str, object],
        candidate: Path,
    ) -> bool:
        """以伴随文件辨识候选，避免只凭同名 exe 自动改址。"""
        if not candidate.is_file():
            return False
        if candidate.name.casefold() != str(spec["filename"]).casefold():
            return False
        parent = candidate.parent
        kind = str(spec.get("kind", ""))
        if kind == "unity_game":
            return (parent / f"{candidate.stem}_Data").is_dir()
        if kind == "maa":
            has_config = any(
                (parent / "config" / name).is_file()
                for name in ("gui.new.json", "gui.json")
            )
            return bool(
                (parent / "MAA.dll").is_file()
                and (parent / "MAA.runtimeconfig.json").is_file()
                and (parent / "resource").is_dir()
                and has_config
            )
        if kind == "maaend":
            return bool(
                (parent / "interface.json").is_file()
                and (parent / "resource").is_dir()
                and (parent / "config" / "mxu-MaaEnd.json").is_file()
            )
        if kind == "aalc":
            return bool(
                (parent / "config.yaml").is_file()
                and (parent / "assets").is_dir()
                and (parent / "_internal").is_dir()
            )
        return False

    def _software_linked_updates(
        self,
        spec: Mapping[str, object],
        candidate: Path,
        cfg: Mapping[str, object],
    ) -> dict[tuple[str, ...], str]:
        """由已验证候选导出程序与所有必须同步的路径字段。"""
        executable_keys = tuple(spec["executable_path"])  # type: ignore[arg-type]
        updates: dict[tuple[str, ...], str] = {
            executable_keys: str(candidate),
        }
        software_id = str(spec["software_id"])
        parent = candidate.parent

        if software_id == "maa":
            config_dir_keys = ("games", "arknights", "helper", "config_dir")
            config_file_keys = ("games", "arknights", "helper", "config_file")
            config_dir = parent / "config"
            config_name = (
                "gui.new.json"
                if (config_dir / "gui.new.json").is_file()
                else "gui.json"
            )
            configured_file = _get_nested(cfg, config_file_keys)
            updates[config_dir_keys] = str(config_dir)
            updates[config_file_keys] = _same_path_style(
                configured_file, config_dir / config_name, config_name,
            )
        elif software_id == "maaend":
            config_dir_keys = ("games", "endfield", "helper", "config_dir")
            config_file_keys = ("games", "endfield", "helper", "config_file")
            evidence_keys = (
                "games", "endfield", "helper", "task_evidence_log",
            )
            config_dir = parent / "config"
            config_name = "mxu-MaaEnd.json"
            configured_file = _get_nested(cfg, config_file_keys)
            updates[config_dir_keys] = str(config_dir)
            updates[config_file_keys] = _same_path_style(
                configured_file, config_dir / config_name, config_name,
            )
            updates[evidence_keys] = str(parent / "debug" / "maafw.log")
        elif software_id == "aalc":
            updates[("games", "limbus", "helper", "config_file")] = str(
                parent / "config.yaml"
            )
        elif software_id == "limbus_game":
            updates[("games", "limbus", "launch", "fallback_executable")] = str(
                candidate
            )
            updates[("games", "limbus", "helper", "game_path_current")] = str(
                candidate
            )
            updates[("games", "limbus", "helper", "game_path_correct")] = str(
                candidate
            )
        return updates

    @staticmethod
    def _software_updates_differ(
        cfg: Mapping[str, object],
        updates: Mapping[tuple[str, ...], str],
    ) -> bool:
        return any(
            not _path_values_equal(_get_nested(cfg, keys), value)
            for keys, value in updates.items()
        )

    def _check_path_exists(
        self, name: str, value: object, *, required: bool, is_dir: bool = False,
    ) -> list[CheckResult]:
        if not value or not isinstance(value, str):
            return [CheckResult(
                name=name,
                ok=False,
                code=CODE_KEY_MISSING,
                message=f"缺少路径配置：{name}",
                severity=SEVERITY_ERROR if required else SEVERITY_WARNING,
                details={},
            )]
        p = Path(value)
        exists = p.is_dir() if is_dir else p.is_file()
        if exists:
            return [CheckResult(
                name=name,
                ok=True,
                code=CODE_OK,
                message=f"路径存在：{p}",
                severity=SEVERITY_INFO,
                details={"path": str(p)},
            )]
        return [CheckResult(
            name=name,
            ok=False,
            code=CODE_FILE_NOT_FOUND,
            message=f"路径不存在：{p}（{('目录' if is_dir else '文件')}）",
            severity=SEVERITY_ERROR if required else SEVERITY_WARNING,
            details={"path": str(p), "is_dir": is_dir},
        )]

    def _maaend_active_instance_state(
        self,
        helper: Mapping[str, object],
    ) -> tuple[bool, dict[str, object]]:
        """检查当前活动实例是否具备名称且使用 Win32 控制器。"""
        cfg_dir = helper.get("config_dir")
        cfg_file = helper.get("config_file")
        details: dict[str, object] = {
            "active_instance_found": False,
            "active_instance_named": False,
            "win32_controller": False,
        }
        if not (cfg_dir and cfg_file):
            return False, details
        path = Path(str(cfg_dir)) / str(cfg_file)  # type: ignore[arg-type]
        if not path.is_file():
            return False, details
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False, details
        if not isinstance(data, dict):
            return False, details
        instance_id = data.get("lastActiveInstanceId")
        if not isinstance(instance_id, str) or not instance_id:
            return False, details
        instances = data.get("instances", [])
        if not isinstance(instances, list):
            return False, details
        for inst in instances:
            if isinstance(inst, dict) and inst.get("id") == instance_id:
                details["active_instance_found"] = True
                name = inst.get("name")
                controller = inst.get("controllerName")
                details["active_instance_named"] = isinstance(name, str) and bool(name.strip())
                details["win32_controller"] = (
                    isinstance(controller, str) and controller.lower().startswith("win32")
                )
                return bool(
                    details["active_instance_named"] and details["win32_controller"]
                ), details
        return False, details

    @staticmethod
    def _resolve_proposed_value(
        spec: Mapping[str, object],
        cfg: Mapping[str, object],
    ) -> object:
        configured_path = spec.get("proposed_config_path")
        if configured_path is not None:
            keys = tuple(configured_path)  # type: ignore[arg-type]
            value = _get_nested(cfg, keys)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ConfigManagerError(
                    CODE_KEY_MISSING,
                    f"建議值來源不存在：{'.'.join(keys)}",
                    {"config_path": list(keys)},
                )
            return value
        if "proposed" not in spec:
            raise ConfigManagerError(
                CODE_KEY_MISSING,
                f"變更 {spec.get('change_id', '')} 缺少建議值",
                {"change_id": spec.get("change_id", "")},
            )
        return spec["proposed"]

    def _read_current_value(self, spec: Mapping[str, object], cfg: Mapping[str, Any]) -> str:
        """读取某变更的当前值（尽量从实际文件读；失败回“未读取”）。"""
        try:
            if "json_pointer" in spec:
                target = self._resolve_target_file(spec, cfg)
                if not target.is_file():
                    return "<未读取：文件不存在>"
                with open(target, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                keys = list(spec["json_pointer"])  # type: ignore[arg-type]
                # 替换 __current__ 占位
                cur = self._maa_current_config_name(target)
                resolved_keys = [cur if k == "__current__" else k for k in keys]
                node: object = data
                for k in resolved_keys:
                    if isinstance(node, Mapping) and k in node:
                        node = node[k]
                    else:
                        return "<未读取：键不存在>"
                return _value_to_display(node)
            if "yaml_key" in spec:
                target = self._resolve_target_file(spec, cfg)
                if not target.is_file():
                    return "<未读取：文件不存在>"
                with open(target, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines(keepends=True)
                occ = _find_scalar_key_lines(lines, str(spec["yaml_key"]))
                if len(occ) != 1 or occ[0][3]:
                    return "<未读取：键缺失/重复/跨行>"
                _, _, value_span, _ = occ[0]
                return lines[occ[0][0]][value_span[0]:value_span[1]].rstrip()
        except (OSError, json.JSONDecodeError):
            return "<未读取：解析失败>"
        return "<未读取>"

    def _maa_current_config_name(self, gui_json_path: Path) -> str:
        try:
            with open(gui_json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            cur = data.get("Current")
            if isinstance(cur, str) and cur:
                return cur
        except (OSError, json.JSONDecodeError):
            pass
        return "Default"

    def _resolve_target_file(self, spec: Mapping[str, object], cfg: Mapping[str, Any]) -> Path:
        """根据 spec 的 file_key 与配置解析出实际目标文件路径。"""
        fk = str(spec.get("file_key", ""))
        games = cfg.get("games", {})
        if fk == "maa_gui":
            helper = games["arknights"]["helper"]
            configured = helper.get("config_file")
            if configured:
                configured_path = Path(str(configured))
                if configured_path.is_absolute():
                    return configured_path
                config_dir = helper.get("config_dir")
                if config_dir:
                    return Path(str(config_dir)) / configured_path
            maa_dir = Path(str(helper["executable"])).parent / "config"
            preferred = maa_dir / "gui.new.json"
            return preferred if preferred.is_file() else maa_dir / "gui.json"
        if fk == "maaend_mxu":
            helper = games["endfield"]["helper"]
            return Path(str(helper["config_dir"])) / str(helper["config_file"])
        if fk == "aalc_yaml":
            return Path(str(games["limbus"]["helper"]["config_file"]))
        raise ConfigManagerError(CODE_AMBIGUOUS, f"未知的 file_key：{fk}", {"file_key": fk})

    def _collect_target_files(self, specs: Iterable[dict[str, object]], cfg: Mapping[str, Any]) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for spec in specs:
            p = self._resolve_target_file(spec, cfg)
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
        return out

    def _apply_specs_to_one_file(
        self, target: Path, specs: list[dict[str, object]], cfg: Mapping[str, Any],
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        if not target.is_file():
            for spec in specs:
                results.append(CheckResult(
                    name=f"apply.{spec['change_id']}",
                    ok=False,
                    code=CODE_FILE_NOT_FOUND,
                    message=f"目标文件不存在：{target}",
                    severity=SEVERITY_ERROR,
                    details={"change_id": spec["change_id"]},
                ))
            return results

        is_json = target.suffix.lower() == ".json"
        is_yaml = target.suffix.lower() in (".yaml", ".yml")
        if is_json:
            updates: dict[str, object] = {}
            for spec in specs:
                keys = list(spec["json_pointer"])  # type: ignore[arg-type]
                cur = self._maa_current_config_name(target)
                resolved = [cur if k == "__current__" else k for k in keys]
                dotted = ".".join(resolved)
                updates[dotted] = self._resolve_proposed_value(spec, cfg)
            return self.patch_json(target, updates)
        if is_yaml:
            updates_yaml: dict[str, str] = {}
            multiline_repairs: set[str] = set()
            for spec in specs:
                yaml_key = str(spec["yaml_key"])
                updates_yaml[yaml_key] = str(
                    self._resolve_proposed_value(spec, cfg)
                )
                if spec.get("yaml_multiline_repair") is True:
                    multiline_repairs.add(yaml_key)
            return self.patch_yaml_scalars(
                target,
                updates_yaml,
                _allow_multiline_repair=frozenset(multiline_repairs),
            )
        # 不支持的格式
        for spec in specs:
            results.append(CheckResult(
                name=f"apply.{spec['change_id']}",
                ok=False,
                code=CODE_NOT_IMPLEMENTED,
                message=f"不支持的文件格式：{target.suffix}",
                severity=SEVERITY_ERROR,
                details={},
            ))
        return results

    def _set_nested_existing(
        self, data: dict[str, Any], keys: list[str], value: object,
    ) -> tuple[bool, str, str]:
        """只更新已存在的键路径；不添加。返回 (ok, code, message)。"""
        node: object = data
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                return False, CODE_KEY_MISSING, f"键路径不存在：{'.'.join(keys)}"
            node = node[k]
        last = keys[-1]
        if not isinstance(node, dict) or last not in node:
            return False, CODE_KEY_MISSING, f"键不存在：{'.'.join(keys)}"
        node[last] = value
        return True, CODE_OK, f"已更新：{'.'.join(keys)}"

    def _verify_json_values(
        self, target: Path, updates: Mapping[str, object],
    ) -> tuple[bool, str]:
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"写入后 JSON 解析失败：{exc}"
        for dotted, value in updates.items():
            keys = dotted.split(".")
            node: object = data
            for k in keys:
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    return False, f"写入后找不到键：{dotted}"
            if node != value:
                return False, f"写入后值不符：{dotted}（预期 {value!r}，实际 {node!r}）"
        return True, "写入后验证通过"

    def _verify_yaml_values(
        self, target: Path, updates: Mapping[str, str],
    ) -> tuple[bool, str]:
        try:
            with open(target, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines(keepends=True)
        except OSError as exc:
            return False, f"写入后读取失败：{exc}"
        for key, expected in updates.items():
            occ = _find_scalar_key_lines(lines, key)
            if len(occ) != 1:
                return False, f"写入后键 {key} 出现次数异常：{len(occ)}"
            _, _, value_span, corrupt = occ[0]
            if corrupt:
                return False, f"写入后键 {key} 仍跨行/损坏"
            actual = lines[occ[0][0]][value_span[0]:value_span[1]].rstrip()
            if actual != expected:
                return False, f"写入后值不符：{key}（预期 {expected!r}，实际 {actual!r}）"
        return True, "写入后验证通过"

    def _find_backup_manifest(self, backup_id: str) -> tuple[Path, dict[str, Any] | None]:
        if (
            not _is_plain_directory(self._backup_root)
            or not isinstance(backup_id, str)
            or not _BACKUP_ID_RE.fullmatch(backup_id)
        ):
            return Path(), None
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for entry in self._backup_root.iterdir():
            if not _is_plain_directory(entry):
                continue
            mpath = entry / "manifest.json"
            if not _is_plain_file(mpath):
                continue
            try:
                with open(mpath, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                _validate_backup_manifest(
                    mpath,
                    m,
                    expected_backup_id=backup_id,
                    verify_payloads=False,
                )
            except (
                ConfigManagerError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue
            candidates.append((mpath, dict(m)))
        if len(candidates) == 1:
            return candidates[0]
        return Path(), None


# --------------------------------------------------------------------------- #
# YAML 标量键定位（无 PyYAML 的安全实现）                                      #
# --------------------------------------------------------------------------- #


# 一个“顶层或任意缩进的标量键”：行首为空白、键名、冒号、空白、值。
# 值不含 # 注释后缀时才算有边界。
_KEY_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_\-\.]+):(?P<rest>.*)$")


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _get_nested(data: Mapping[str, object], keys: Sequence[str]) -> object | None:
    node: object = data
    for key in keys:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _normalized_path(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value))).casefold()


def _path_values_equal(left: object, right: object) -> bool:
    if not isinstance(left, (str, os.PathLike)) or not isinstance(
        right, (str, os.PathLike),
    ):
        return left == right
    try:
        return _normalized_path(left) == _normalized_path(right)
    except (OSError, TypeError, ValueError):
        return str(left) == str(right)


def _deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        normalized = _normalized_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    result.sort(key=lambda path: _normalized_path(path))
    return result


def _same_path_style(
    configured_value: object,
    absolute_path: Path,
    relative_name: str,
) -> str:
    """原字段是相对路径时维持相对形式，避免无必要的格式变动。"""
    if isinstance(configured_value, str) and configured_value:
        try:
            if not Path(configured_value).is_absolute():
                return relative_name
        except (OSError, ValueError):
            pass
    return str(absolute_path)


def _find_scalar_key_lines(
    lines: Sequence[str], key: str,
) -> list[tuple[int, int, tuple[int, int], bool]]:
    """返回符合 key 的行信息：(行索引, 缩进长度, 值字符区间, 是否疑似损坏)。

    值字符区间为该行（去尾换行）内值的起止索引 [start, end)。
    若值疑似跨行（plain scalar 折行）、为空、或为容器，标记 corrupt=True，
    调用端应拒绝写入。
    """
    results: list[tuple[int, int, tuple[int, int], bool]] = []
    for idx, raw in enumerate(lines):
        # 去除行尾换行以利定位
        line = raw[:-1] if raw.endswith("\n") else raw
        m = _KEY_LINE_RE.match(line)
        if not m:
            continue
        if m.group("key") != key:
            continue
        indent = m.group("indent")
        indent_len = len(indent)
        rest = m.group("rest")
        # rest 开头应为空白或为空
        if rest == "":
            # 无值，可能是 mapping/sequence 容器或空值 -> 不确定
            results.append((idx, indent_len, (len(line), len(line)), True))
            continue
        if not rest.startswith(" "):
            # 例如 "key:value" 无空格，YAML 视为标量字符串 "key:value" 整行 -> 不视为键
            continue
        # 冒号位置 = indent_len + len(key)
        colon_pos = indent_len + len(key)
        # 从 colon_pos+1 开始找第一个非空白
        vstart = colon_pos + 1
        while vstart < len(line) and line[vstart] == " ":
            vstart += 1
        # 从 vstart 起的值（含可能的行尾注释）
        value_with_comment = line[vstart:]
        value_text_no_comment, _comment = _strip_yaml_trailing_comment(value_with_comment)
        value_text_no_comment = value_text_no_comment.rstrip()
        if value_text_no_comment == "":
            # 值为空 -> 不确定
            results.append((idx, indent_len, (len(line), len(line)), True))
            continue
        vend = vstart + len(value_text_no_comment)
        # 检测跨行 plain scalar：若下一行非空白且缩进大于本行，可能是续行
        corrupt = _looks_like_multiline_continuation(lines, idx, indent_len)
        results.append((idx, indent_len, (vstart, vend), corrupt))
    return results


def _strip_yaml_trailing_comment(value: str) -> tuple[str, str]:
    """分离行尾注释。简单处理：值中 '#' 前有空白视为注释起点。

    不处理引号内的 '#'（我们只处理标量纯值，不处理含特殊字符的复杂情况）。
    """
    # 找到 " #" 模式
    idx = value.find(" #")
    if idx == -1 and value.startswith("#"):
        return "", value
    if idx != -1:
        return value[:idx], value[idx:]
    return value, ""


def _looks_like_multiline_continuation(lines: Sequence[str], idx: int, indent: int) -> bool:
    """若下一非空行缩进严格大于本行，视为 plain scalar 可能续行（保守拒绝）。"""
    j = idx + 1
    while j < len(lines):
        nxt = lines[j].rstrip("\n")
        if nxt.strip() == "":
            j += 1
            continue
        # 若下一行像另一个键（行首缩进 <= 本行且有冒号），不算续行
        m = _KEY_LINE_RE.match(nxt)
        if m:
            return False
        # 若缩进大于本行且非注释，视为续行 -> 损坏风险
        stripped_leading = len(nxt) - len(nxt.lstrip(" "))
        if stripped_leading > indent and not nxt.lstrip().startswith("#"):
            return True
        return False
    return False


def _replace_multiline_plain_scalar(
    lines: list[str],
    line_idx: int,
    indent: int,
    value_span: tuple[int, int],
    new_value: str,
) -> list[str] | None:
    """在边界明确时把唯一的 YAML plain scalar 续行合并为一行。

    只接受紧邻、缩进更深、且不构成新键/列表的续行。这样可以修复 AALC
    把 Windows 路径自动折行的已知格式，同时仍拒绝一般映射、列表与块标量。
    """
    first_raw = lines[line_idx]
    first_line = first_raw.rstrip("\r\n")
    newline = first_raw[len(first_line):] or "\n"
    first_value = first_line[value_span[0]:value_span[1]].strip()
    if first_value.startswith(("|", ">", "[", "{")):
        return None

    continuation_indexes: list[int] = []
    preserved_comment = ""
    cursor = line_idx + 1
    while cursor < len(lines):
        raw = lines[cursor]
        line = raw.rstrip("\r\n")
        if not line.strip():
            break
        leading = len(line) - len(line.lstrip(" "))
        if leading <= indent or line.lstrip().startswith(("#", "-")):
            break
        if _KEY_LINE_RE.match(line):
            return None
        value_part, comment = _strip_yaml_trailing_comment(line.lstrip())
        if not value_part.strip():
            return None
        if preserved_comment and comment:
            return None
        if comment:
            preserved_comment = comment.rstrip()
        continuation_indexes.append(cursor)
        cursor += 1

    if not continuation_indexes:
        return None

    _first_without_comment, first_comment = _strip_yaml_trailing_comment(
        first_line[value_span[0]:],
    )
    if first_comment:
        if preserved_comment:
            return None
        preserved_comment = first_comment.rstrip()

    prefix = first_line[:value_span[0]]
    suffix = f"{preserved_comment}" if preserved_comment else ""
    replacement = f"{prefix}{new_value}{suffix}{newline}"
    repaired = list(lines)
    repaired[line_idx:continuation_indexes[-1] + 1] = [replacement]
    return repaired


def _value_to_display(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "True" if value else "False"
    return repr(value)


__all__ = [
    "CheckResult",
    "BackupInfo",
    "ConfigChange",
    "ConfigManager",
    "ConfigManagerError",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
]
