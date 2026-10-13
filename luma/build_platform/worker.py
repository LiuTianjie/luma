from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from .activities import Activities, control_request
from .blobs import Blobs
from .config import Settings
from .nomad import Nomad
from .registry import Harbor
from .store import Store
from .workflow import BuildWorkflow

log = logging.getLogger(__name__)


async def dispatch(client, store, settings):
    while True:
        try:
            row = await asyncio.to_thread(store.outbox_claim)
            if not row:
                await asyncio.sleep(1)
                continue
            if row['operation'] == 'start':
                try:
                    await client.start_workflow(BuildWorkflow.run, row['run_id'], id='build/' + row['run_id'],
                        task_queue=settings.task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
                except WorkflowAlreadyStartedError:
                    pass
            elif row['operation'] == 'project':
                await asyncio.to_thread(control_request, settings, 'project', {'buildRunId': row['run_id']})
            # Cancellation is a durable DB flag observed by advance, rather than
            # terminating the workflow before it can confirm executor shutdown.
            await asyncio.to_thread(store.outbox_done, row['id'], row['revision'])
        except Exception:
            log.exception('Build outbox dispatch failed; lease will be retried')
            await asyncio.sleep(5)


async def main():
    settings = Settings.from_env()
    store = Store(settings)
    store.migrate()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    activities = Activities(settings, store, Blobs(settings), Nomad(settings), Harbor(settings))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            worker = Worker(client, task_queue=settings.task_queue, workflows=[BuildWorkflow],
                activities=[activities.advance, activities.failed], activity_executor=executor, max_concurrent_activities=32)
            async with worker:
                await dispatch(client, store, settings)
    finally:
        store.close()
