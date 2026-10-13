from __future__ import annotations

import json
import contextvars
import threading
from contextlib import contextmanager
import time
import urllib.request
import urllib.error

from ..errors import LumaError

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .model import Conflict, TERMINAL
from .source import deployment_result, prepare
from .placement import selected_nodes


def control_request(settings, path, body):
    request = urllib.request.Request(settings.control_url + '/v1/build-platform/' + path,
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + settings.control_token, 'Content-Type': 'application/json'})
    # A publish response is received only after the existing Control rollout
    # verifier finishes. On transport loss the next call reconciles the same ID.
    try:
        with urllib.request.urlopen(request, timeout=3000 if path == 'publish' else 30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403, 404, 409, 422}:
            raise Conflict('Control rejected ' + path + f' (HTTP {exc.code}); inspect Control deployment history') from None
        raise


@contextmanager
def heartbeats(run_id):
    if not activity.in_activity():
        yield
        return
    stopped = threading.Event()
    context = contextvars.copy_context()
    activity.heartbeat(run_id)
    def beat():
        while not stopped.wait(5):
            context.run(activity.heartbeat, run_id)
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


class Activities:
    def __init__(self, settings, store, blobs, nomad, harbor):
        self.settings, self.store, self.blobs, self.nomad, self.harbor = settings, store, blobs, nomad, harbor
        self.preparing = threading.BoundedSemaphore(2)

    @activity.defn(name='luma-build-advance')
    def advance(self, run_id: str) -> bool:
        try:
            with heartbeats(run_id):
                return self._advance(run_id)
        except Conflict as exc:
            self.store.update(run_id, message=str(exc))
            raise ApplicationError(str(exc), non_retryable=True) from None

    def _advance(self, run_id: str) -> bool:
        run = self.store.get(run_id, private=True)
        if run['status'] in TERMINAL:
            return True
        if run['cancel_requested']:
            return self.cleanup(run_id, 'canceled')
        if run['phase'] == 'releasing':
            return self.publish(run)
        if run['artifact_id']:
            if run['body'].get('autoDeploy') is False:
                self.store.update(run_id, status='succeeded', phase='complete', build_status='succeeded', capacity_held=False)
                return True
            artifact = self.store.artifact(run['artifact_id'])
            self.store.reserve_target(run_id, artifact['result']['targetKey'])
            self.store.create_release(run_id, artifact['id'], artifact['result']['targetKey'])
            if self.store.begin_release(run_id):
                return self.publish(self.store.get(run_id, private=True))
            return False
        if not run['prepared'].get('sourceObject'):
            if not self.preparing.acquire(blocking=False):
                return False
            try:
                self.store.update(run_id, status='running', phase='preparing', build_status='preparing')
                context = control_request(self.settings, 'prepare', run['body'])
                plan = prepare(run, context, self.blobs, self.settings, self.store)
                self.store.update(run_id, prepared=plan, status='queued', phase='waiting-node', build_status='queued')
                self.blobs.milestone(self.store, run_id, 'source', 'Freeze source', plan['sha'])
            except LumaError as exc:
                raise Conflict(str(exc)) from None
            finally:
                self.preparing.release()
            return False
        with self.store.execution_guard(run_id) as owned:
            if not owned:
                return False
            return self._build(self.store.get(run_id, private=True))

    def _build(self, run):
        run_id = run['id']
        if run['status'] in TERMINAL or run['artifact_id']:
            return False
        if run['cancel_requested']:
            return self.cleanup(run_id, 'canceled')
        plan = run['prepared']
        receipts = []
        recorded = {a['number']: a for a in self.store.attempts(run_id)}
        if recorded and all(a['status'] == 'succeeded' for a in recorded.values()) and run.get('capacity_held'):
            self.store.update(run_id, capacity_held=False, active_platform='', phase='waiting-node')
        for number, platform in enumerate(plan['platforms'], 1):
            attempt = recorded.get(number)
            if attempt and attempt['status'] == 'succeeded':
                receipts.append(attempt['result'])
                continue
            if not attempt or attempt['status'] in {'pending', 'submitting'}:
                pool = control_request(self.settings, 'pool', {})['nodes']
                platform = plan['platforms'][number - 1]
                try:
                    selected_nodes(pool, platform, run['body'].get('buildNode', ''))
                    allowed = self.nomad.eligible(platform, pool, run['body'].get('buildNode', ''))
                except LumaError as exc:
                    raise Conflict(str(exc)) from None
                if not allowed and not attempt:
                    message = 'Waiting for an approved, ready native build node with sufficient resources: ' + platform
                    if run['phase'] != 'waiting-node' or run['message'] != message or run.get('capacity_held'):
                        self.store.update(run_id, status='queued', phase='waiting-node', build_status='queued', capacity_held=False, active_platform='', message=message)
                    return False
                if not self.store.acquire(run_id, platform):
                    return False
                attempt = self.nomad.submit(self.store, run, number, allowed_nodes=allowed)
            state = self.nomad.observe(attempt['job_id'])
            if not state['terminal']:
                if state['status'] == 'running' and run['phase'] != 'building':
                    self.store.update(run_id, phase='building', build_status='building', message='')
                if state['status'] == 'pending' and time.time() - attempt['created_at'] > self.settings.placement_timeout:
                    raise Conflict('native build placement timed out; verify selected node capacity, executor image and Docker permissions')
                if state['status'] == 'starting':
                    if run['phase'] != 'starting':
                        self.store.update(run_id, phase='starting', build_status='preparing', message='Starting native executor')
                    if time.time() - attempt['created_at'] > self.settings.startup_timeout:
                        raise Conflict('native executor startup timed out; inspect image pull and bootstrap events')
                timeout = max(60, min(int(run['body'].get('buildTimeout') or 7200), 86400))
                if time.time() - attempt['created_at'] > timeout + 300:
                    raise Conflict('build allocation deadline exceeded')
                return False
            # Only revoke the lease after Nomad confirms termination. A receipt
            # alone cannot release capacity while BuildKit is still running.
            attempt = self.store.attempt(run_id, number)
            if state['status'] != 'succeeded' or not attempt['result']:
                raise Conflict('executor terminated without a successful image receipt')
            receipts.append(attempt['result'])
            if attempt['status'] != 'succeeded':
                self.store.finish_attempt(attempt['id'], 'succeeded')
                self.nomad.revoke(attempt)
                self.store.update(run_id, capacity_held=False, active_platform='', phase='waiting-node')
        self.store.update(run_id, phase='verifying', build_status='verifying')
        images, caches = {}, []
        for target in plan['targets']:
            parts = []
            for receipt in receipts:
                if receipt['platform'] in target['platforms']:
                    part = self.harbor.platform_manifest(receipt['images'][target['key']], receipt['platform'])
                    part['platform'] = dict(zip(('os', 'architecture'), receipt['platform'].split('/')))
                    parts.append(part)
            if len(parts) != len(target['platforms']):
                raise Conflict('image receipt does not cover the requested platforms')
            image = self.harbor.index(target['repository'], 'build-' + run_id, parts)
            self.harbor.pin(image, 'build-' + run_id)
            images[target['key']] = image
        for receipt in receipts:
            caches.extend(receipt['cacheRefs'])
        result = {**deployment_result(plan, images), 'targetKey': plan['targetKey']}
        artifact_id = self.store.save_artifact(run_id, result, caches)
        self.store.update(run_id, artifact_id=artifact_id, result=result, build_status='succeeded', capacity_held=False)
        self.blobs.milestone(self.store, run_id, 'artifact', 'Verify artifact', artifact_id)
        if run['body'].get('autoDeploy') is False:
            self.store.update(run_id, status='canceled' if self.store.get(run_id)['cancel_requested'] else 'succeeded', phase='complete')
            return True
        return False

    def publish(self, run):
        release = self.store.release(run['release_id'])
        if release['status'] == 'succeeded':
            self.store.update(run['id'], status='succeeded', phase='complete', release_status='succeeded', result=release['result'])
            return True
        if release['generation'] < release['desired_generation']:
            self.store.finish_release(release['id'], 'superseded', {})
            self.store.update(run['id'], status='superseded', phase='complete', release_status='superseded', message='A newer request owns this deployment target')
            return True
        result = control_request(self.settings, 'publish', {'releaseId': release['id']})
        status = 'superseded' if result.get('superseded') else 'succeeded'
        self.store.finish_release(release['id'], status, result)
        self.store.update(run['id'], status=status, phase='complete', release_status=status, result=result)
        return True

    def cleanup(self, run_id, status):
        with self.store.execution_guard(run_id) as owned:
            if not owned:
                return False
            return self._cleanup(run_id, status)

    def _cleanup(self, run_id, status):
        all_terminal = True
        for attempt in self.store.attempts(run_id):
            if attempt['status'] in {'succeeded', 'failed', 'canceled', 'rejected'}:
                self.nomad.revoke(attempt)
                continue
            state = self.nomad.observe(attempt['job_id'])
            if state['status'] == 'missing' and attempt['status'] == 'pending':
                # No registration was attempted. Guard excludes a live submitter.
                self.store.finish_attempt(attempt['id'], 'rejected')
                self.nomad.revoke(attempt)
                continue
            if state['status'] == 'missing' and attempt['status'] == 'submitting':
                state = self.nomad.fence_submission(attempt)
            if not state['terminal']:
                state = self.nomad.stop(attempt['job_id'])
            if not state['terminal']:
                all_terminal = False
            else:
                self.store.finish_attempt(attempt['id'], state['status'])
                self.nomad.revoke(attempt)
        if all_terminal:
            run = self.store.get(run_id)
            if run['release_id']:
                self.store.finish_release(run['release_id'], status, run['result'], 'Release did not complete')
            self.store.update(run_id, status=status, phase='complete',
                build_status=run['build_status'] if run['artifact_id'] else status,
                release_status=status if run['release_id'] else '',
                message='Canceled after execution stopped' if status == 'canceled' else run['message'] or 'Build or release failed; inspect workflow and executor logs')
        else:
            self.store.update(run_id, status='canceling', message='Waiting for Nomad to confirm execution has stopped')
        return all_terminal

    @activity.defn(name='luma-build-cleanup')
    def failed(self, run_id: str) -> bool:
        with heartbeats(run_id):
            return self.cleanup(run_id, 'canceled' if self.store.get(run_id)['cancel_requested'] else 'failed')
