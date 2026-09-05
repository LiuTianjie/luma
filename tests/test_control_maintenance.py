import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import database, maintenance
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


class ControlMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "manager"
        self.env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.state_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        state = init_state(domain="isolated.example")
        state["buildRuns"] = {"build-one": {"id": "build-one", "status": "failed", "events": [{"message": "first attempt"}]}}
        save_state(state)
        (self.state_root / "metrics-token").write_text("private-test-token" * 4)

    def test_backup_restore_and_second_generation_keep_state_and_private_files(self):
        first = self.root / "first.tgz"
        report = maintenance.backup(first)
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-test-token", json.dumps(report))
        restored = self.root / "restored"
        maintenance.restore(first, restored)
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(restored)}):
            self.assertEqual(load_state()["buildRuns"]["build-one"]["events"][0]["message"], "first attempt")
            self.assertEqual((restored / "metrics-token").read_text(), "private-test-token" * 4)
            second = self.root / "second.tgz"
            maintenance.backup(second)
        maintenance.restore(second, self.root / "third")
        self.assertTrue(database.check_database(self.root / "third" / database.DATABASE_FILE)["ok"])

    def test_backup_avoids_live_database_wal_and_preserves_committed_rows(self):
        conn = database.connect()
        self.addCleanup(conn.close)
        with database.transaction() as writer:
            writer.execute("INSERT INTO control_config VALUES(?,?)", ("afterCheckpoint", '"committed"'))
        archive = self.root / "snapshot.tgz"
        maintenance.backup(archive)
        with tarfile.open(archive) as saved:
            self.assertNotIn(database.DATABASE_FILE + "-wal", saved.getnames())
        maintenance.restore(archive, self.root / "fresh")
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "fresh")}):
            self.assertEqual(load_state()["afterCheckpoint"], "committed")

    def test_restore_never_overwrites_existing_directory(self):
        archive = self.root / "backup.tgz"
        maintenance.backup(archive)
        with self.assertRaises(LumaError):
            maintenance.restore(archive, self.state_root)
        self.assertEqual(load_state()["domain"], "isolated.example")

    def test_archive_path_traversal_rejected_without_publishing_destination(self):
        archive = self.root / "unsafe.tgz"
        with tarfile.open(archive, "w:gz") as saved:
            for name in (maintenance.MANIFEST, "../escape"):
                member = tarfile.TarInfo(name)
                member.size = 2
                saved.addfile(member, io.BytesIO(b"{}"))
        with self.assertRaises(LumaError):
            maintenance.restore(archive, self.root / "unsafe-restore")
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.root / "unsafe-restore").exists())

    def test_matching_checksums_do_not_bypass_database_integrity(self):
        snapshot = self.root / "bad.sqlite3"
        database.backup_database(snapshot)
        with sqlite3.connect(snapshot) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO control_events VALUES('buildRuns','missing','events',0,'{}')")
        content = snapshot.read_bytes()
        manifest = {"format": 1, "scope": "control-state", "files": {database.DATABASE_FILE: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}}
        archive = self.root / "bad.tgz"
        with tarfile.open(archive, "w:gz") as saved:
            for name, raw in ((maintenance.MANIFEST, json.dumps(manifest).encode()), (database.DATABASE_FILE, content)):
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                saved.addfile(member, io.BytesIO(raw))
        with self.assertRaises(LumaError):
            maintenance.restore(archive, self.root / "bad-restore")
        self.assertFalse((self.root / "bad-restore").exists())
