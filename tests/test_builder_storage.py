import hashlib
import os
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.builder_storage import MIN_GRACE_SECONDS, PLAN_TTL_SECONDS, execute_builder_storage, execution_guard
from luma.errors import LumaError


class BuilderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'snapshots'
        self.now = 2_000_000_000

    def content(self, data=b'source', namespace='sha256', suffix='.tar'):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / namespace / digest[:2] / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (self.now - 2 * MIN_GRACE_SECONDS, self.now - 2 * MIN_GRACE_SECONDS))
        return path, 'sha256:' + digest

    def run_operation(self, operation, *, now=None, protected=None, **kwargs):
        now = self.now if now is None else now
        return execute_builder_storage({'operation': operation, 'references': {'protectedDigests': protected or [], 'coverageComplete': True, 'collectedAt': now}, **kwargs}, root=self.root, now=now)

    def test_inventory_does_not_mutate_content_and_unknown_caches_are_null(self):
        path, digest = self.content()
        result = self.run_operation('inventory', protected=[digest])
        self.assertEqual(result['protectedBytes'], 6)
        self.assertEqual(result['reclaimableBytes'], 0)
        self.assertIsNone(result['buildkitBytes'])
        self.assertEqual(path.read_bytes(), b'source')

    def test_quarantine_and_restore_are_recoverable_with_grace(self):
        path, _ = self.content()
        plan = self.run_operation('preview')
        self.assertTrue(path.exists())
        with self.assertRaisesRegex(LumaError, 'grace'):
            self.run_operation('quarantine', planId=plan['planId'], confirmed=True)
        later = self.now + MIN_GRACE_SECONDS
        result = self.run_operation('quarantine', now=later, planId=plan['planId'], confirmed=True)
        self.assertEqual(result['status'], 'quarantined')
        self.assertFalse(path.exists())
        self.assertEqual(self.run_operation('inventory', now=later)['quarantinedBytes'], 6)
        self.run_operation('restore', now=later, planId=plan['planId'], confirmed=True)
        self.assertEqual(path.read_bytes(), b'source')

    def test_purge_has_second_grace_and_fresh_reference_recheck(self):
        path, digest = self.content()
        plan = self.run_operation('preview')
        later = self.now + MIN_GRACE_SECONDS
        self.run_operation('quarantine', now=later, planId=plan['planId'], confirmed=True)
        with self.assertRaisesRegex(LumaError, 'grace'):
            self.run_operation('purge', now=later, planId=plan['planId'], confirmed=True)
        with self.assertRaisesRegex(LumaError, 'referenced'):
            self.run_operation('purge', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True, protected=[digest])
        self.run_operation('purge', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertFalse(path.exists())
        self.assertEqual(self.run_operation('inventory')['quarantinedBytes'], 0)

    def test_new_reference_same_content_in_another_task_blocks_whole_plan(self):
        path, digest = self.content()
        other, _ = self.content(b'other')
        plan = self.run_operation('preview')
        with self.assertRaisesRegex(LumaError, 'referenced'):
            self.run_operation('quarantine', now=self.now + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True, protected=[digest])
        self.assertTrue(path.exists())
        self.assertTrue(other.exists())

    def test_changed_and_recreated_files_invalidate_plan(self):
        path, _ = self.content()
        plan = self.run_operation('preview')
        path.write_bytes(b'modified')
        with self.assertRaisesRegex(LumaError, 'changed'):
            self.run_operation('quarantine', now=self.now + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertTrue(path.exists())

    def test_new_files_are_not_swept_into_an_existing_plan(self):
        first, _ = self.content()
        plan = self.run_operation('preview')
        new, _ = self.content(b'new')
        self.run_operation('quarantine', now=self.now + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertFalse(first.exists())
        self.assertTrue(new.exists())

    def test_expired_plan_and_missing_confirmation_rejected(self):
        self.content()
        plan = self.run_operation('preview')
        with self.assertRaisesRegex(LumaError, 'confirmation'):
            self.run_operation('quarantine', now=self.now + MIN_GRACE_SECONDS, planId=plan['planId'])
        with self.assertRaisesRegex(LumaError, 'expired'):
            self.run_operation('quarantine', now=self.now + PLAN_TTL_SECONDS + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)

    def test_incomplete_stale_and_active_reference_manifests_block(self):
        self.content()
        for references in ({'coverageComplete': False}, {'coverageComplete': True, 'collectedAt': self.now - 301, 'protectedDigests': []}, {'coverageComplete': True, 'collectedAt': self.now, 'protectedDigests': [], 'blockedReasons': ['Active task']}):
            result = execute_builder_storage({'operation': 'preview', 'references': references}, root=self.root, now=self.now)
            self.assertTrue(result['blockedReasons'])
            self.assertNotIn('planId', result)

    def test_symlink_substitution_and_root_redirect_rejected(self):
        path, _ = self.content()
        target = Path(self.temp.name) / 'untouched'
        target.write_bytes(b'keep')
        plan = self.run_operation('preview')
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(LumaError, 'symlink'):
            self.run_operation('quarantine', now=self.now + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertEqual(target.read_bytes(), b'keep')
        linked = Path(self.temp.name) / 'linked'
        linked.symlink_to(self.root)
        with self.assertRaisesRegex(LumaError, 'symlink'):
            execute_builder_storage({'operation': 'inventory'}, root=linked, now=self.now)

    def test_build_and_external_artifact_namespaces_are_recognized(self):
        for namespace in ('artifacts/build/sbom/sha256', 'artifacts/build/scan/sha256', 'artifacts/external/resolution/sha256', 'artifacts/evidence/sha256'):
            self.content(namespace.encode(), namespace, '.json')
        result = self.run_operation('preview')
        self.assertFalse(result['blockedReasons'])
        self.assertEqual(result['candidateCount'], 4)

    def test_unknown_file_blocks_deletion_and_is_counted(self):
        self.content()
        (self.root / 'unknown').write_bytes(b'123')
        result = self.run_operation('preview')
        self.assertEqual(result['totalBytes'], 9)
        self.assertEqual(result['reclaimableBytes'], 0)
        self.assertNotIn('planId', result)

    def test_long_grace_always_precedes_expiry(self):
        self.content()
        result = self.run_operation('preview', graceSeconds=30 * MIN_GRACE_SECONDS)
        self.assertEqual(result['expiresAt'] - result['eligibleAfter'], PLAN_TTL_SECONDS)
        self.run_operation('quarantine', now=result['eligibleAfter'], planId=result['planId'], confirmed=True)

    def test_interrupted_quarantine_can_resume_or_restore(self):
        paths = [self.content(value)[0] for value in (b'first', b'second')]
        plan = self.run_operation('preview')
        later = self.now + MIN_GRACE_SECONDS
        original = Path.rename
        calls = []
        def interrupted(path, destination):
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt('simulated process death')
            return original(path, destination)
        with patch.object(Path, 'rename', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_operation('quarantine', now=later, planId=plan['planId'], confirmed=True)
        self.assertEqual(sum(path.exists() for path in paths), 1)
        self.run_operation('restore', now=later, planId=plan['planId'], confirmed=True)
        self.assertTrue(all(path.exists() for path in paths))
        plan = self.run_operation('preview', now=later)
        with patch.object(Path, 'rename', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_operation('quarantine', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.run_operation('quarantine', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertFalse(any(path.exists() for path in paths))

    def test_interrupted_purge_reconciles_already_deleted_files(self):
        for value in (b'first', b'second'):
            self.content(value)
        plan = self.run_operation('preview')
        later = self.now + MIN_GRACE_SECONDS
        self.run_operation('quarantine', now=later, planId=plan['planId'], confirmed=True)
        original = Path.unlink
        calls = []
        def interrupted(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt('simulated process death')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_operation('purge', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        result = self.run_operation('purge', now=later + MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertEqual(result['status'], 'purged')
        self.assertEqual(self.run_operation('inventory')['quarantinedBytes'], 0)

    def test_identical_previews_reuse_plan_and_old_metadata_expires(self):
        self.content()
        first = self.run_operation('preview')
        second = self.run_operation('preview')
        self.assertEqual(first['planId'], second['planId'])
        self.run_operation('inventory', now=first['expiresAt'] + 31 * MIN_GRACE_SECONDS)
        conn = sqlite3.connect(self.root / '.governance.sqlite3')
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 0)
        finally:
            conn.close()

    def test_quarantine_metadata_survives_expiry_and_restore_remains_available(self):
        path, _ = self.content()
        plan = self.run_operation('preview')
        later = self.now + MIN_GRACE_SECONDS
        self.run_operation('quarantine', now=later, planId=plan['planId'], confirmed=True)
        self.run_operation('inventory', now=later + 90 * MIN_GRACE_SECONDS)
        self.run_operation('restore', now=later + 90 * MIN_GRACE_SECONDS, planId=plan['planId'], confirmed=True)
        self.assertTrue(path.exists())

    def test_guard_excludes_build_and_cleanup_across_handles(self):
        with execution_guard(root=self.root):
            with self.assertRaisesRegex(LumaError, 'busy'):
                self.run_operation('inventory')


if __name__ == '__main__':
    unittest.main()
