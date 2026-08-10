"""config_manager 单元测试。

只操作临时目录，不接触真实 MAA / MaaEnd / AALC 配置。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from daily_games.config_manager import (
    CODE_CHANGE_DUPLICATE,
    CODE_CHANGE_NOT_VERIFIED,
    CODE_CHANGE_UNKNOWN,
    CODE_FILE_NOT_FOUND,
    CODE_HASH_MISMATCH,
    CODE_KEY_DUPLICATE,
    CODE_KEY_MISSING,
    CODE_OK,
    CODE_PARSE_FAILED,
    CODE_SOFTWARE_PATH_AMBIGUOUS,
    CODE_SOFTWARE_PATH_CHANGE_REQUIRED,
    CODE_SOFTWARE_PATH_NOT_FOUND,
    CODE_SOFTWARE_PATH_REPAIRED,
    CODE_VERIFY_AFTER_WRITE_FAILED,
    CODE_YAML_MULTILINE_OR_CORRUPT,
    ConfigManager,
    ConfigManagerError,
)


def _make_user_config(tmp: Path, games_override: dict | None = None) -> Path:
    """生成一份最小且可加载的 user.json，指向 tmp 下的虚构第三方配置。"""
    maa_exe = tmp / "MAA.exe"
    maa_exe.write_text("dummy")
    maaend_exe = tmp / "MaaEnd.exe"
    maaend_exe.write_text("dummy")
    aalc_exe = tmp / "AALC.exe"
    aalc_exe.write_text("dummy")
    arknights_exe = tmp / "Arknights.exe"
    arknights_exe.write_text("dummy")
    endfield_exe = tmp / "Endfield.exe"
    endfield_exe.write_text("dummy")
    limbus_exe = tmp / "LimbusCompany.exe"
    limbus_exe.write_text("dummy")
    launcher_exe = tmp / "Launcher.exe"
    launcher_exe.write_text("dummy")

    # 第三方配置文件
    maa_gui_dir = tmp / "maa_config"
    maa_gui_dir.mkdir()
    maa_gui = maa_gui_dir / "gui.new.json"
    maa_gui.write_text(json.dumps({
        "Current": "Default",
        "Configurations": {"Default": {"Gui": {
            "StartUpSettings": {"RunDirectly": False},
            "PostActions": "",
        }}},
    }, indent=2))

    maaend_cfg_dir = tmp / "maaend_config"
    maaend_cfg_dir.mkdir()
    maaend_mxu = maaend_cfg_dir / "mxu-MaaEnd.json"
    maaend_mxu.write_text(json.dumps({
        "instances": [{
            "id": "inst1", "name": "日常",
            "controllerName": "Win32-Front",
            "tasks": [{"taskName": "VisitFriends", "enabled": True}],
        }],
        "settings": {"autoRunOnLaunch": False, "hotkeys": {"startTasks": "F10"}},
        "lastActiveInstanceId": "inst1",
    }, indent=2))

    aalc_yaml = tmp / "aalc_config.yaml"
    aalc_yaml.write_text(
        "game_title_name: LimbusCompany\n"
        "game_path: D:\\\\Steam\\\\LimbusCompany.exe\n"
        "autostart: true\n"
        "check_update: true\n"
        "image_resource_sync: true\n"
        "after_completion_actions: []\n"
    )

    games = {
        "arknights": {
            "display_name": "明日方舟",
            "launcher": {"executable": str(launcher_exe), "args": ["--game=arknights"], "verified": True},
            "game_executable": str(arknights_exe),
            "game_process_name": "Arknights.exe",
            "helper": {
                "id": "maa", "executable": str(maa_exe), "process_name": "MAA.exe",
                "reuse_existing": False,
                "config_dir": str(maa_gui_dir), "config_file": "gui.new.json",
                "use_coordinate_click": False,
            },
        },
        "endfield": {
            "display_name": "终末地",
            "launcher": {"executable": str(launcher_exe), "args": ["--game=endfield"], "verified": True},
            "game_executable": str(endfield_exe),
            "game_process_name": "Endfield.exe",
            "game_window_title_regex": "Endfield",
            "game_window_title_verified": True,
            "helper": {
                "id": "maaend", "executable": str(maaend_exe), "process_name": "MaaEnd.exe",
                "reuse_existing": False,
                "config_dir": str(maaend_cfg_dir), "config_file": "mxu-MaaEnd.json",
                "instance_id": "inst1", "instance_name": "日常",
                "cli_autostart_verified": True,
                "quit_after_run_verified": True,
                "close_owned_game_after_tasks": True,
                "web_server_port": 12701, "web_server_port_do_not_use": True,
            },
        },
        "limbus": {
            "display_name": "边狱巴士",
            "launch": {
                "method": "steam_protocol", "steam_uri": "steam://run/1973530",
                "fallback_method": "direct_executable",
                "fallback_executable": str(limbus_exe), "fallback_verified": False,
            },
            "game_executable": str(limbus_exe),
            "game_process_name": "LimbusCompany.exe",
            "language": {
                "steam_manifest_language": "koreana",
                "supported_by_helper": ["zh_cn", "en_us"],
                "actual_language": "zh_cn", "user_confirmed_hanization": True,
                "verified": True,
            },
            "helper": {
                "id": "aalc", "executable": str(aalc_exe), "process_name": "AALC.exe",
                "reuse_existing": False, "config_file": str(aalc_yaml),
                "cli_args": ["start", "--exit", "6"],
                "cli_start_verified": True,
                "cli_exit_both_verified": True,
                "autostart_key": "autostart", "autostart_verified": True,
                "game_path_key": "game_path",
                "game_path_current": "C:\\old\\LimbusCompany.exe",
                "game_path_correct": "D:\\Steam\\LimbusCompany.exe",
                "game_path_verified": False,
            },
        },
    }
    if games_override:
        games.update(games_override)

    cfg = {
        "schema_version": 1,
        "execution": {"order": ["arknights", "endfield", "limbus"], "pause_on_failure": True},
        "paths": {"logs_dir": str(tmp / "logs"), "backup_root": str(tmp / "backups")},
        "timeouts": {"completion_s": 60},
        "games": games,
    }
    user_path = tmp / "user.json"
    user_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return user_path


def _enable_software_path_check(
    user_path: Path,
    search_root: Path,
    *software_ids: str,
) -> None:
    data = json.loads(user_path.read_text(encoding="utf-8"))
    data["software_path_self_check"] = {
        "enabled": True,
        "auto_repair_unique_match": True,
        "software_ids": list(software_ids),
        "search_roots": [str(search_root)],
        "max_depth": 3,
        "max_directories": 500,
    }
    user_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _make_valid_maa_install(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "resource").mkdir()
    (directory / "config").mkdir()
    (directory / "MAA.exe").write_text("exe")
    (directory / "MAA.dll").write_text("dll")
    (directory / "MAA.runtimeconfig.json").write_text("{}")
    (directory / "config" / "gui.new.json").write_text(
        json.dumps({
            "Current": "Default",
            "Configurations": {"Default": {"Gui": {
                "StartUpSettings": {"RunDirectly": True},
                "PostActions": "ExitArknights, ExitSelf",
            }}},
        }),
        encoding="utf-8",
    )
    return directory / "MAA.exe"


class TestLoad(unittest.TestCase):
    def test_load_ok(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "backups")
            data = cm.load()
            self.assertEqual(data["execution"]["order"], ["arknights", "endfield", "limbus"])

    def test_load_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            cm = ConfigManager(Path(td) / "nope.json", Path(td) / "b")
            with self.assertRaises(ConfigManagerError) as ctx:
                cm.load()
            self.assertEqual(ctx.exception.code, CODE_FILE_NOT_FOUND)

    def test_load_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = tmp / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            cm = ConfigManager(p, tmp / "b")
            with self.assertRaises(ConfigManagerError) as ctx:
                cm.load()
            self.assertEqual(ctx.exception.code, CODE_PARSE_FAILED)

    def test_load_non_object_root_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = tmp / "arr.json"
            p.write_text("[1,2,3]", encoding="utf-8")
            cm = ConfigManager(p, tmp / "b")
            with self.assertRaises(ConfigManagerError) as ctx:
                cm.load()
            self.assertEqual(ctx.exception.code, "type_mismatch")

    def test_load_cached(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            self.assertIs(cm.load(), cm.load())


class TestPreflight(unittest.TestCase):
    def test_preflight_reports_paths_and_one_shot_completion(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            results = cm.preflight()
            names = {r.name for r in results}
            self.assertIn("execution.order", names)
            # 所有必要的可执行文件应返回 ok
            ok_names = [r.name for r in results if r.ok]
            self.assertIn("games.arknights.game_executable", ok_names)
            self.assertIn("games.endfield.helper.executable", ok_names)
            self.assertIn("games.limbus.launch.fallback_executable", ok_names)
            completion_results = [r for r in results if r.name.endswith("one_shot_completion")]
            self.assertEqual(len(completion_results), 2)
            self.assertTrue(all(result.ok for result in completion_results))

    def test_preflight_missing_executable(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            # 删掉 MAA.exe
            (tmp / "MAA.exe").unlink()
            cm = ConfigManager(p, tmp / "b")
            results = cm.preflight()
            maa_results = [r for r in results if r.name == "games.arknights.helper.executable"]
            self.assertEqual(len(maa_results), 1)
            self.assertFalse(maa_results[0].ok)
            self.assertEqual(maa_results[0].code, CODE_FILE_NOT_FOUND)

    def test_preflight_resolves_relative_config_file(self):
        """config_file 为相对文件名时，应与 config_dir 合并检查存在性。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            results = cm.preflight()
            # endfield 的 mxu-MaaEnd.json 是相对文件名 + config_dir，应通过
            cfg_file_results = [r for r in results if r.name == "games.endfield.helper.config_file"]
            self.assertEqual(len(cfg_file_results), 1)
            self.assertTrue(cfg_file_results[0].ok, msg=cfg_file_results[0].message)

    def test_preflight_rejects_non_win32_actual_active_instance(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            mxu_path = tmp / "maaend_config" / "mxu-MaaEnd.json"
            data = json.loads(mxu_path.read_text(encoding="utf-8"))
            data["instances"][0]["controllerName"] = "ADB"
            mxu_path.write_text(json.dumps(data), encoding="utf-8")

            results = ConfigManager(p, tmp / "b").preflight()

            completion = next(
                r for r in results
                if r.name == "games.endfield.helper.one_shot_completion"
            )
            self.assertFalse(completion.ok)
            self.assertFalse(completion.details["win32_controller"])

    def test_preflight_rejects_unverified_aalc_one_shot_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            data = json.loads(p.read_text(encoding="utf-8"))
            data["games"]["limbus"]["helper"]["cli_args"] = [
                "start", "--exit", "5",
            ]
            p.write_text(json.dumps(data), encoding="utf-8")

            results = ConfigManager(p, tmp / "b").preflight()

            completion = next(
                r for r in results
                if r.name == "games.limbus.helper.one_shot_completion"
            )
            self.assertFalse(completion.ok)
            self.assertEqual(completion.code, "aalc_one_shot_unverified")


class TestSoftwarePathSelfCheck(unittest.TestCase):
    def test_read_only_check_reports_unique_candidate_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            configured_maa = tmp / "MAA.exe"
            configured_maa.unlink()
            search_root = tmp / "software"
            candidate = _make_valid_maa_install(search_root / "MAA-v7")
            _enable_software_path_check(user_path, search_root, "maa")
            before = user_path.read_bytes()

            results = ConfigManager(
                user_path, tmp / "backups",
            ).reconcile_software_paths(apply=False)

            self.assertEqual(len(results), 1)
            self.assertEqual(
                results[0].code, CODE_SOFTWARE_PATH_CHANGE_REQUIRED,
            )
            self.assertFalse(results[0].ok)
            self.assertEqual(results[0].details["candidate"], str(candidate))
            self.assertEqual(user_path.read_bytes(), before)
            self.assertFalse((tmp / "backups").exists())

    def test_unique_candidate_repairs_executable_and_linked_paths_with_backup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            (tmp / "MAA.exe").unlink()
            search_root = tmp / "software"
            install_dir = search_root / "MAA-v7"
            candidate = _make_valid_maa_install(install_dir)
            _enable_software_path_check(user_path, search_root, "maa")

            results = ConfigManager(
                user_path, tmp / "backups",
            ).reconcile_software_paths(apply=True)

            repaired = next(
                result for result in results
                if result.code == CODE_SOFTWARE_PATH_REPAIRED
            )
            self.assertTrue(repaired.ok)
            data = json.loads(user_path.read_text(encoding="utf-8"))
            helper = data["games"]["arknights"]["helper"]
            self.assertEqual(helper["executable"], str(candidate))
            self.assertEqual(helper["config_dir"], str(install_dir / "config"))
            self.assertEqual(helper["config_file"], "gui.new.json")
            backup_id = repaired.details["backup_id"]
            manifests = list((tmp / "backups").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["backup_id"], backup_id)
            self.assertEqual(
                manifest["entries"][0]["original_absolute_path"],
                str(user_path.resolve()),
            )

    def test_multiple_valid_candidates_are_ambiguous_and_never_written(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            (tmp / "MAA.exe").unlink()
            search_root = tmp / "software"
            _make_valid_maa_install(search_root / "MAA-v7-a")
            _make_valid_maa_install(search_root / "MAA-v7-b")
            _enable_software_path_check(user_path, search_root, "maa")
            before = user_path.read_bytes()

            results = ConfigManager(
                user_path, tmp / "backups",
            ).reconcile_software_paths(apply=True)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].code, CODE_SOFTWARE_PATH_AMBIGUOUS)
            self.assertFalse(results[0].ok)
            self.assertEqual(len(results[0].details["candidates"]), 2)
            self.assertEqual(user_path.read_bytes(), before)
            self.assertFalse((tmp / "backups").exists())

    def test_same_name_without_identity_files_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            (tmp / "MAA.exe").unlink()
            search_root = tmp / "software"
            fake_dir = search_root / "unrelated"
            fake_dir.mkdir(parents=True)
            (fake_dir / "MAA.exe").write_text("not MAA")
            _enable_software_path_check(user_path, search_root, "maa")

            results = ConfigManager(
                user_path, tmp / "backups",
            ).reconcile_software_paths(apply=True)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].code, CODE_SOFTWARE_PATH_NOT_FOUND)
            self.assertFalse(results[0].ok)
            self.assertFalse((tmp / "backups").exists())

    def test_drive_root_is_rejected_without_scanning_or_writing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            (tmp / "MAA.exe").unlink()
            drive_root = Path(tmp.anchor)
            _enable_software_path_check(user_path, drive_root, "maa")

            results = ConfigManager(
                user_path, tmp / "backups",
            ).reconcile_software_paths(apply=True)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].code, CODE_SOFTWARE_PATH_NOT_FOUND)
            self.assertTrue(any(
                "整个磁盘根目录" in issue
                for issue in results[0].details["scan_issues"]
            ))
            self.assertFalse((tmp / "backups").exists())

    def test_preflight_includes_read_only_candidate_diagnostic(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_path = _make_user_config(tmp)
            (tmp / "MAA.exe").unlink()
            search_root = tmp / "software"
            _make_valid_maa_install(search_root / "MAA-v7")
            _enable_software_path_check(user_path, search_root, "maa")

            results = ConfigManager(user_path, tmp / "backups").preflight()
            codes = {result.code for result in results}

            self.assertIn(CODE_SOFTWARE_PATH_CHANGE_REQUIRED, codes)
            self.assertIn(CODE_FILE_NOT_FOUND, codes)
            self.assertFalse((tmp / "backups").exists())


class TestPlanChanges(unittest.TestCase):
    def test_plan_returns_defined_changes_with_masked_values(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            changes = cm.plan_required_changes()
            ids = {c.change_id for c in changes}
            self.assertIn("maa.autorun", ids)
            self.assertIn("maa.post_actions", ids)
            self.assertIn("aalc.autostart", ids)
            self.assertIn("aalc.game_path", ids)
            self.assertIn("aalc.check_update", ids)
            self.assertIn("aalc.image_resource_sync", ids)
            # MAA 当前 schema 与 AALC 唯一跨行路径修复都已核验，可以安全应用。
            maa_autorun = next(c for c in changes if c.change_id == "maa.autorun")
            self.assertTrue(maa_autorun.verified)
            self.assertEqual(maa_autorun.current_display, "False")
            maa_actions = next(c for c in changes if c.change_id == "maa.post_actions")
            self.assertEqual(maa_actions.proposed_display, "ExitArknights, ExitSelf")
            aalc_path = next(c for c in changes if c.change_id == "aalc.game_path")
            self.assertTrue(aalc_path.verified)


class TestApplyChanges(unittest.TestCase):
    def test_apply_rejects_unknown_id(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            results = cm.apply_required_changes(["does.not.exist"])
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].ok)
            self.assertEqual(results[0].code, CODE_CHANGE_UNKNOWN)

    def test_apply_rejects_duplicate_id(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            results = cm.apply_required_changes(["maa.autorun", "maa.autorun"])
            codes = {r.code for r in results}
            self.assertIn(CODE_CHANGE_DUPLICATE, codes)

    def test_apply_rejects_unverified_change(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            specs = [dict(spec) for spec in cm._REQUIRED_CHANGE_SPECS]
            specs[0]["verified"] = False
            cm._REQUIRED_CHANGE_SPECS = tuple(specs)
            results = cm.apply_required_changes(["maa.autorun"])
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].ok)
            self.assertEqual(results[0].code, CODE_CHANGE_NOT_VERIFIED)

    def test_apply_verified_json_change_creates_backup_and_writes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            backups = tmp / "b"
            cm = ConfigManager(p, backups)
            results = cm.apply_required_changes(["maa.autorun"])
            ok_results = [r for r in results if r.ok]
            self.assertTrue(any(r.name == "backup" for r in ok_results))
            # 写入后验证应通过
            verify = [r for r in results if r.name.endswith(".verify")]
            self.assertTrue(verify and verify[0].ok)
            # 文件确实被更新
            gui = json.loads((tmp / "maa_config" / "gui.new.json").read_text(encoding="utf-8"))
            self.assertTrue(
                gui["Configurations"]["Default"]["Gui"]["StartUpSettings"]["RunDirectly"]
            )
            # 备份已创建
            self.assertTrue(backups.is_dir())
            self.assertGreaterEqual(len(list(backups.iterdir())), 1)
            # 已符合 proposed 的项目不再出现在只读规划中。
            self.assertNotIn(
                "maa.autorun",
                {change.change_id for change in cm.plan_required_changes()},
            )

    def test_apply_verified_multiline_game_path_repairs_only_the_scalar(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            yaml_path = tmp / "aalc_config.yaml"
            yaml_path.write_text(
                "game_title_name: LimbusCompany\n"
                "game_path: C:\\Program Files (x86\\Steam\\steamapps\\common\\Limbus \n"
                "  Company\\LimbusCompany.exe # 游戏启动路径。\n"
                "autostart: false\n"
                "after_completion_actions: []\n",
                encoding="utf-8",
            )
            cm = ConfigManager(p, tmp / "b")

            results = cm.apply_required_changes(["aalc.game_path"])

            self.assertTrue(all(result.ok for result in results))
            text = yaml_path.read_text(encoding="utf-8")
            self.assertIn(
                "game_path: D:\\Steam\\LimbusCompany.exe # 游戏启动路径。",
                text,
            )
            self.assertNotIn("\n  Company\\LimbusCompany.exe", text)

    def test_apply_verified_yaml_change_writes_and_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _make_user_config(tmp)
            cm = ConfigManager(p, tmp / "b")
            results = cm.apply_required_changes(["aalc.autostart"])
            verify = [r for r in results if r.name.endswith(".verify")]
            self.assertTrue(verify and verify[0].ok)
            content = (tmp / "aalc_config.yaml").read_text(encoding="utf-8")
            self.assertIn("autostart: false", content)


class TestBackupRestore(unittest.TestCase):
    def test_backup_creates_manifest_with_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            backups = tmp / "b"
            src = tmp / "a.json"
            src.write_text('{"x": 1}', encoding="utf-8")
            cm = ConfigManager(tmp / "user.json", backups)  # user.json 加载在此测试不必要
            info = cm.backup_files([str(src)])
            self.assertEqual(info.source_count, 1)
            self.assertTrue(info.manifest_path.is_file())
            manifest = json.loads(info.manifest_path.read_text(encoding="utf-8"))
            entry = manifest["entries"][0]
            self.assertEqual(entry["original_absolute_path"], str(src.resolve()))
            self.assertIn("sha256", entry)
            self.assertEqual(entry["size"], len('{"x": 1}'))

    def test_backup_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            with self.assertRaises(ConfigManagerError) as ctx:
                cm.backup_files([str(tmp / "nope.txt")])
            self.assertEqual(ctx.exception.code, CODE_FILE_NOT_FOUND)

    def test_backup_hash_mismatch_on_corrupt_copy(self):
        # 通过 monkeypatch shutil.copy2 模拟复制后内容不符
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "a.json"
            src.write_text("hello", encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            import daily_games.config_manager as cm_mod
            import shutil as _shutil
            orig = _shutil.copy2

            def bad(src_p, dst_p, *_a, **_kw):
                orig(src_p, dst_p, *_a, **_kw)
                # 故意破坏目标文件
                Path(dst_p).write_text("corrupt", encoding="utf-8")

            _shutil.copy2 = bad
            try:
                with self.assertRaises(ConfigManagerError) as ctx:
                    cm.backup_files([str(src)])
                self.assertEqual(ctx.exception.code, CODE_HASH_MISMATCH)
            finally:
                _shutil.copy2 = orig
            # 部分备份目录应被清理
            self.assertFalse((tmp / "b").exists() and any((tmp / "b").iterdir()))

    def test_restore_backs_up_current_then_restores(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            backups = tmp / "b"
            src = tmp / "a.json"
            src.write_text('{"v": 1}', encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", backups)
            info = cm.backup_files([str(src)])
            # 修改当前值
            src.write_text('{"v": 999}', encoding="utf-8")
            # 恢复
            results = cm.restore_backup(info.backup_id)
            # 应有 pre_backup 成功 + 恢复成功
            self.assertTrue(any(r.name == "restore.pre_backup" and r.ok for r in results))
            self.assertTrue(any(r.name == "restore.summary" and r.ok for r in results))
            self.assertEqual(src.read_text(encoding="utf-8"), '{"v": 1}')

    def test_restore_unknown_id_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.restore_backup("nonexistent")
            self.assertFalse(results[0].ok)
            self.assertEqual(results[0].code, CODE_FILE_NOT_FOUND)

    def test_list_backups(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            backups = tmp / "b"
            src = tmp / "a.json"
            src.write_text("1", encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", backups)
            cm.backup_files([str(src)])
            cm.backup_files([str(src)])
            infos = cm.list_backups()
            self.assertEqual(len(infos), 2)


class TestPatchJson(unittest.TestCase):
    def test_patch_updates_existing_key_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "t.json"
            target.write_text(json.dumps({"a": {"b": 1}, "keep": True}), encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_json(target, {"a.b": 2})
            verify = [r for r in results if r.name.endswith(".verify")]
            self.assertTrue(verify[0].ok)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["a"]["b"], 2)
            self.assertTrue(data["keep"])

    def test_patch_missing_key_reports_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "t.json"
            target.write_text(json.dumps({"a": 1}), encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_json(target, {"nonexistent.key": 2})
            missing = [r for r in results if r.code == CODE_KEY_MISSING]
            self.assertTrue(missing)
            # 文件未被改动
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})

    def test_patch_invalid_json_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "t.json"
            target.write_text("{bad", encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_json(target, {"a": 1})
            self.assertEqual(results[0].code, CODE_PARSE_FAILED)

    def test_patch_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_json(tmp / "nope.json", {"a": 1})
            self.assertEqual(results[0].code, CODE_FILE_NOT_FOUND)

    def test_patch_atomic_write_verify_failure_detects_corruption(self):
        # 模拟写入后验证失败（值不符）
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "t.json"
            target.write_text(json.dumps({"a": 1}), encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            import daily_games.config_manager as cm_mod
            orig_verify = cm_mod.ConfigManager._verify_json_values

            def fake_verify(self, t, u):
                return False, "模拟不匹配"

            cm_mod.ConfigManager._verify_json_values = fake_verify
            try:
                results = cm.patch_json(target, {"a": 2})
                verify = [r for r in results if r.name.endswith(".verify")]
                self.assertTrue(verify)
                self.assertEqual(verify[0].code, CODE_VERIFY_AFTER_WRITE_FAILED)
            finally:
                cm_mod.ConfigManager._verify_json_values = orig_verify


class TestPatchYaml(unittest.TestCase):
    def test_patch_unique_scalar_key(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "c.yaml"
            target.write_text(
                "autostart: false\n"
                "game_path: D:\\\\old.exe\n"
                "# 注释行\n"
                "after_completion_actions: []\n",
                encoding="utf-8",
            )
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_yaml_scalars(target, {"autostart": "true"})
            verify = [r for r in results if r.name.endswith(".verify")]
            self.assertTrue(verify[0].ok)
            content = target.read_text(encoding="utf-8")
            self.assertIn("autostart: true", content)
            # 其他行保留
            self.assertIn("game_path: D:\\\\old.exe", content)

    def test_patch_missing_key_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "c.yaml"
            target.write_text("autostart: false\n", encoding="utf-8")
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_yaml_scalars(target, {"nonexistent": "x"})
            self.assertEqual(results[0].code, CODE_KEY_MISSING)

    def test_patch_duplicate_key_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "c.yaml"
            target.write_text(
                "autostart: false\n"
                "autostart: true\n",
                encoding="utf-8",
            )
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_yaml_scalars(target, {"autostart": "x"})
            self.assertEqual(results[0].code, CODE_KEY_DUPLICATE)

    def test_patch_multiline_or_corrupt_rejected(self):
        # 模拟 AALC 实际 game_path 跨行 + 缺右括号的情况
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            target = tmp / "c.yaml"
            target.write_text(
                "game_path: C:\\\\Program Files (x86\\Steam\\steamapps\\common\\Limbus \n"
                "  Company\\LimbusCompany.exe\n"
                "autostart: false\n",
                encoding="utf-8",
            )
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_yaml_scalars(target, {"game_path": "D:\\\\new.exe"})
            # game_path 的当前值跨行，应被拒绝
            gp_result = next(r for r in results if "game_path" in r.name)
            self.assertEqual(gp_result.code, CODE_YAML_MULTILINE_OR_CORRUPT)

    def test_patch_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cm = ConfigManager(tmp / "u.json", tmp / "b")
            results = cm.patch_yaml_scalars(tmp / "nope.yaml", {"a": "b"})
            self.assertEqual(results[0].code, CODE_FILE_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
