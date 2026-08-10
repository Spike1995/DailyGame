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
        "absolute_path": str(path.resolve()),
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

    # 軟體位址自查只處理可由檔名與伴隨檔案可靠辨識的程式。鷹角啟動器
    # 的按鈕自動化另有版本與簽章凍結條件，不能只憑路徑候選自動改址。
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
    # 軟體位址自查                                                        #
    # ------------------------------------------------------------------ #

    def reconcile_software_paths(self, *, apply: bool = False) -> list[CheckResult]:
        """檢查並在安全條件成立時修復軟體執行檔位址。

        自查由 ``software_path_self_check.enabled`` 明確開啟。既有執行檔與
        關聯欄位一致時不掃描、不寫入。需要修復時，只在設定的搜尋根目錄內
        做有限深度搜尋，並以軟體伴隨檔案辨識候選。只有完整掃描得到唯一候選
        時才允許自動套用；零候選、多候選或掃描不完整一律維持原設定。

        ``apply=False`` 永遠唯讀，用於 ``preflight`` 與 ``--dry-run``。
        ``apply=True`` 仍受 ``auto_repair_unique_match`` 控制。套用前會備份
        ``user.json``，之後以原子替換寫入並重新解析核對全部關聯欄位。
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

        states: list[dict[str, object]] = []
        filenames_to_scan: set[str] = set()
        for spec in self._SOFTWARE_PATH_SPECS:
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
                        f"{display_name} 位址需要修復，但搜尋未完整完成；"
                        "為避免選錯程式，已保留原設定"
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
                        f"找不到可驗證的 {display_name}；請安裝軟體或把安裝目錄"
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
                        f"找到 {len(candidates)} 個可驗證的 {display_name}，"
                        "無法安全判定應使用哪一個"
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
                # 這個分支只會出現在掃描與狀態判斷間檔案狀態改變時。
                continue
            if not apply or not auto_repair:
                policy_note = (
                    "；自動修復目前未啟用"
                    if apply and not auto_repair
                    else ""
                )
                results.append(CheckResult(
                    name=f"software_paths.{software_id}",
                    ok=False,
                    code=CODE_SOFTWARE_PATH_CHANGE_REQUIRED,
                    message=(
                        f"{display_name} 已找到唯一可信位址 {candidate}，"
                        f"需要更新 {len(changed_fields)} 個關聯欄位{policy_note}"
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
                message="軟體位址關聯欄位出現衝突，已拒絕寫入",
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
                message="軟體位址關聯欄位不存在，已拒絕寫入",
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
                message=f"軟體位址寫入前備份失敗，已中止：{exc}",
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
                raise ValueError("寫入後設定根層級不是物件")
            mismatches = [
                ".".join(keys)
                for keys, value in merged_updates.items()
                if not _path_values_equal(_get_nested(verified_cfg, keys), value)
            ]
            if mismatches:
                raise ValueError(f"寫入後欄位不符：{mismatches}")
        except Exception as exc:
            rollback_error = ""
            try:
                _atomic_write_text(self._config_path, original_text)
                self._config_cache = cfg
            except Exception as rollback_exc:  # pragma: no cover - 極端磁碟故障
                rollback_error = f"；回復原設定也失敗：{rollback_exc}"
                self._config_cache = None
            results.append(CheckResult(
                name="software_paths.write",
                ok=False,
                code=CODE_SOFTWARE_PATH_WRITE_FAILED,
                message=f"軟體位址寫入或驗證失敗：{exc}{rollback_error}",
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
                    f"已自動更新 {spec['display_name']} 位址與關聯設定，"  # type: ignore[index]
                    "管理員排程將依新設定重新核對"
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
        - 在任何写入前，统一为所有涉及的目标文件创建一份备份。
        - 应用后重新解析验证；任一验证失败则记录原因并尝试恢复该文件。
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

        if not selected:
            return results

        # 2. 任何写入前统一备份所有涉及文件
        cfg = self.load()
        target_files = self._collect_target_files(selected, cfg)
        try:
            backup = self.backup_files([str(p) for p in target_files])
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

        # 3. 逐个文件应用
        # 先按文件分组变更，避免重复读写同一个文件
        files_to_specs: dict[Path, list[dict[str, object]]] = {}
        for spec in selected:
            p = self._resolve_target_file(spec, cfg)
            files_to_specs.setdefault(p, []).append(spec)

        for target, file_specs in files_to_specs.items():
            file_results = self._apply_specs_to_one_file(target, file_specs, cfg)
            results.extend(file_results)

        return results

    # ------------------------------------------------------------------ #
    # 备份                                                                #
    # ------------------------------------------------------------------ #

    def backup_files(self, paths: Sequence[str | os.PathLike[str]]) -> BackupInfo:
        """把指定文件复制到 backup_root/<时间戳>/，并生成列表。

        列表记录原始绝对路径、SHA-256、大小、修改时间。
        任一文件不存在或读取失败则整体失败（不创建部分备份）。
        """
        src_paths = [_coerce_path(p) for p in paths]
        # 先全部验证存在
        for p in src_paths:
            if not p.is_file():
                raise ConfigManagerError(
                    CODE_FILE_NOT_FOUND,
                    f"待备份文件不存在：{p}",
                    {"path": str(p)},
                )

        backup_id = secrets.token_hex(4)
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        # 时间戳目录 + 短随机后缀避免同秒冲突
        dest_dir = self._backup_root / f"{ts}_{backup_id}"
        dest_dir.mkdir(parents=True, exist_ok=False)

        manifest_entries: list[dict[str, object]] = []
        copied: list[Path] = []
        try:
            for idx, p in enumerate(src_paths):
                meta = _file_meta(p)
                # 为避免文件名冲突，在原名之前添加序号前缀
                safe_name = f"{idx:03d}_{p.name}"
                dest = dest_dir / safe_name
                shutil.copy2(p, dest)
                copied.append(dest)
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
                    "original_absolute_path": str(p.resolve()),
                    "original_name": p.name,
                    "backup_name": safe_name,
                    **meta,
                })
        except Exception:
            # 清理已创建的备份目录，避免部分备份
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise

        manifest = {
            "backup_id": backup_id,
            "created_at": _now_iso(),
            "source_root_hint": str(self._backup_root),
            "source_count": len(src_paths),
            "entries": manifest_entries,
        }
        manifest_path = dest_dir / "manifest.json"
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))
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
        if not target.is_file():
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

        if not changed:
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
        if not target.is_file():
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

        if not any_applied:
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
        if not self._backup_root.is_dir():
            return infos
        for entry in sorted(self._backup_root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            infos.append(BackupInfo(
                backup_id=str(m.get("backup_id", entry.name)),
                created_at=str(m.get("created_at", "")),
                manifest_path=manifest,
                source_count=int(m.get("source_count", 0)),
            ))
        return infos

    def restore_backup(self, backup_id: str) -> list[CheckResult]:
        """恢复指定备份。

        恢复前先再次备份各文件的当前版本（避免部分恢复后无法回退），
        然后逐个文件核对哈希，并从备份复制回原路径。
        """
        # 找到对应备份目录
        manifest_path, manifest = self._find_backup_manifest(backup_id)
        if manifest is None:
            return [CheckResult(
                name="restore",
                ok=False,
                code=CODE_FILE_NOT_FOUND,
                message=f"找不到备份：{backup_id}",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_id},
            )]

        entries = manifest.get("entries", [])
        if not entries:
            return [CheckResult(
                name="restore",
                ok=False,
                code=CODE_AMBIGUOUS,
                message="备份列表为空，无法恢复",
                severity=SEVERITY_ERROR,
                details={"backup_id": backup_id},
            )]

        results: list[CheckResult] = []

        # 1. 恢复前先把“当前版本”再备份一次（仅针对仍存在的文件）
        current_paths: list[str] = []
        backup_dir = manifest_path.parent
        for e in entries:
            orig = e.get("original_absolute_path")
            if orig and Path(orig).is_file():
                current_paths.append(orig)
        pre_restore_backup: BackupInfo | None = None
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
            except ConfigManagerError as exc:
                results.append(CheckResult(
                    name="restore.pre_backup",
                    ok=False,
                    code=exc.code,
                    message=f"恢复前备份失败，已中止恢复：{exc.message}",
                    severity=SEVERITY_ERROR,
                    details={"error": exc.details},
                ))
                return results

        # 2. 逐个文件核对备份哈希并复制回原路径
        restored_any = False
        for e in entries:
            orig = Path(str(e["original_absolute_path"]))
            backup_file = backup_dir / str(e["backup_name"])
            expected_hash = str(e["sha256"])
            name = f"restore.{e.get('original_name', orig.name)}"

            if not backup_file.is_file():
                results.append(CheckResult(
                    name=name,
                    ok=False,
                    code=CODE_FILE_NOT_FOUND,
                    message=f"备份文件缺失：{backup_file}",
                    severity=SEVERITY_ERROR,
                    details={},
                ))
                continue
            actual_hash = _sha256(backup_file)
            if actual_hash != expected_hash:
                results.append(CheckResult(
                    name=name,
                    ok=False,
                    code=CODE_HASH_MISMATCH,
                    message=f"备份文件哈希不符：{backup_file}",
                    severity=SEVERITY_ERROR,
                    details={"expected": expected_hash, "actual": actual_hash},
                ))
                continue

            # 确保父目录存在后原子复制
            try:
                orig.parent.mkdir(parents=True, exist_ok=True)
                # 复制到暂存再替换
                fd, tmp_name = tempfile.mkstemp(prefix=".restore_", dir=str(orig.parent))
                tmp_path = Path(tmp_name)
                os.close(fd)
                shutil.copy2(backup_file, tmp_path)
                os.replace(tmp_path, orig)
            except OSError as exc:
                results.append(CheckResult(
                    name=name,
                    ok=False,
                    code=CODE_ATOMIC_REPLACE_FAILED,
                    message=f"恢复写入失败：{exc}",
                    severity=SEVERITY_ERROR,
                    details={},
                ))
                continue

            # 复制后再次核对
            after_hash = _sha256(orig)
            if after_hash != expected_hash:
                results.append(CheckResult(
                    name=name,
                    ok=False,
                    code=CODE_HASH_MISMATCH,
                    message=f"恢复后哈希不符：{orig}",
                    severity=SEVERITY_ERROR,
                    details={"expected": expected_hash, "actual": after_hash},
                ))
                continue

            restored_any = True
            results.append(CheckResult(
                name=name,
                ok=True,
                code=CODE_OK,
                message=f"已恢复：{orig}",
                severity=SEVERITY_INFO,
                details={"path": str(orig)},
            ))

        results.append(CheckResult(
            name="restore.summary",
            ok=restored_any,
            code=CODE_OK if restored_any else CODE_AMBIGUOUS,
            message=("恢复完成" if restored_any else "未恢复任何文件"),
            severity=SEVERITY_INFO if restored_any else SEVERITY_WARNING,
            details={"backup_id": backup_id},
        ))
        return results

    # ================================================================== #
    # 内部辅助                                                            #
    # ================================================================== #

    def _software_search_roots(
        self,
        options: Mapping[str, object],
    ) -> tuple[list[Path], list[str]]:
        """解析受限搜尋根目錄；拒絕整個磁碟根目錄。"""
        raw_roots = options.get("search_roots", [])
        if not isinstance(raw_roots, (list, tuple)):
            return [], ["search_roots 必須是路徑陣列"]
        roots: list[Path] = []
        issues: list[str] = []
        seen: set[str] = set()
        for raw_root in raw_roots:
            if not isinstance(raw_root, str) or not raw_root.strip():
                issues.append("忽略空白或非字串搜尋根目錄")
                continue
            expanded = os.path.expandvars(os.path.expanduser(raw_root.strip()))
            root = Path(expanded)
            if not root.is_absolute():
                issues.append(f"搜尋根目錄不是絕對路徑：{raw_root}")
                continue
            normalized = _normalized_path(root)
            anchor = _normalized_path(Path(root.anchor)) if root.anchor else ""
            if normalized == anchor:
                issues.append(f"拒絕掃描整個磁碟根目錄：{root}")
                continue
            if not root.is_dir():
                issues.append(f"搜尋根目錄不存在：{root}")
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
        """一次掃描建立候選索引，避免每種軟體重複走訪目錄。"""
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
            issues.append(f"無法讀取目錄：{getattr(exc, 'filename', '')}（{exc}）")

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
                        f"搜尋目錄數超過上限 {max_directories}，已停止掃描"
                    )
                    stop_scan = True
                    break
                current_path = Path(current_dir)
                try:
                    depth = len(current_path.relative_to(root).parts)
                except ValueError:
                    scan_complete = False
                    issues.append(f"搜尋目錄逸出根目錄：{current_path}")
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
        """以伴隨檔案辨識候選，避免只憑同名 exe 自動改址。"""
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
        """由已驗證候選導出執行檔與所有必須同步的路徑欄位。"""
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
        if not self._backup_root.is_dir():
            return Path(), None
        # 先尝试目录名后缀比较（格式 <ts>_<id>）
        candidates: list[Path] = []
        for entry in self._backup_root.iterdir():
            if entry.is_dir() and entry.name.endswith(f"_{backup_id}"):
                candidates.append(entry / "manifest.json")
        # 也比较 manifest 内 backup_id
        for entry in self._backup_root.iterdir():
            if entry.is_dir():
                mpath = entry / "manifest.json"
                if mpath.is_file() and mpath not in candidates:
                    candidates.append(mpath)
        for mpath in candidates:
            if not mpath.is_file():
                continue
            try:
                with open(mpath, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if str(m.get("backup_id")) == backup_id or mpath.parent.name.endswith(f"_{backup_id}"):
                return mpath, m
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
    """原欄位是相對路徑時維持相對形式，避免無必要的格式變動。"""
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
