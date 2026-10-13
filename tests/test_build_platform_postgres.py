"""Opt-in tests against a disposable PostgreSQL database, never Control state."""
from __future__ import annotations

import concurrent.futures
import os
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from luma.build_platform.model import Conflict, QueueFull
from luma.build_platform.store import Store

URL = os.environ.get('LUMA_BUILD_TEST_DATABASE_URL', '')


@unittest.skipUnless(URL, 'set LUMA_BUILD_TEST_DATABASE_URL to a disposable *_test database')
class BuildPlatformPostgres(unittest.TestCase):
    def setUp(self):
        if not urlsplit(URL).path.endswith('_test'):
            self.fail('integration database name must end in _test')
        from cryptography.fernet import Fernet
        self.settings = SimpleNamespace(database_url=URL, encryption_key=Fernet.generate_key().decode(), max_active=10, max_pending=100, max_per_project=10)
        self.store = Store(self.settings)
        self.store.migrate()
        with self.store.tx() as conn:
            conn.execute('TRUNCATE bp_runs,bp_targets,bp_attempts,bp_artifacts,bp_releases,bp_outbox,bp_log_chunks,bp_settings RESTART IDENTITY CASCADE')

    def tearDown(self):
        self.store.close()

    def create(self, key='', **body):
        return self.store.create({'repoUrl': 'https://git/app', **body, **({'idempotencyKey': key} if key else {})}, source='https://git/app', project='git/app')

    def test_concurrent_idempotent_admission_and_secret_encryption(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            rows = list(pool.map(lambda _: self.create('same', envSecrets={'KEY': 'do-not-expose'}), range(10)))
        self.assertEqual(len({row['id'] for row in rows}), 1)
        run_id = rows[0]['id']
        self.assertEqual(self.store.get(run_id, private=True)['body']['envSecrets']['KEY'], 'do-not-expose')
        with self.store.tx() as conn:
            row = conn.execute('SELECT private_request,request FROM bp_runs').fetchone()
            self.assertNotIn(b'do-not-expose', bytes(row['private_request']))
            self.assertNotIn('envSecrets', row['request'])
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bp_outbox WHERE operation='start'").fetchone()['n'], 1)
        with self.assertRaises(Conflict):
            self.create('same', ref='different')

    def test_ten_slots_are_shared_across_store_instances(self):
        runs = [self.create() for _ in range(20)]
        second = Store(self.settings)
        try:
            # Admission order is fixed; independent schedulers race for slots.
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                for _ in range(3):
                    list(pool.map(lambda pair: (self.store if pair[0] % 2 else second).acquire(pair[1]['id']), enumerate(runs)))
            with self.store.tx() as conn:
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM bp_runs WHERE capacity_held').fetchone()['n'], 10)
            self.store.update(runs[0]['id'], status='succeeded')
            self.assertTrue(second.acquire(runs[10]['id']))
        finally:
            second.close()

    def test_queue_is_bounded(self):
        self.settings.max_active, self.settings.max_pending = 1, 1
        self.create('one')
        self.create('two')
        self.create('two')
        with self.assertRaises(QueueFull):
            self.create('three')

    def test_old_build_finishing_late_keeps_older_generation(self):
        old, new = self.create(), self.create()
        for run in [new, old]:
            self.store.reserve_target(run['id'], 'app')
            self.store.update(run['id'], prepared={'sha': 'a' * 40, 'recipeHash': 'recipe'})
            artifact = self.store.save_artifact(run['id'], {'targetKey': 'app'}, [])
            self.store.create_release(run['id'], artifact, 'app')
        previous = self.store.release('rel-' + old['id'])
        latest = self.store.release('rel-' + new['id'])
        self.assertLess(previous['generation'], previous['desired_generation'])
        self.assertEqual(latest['generation'], latest['desired_generation'])

    def test_log_batches_retry_without_duplicate_events(self):
        run_id = self.create()['id']
        first = self.store.append_chunk(run_id, 'batch', 'object', 'checksum', 24)
        replay = self.store.append_chunk(run_id, 'batch', 'object', 'checksum', 24)
        self.assertEqual(first['first_seq'], replay['first_seq'])
        self.assertEqual(self.store.get(run_id)['next_event'], 24)
        with self.assertRaises(Conflict):
            self.store.append_chunk(run_id, 'batch', 'other', 'different', 24)

    def test_projection_update_during_delivery_is_not_lost(self):
        run_id = self.create()['id']
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM bp_outbox WHERE operation='project'").fetchone()
        self.store.update(run_id, message='newer state')
        self.store.outbox_done(row['id'], row['revision'])
        with self.store.tx() as conn:
            after = conn.execute('SELECT * FROM bp_outbox WHERE id=%s', (row['id'],)).fetchone()
        self.assertFalse(after['delivered'])
        self.assertGreater(after['revision'], row['revision'])

    def test_cancel_and_release_are_mutually_exclusive(self):
        first = self.create()['id']
        self.store.cancel(first)
        self.assertFalse(self.store.begin_release(first))
        second = self.create()['id']
        self.assertTrue(self.store.begin_release(second))
        with self.assertRaises(Conflict):
            self.store.cancel(second)

    def test_pagination_freezes_same_second_admissions(self):
        self.create()
        self.create()
        first, page = self.store.list({'limit': 1})
        third = self.create()
        rest, _ = self.store.list({'limit': 1, 'cursor': page['nextCursor']})
        self.assertEqual(len(rest), 1)
        self.assertNotEqual(rest[0]['id'], third['id'])
        self.assertNotEqual(rest[0]['id'], first[0]['id'])

    def test_overlapping_activities_cannot_change_frozen_source(self):
        run_id = self.create()['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            frozen = list(pool.map(lambda n: self.store.freeze_revision(run_id, str(n) * 40), range(10)))
        self.assertEqual(len({row['sha'] for row in frozen}), 1)
        sha = frozen[0]['sha']
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            prepared = list(pool.map(lambda n: self.store.save_prepared(run_id, {'sha': sha, 'sourceObject': 'unique-checksum-' + str(n)}), range(10)))
        self.assertEqual(len({row['sourceObject'] for row in prepared}), 1)

    def test_artifact_only_completion_cannot_overwrite_a_cancel(self):
        run_id = self.create()['id']
        self.store.cancel(run_id)
        self.store.update(run_id, status='succeeded')
        self.assertEqual(self.store.get(run_id)['status'], 'canceled')

    def test_unplaced_arm_cannot_consume_all_x86_slots(self):
        arms = [self.create()['id'] for _ in range(10)]
        x86 = [self.create()['id'] for _ in range(10)]
        self.assertTrue(self.store.acquire(arms[0], 'linux/arm64'))
        for run_id in arms[1:]:
            self.assertFalse(self.store.acquire(run_id, 'linux/arm64'))
        for run_id in x86[:9]:
            self.assertTrue(self.store.acquire(run_id, 'linux/amd64'))
            self.store.update(run_id, phase='building')
        self.assertFalse(self.store.acquire(x86[9], 'linux/amd64'))

    def test_execution_guard_blocks_overlapping_activity_and_exposes_commits(self):
        run_id = self.create()['id']
        second = Store(self.settings)
        try:
            with self.store.execution_guard(run_id) as owned:
                self.assertTrue(owned)
                with second.execution_guard(run_id) as other:
                    self.assertFalse(other)
                self.store.attempt(run_id, 1, token='lease')
                self.assertEqual(second.attempt(run_id, 1)['status'], 'pending')
                self.store.submitting_attempt(run_id + '-a1')
                self.assertEqual(second.attempt(run_id, 1)['status'], 'submitting')
            with second.execution_guard(run_id) as owned:
                self.assertTrue(owned)
        finally:
            second.close()

    def test_source_completion_cannot_resurrect_canceled_run(self):
        run_id = self.create()['id']
        self.store.update(run_id, status='canceled')
        self.store.update(run_id, status='queued', phase='waiting-node')
        self.assertEqual(self.store.get(run_id)['status'], 'canceled')

    def test_canceled_source_preparation_cannot_reserve_a_release(self):
        run_id = self.create()['id']
        self.store.cancel(run_id)
        self.store.reserve_target(run_id, 'app')
        with self.store.tx() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM bp_targets').fetchone()['n'], 0)
