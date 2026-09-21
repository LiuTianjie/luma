"""Migration fixture: both implementations consume the same on-node files.

Python prepares old-format archives and checks Go output. The Go adapter never
starts Python and does not require this harness in a deployed Agent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from luma.builder_executor import (
    _create_deterministic_snapshot,
    _persist_content_file,
    _snapshot_id,
    _source_tree_digest_from_snapshot,
    open_builder_analysis_artifact,
)
from luma.builder_build_executor import _extract_snapshot_secure
from luma.builder_storage import execution_guard

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("go"), "Go toolchain required for migration contracts")
class GoBuilderStorageContracts(unittest.TestCase):
    def test_existing_snapshots_artifacts_and_execution_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source = base / "source"
            source.mkdir()
            for relative, contents in {
                ".git/ignored": "must not enter the snapshot",
                "a/inner/run.sh": "#!/bin/sh\necho dirty-working-tree\n",
                "a/.git/keep": "nested git retained",
                "dirty.txt": "uncommitted source",
                "中文\u2028<&>\t文件.txt": "UTF-8 content",
                "long/" + "中" * 70 + ".txt": "PAX long UTF-8 path",
                "quote\"name.txt": "quoted path",
            }.items():
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            (source / "a/inner/run.sh").chmod(0o755)
            (source / "empty").mkdir()
            (source / "run").symlink_to("a/inner/run.sh")
            (source / "nested-dir").symlink_to("a/inner", target_is_directory=True)
            snapshot = base / "python.tar"
            tree_digest, digest, source_bytes = _create_deterministic_snapshot(
                source, snapshot, disk_limit_bytes=8 << 20, cancel_event=None
            )
            store = base / "store"
            _persist_content_file(store, namespace="sha256", digest=digest, source=snapshot, suffix=".tar")
            old_artifact = base / "evidence.json"
            old_artifact.write_bytes(b'{"origin":"legacy","evidence":"fixture"}')
            artifact_digest = "sha256:" + hashlib.sha256(old_artifact.read_bytes()).hexdigest()
            _persist_content_file(store, namespace="artifacts/evidence/sha256", digest=artifact_digest,
                                  source=old_artifact, suffix=".json")
            expected = dict(
                snapshot=dict(sourceTreeDigest=tree_digest, sourceSnapshotDigest=digest,
                              sourceBytes=source_bytes, archiveBytes=snapshot.stat().st_size),
                snapshotId=_snapshot_id(digest, builder_task_id="task-one"),
                artifact=dict(name="evidence", digest=artifact_digest,
                              mediaType="application/vnd.lae.evidence+json", sizeBytes=old_artifact.stat().st_size),
            )
            (base / "expected.json").write_text(json.dumps(expected))
            env = dict(os.environ, LUMA_TEST_BUILDER_FIXTURE=str(base))
            command = ["go", "test", "./internal/adapters/builderfs", "-run", "^TestMigrationContract$", "-count=1"]
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads((base / "go-result.json").read_text())
            actual_tree, actual_bytes = _source_tree_digest_from_snapshot(base / "go.tar", cancel_event=None)
            self.assertEqual((actual_tree, actual_bytes), (tree_digest, source_bytes))
            self.assertEqual(result["snapshot"]["sourceSnapshotDigest"],
                             "sha256:" + hashlib.sha256((base / "go.tar").read_bytes()).hexdigest())
            # Existing consumers can extract a Go-produced snapshot and retain
            # modes, links, UTF-8 names and working-tree contents.
            extracted = base / "go-materialized"
            _extract_snapshot_secure(base / "go.tar", extracted, disk_limit_bytes=8 << 20, cancel_event=None)
            for name in ("legacy-materialized", "go-materialized"):
                directory = base / name
                self.assertEqual((directory / "dirty.txt").read_text(), "uncommitted source")
                self.assertEqual((directory / "run").read_text(), "#!/bin/sh\necho dirty-working-tree\n")
                self.assertEqual((directory / "a/inner/run.sh").stat().st_mode & 0o777, 0o755)
                self.assertEqual((directory / "nested-dir").readlink(), Path("a/inner"))
                self.assertFalse((directory / ".git").exists())
            request = dict(leaseId="artdl_" + "a" * 32, builderTaskId="task-one", artifact=result["artifact"])
            with patch.dict(os.environ, LUMA_BUILDER_SNAPSHOT_ROOT=str(store)):
                exported = open_builder_analysis_artifact(request)
                try:
                    self.assertEqual(json.load(exported.stream), dict(origin="Go", evidence="新制品"))
                finally:
                    exported.close()
            with execution_guard(root=store):
                locked = subprocess.run(command, cwd=ROOT, env=dict(env, LUMA_TEST_BUILDER_LOCKED="1"),
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(locked.returncode, 0, locked.stdout + locked.stderr)
