import argparse
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from luma.control import build_queue as queue
from luma.control import server as srv
from luma.control.client import ControlClient
from luma.control.state import init_state, load_state, save_state
from luma.cli import _wait_for_queued_build
from luma.errors import LumaError


class BuildQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / 'state'), LUMA_CONTROL_CONFIG=str(root / 'luma.yaml'))
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / 'luma.yaml').write_text('providers: {}\n')
        state = init_state(domain='luma.example.com', cluster_id='test', overwrite=True)
        state['build'] = {'defaultNode': 'builder', 'registryHost': 'builder:5000'}
        state['nodes'] = {'cn': {'name': 'cn', 'region': 'cn', 'nomadStatus': 'ready',
                                  'agent': {'status': 'ready', 'os': 'linux', 'arch': 'amd64'}}}
        for name in ('builder', 'builder2'):
            state['nodes'][name] = {'name': name, 'agent': {'status': 'ready', 'capabilities': ['docker-build']}}
        save_state(state)
        self.token = state['deployToken']

    def remote(self, project='acme/app', build_node='builder', **body):
        return srv._create_build_run(body, source=f'https://github.com/{project}.git', build_node=build_node,
                                     project_key=project, queued_work=body)

    def prepare(self, **body):
        return srv.handle_local_build_prepare(self.token, {'repoUrl': 'https://github.com/acme/app.git', 'region': 'cn', 'queue': True, **body})

    def submit(self, prepared, **extra):
        return srv.handle_local_build_complete(self.token, prepared['run']['id'], {'queue': True,
            'buildResult': {'kind': 'service', 'image': prepared['upload']['image'],
                            'manifest': 'name: app\nregion: cn\nexposure: none\n'}, **extra})

    def finish(self, item, status='succeeded'):
        srv._complete_build_run(item[0], status)
        srv._mutate_control_state(lambda state: queue.discard(state, state['buildRuns'][item[0]]))

    def test_fifo_atomic_claim_and_other_project_independence(self):
        first, second, other = self.remote(), self.remote(), self.remote('acme/other', build_node='builder2')
        self.assertEqual(queue.position(second), {'queuePosition': 2, 'waitingFor': first, 'waitReason': 'target'})
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: queue.claim(), range(6)))
        self.assertEqual({item[0] for item in claims if item}, {first, other})
        self.assertIsNone(queue.claim())
        self.finish(next(item for item in claims if item and item[0] == first), 'failed')
        self.assertEqual(queue.claim()[0], second)

    def test_local_uploads_replace_same_target_on_submission(self):
        a, b = self.prepare(), self.prepare()
        remote = self.remote()
        self.submit(b)
        self.submit(a)
        self.assertEqual(queue.claim()[0], remote)
        self.finish((remote, {}))
        self.assertEqual(load_state()['buildRuns'][b['run']['id']]['status'], 'canceled')
        self.assertEqual(queue.claim()[0], a['run']['id'])

    def test_same_sidecar_only_latest_survives(self):
        first = self.remote(composeSidecar='deploy/prod/luma.compose.yml')
        second = self.remote(composeSidecar='deploy/prod/luma.compose.yml')
        latest = self.remote(composeSidecar='deploy/prod/luma.compose.yml')
        state = load_state()
        for old in (first, second):
            self.assertEqual(state['buildRuns'][old]['status'], 'canceled')
            self.assertNotIn(old, state['buildQueue'])
        self.assertEqual(queue.claim()[0], latest)

    def test_different_sidecars_run_independently(self):
        first = self.remote(composeSidecar='deploy/prod/luma.compose.yml')
        second = self.remote(build_node='builder2', composeSidecar='deploy/test/luma.compose.yml')
        self.assertEqual(queue.position(second), {'queuePosition': 1, 'waitingFor': '', 'waitReason': 'ready'})
        self.assertEqual(queue.claim()[0], first)
        self.assertEqual(queue.claim()[0], second)

    def test_same_sidecar_different_refs_run_independently(self):
        first = self.remote(composeSidecar='deploy/test/luma.compose.yml', ref='dev')
        second = self.remote(build_node='builder2', composeSidecar='deploy/test/luma.compose.yml', ref='main')
        state = load_state()
        self.assertEqual(state['buildRuns'][first]['status'], 'queued')
        self.assertNotIn('supersededBy', state['buildRuns'][first])
        self.assertEqual(queue.claim()[0], first)
        self.assertEqual(queue.claim()[0], second)

    def test_bare_imports_lock_only_their_branch(self):
        main = self.remote(ref='main')
        dev = self.remote(ref='dev', build_node='builder2')
        self.assertEqual(queue.claim()[0], main)
        self.assertEqual(queue.claim()[0], dev)

    def test_local_uploads_keep_their_prepared_branch_scope(self):
        main, dev = self.prepare(ref='main'), self.prepare(ref='dev')
        self.submit(main)
        self.submit(dev)
        self.assertEqual(queue.claim()[0], main['run']['id'])
        self.assertEqual(queue.claim()[0], dev['run']['id'])

    def test_same_paths_on_different_git_hosts_do_not_replace(self):
        first = self.remote(ref='main', composeSidecar='luma.compose.yml')
        body = {'ref': 'main', 'composeSidecar': 'luma.compose.yml'}
        other = srv._create_build_run(body, source='https://git.example.com/acme/app.git',
                                     build_node='builder2', project_key='acme/app', queued_work=body)
        self.assertEqual(queue.claim()[0], first)
        self.assertEqual(queue.claim()[0], other)

    def test_unknown_target_fences_known_target_only_on_same_branch(self):
        first = self.remote(ref='main')
        same = self.remote(ref='main', composeSidecar='prod.yml', build_node='builder2')
        dev = self.remote(ref='dev', composeSidecar='prod.yml', build_node='builder2')
        self.assertEqual(queue.claim()[0], first)
        self.assertEqual(queue.position(same)['waitReason'], 'target')
        self.assertEqual(queue.claim()[0], dev)

    def test_main_latest_wins_does_not_cancel_dev(self):
        main = self.remote(ref='main', composeSidecar='luma.compose.yml')
        dev = self.remote(ref='dev', composeSidecar='luma.compose.yml', build_node='builder2')
        queue.claim()
        latest = self.remote(ref='refs/heads/main', composeSidecar='./luma.compose.yml')
        newer = self.remote(ref='main', composeSidecar='luma.compose.yml')
        runs = load_state()['buildRuns']
        self.assertEqual(runs[main]['status'], 'canceling')
        self.assertEqual(runs[latest]['status'], 'canceled')
        self.assertEqual(runs[latest]['supersededBy'], newer)
        self.assertNotIn('supersededBy', runs[dev])
        self.assertEqual(queue.claim()[0], dev)
        self.assertIsNone(queue.claim())
        self.finish((main, {}), 'canceled')
        self.assertEqual(queue.claim()[0], newer)

    def test_legacy_keys_are_recomputed_after_upgrade(self):
        main = self.remote(ref='main', composeSidecar='luma.compose.yml')
        dev = self.remote(ref='dev', composeSidecar='luma.compose.yml', build_node='builder2')
        state = load_state()
        for build_id in (main, dev):
            state['buildRuns'][build_id].pop('queueIdentityVersion')
            state['buildRuns'][build_id]['queueKey'] = '["project","acme/app"]'
            state['buildRuns'][build_id]['replacementKey'] = '["sidecar","https://github.com/acme/app.git","luma.compose.yml"]'
        save_state(state)
        newer = self.remote(ref='main', composeSidecar='luma.compose.yml')
        self.assertEqual(load_state()['buildRuns'][main]['status'], 'canceled')
        self.assertEqual(queue.claim()[0], dev)
        self.assertEqual(queue.claim()[0], newer)

    def test_builder_slot_is_released_before_deployment_finishes(self):
        main = self.remote(ref='main')
        dev = self.remote(ref='dev')
        queue.claim()
        self.assertIsNone(queue.claim())
        self.assertEqual(queue.position(dev)['waitReason'], 'builder')
        state = load_state()
        state['buildRuns'][main]['agentTaskId'] = 'built'
        state['agentTasks'] = {'built': {'id': 'built', 'status': 'succeeded', 'nodeName': 'builder'}}
        save_state(state)
        self.assertEqual(queue.claim()[0], dev)
        self.assertTrue(load_state()['buildRuns'][main]['queueExecuting'])

    def test_busy_builder_backlog_does_not_occupy_other_slots(self):
        backlog = [self.remote(f'acme/app-{i}') for i in range(8)]
        other = self.remote('acme/other', build_node='builder2')
        local = self.prepare()
        self.submit(local)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: queue.claim(), range(8)))
        self.assertEqual({item[0] for item in claims if item}, {backlog[0], other, local['run']['id']})
        runs = load_state()['buildRuns']
        self.assertTrue(all(runs[build_id]['status'] == 'queued' for build_id in backlog[1:]))
        self.assertEqual(queue.position(backlog[-1])['queuePosition'], 8)

    def test_unrelated_agent_task_reserves_builder(self):
        first = self.remote()
        state = load_state()
        state['agentTasks'] = {'maintenance': {'id': 'maintenance', 'status': 'queued', 'nodeName': 'builder'}}
        save_state(state)
        self.assertIsNone(queue.claim())
        self.assertEqual(queue.position(first)['waitingFor'], 'maintenance')

    def test_offline_builder_does_not_block_ready_builder(self):
        first = self.remote()
        other = self.remote('acme/other', build_node='builder2')
        state = load_state()
        state['nodes']['builder']['agent']['status'] = 'offline'
        save_state(state)
        self.assertEqual(queue.position(first)['waitReason'], 'builder-offline')
        self.assertEqual(queue.claim()[0], other)
        state = load_state()
        state['nodes']['builder']['agent']['status'] = 'ready'
        save_state(state)
        self.assertEqual(queue.claim()[0], first)

    def test_global_capacity_is_atomic_and_reported(self):
        self.remote()
        second = self.remote('acme/other', build_node='builder2')
        with patch.dict(os.environ, LUMA_BUILD_QUEUE_CONCURRENCY='1'):
            with ThreadPoolExecutor(max_workers=4) as pool:
                claims = list(pool.map(lambda _: queue.claim(), range(4)))
            self.assertEqual(len([item for item in claims if item]), 1)
            self.assertEqual(queue.position(second)['waitReason'], 'capacity')

    def test_idle_and_blocked_polls_do_not_rewrite_state(self):
        from luma.control import database
        with patch.object(database, 'write_state', wraps=database.write_state) as write:
            self.assertIsNone(queue.claim())
            write.assert_not_called()
        self.remote()
        self.remote(ref='dev')
        queue.claim()
        with patch.object(database, 'write_state', wraps=database.write_state) as write:
            self.assertIsNone(queue.claim())
            write.assert_not_called()

    def test_cleanup_retries_database_contention_without_replaying_work(self):
        first = self.remote()
        item = queue.claim()
        mutate = queue.mutate_state
        attempts = []
        def locked_once(callback):
            attempts.append(True)
            if len(attempts) == 1:
                raise sqlite3.OperationalError('database is locked')
            return mutate(callback)
        def finish(*args, **kwargs):
            srv._complete_build_run(first, 'succeeded')
            return {}
        with patch.object(srv, 'handle_build_deploy', side_effect=finish) as deploy, \
             patch.object(queue, 'mutate_state', side_effect=locked_once), patch.object(queue.time, 'sleep'):
            queue.execute(item)
        deploy.assert_called_once()
        self.assertEqual(len(attempts), 2)
        state = load_state()
        self.assertEqual(state['buildRuns'][first]['status'], 'succeeded')
        self.assertNotIn('queueExecuting', state['buildRuns'][first])
        self.assertNotIn(first, state['buildQueue'])

    def test_queue_reads_do_not_hydrate_unrelated_history(self):
        from luma.control import database
        state = load_state()
        state.setdefault('buildRuns', {})['old-history'] = {'id': 'old-history', 'status': 'succeeded'}
        save_state(state)
        queued = self.remote()
        read = database.read_entity
        def guard(conn, kind, identifier):
            self.assertNotEqual(identifier, 'old-history')
            return read(conn, kind, identifier)
        with patch.object(database, 'read_entity', side_effect=guard):
            self.assertEqual(queue.position(queued)['waitReason'], 'ready')
            self.assertEqual(queue.claim()[0], queued)

    def test_legacy_submission_cannot_bypass_terminal_worker_fence(self):
        first = self.remote(ref='main')
        queue.claim()
        srv._complete_build_run(first, 'failed', message='still unwinding')
        with self.assertRaisesRegex(LumaError, 'active build'):
            srv._create_build_run({'ref': 'main'}, source='https://github.com/acme/app.git',
                                  build_node='builder', project_key='acme/app')
        srv._create_build_run({'ref': 'dev'}, source='https://github.com/acme/app.git',
                              build_node='builder2', project_key='acme/app')

    def test_manager_update_waits_for_terminal_worker_exit(self):
        build_id = self.remote()
        item = queue.claim()
        self.assertEqual(item[0], build_id)
        state = load_state()
        state['buildRuns'][build_id]['status'] = 'canceled'
        self.assertIn(build_id, srv._active_build_blockers(state))
        queue.discard(state, state['buildRuns'][build_id])
        self.assertNotIn(build_id, srv._active_build_blockers(state))

    def test_cancel_can_unwind_while_waiting_for_runtime_lock(self):
        first = self.remote(ref='main')
        queue.claim()
        entered, exited = threading.Event(), threading.Event()
        failures = []
        original = srv._build_run_cancel_requested
        def observe(build_id):
            entered.set()
            return original(build_id)
        @srv._serialize_deploy
        def deploy(token, body):
            self.fail('a canceled deployment entered the runtime')
        def execute():
            try:
                deploy(self.token, {'gitSource': {'buildRunId': first}})
            except LumaError as exc:
                failures.append(str(exc))
            finally:
                exited.set()
        with patch.object(srv, '_build_run_cancel_requested', side_effect=observe), srv._DEPLOY_LOCK:
            worker = threading.Thread(target=execute)
            worker.start()
            self.assertTrue(entered.wait(2))
            srv.handle_build_run_cancel(self.token, first)
            self.assertTrue(exited.wait(2))
        worker.join(2)
        self.assertEqual(failures, ['build canceled while waiting for runtime deployment'])

    def test_concurrent_agent_polls_lease_only_one_serial_task(self):
        token = srv.handle_node_agent_token(self.token, {'nodeName': 'builder'})['agentToken']
        state = load_state()
        state['agentTasks'] = {task_id: {'id': task_id, 'status': 'queued', 'nodeName': 'builder',
                                        'action': 'build-image', 'payload': {}}
                               for task_id in ('task-1', 'task-2')}
        save_state(state)
        body = {'nodeName': 'builder', 'os': 'linux', 'capabilities': ['docker-build'], 'activeTaskId': ''}
        with patch.object(srv, '_record_metrics_history'), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: srv.handle_node_agent_lease(token, body), range(2)))
        self.assertEqual(len([result for result in results if result['task']]), 1)
        tasks = load_state()['agentTasks']
        self.assertEqual(tasks['task-1']['status'], 'running')
        self.assertEqual(tasks['task-2']['status'], 'queued')

    def test_completed_task_heartbeat_cannot_fail_next_build(self):
        token = srv.handle_node_agent_token(self.token, {'nodeName': 'builder'})['agentToken']
        state = load_state()
        state['nodes']['builder']['agent'].update(activeTaskId='next', activeTaskObservedAt=123)
        state['agentTasks'] = {
            'old': {'id': 'old', 'status': 'succeeded', 'nodeName': 'builder', 'completedAt': int(time.time())},
            'next': {'id': 'next', 'status': 'running', 'nodeName': 'builder', 'leasedAt': int(time.time()) - 120},
        }
        save_state(state)
        with patch.object(srv, '_record_metrics_history'):
            srv.handle_node_agent_heartbeat(token, {'nodeName': 'builder', 'activeTaskId': 'old'})
        state = load_state()
        self.assertEqual(state['agentTasks']['next']['status'], 'running')
        self.assertEqual(state['nodes']['builder']['agent']['activeTaskId'], 'next')
        self.assertEqual(state['nodes']['builder']['agent']['activeTaskObservedAt'], 123)

    def test_older_running_task_heartbeat_cannot_reap_newer_lease(self):
        state = load_state()
        now = int(time.time())
        state['agentTasks'] = {
            'old': {'id': 'old', 'status': 'running', 'nodeName': 'builder', 'leasedAt': now - 300},
            'next': {'id': 'next', 'status': 'running', 'nodeName': 'builder', 'leasedAt': now - 120},
        }
        srv._reconcile_interrupted_agent_tasks(state, 'builder', 'old', now=now)
        self.assertEqual(state['agentTasks']['next']['status'], 'running')

    def test_delayed_idle_request_cannot_reap_a_later_lease(self):
        state = load_state()
        state['agentTasks'] = {'new': {'id': 'new', 'status': 'running', 'nodeName': 'builder', 'leasedAt': 110}}
        srv._reconcile_interrupted_agent_tasks(state, 'builder', '', observed_at=100, now=200)
        self.assertEqual(state['agentTasks']['new']['status'], 'running')
        srv._reconcile_interrupted_agent_tasks(state, 'builder', '', observed_at=201, now=201)
        self.assertEqual(state['agentTasks']['new']['status'], 'failed')

    def test_delayed_idle_request_is_not_fresh_proof_of_child_exit(self):
        state = load_state()
        run = {'agentTaskId': 'timed-out', 'buildNode': 'builder', 'queueOwnerReleasedAt': 150}
        state['agentTasks'] = {'timed-out': {'id': 'timed-out', 'status': 'timeout'}}
        with patch.object(srv.time, 'time', return_value=200):
            srv._update_agent_heartbeat(state['nodes']['builder'], {'activeTaskId': ''}, state=state, observed_at=100)
            self.assertFalse(queue._safe_to_release(state, run))
            srv._update_agent_heartbeat(state['nodes']['builder'], {'activeTaskId': ''}, state=state, observed_at=201)
            self.assertTrue(queue._safe_to_release(state, run))

    def test_parallel_workers_execute_independent_branches(self):
        main = self.remote(ref='main')
        dev = self.remote(ref='dev', build_node='builder2')
        entered = {build_id: threading.Event() for build_id in (main, dev)}
        release = threading.Event()
        def deploy(*args, _queued_run_id, **kwargs):
            entered[_queued_run_id].set()
            if not release.wait(5):
                raise RuntimeError('test execution was not released')
            srv._complete_build_run(_queued_run_id, 'succeeded')
            return {}
        worker = queue.BuildQueueWorker(concurrency=2)
        with patch.object(srv, 'handle_build_deploy', side_effect=deploy):
            try:
                worker.start()
                self.assertTrue(entered[main].wait(2))
                self.assertTrue(entered[dev].wait(2))
            finally:
                release.set()
                worker.close()
        runs = load_state()['buildRuns']
        self.assertEqual({runs[main]['status'], runs[dev]['status']}, {'succeeded'})

    def test_replacement_waits_for_owner_and_remote_child(self):
        first = self.remote(composeSidecar='deploy/prod.yml')
        item = queue.claim()
        state = load_state()
        state['buildRuns'][first]['agentTaskId'] = 'child'
        state['agentTasks'] = {'child': {'status': 'running', 'nodeName': 'builder'}}
        save_state(state)
        second = self.remote(composeSidecar='deploy/prod.yml')
        latest = self.remote(composeSidecar='deploy/prod.yml')
        self.assertIsNone(queue.claim())
        with patch.object(srv, 'handle_build_deploy', side_effect=LumaError('canceled')):
            queue.execute(item)
        state = load_state()
        self.assertTrue(state['buildRuns'][first]['queueOwnerReleased'])
        self.assertTrue(state['agentTasks']['child']['cancelRequestedAt'])
        self.assertEqual(state['buildRuns'][second]['status'], 'canceled')
        self.assertIsNone(queue.claim())
        state['agentTasks']['child']['status'] = 'canceled'
        save_state(state)
        self.assertEqual(queue.claim()[0], latest)
        self.assertNotIn('queueExecuting', load_state()['buildRuns'][first])

    def test_missing_receipt_requires_explicit_fresh_idle_observation(self):
        first = self.remote()
        item = queue.claim()
        second = self.remote()
        state = load_state()
        state['buildRuns'][first]['agentTaskId'] = 'missing-child'
        state['buildRuns'][first]['status'] = 'canceled'
        save_state(state)
        with patch.object(srv, 'handle_build_deploy', side_effect=LumaError('missing receipt')):
            queue.execute(item)
        state = load_state()
        released = state['buildRuns'][first]['queueOwnerReleasedAt']
        state['nodes']['builder'] = {'name': 'builder', 'agent': {'lastSeen': released + 2}}
        save_state(state)
        with patch.object(srv, '_node_agent_is_ready', return_value=True):
            self.assertIsNone(queue.claim())  # Missing activeTaskId is NOT idle.
            state['nodes']['builder']['agent'].update(activeTaskId='missing-child', activeTaskObservedAt=released + 2)
            save_state(state)
            self.assertIsNone(queue.claim())
            state['nodes']['builder']['agent']['activeTaskId'] = ''
            state['nodes']['builder']['agent']['activeTaskObservedAt'] = released
            save_state(state)
            self.assertIsNone(queue.claim())  # An old observation is not proof.
            state['nodes']['builder']['agent']['activeTaskObservedAt'] = released + 2
            save_state(state)
            self.assertEqual(queue.claim()[0], second)

    def test_retention_preserves_unconsumed_completion_receipt(self):
        first = self.remote()
        queue.claim()
        state = load_state()
        state['buildRuns'][first]['agentTaskId'] = 'child'
        state['agentTasks'] = {'child': {'status': 'canceled', 'completedAt': 1}}
        srv._prune_agent_tasks(state, now=100000)
        self.assertIn('child', state['agentTasks'])
        queue.discard(state, state['buildRuns'][first])
        srv._prune_agent_tasks(state, now=100000)
        self.assertNotIn('child', state['agentTasks'])

    def test_missing_receipt_unwinds_without_waiting_for_build_timeout(self):
        with patch.object(srv, 'load_state', side_effect=AssertionError('full history read')), patch.object(srv.time, 'sleep') as sleep:
            with self.assertRaisesRegex(LumaError, 'receipt missing'):
                srv._wait_node_agent_task('missing', 'builder', 'build-image', timeout=7200)
            sleep.assert_not_called()

    def test_recovery_retains_remote_child_fence(self):
        first = self.remote()
        queue.claim()
        second = self.remote()
        state = load_state()
        state['buildRuns'][first].update(status='canceled', agentTaskId='child')
        state['agentTasks'] = {'child': {'status': 'running', 'nodeName': 'builder'}}
        save_state(state)
        with patch.object(srv, '_CONTROL_PROCESS_INSTANCE_ID', 'restarted'):
            queue.recover()
        self.assertIsNone(queue.claim())
        state = load_state()
        state['agentTasks']['child']['status'] = 'canceled'
        save_state(state)
        self.assertEqual(queue.claim()[0], second)

    def test_stale_finalizing_does_not_release_a_live_worker(self):
        first = self.remote()
        second = self.remote()
        item = queue.claim()
        self.assertEqual(item[0], first)
        state = load_state()
        state['buildRuns'][first].update(status='finalizing', updatedAt=int(time.time()) - queue.FINALIZING_STALE_SECONDS - 1)
        save_state(state)
        self.assertIsNone(queue.claim())
        self.assertEqual(load_state()['buildRuns'][first]['status'], 'failed')
        self.assertIn('stalled without progress', load_state()['buildRuns'][first]['message'])
        self.assertTrue(load_state()['buildRuns'][first]['queueExecuting'])
        with patch.object(srv, 'handle_build_deploy', side_effect=LumaError('canceled')):
            queue.execute(item)
        self.assertEqual(queue.claim()[0], second)

    def test_stale_canceling_retains_fence_until_worker_exits(self):
        first = self.remote()
        second = self.remote()
        item = queue.claim()
        now = int(time.time())
        state = load_state()
        state['buildRuns'][first].update(
            status='canceling', cancelRequestedAt=now - queue.CANCELING_STALE_SECONDS - 1, updatedAt=now - queue.CANCELING_STALE_SECONDS - 1,
        )
        save_state(state)
        self.assertIsNone(queue.claim())
        self.assertEqual(load_state()['buildRuns'][first]['status'], 'canceled')
        with patch.object(srv, 'handle_build_deploy', side_effect=LumaError('canceled')):
            queue.execute(item)
        self.assertEqual(queue.claim()[0], second)

    def test_operator_can_finish_stuck_canceling(self):
        first = self.remote()
        queue.claim()
        state = load_state()
        state['buildRuns'][first]['status'] = 'canceling'
        save_state(state)
        public = srv.handle_build_run_cancel(self.token, first)
        self.assertEqual(public['run']['status'] if 'run' in public else public.get('status'), 'canceled')
        self.assertEqual(load_state()['buildRuns'][first]['status'], 'canceled')
        self.assertTrue(load_state()['buildRuns'][first]['queueExecuting'])
        self.remote()
        self.assertIsNone(queue.claim())

    def test_complete_does_not_resurrect_failed_run(self):
        first = self.remote()
        queue.claim()
        srv._complete_build_run(first, 'failed', message='stalled')
        srv._complete_build_run(first, 'succeeded', message='late success')
        self.assertEqual(load_state()['buildRuns'][first]['status'], 'failed')

    def test_cancel_queued_does_not_cancel_active_or_leak_payload(self):
        first = self.remote(envSecrets={'PASSWORD': 'first-value'})
        second = self.remote(envSecrets={'PASSWORD': 'second-value'})
        item = queue.claim()
        srv.handle_build_run_cancel(self.token, second)
        state = load_state()
        self.assertEqual(state['buildRuns'][first]['status'], 'running')
        self.assertEqual(state['buildRuns'][second]['status'], 'canceled')
        self.assertNotIn(second, state['buildQueue'])
        public = json.dumps(srv.handle_build_run_get(self.token, first))
        self.assertNotIn('first-value', public)
        self.assertNotIn('envSecrets', public)
        self.assertEqual(item[1]['body']['envSecrets']['PASSWORD'], 'first-value')

    def test_accepted_upload_replay_is_idempotent_and_validated(self):
        prepared = self.prepare()
        first = self.submit(prepared)
        second = self.submit(prepared)
        self.assertEqual(first['buildRunId'], second['buildRunId'])
        self.assertEqual(len(load_state()['buildQueue']), 1)
        with self.assertRaisesRegex(LumaError, 'different result'):
            self.submit(prepared, envSecrets={'PASSWORD': 'different'})
        other = self.prepare()
        with self.assertRaisesRegex(LumaError, 'reserved project repository'):
            srv.handle_local_build_complete(self.token, other['run']['id'], {'queue': True, 'buildResult': {
                'image': 'evil:5000/other:latest', 'kind': 'service', 'manifest': 'name: app'}})
        self.assertEqual(load_state()['buildRuns'][other['run']['id']]['status'], 'running')

    def test_worker_runs_local_deployment_with_attempt_secrets_and_cleans_up(self):
        prepared = self.prepare()
        self.submit(prepared, envSecrets={'PASSWORD': 'private-value'})
        item = queue.claim()
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}) as deploy:
            queue.execute(item)
        self.assertEqual(deploy.call_args.args[1]['envSecrets'], {'PASSWORD': 'private-value'})
        state = load_state()
        self.assertEqual(state['buildRuns'][item[0]]['status'], 'succeeded')
        self.assertNotIn(item[0], state['buildQueue'])
        self.assertNotIn('private-value', json.dumps(srv.handle_build_run_get(self.token, item[0])))

    def test_failed_progress_does_not_release_project_before_unwind(self):
        first, second = self.remote(), self.remote()
        queue.claim()
        srv._append_build_run_event(first, {'status': 'fail', 'message': 'step failed'})
        self.assertIsNone(queue.claim())
        self.finish((first, {}), 'failed')
        self.assertEqual(queue.claim()[0], second)

    def test_restart_keeps_queue_but_does_not_replay_active_deploy(self):
        first = self.prepare()
        self.submit(first)
        queued = self.remote()
        item = queue.claim()
        unuploaded = self.prepare()
        with patch.object(srv, '_CONTROL_PROCESS_INSTANCE_ID', 'new-process'):
            srv._reconcile_orphaned_build_runs_after_control_restart()
            queue.recover()
        state = load_state()
        self.assertEqual(state['buildRuns'][item[0]]['status'], 'failed')
        self.assertNotIn(item[0], state['buildQueue'])
        self.assertEqual(state['buildRuns'][queued]['status'], 'queued')
        self.assertEqual(state['buildRuns'][unuploaded['run']['id']]['status'], 'running')
        self.assertEqual(queue.claim()[0], queued)

    def test_upload_lease_no_longer_expires_after_acceptance(self):
        prepared = self.prepare()
        self.submit(prepared)
        item = queue.claim()
        srv._mutate_control_state(lambda state: srv._expire_stale_local_build_runs(state['buildRuns'], 9999999999))
        self.assertEqual(load_state()['buildRuns'][item[0]]['status'], 'finalizing')

    def test_retry_cannot_duplicate_a_queued_attempt(self):
        original = self.remote()
        with self.assertRaisesRegex(LumaError, 'active build cannot be retried'):
            srv._create_build_run({}, source='test', build_node='builder', project_key='acme/app', retry_of=original, queued_work={})

    def test_remote_submission_and_execution_keep_one_build_identity(self):
        with patch.object(srv, '_require_build_node', return_value='builder'), patch.object(srv, '_build_proxy_for_request', return_value=''):
            response = srv.handle_build_deploy(self.token, {'queue': True, 'repoUrl': 'https://github.com/acme/app.git', 'envSecrets': {'PASSWORD': 'one'}})
            build_id = response['buildRunId']
            self.assertEqual(response['run']['status'], 'queued')
            with patch.object(srv, '_run_node_agent_task', return_value={
                'kind': 'service', 'image': 'builder:5000/acme/app:immutable',
                'manifest': 'name: app\nimage: placeholder\nregion: cn\nexposure: none\n',
            }), patch.object(srv, 'handle_deployment', return_value={'service': 'app'}) as deploy:
                queue.execute(queue.claim())
        state = load_state()
        self.assertEqual(len(state['buildRuns']), 1)
        self.assertEqual(state['buildRuns'][build_id]['status'], 'succeeded')
        self.assertEqual(deploy.call_args.args[1]['gitSource']['buildRunId'], build_id)
        self.assertEqual(deploy.call_args.args[1]['envSecrets'], {'PASSWORD': 'one'})

    def test_workflow_is_recorded_without_a_waiting_client(self):
        from luma.cli import build_parser
        from luma.deploy_workflow import make_recipe
        prepared = self.prepare()
        recipe = make_recipe(build_parser().parse_args(['build', 'local', '.']))
        self.submit(prepared, workflow={'recipe': recipe, 'expectedRevision': 0,
                                       'selector': {'name': 'app', 'repoUrl': 'https://github.com/acme/app.git'}})
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}):
            queue.execute(queue.claim())
        state = load_state()
        self.assertEqual(state['deploymentWorkflows']['app']['lastSuccess']['buildId'], prepared['run']['id'])
        self.assertTrue(state['buildRuns'][prepared['run']['id']]['result']['workflow']['saved'])
        self.assertNotIn('workflow', state['buildRuns'][prepared['run']['id']]['request'])

    def test_workflow_conflict_does_not_fail_deployment(self):
        prepared = self.prepare()
        self.submit(prepared, workflow={'expectedRevision': 10})
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}), patch.object(srv, 'handle_workflow_record', side_effect=LumaError('newer recipe preserved')):
            queue.execute(queue.claim())
        run = srv.handle_build_run_get(self.token, prepared['run']['id'])['run']
        self.assertEqual(run['status'], 'succeeded')
        self.assertFalse(run['result']['workflow']['saved'])
        self.assertEqual(run['result']['workflow']['warning'], 'newer recipe preserved')

    def test_queue_result_preserves_compose_sidecar_confirmation(self):
        self.assertEqual(srv._build_run_result_summary({'deployment': 'app', 'composeSidecar': 'luma.compose.yml'})['composeSidecar'], 'luma.compose.yml')

    def test_uploaded_images_are_protected_while_queued(self):
        from luma.registry_management import collect_state_image_references
        prepared = self.prepare()
        self.submit(prepared)
        refs = collect_state_image_references(load_state(), 'builder:5000')
        self.assertTrue(any(ref['repository'] == 'acme/app' and ref['tag'] == prepared['upload']['tag'] for ref in refs))

    def test_http_acceptance_is_independent_of_deployment_completion(self):
        from starlette.testclient import TestClient
        prepared = self.prepare()
        entered, release = threading.Event(), threading.Event()
        def deploy(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test deployment was not released')
            return {'service': 'app'}
        with patch.object(srv, 'handle_deployment', side_effect=deploy), patch.object(srv.operations_api.OperationsWorker, 'start'), patch.object(srv.operations_api.OperationsWorker, 'close'):
            with TestClient(srv.create_app()) as client:
                try:
                    response = client.post(f"/v1/builds/local/{prepared['run']['id']}/complete", headers={'Authorization': f'Bearer {self.token}'}, json={
                        'queue': True, 'buildResult': {'kind': 'service', 'image': prepared['upload']['image'], 'manifest': 'name: app\nregion: cn\nexposure: none\n'}})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()['queued'])
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(load_state()['buildRuns'][prepared['run']['id']]['status'], 'finalizing')
                finally:
                    release.set()
        self.assertEqual(load_state()['buildRuns'][prepared['run']['id']]['status'], 'succeeded')

    def test_worker_calls_existing_remote_executor_without_creating_another_run(self):
        run = self.remote(repoUrl='https://github.com/acme/app.git')
        with patch.object(srv, 'handle_build_deploy') as execute:
            queue.execute(queue.claim())
        self.assertEqual(execute.call_args.kwargs['_queued_run_id'], run)
        self.assertNotIn('queue', execute.call_args.args[1])


