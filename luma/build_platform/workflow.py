"""Durable orchestration: history contains IDs and decisions, never build logs."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class BuildWorkflow:
    @workflow.run
    async def run(self, run_id: str, failing: bool = False) -> None:
        for _ in range(200):
            try:
                done = await workflow.execute_activity(
                    'luma-build-cleanup' if failing else 'luma-build-advance', run_id,
                    start_to_close_timeout=timedelta(minutes=55), heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30), maximum_attempts=0),
                )
                if done:
                    return
            except ActivityError:
                failing = True
            await workflow.sleep(2)
        workflow.continue_as_new(args=[run_id, failing])
