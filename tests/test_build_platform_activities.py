from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock

HAS_TEMPORAL = importlib.util.find_spec('temporalio') is not None
if HAS_TEMPORAL:
    from luma.build_platform.activities import Activities


@unittest.skipUnless(HAS_TEMPORAL, 'build-platform extra required')
class BuildActivityInvariants(unittest.TestCase):
    def setUp(self):
        self.store, self.nomad, self.blobs, self.harbor = Mock(), Mock(), Mock(), Mock()
        self.run = {'id': 'bld2-aabb', 'body': {}, 'status': 'running', 'phase': 'building',
                    'artifact_id': '', 'release_id': '', 'cancel_requested': False,
                    'build_status': 'building', 'message': '', 'result': {},
                    'prepared': {'sourceObject': 'source', 'platforms': ['linux/amd64']}}
        self.store.execution_guard.side_effect = lambda _: nullcontext(True)
        self.store.get.return_value = self.run
        self.store.acquire.return_value = True
        self.activities = Activities(SimpleNamespace(), self.store, self.blobs, self.nomad, self.harbor)

    def test_receipt_without_nomad_termination_does_not_release_capacity(self):
        import time
        attempt = {'id': 'attempt', 'job_id': 'job', 'status': 'submitted', 'number': 1, 'created_at': int(time.time()), 'result': {'images': {'app': 'digest'}}}
        self.store.attempts.return_value = [attempt]
        self.nomad.observe.return_value = {'terminal': False, 'status': 'running'}
        self.assertFalse(self.activities._advance(self.run['id']))
        self.nomad.submit.assert_not_called()
        self.store.save_artifact.assert_not_called()
        self.store.update.assert_not_called()

    def test_unknown_allocation_holds_capacity_during_cancel(self):
        self.store.attempts.return_value = [{'id': 'attempt', 'job_id': 'job', 'status': 'submitted'}]
        self.nomad.observe.return_value = self.nomad.stop.return_value = {'terminal': False, 'status': 'pending'}
        self.assertFalse(self.activities.cleanup(self.run['id'], 'canceled'))
        self.assertEqual(self.store.update.call_args.kwargs['status'], 'canceling')
        self.assertNotIn('capacity_held', self.store.update.call_args.kwargs)
        self.store.finish_attempt.assert_not_called()

    def test_rejected_job_does_not_leave_a_permanent_capacity_hold(self):
        self.store.attempts.return_value = [{'id': 'attempt', 'job_id': 'job', 'status': 'rejected'}]
        self.assertTrue(self.activities.cleanup(self.run['id'], 'failed'))
        self.nomad.observe.assert_not_called()
        self.assertEqual(self.store.update.call_args.kwargs['status'], 'failed')

    def test_artifact_only_crash_recovery_does_not_publish(self):
        self.run.update(artifact_id='artifact', body={'autoDeploy': False})
        self.assertTrue(self.activities._advance(self.run['id']))
        self.store.create_release.assert_not_called()
        self.nomad.submit.assert_not_called()
        self.assertEqual(self.store.update.call_args.kwargs['status'], 'succeeded')

    def test_release_retry_uses_existing_artifact_without_build_capacity(self):
        self.run.update(artifact_id='artifact')
        self.store.artifact.return_value = {'id': 'artifact', 'result': {'targetKey': 'app'}}
        self.store.begin_release.return_value = True
        self.activities.publish = Mock(return_value=True)
        self.assertTrue(self.activities._advance(self.run['id']))
        self.store.create_release.assert_called_once_with(self.run['id'], 'artifact', 'app')
        self.store.acquire.assert_not_called()
        self.nomad.submit.assert_not_called()

    @unittest.mock.patch('luma.build_platform.activities.control_request')
    def test_no_native_node_releases_pre_submit_crash_reservation(self, control):
        self.run.update(capacity_held=True, phase='allocating')
        self.store.attempts.return_value = []
        control.return_value = {'nodes': []}
        self.nomad.eligible.return_value = []
        self.assertFalse(self.activities._advance(self.run['id']))
        self.assertFalse(self.store.update.call_args.kwargs['capacity_held'])
        self.assertEqual(self.store.update.call_args.kwargs['phase'], 'waiting-node')
        self.nomad.submit.assert_not_called()

    def test_known_unsubmitted_attempt_is_canceled_without_unknown_wait(self):
        self.store.attempts.return_value = [{'id': 'attempt', 'job_id': 'job', 'status': 'pending'}]
        self.nomad.observe.return_value = {'terminal': False, 'status': 'missing'}
        self.assertTrue(self.activities.cleanup(self.run['id'], 'canceled'))
        self.store.finish_attempt.assert_called_once_with('attempt', 'rejected')
        self.nomad.stop.assert_not_called()

    def test_receipt_is_reread_after_nomad_observes_completion(self):
        attempt = {'id': 'attempt', 'job_id': 'job', 'status': 'submitted', 'number': 1, 'result': {}}
        self.store.attempts.return_value = [attempt]
        self.store.attempt.return_value = {**attempt, 'result': {'images': {}, 'cacheRefs': []}}
        self.nomad.observe.return_value = {'terminal': True, 'status': 'succeeded'}
        self.run['prepared'].update(targets=[], sha='a'*40, manifest='name: app', kind='service', targetKey='app')
        self.assertFalse(self.activities._advance(self.run['id']))
        self.store.attempt.assert_called_once_with(self.run['id'], 1)
        self.store.save_artifact.assert_called_once()

    def test_allocated_image_pull_gets_startup_budget_not_placement_budget(self):
        import time
        self.activities.settings = SimpleNamespace(placement_timeout=60, startup_timeout=600)
        self.store.attempts.return_value = [{'id': 'attempt', 'job_id': 'job', 'status': 'submitted', 'number': 1, 'created_at': int(time.time()) - 100, 'result': {}}]
        self.nomad.observe.return_value = {'terminal': False, 'status': 'starting'}
        self.assertFalse(self.activities._advance(self.run['id']))
        self.assertEqual(self.store.update.call_args.kwargs['phase'], 'starting')
