import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import database, storage_governance as governance
from luma.control.state import load_state, save_state
from luma.errors import LumaError


class StorageGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 2_000_000_000
        save_state({'clusterId': 'test', 'deployToken': 'secret', 'buildRuns': {}, 'deploymentEvents': {}})

    def seed(self, **records):
        state = load_state()
        state.update(records)
        save_state(state)

    def run_record(self, identifier, *, age=100, status='succeeded', **extra):
        return {'id': identifier, 'status': status, 'createdAt': self.now - age * governance.DAY, 'updatedAt': self.now - age * governance.DAY, 'events': [{'message': 'build finished'}], **extra}

    def test_history_preview_only_terminal_old_unreferenced_records(self):
        self.seed(buildRuns={key: self.run_record(key, **args) for key, args in {'old': {}, 'running': {'status': 'running'}, 'recent': {'age': 1}, 'details': {'age': 30}, 'retry-source': {}}.items()}, agentTasks={'retry': {'id': 'retry', 'status': 'queued', 'payload': {'buildId': 'retry-source'}}})
        plan = governance.preview_history(now=self.now)
        self.assertEqual({item['id']: item['action'] for item in plan['candidates']}, {'old': 'summary', 'details': 'details'})
        self.assertEqual(len(load_state()['buildRuns']), 5)
        fetched = governance.dispatch('GET', 'history/plans/' + plan['planId'])
        self.assertEqual(fetched['candidateCount'], 2)
        self.assertNotIn('fingerprint', fetched['candidates'][0])

    def test_apply_is_transactional_idempotent_and_details_keep_summary(self):
        self.seed(buildRuns={'old': self.run_record('old'), 'details': self.run_record('details', age=30)})
        plan = governance.preview_history(now=self.now)
        body = {'planId': plan['planId'], 'confirmed': True}
        with self.assertRaisesRegex(LumaError, 'grace'):
            governance.apply_history(body, now=self.now)
        result = governance.apply_history(body, now=self.now + governance.DAY)
        self.assertEqual(result['removedCount'], 2)
        state = load_state()
        self.assertNotIn('old', state['buildRuns'])
        self.assertEqual(state['buildRuns']['details']['status'], 'succeeded')
        self.assertEqual(state['buildRuns']['details']['events'], [])
        self.assertEqual(state['buildRuns']['details']['detailsExpiredAt'], self.now + governance.DAY)
        self.assertEqual(state['buildRuns']['details']['detailsRetentionDays'], 14)
        self.assertEqual(governance.apply_history(body, now=self.now + governance.DAY), result)

    def test_changed_records_and_new_retry_reference_are_rechecked(self):
        self.seed(buildRuns={'changed': self.run_record('changed'), 'new-reference': self.run_record('new-reference')})
        plan = governance.preview_history(now=self.now)
        state = load_state()
        state['buildRuns']['changed']['events'].append({'message': 'late log'})
        state['builderTasks'] = {'retry': {'id': 'retry', 'status': 'running', 'payload': {'buildId': 'new-reference'}}}
        save_state(state)
        result = governance.apply_history({'planId': plan['planId'], 'confirmed': True}, now=self.now + governance.DAY)
        self.assertEqual(result['removedCount'], 0)
        self.assertEqual(result['skippedCount'], 2)

    def test_current_deployment_and_rollback_references_protected(self):
        self.seed(buildRuns={'current': self.run_record('current'), 'rollback': self.run_record('rollback')}, deployments={'web': {'buildId': 'current', 'previous': {'buildId': 'rollback'}}})
        self.assertEqual(governance.preview_history(now=self.now)['candidateCount'], 0)

    def test_policy_change_confirmation_and_expiry_fail_closed(self):
        self.seed(buildRuns={'old': self.run_record('old')})
        plan = governance.preview_history(now=self.now)
        with self.assertRaisesRegex(LumaError, 'confirmation'):
            governance.apply_history({'planId': plan['planId']}, now=self.now + governance.DAY)
        governance.retention_policy({'summaryDays': 100})
        with self.assertRaisesRegex(LumaError, 'policy changed'):
            governance.apply_history({'planId': plan['planId'], 'confirmed': True}, now=self.now + governance.DAY)
        with self.assertRaisesRegex(LumaError, 'expired'):
            governance.apply_history({'planId': plan['planId'], 'confirmed': True}, now=self.now + 30 * governance.DAY)
        for body in ({'graceHours': 0}, {'summaryDays': 1}, {'detailDays': True}, {'unknown': 1}):
            with self.assertRaises(LumaError):
                governance.retention_policy(body)

    def test_inventory_reports_unknown_external_capacity_and_growth(self):
        first = governance.storage_inventory(now=self.now)
        self.assertIsNone(first['growthSince'])
        self.assertTrue(all(item['growthBytes'] is None for item in first['components']))
        (Path(self.temp.name) / 'metrics-history.json').write_text('12345')
        second = governance.storage_inventory(now=self.now + 3600)
        components = {item['id']: item for item in second['components']}
        self.assertEqual(components['metrics']['growthBytes'], 5)
        self.assertIsNone(components['builder']['bytes'])
        self.assertIsNone(components['volumes']['bytes'])
        self.assertNotIn('secret', json.dumps(second))
        self.assertFalse(second['policy']['automaticDeletion'])

    def test_builder_reference_digest_coverage_and_active_barrier(self):
        digest = 'sha256:' + 'a' * 64
        state = {'clusterId': 'test', '_storageReferenceCoverage': True, 'builderSourceSnapshots': {'s': {'digest': digest}}, 'agentTasks': {'self': {'status': 'running'}, 'build': {'status': 'running', 'node': 'builder'}}}
        manifest = governance.builder_reference_manifest(state, 'builder', exclude_task_id='self', now=self.now)
        self.assertEqual(manifest['protectedDigests'], [digest])
        self.assertTrue(manifest['blockedReasons'])
        del state['agentTasks']['build']
        manifest = governance.builder_reference_manifest(state, 'builder', exclude_task_id='self', now=self.now)
        self.assertFalse(manifest['blockedReasons'])
        self.assertFalse(governance.builder_reference_manifest({})['coverageComplete'])

    def test_prior_governance_inventory_never_turns_orphans_into_references(self):
        digest = 'sha256:' + 'b' * 64
        state = {'clusterId': 'test', '_storageReferenceCoverage': True, 'agentTasks': {'inventory': {'action': 'builder-storage', 'status': 'succeeded', 'result': {'files': [{'digest': digest}]}, 'payload': {'references': {'protectedDigests': [digest]}}}}}
        manifest = governance.builder_reference_manifest(state, now=self.now)
        self.assertFalse(manifest['protectedDigests'])
        self.assertFalse(manifest['blockedReasons'])
        self.assertFalse(governance.builder_reference_manifest({'clusterId': 'test'})['coverageComplete'])

    def test_timed_out_builds_are_terminal_and_other_nodes_do_not_block(self):
        state = {'clusterId': 'test', '_storageReferenceCoverage': True, 'builderTasks': {'finished': {'status': 'timed_out'}}, 'agentTasks': {'expired': {'status': 'timeout'}}, 'buildRuns': {'other': {'status': 'running', 'buildNode': 'other'}}}
        self.assertFalse(governance.builder_reference_manifest(state, 'builder')['blockedReasons'])

    def test_identical_previews_reuse_plan_and_metadata_expires(self):
        self.seed(buildRuns={'old': self.run_record('old')})
        first = governance.preview_history(now=self.now)
        second = governance.preview_history(now=self.now)
        self.assertEqual(first['planId'], second['planId'])
        governance.storage_inventory(now=first['expiresAt'] + 31 * governance.DAY)
        with self.assertRaisesRegex(LumaError, 'not found'):
            governance.dispatch('GET', 'history/plans/' + first['planId'])

    def test_reviewed_alert_retention_protects_delivery_that_becomes_pending(self):
        from luma.control import alerting
        old = self.now - 100 * governance.DAY
        with database.transaction() as conn:
            alerting._schema(conn)
            conn.execute("INSERT INTO alert_incidents(id,rule_id,rule_name,target,metric,severity,status,started_at,resolved_at,updated_at) VALUES(1,'r','Disk','node','disk','warning','resolved',?,?,?)", (old, old, old))
            conn.execute("INSERT INTO alert_events(incident_id,kind,at,detail) VALUES(1,'resolved',?,'recovered')", (old,))
            conn.execute("INSERT INTO alert_outbox(id,channel_id,incident_id,kind,text,status,next_attempt_at,created_at) VALUES(1,'channel',1,'resolved','recovered','sent',?,?)", (old, old))
        plan = governance.preview_history(now=self.now)
        self.assertEqual(plan['alertHistory']['incidentIds'], [1])
        self.assertEqual(plan['alertHistory']['deliveryIds'], [1])
        reopened = governance.dispatch('GET', 'history/plans/' + plan['planId'])
        self.assertEqual(reopened['alertHistory']['eventsCount'], 1)
        with database.transaction() as conn:
            conn.execute("UPDATE alert_outbox SET status='retry' WHERE id=1")
        result = governance.apply_history({'planId': plan['planId'], 'confirmed': True}, now=self.now + governance.DAY)
        self.assertEqual(result['alertHistory'], {'incidentsDeleted': 0, 'deliveriesDeleted': 0})
        with database.transaction() as conn:
            conn.execute("UPDATE alert_outbox SET status='sent' WHERE id=1")
        plan = governance.preview_history(now=self.now + governance.DAY)
        result = governance.apply_history({'planId': plan['planId'], 'confirmed': True}, now=self.now + 2 * governance.DAY)
        self.assertEqual(result['alertHistory'], {'incidentsDeleted': 1, 'deliveriesDeleted': 1})
        with database.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM alert_events').fetchone()[0], 0)

    def test_preview_is_bounded_without_global_history_clipping(self):
        self.seed(buildRuns={str(n): self.run_record(str(n)) for n in range(1002)})
        plan = governance.preview_history(now=self.now)
        self.assertTrue(plan['truncated'])
        self.assertEqual(plan['candidateCount'], 1000)
        self.assertEqual(len(load_state()['buildRuns']), 1002)


if __name__ == '__main__':
    unittest.main()