class QueueClientTests(unittest.TestCase):
    def test_idle_lease_explicitly_reports_no_active_task(self):
        client = ControlClient('https://control.example.com', 'token')
        with patch.object(client, 'request', return_value={}) as request:
            client.lease_agent_task(node_name='builder')
        self.assertEqual(request.call_args.args[2]['activeTaskId'], '')

    def test_capability_negotiation_and_legacy_fallback(self):
        for supported in (True, False):
            client = ControlClient('https://control.example.com', 'token')
            with patch.object(client, 'health', return_value={'capabilities': ['build-queue-v1'] if supported else []}), patch.object(client, 'request', return_value={}) as request:
                client.prepare_local_build({'repoUrl': 'https://github.com/acme/app.git'})
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)
                client.complete_local_build('b1', build_result={})
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)
                client.build_deploy(repo_url='https://github.com/acme/app.git')
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)

    def test_poll_timeout_never_cancels_or_fails_server_job(self):
        args = argparse.Namespace(timeout=0, format='json', quiet=True)
        client = Mock()
        client.get_build.return_value = {'run': {'status': 'queued', 'queuePosition': 2, 'waitingFor': 'b0'}}
        with self.assertRaisesRegex(LumaError, 'server task continues'):
            _wait_for_queued_build(args, client, {'queued': True, 'buildRunId': 'b1'})
        client.cancel_build.assert_not_called()
        client.fail_local_build.assert_not_called()

    def test_poll_does_not_treat_acceptance_as_deployment_success(self):
        args = argparse.Namespace(timeout=10, format='json', quiet=True)
        client = Mock()
        client.get_build.side_effect = [
            {'run': {'status': 'queued', 'queuePosition': 1}},
            {'run': {'status': 'succeeded', 'result': {'service': 'app', 'image': 'image:immutable'}}},
        ]
        with patch('luma.cli.time.sleep'):
            result = _wait_for_queued_build(args, client, {'queued': True, 'buildRunId': 'b1'})
        self.assertEqual(result, {'service': 'app', 'image': 'image:immutable', 'buildRunId': 'b1'})


if __name__ == '__main__':
    unittest.main()
