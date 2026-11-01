"""Release-paired skills: upgrade migration, local changes and atomic replacement."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from luma import skills


class SkillUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.bundle = self.home / "bundle"
        self.root = self.home / ".codex" / "skills"
        for name in ("luma-deployment-yaml", "luma-logs"):
            folder = self.bundle / name
            (folder / "references").mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"name: {name}\nrelease one\n")
            (folder / "references" / "api.md").write_text("reference one")
        patcher = patch.object(skills, "bundle_root", return_value=self.bundle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self):
        return skills.sync_skills([self.root], user_home=self.home, register=True)

    def test_no_implicit_first_install_and_custom_roots_are_remembered(self):
        self.assertEqual(skills.sync_skills(user_home=self.home)["items"], [])
        custom = self.home / "other-assistant" / "skills"
        result = skills.sync_skills([custom], user_home=self.home, register=True)
        self.assertEqual(len(result["items"]), 2)
        self.assertIn(custom, skills.installed_roots(self.home))
        self.assertFalse((self.home / ".claude").exists())

    def test_invalid_root_metadata_does_not_break_optional_sync_or_install(self):
        config = self.home / ".config" / "luma" / "skill-roots.json"
        config.parent.mkdir(parents=True)
        for value in (None, 42, "not-a-list", [None, {}, "relative"]):
            with self.subTest(value=value):
                config.write_text(json.dumps({"roots": value}))
                result = skills.sync_skills(user_home=self.home)
                self.assertEqual(result["warnings"], [])
                self.install()
                self.assertEqual(json.loads(config.read_text())["roots"], [str(self.root)])

    def test_legacy_suite_gets_new_skill_and_preserves_old_customizations(self):
        legacy = self.root / "luma-deployment-yaml"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("old customized rules")
        result = skills.sync_skills(user_home=self.home)
        self.assertEqual(result["warnings"], [])
        item = next(i for i in result["items"] if i["name"] == legacy.name)
        self.assertEqual((Path(item["backup"]) / "SKILL.md").read_text(), "old customized rules")
        self.assertTrue((self.root / "luma-logs" / "SKILL.md").exists())

    def test_unchanged_upgrade_replaces_removed_files_without_backups(self):
        self.install()
        (self.bundle / "luma-logs" / "references" / "api.md").unlink()
        with patch.object(skills, "__version__", "9.0.0"):
            result = skills.sync_skills(user_home=self.home)
        self.assertFalse((self.root / "luma-logs" / "references" / "api.md").exists())
        self.assertFalse(any("backup" in item for item in result["items"]))
        marker = json.loads((self.root / "luma-logs" / skills.MARKER).read_text())
        self.assertEqual(marker["version"], "9.0.0")

    def test_local_edits_survive_current_version_and_are_backed_up_on_upgrade(self):
        self.install()
        edited = self.root / "luma-logs" / "SKILL.md"
        edited.write_text("custom instructions")
        self.assertTrue(all(i["status"] == "current" for i in skills.sync_skills(user_home=self.home)["items"]))
        self.assertEqual(edited.read_text(), "custom instructions")
        with patch.object(skills, "__version__", "9.0.0"):
            result = skills.sync_skills(user_home=self.home)
        item = next(i for i in result["items"] if i["name"] == "luma-logs")
        self.assertEqual((Path(item["backup"]) / "SKILL.md").read_text(), "custom instructions")
        self.assertEqual(edited.read_text(), (self.bundle / "luma-logs" / "SKILL.md").read_text())

    def test_force_restores_current_bundle_and_backs_up_edits(self):
        self.install()
        (self.root / "luma-logs" / "extra.md").write_text("local note")
        result = skills.sync_skills(user_home=self.home, force=True)
        item = next(i for i in result["items"] if i["name"] == "luma-logs")
        self.assertEqual((Path(item["backup"]) / "extra.md").read_text(), "local note")
        self.assertFalse((self.root / "luma-logs" / "extra.md").exists())

    def test_symlinked_skill_does_not_block_other_suite_updates(self):
        self.root.mkdir(parents=True)
        outside = self.home / "owned-elsewhere"
        outside.mkdir()
        (outside / "SKILL.md").write_text("custom")
        (self.root / "luma-deployment-yaml").symlink_to(outside, target_is_directory=True)
        result = skills.sync_skills(user_home=self.home)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual((outside / "SKILL.md").read_text(), "custom")
        self.assertTrue((self.root / "luma-logs" / "SKILL.md").exists())

    def test_failed_replacement_rolls_back_the_previous_skill(self):
        self.install()
        original = (self.root / "luma-logs" / "SKILL.md").read_text()
        (self.bundle / "luma-logs" / "SKILL.md").write_text("new release")
        rename = Path.rename

        def fail_candidate(path, target):
            if path.name == "candidate":
                raise OSError("simulated filesystem failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_candidate):
            result = skills.sync_skills(user_home=self.home)
        self.assertTrue(result["warnings"])
        self.assertEqual((self.root / "luma-logs" / "SKILL.md").read_text(), original)

    def test_packaged_startup_syncs_but_development_checkout_does_not(self):
        with patch.object(skills, "PACKAGED", self.home / "missing"), \
                patch.object(skills, "sync_skills") as sync:
            skills.auto_sync()
            sync.assert_not_called()
        with patch.object(skills, "PACKAGED", self.bundle), \
                patch.object(skills, "sync_skills", return_value={"items": [], "warnings": []}) as sync, \
                contextlib.redirect_stderr(io.StringIO()):
            skills.auto_sync()
            sync.assert_called_once_with()

    def test_failed_rollback_keeps_the_only_previous_copy_outside_temporary_stage(self):
        self.install()
        (self.root / "luma-logs" / "SKILL.md").write_text("unique custom content")
        rename = Path.rename

        def fail_publish_and_restore(path, target):
            if Path(target).name == "luma-logs":
                raise OSError("destination unavailable")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish_and_restore):
            result = skills.sync_skills(user_home=self.home, force=True)
        self.assertTrue(any("previous files preserved" in warning for warning in result["warnings"]))
        backups = list((self.root.parent / "luma-skill-backups").glob("luma-logs-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_text(), "unique custom content")
