from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_luma_adapter import (  # noqa: E402
    AdapterErrorCode,
    BuilderLimits,
    BuilderTaskEventPage,
    FakeLuma,
    LumaAdapterError,
    LumaCallContext,
    ServicePrincipal,
)
from lae_store import (  # noqa: E402
    CreateOperation,
    EventInput,
    LeaseLost,
    OperationRecord,
    OperationStatus,
    Principal,
    TenantScope,
    UpdateCheckResult,
    new_id,
)
from lae_worker import (  # noqa: E402
    AnalysisRecording,
    ArtifactDescriptor,
    ArtifactIngestingAnalysisRecorder,
    ArtifactTransferBinding,
    AnalyzeContextInvalid,
    AnalyzeSourceContext,
    AnalyzeStepRunner,
    AnalyzeWorker,
    AnalyzeWorkerConfig,
    InMemoryAnalyzeStateStore,
    InMemoryAnalysisArtifactCatalog,
    InMemoryArtifactTransferBroker,
    InMemoryS3CompatibleObjectStore,
    PostgresAnalysisRecorder,
    PostgresAnalyzeStateStore,
    StepStatus,
    build_worker_from_env,
)


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


async def _no_sleep() -> None:
    return None


class FakeOperations:
    def __init__(self, operation: OperationRecord) -> None:
        self.operation = operation
        self.events: list[EventInput] = []
        self.lose_lease = False
        self.heartbeat_calls = 0
        self.cancel_on_heartbeat_call: int | None = None

    async def claim_next(self, *, worker_id, kinds, lease_seconds=60):
        if self.operation.kind not in kinds or self.operation.status != "queued":
            return None
        self.operation = replace(
            self.operation,
            status="running",
            lease_owner=worker_id,
            lease_expires_at=NOW + timedelta(seconds=lease_seconds),
        )
        return self.operation

    async def heartbeat(
        self, scope, operation_id, *, worker_id, lease_seconds=60
    ) -> OperationRecord:
        self._owned(scope, operation_id, worker_id)
        self.heartbeat_calls += 1
        if self.lose_lease:
            raise LeaseLost("simulated lease loss")
        if self.cancel_on_heartbeat_call == self.heartbeat_calls:
            self.request_cancel()
        self.operation = replace(
            self.operation,
            lease_expires_at=NOW + timedelta(seconds=lease_seconds),
        )
        return self.operation

    async def append_event(
        self, scope, operation_id, event, *, worker_id
    ) -> int:
        self._owned(scope, operation_id, worker_id)
        self.events.append(event)
        self.operation = replace(
            self.operation,
            phase=event.phase or self.operation.phase,
            last_event_seq=self.operation.last_event_seq + 1,
        )
        return self.operation.last_event_seq

    async def complete(
        self,
        scope,
        operation_id,
        *,
        worker_id,
        status,
        result=None,
        error_code=None,
        error_message=None,
    ) -> OperationRecord:
        self._owned(scope, operation_id, worker_id)
        effective = (
            OperationStatus.CANCELED
            if self.operation.cancel_requested
            else status
        )
        self.operation = replace(
            self.operation,
            status=effective.value,
            result=result if effective is OperationStatus.SUCCEEDED else None,
            error_code=error_code if effective is OperationStatus.FAILED else None,
            error_message=(
                error_message if effective is OperationStatus.FAILED else None
            ),
            lease_owner=None,
            lease_expires_at=None,
        )
        return self.operation

    def request_cancel(self) -> None:
        self.operation = replace(self.operation, cancel_requested_at=NOW)

    def _owned(self, scope, operation_id, worker_id) -> None:
        if (
            scope.tenant_id != self.operation.tenant_id
            or operation_id != self.operation.id
            or self.operation.lease_owner != worker_id
            or self.operation.status != "running"
        ):
            raise LeaseLost("simulated stale worker")


class StaticContextLoader:
    def __init__(self, context: AnalyzeSourceContext) -> None:
        self.context = context

    async def load(self, operation: OperationRecord) -> AnalyzeSourceContext:
        del operation
        return self.context


class RecordingAdapter:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.create_calls = 0
        self.event_cursors: list[int] = []
        self.fail_next_events = False
        self.inject_secret = False
        self.last_request = None

    def create_analyze_task(self, *args, **kwargs):
        self.create_calls += 1
        self.last_request = args[1]
        return self.delegate.create_analyze_task(*args, **kwargs)

    def get_builder_task_events(self, *args, after=0, **kwargs):
        self.event_cursors.append(after)
        if self.fail_next_events:
            self.fail_next_events = False
            raise LumaAdapterError(
                AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=True
            )
        page = self.delegate.get_builder_task_events(
            *args, after=after, **kwargs
        )
        if self.inject_secret and page.events:
            first, *remaining = page.events
            first = replace(
                first,
                event_type="output",
                message="github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            )
            return replace(page, events=(first, *remaining))
        return page

    def get_builder_task(self, *args, **kwargs):
        return self.delegate.get_builder_task(*args, **kwargs)

    def cancel_builder_task(self, *args, **kwargs):
        return self.delegate.cancel_builder_task(*args, **kwargs)


class AutoCompletingAdapter(RecordingAdapter):
    def __init__(self, delegate, backend: FakeLuma, result) -> None:
        super().__init__(delegate)
        self.backend = backend
        self.result = result

    def create_analyze_task(self, *args, **kwargs):
        mutation = super().create_analyze_task(*args, **kwargs)
        self.backend.start_task(mutation.task.task_id)
        self.backend.complete_task(
            mutation.task.task_id,
            status="succeeded",
            result=self.result,
        )
        return mutation


class FixedRecorder:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    async def record(
        self,
        operation_id,
        context,
        references,
        *,
        builder_task_id=None,
    ) -> AnalysisRecording:
        del operation_id, context, references, builder_task_id
        self.calls += 1
        return AnalysisRecording(
            analysis_status=self.status,
            artifact_state="stored",
            plan_stored=True,
        )


class FixedUpdateCheckResolver:
    def __init__(self, result: UpdateCheckResult) -> None:
        self.result = result
        self.calls: list[tuple[OperationRecord, AnalyzeSourceContext]] = []

    async def resolve(
        self,
        operation: OperationRecord,
        context: AnalyzeSourceContext,
    ) -> UpdateCheckResult:
        self.calls.append((operation, context))
        return self.result


class FailTaskCheckpointOnce:
    def __init__(self, delegate: InMemoryAnalyzeStateStore) -> None:
        self.delegate = delegate
        self.failed = False

    async def load(self, operation_id):
        return await self.delegate.load(operation_id)

    async def save(self, state, *, expected_version):
        if state.luma_task_id is not None and not self.failed:
            self.failed = True
            raise RuntimeError("simulated process crash before task checkpoint")
        return await self.delegate.save(state, expected_version=expected_version)


class AnalyzeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.operation_id = new_id("op")
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.source_id = new_id("src")
        self.worker_id = "worker-test-1"
        self.operation = OperationRecord(
            id=self.operation_id,
            tenant_id=self.tenant_id,
            kind="source.analyze",
            target_type="source-revision",
            target_id=self.source_id,
            status="running",
            phase="source.analyze",
            result=None,
            error_code=None,
            error_message=None,
            cancel_requested_at=None,
            lease_owner=self.worker_id,
            lease_expires_at=NOW + timedelta(seconds=60),
            lease_attempt=1,
            last_event_seq=0,
        )
        self.operations = FakeOperations(self.operation)
        self.context = AnalyzeSourceContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            source_revision_ref=self.source_id,
            repository="https://github.com/acme/application.git",
            ref="main",
            subdirectory="services/web",
        )
        self.states = InMemoryAnalyzeStateStore()
        await self.states.initialize(
            self.operation_id, credential_lease_id=new_id("lease")
        )
        self.fake_luma = FakeLuma(clock=lambda: 1_720_000_000)
        self.adapter = RecordingAdapter(
            self.fake_luma.bind(ServicePrincipal("lae-worker", "service-secret"))
        )
        self.config = AnalyzeWorkerConfig(
            agent_image_digest="registry.internal/lae-agent@sha256:" + "a" * 64,
            policy_version="2026-07-11",
            limits=BuilderLimits(
                cpu=2,
                memory_mib=2048,
                disk_mib=4096,
                timeout_seconds=300,
            ),
            event_page_limit=100,
            poll_interval_seconds=0,
        )
        self.update_checks = FixedUpdateCheckResolver(
            UpdateCheckResult(
                baseline_available=True,
                source_changed=True,
                deployment_plan_changed=False,
                changed=True,
                baseline_source_tree_digest="sha256:" + "1" * 64,
                baseline_deployment_plan_digest="sha256:" + "d" * 64,
                candidate_source_tree_digest="sha256:" + "b" * 64,
                candidate_deployment_plan_digest="sha256:" + "d" * 64,
            )
        )

    def runner(
        self,
        *,
        states=None,
        recorder=None,
        context=None,
        update_checks=True,
    ):
        return AnalyzeStepRunner(
            operations=self.operations,
            contexts=StaticContextLoader(context or self.context),
            states=states or self.states,
            luma=self.adapter,
            config=self.config,
            worker_id=self.worker_id,
            recorder=recorder,
            update_checks=(
                self.update_checks if update_checks is True else update_checks
            ),
        )

    async def submit(self, runner=None) -> str:
        result = await (runner or self.runner()).step(self.operations.operation)
        self.assertEqual(result.status, StepStatus.WAITING)
        state = await self.states.load(self.operation_id)
        assert state is not None and state.luma_task_id is not None
        return state.luma_task_id

    @staticmethod
    def analyze_result() -> dict[str, object]:
        evidence = "sha256:" + "e" * 64
        deployment = "sha256:" + "d" * 64
        build = "sha256:" + "f" * 64
        return {
            "resolvedCommit": "1" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "sourceSnapshotId": "snapshot-analysis-1",
            "sourceSnapshotDigest": "sha256:" + "c" * 64,
            "deploymentPlanDigest": deployment,
            "buildPlanDigest": build,
            "evidenceDigest": evidence,
            "policyVersion": "2026-07-11",
            "agentImageDigest": "registry.internal/lae-agent@sha256:" + "a" * 64,
            "artifacts": {
                "evidence": {
                    "digest": evidence,
                    "mediaType": "application/vnd.lae.evidence+json",
                    "sizeBytes": 100,
                },
                "deploymentPlan": {
                    "digest": deployment,
                    "mediaType": "application/vnd.lae.deployment-plan+json",
                    "sizeBytes": 200,
                },
                "buildPlan": {
                    "digest": build,
                    "mediaType": "application/vnd.lae.build-plan-candidate+json",
                    "sizeBytes": 300,
                },
            },
        }

    async def finish_luma(self, task_id: str) -> None:
        self.fake_luma.start_task(task_id)
        self.fake_luma.emit_event(task_id, "source.fetch")
        self.fake_luma.emit_event(task_id, "analysis")
        self.fake_luma.complete_task(
            task_id, status="succeeded", result=self.analyze_result()
        )

    async def test_success_writes_digest_refs_without_claiming_plan_saved(self) -> None:
        task_id = await self.submit()
        await self.finish_luma(task_id)
        terminal = await self.runner().step(self.operations.operation)
        self.assertEqual(terminal.status, StepStatus.TERMINAL)
        self.assertEqual(terminal.operation.status, "succeeded")
        result = terminal.operation.result
        assert result is not None
        self.assertEqual(result["artifactState"], "descriptor-only")
        self.assertFalse(result["planStored"])
        self.assertEqual(result["deploymentPlanDigest"], "sha256:" + "d" * 64)
        self.assertNotIn("deploymentPlan", result)
        self.assertNotIn("updateCheck", result)

    async def test_secure_fake_ingest_stores_bytes_without_exposing_them(self) -> None:
        task_id = await self.submit()
        bodies = {
            "evidence": b'{"schemaVersion":"lae.analysis-evidence/v1"}\n',
            "deploymentPlan": b'{"schemaVersion":"lae.deployment-plan/v1"}\n',
            "buildPlan": b'{"schemaVersion":"lae.build-plan-candidate/v1"}\n',
        }
        media_types = {
            "evidence": "application/vnd.lae.evidence+json",
            "deploymentPlan": "application/vnd.lae.deployment-plan+json",
            "buildPlan": "application/vnd.lae.build-plan-candidate+json",
        }
        descriptors = {
            name: ArtifactDescriptor(
                name=name,
                digest=f"sha256:{hashlib.sha256(body).hexdigest()}",
                media_type=media_types[name],
                size_bytes=len(body),
            )
            for name, body in bodies.items()
        }
        result = {
            "resolvedCommit": "1" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "sourceSnapshotId": "snapshot-analysis-1",
            "sourceSnapshotDigest": "sha256:" + "c" * 64,
            "deploymentPlanDigest": descriptors["deploymentPlan"].digest,
            "buildPlanDigest": descriptors["buildPlan"].digest,
            "evidenceDigest": descriptors["evidence"].digest,
            "policyVersion": "2026-07-11",
            "agentImageDigest": "registry.internal/lae-agent@sha256:" + "a" * 64,
            "artifacts": {
                name: descriptor.to_result()
                for name, descriptor in descriptors.items()
            },
        }
        self.fake_luma.start_task(task_id)
        self.fake_luma.complete_task(task_id, status="succeeded", result=result)
        catalog = InMemoryAnalysisArtifactCatalog()
        broker = InMemoryArtifactTransferBroker(clock=lambda: NOW, chunk_bytes=8)
        object_store = InMemoryS3CompatibleObjectStore()
        for descriptor in descriptors.values():
            binding = ArtifactTransferBinding(
                tenant_ref=self.tenant_id,
                application_ref=self.application_id,
                operation_id=self.operation_id,
                builder_task_id=task_id,
                descriptor=descriptor,
            )
            broker.register(binding, bodies[descriptor.name])
        recorder = ArtifactIngestingAnalysisRecorder(
            catalog=catalog,
            broker=broker,
            object_store=object_store,
            clock=lambda: NOW,
        )

        terminal = await self.runner(recorder=recorder).step(
            self.operations.operation
        )

        self.assertEqual(terminal.operation.status, "succeeded")
        assert terminal.operation.result is not None
        self.assertEqual(terminal.operation.result["artifactState"], "stored")
        self.assertTrue(terminal.operation.result["planStored"])
        public_result = json.dumps(terminal.operation.result, sort_keys=True)
        for body in bodies.values():
            self.assertNotIn(body.decode(), public_result)
        self.assertNotIn("artdl_", public_result)
        self.assertNotIn("registry.internal", public_result)

    async def test_worker_claims_source_analyze_and_runs_to_terminal(self) -> None:
        self.operations.operation = replace(
            self.operations.operation,
            status="queued",
            lease_owner=None,
            lease_expires_at=None,
        )
        self.adapter = AutoCompletingAdapter(
            self.fake_luma.bind(ServicePrincipal("lae-worker", "service-secret")),
            self.fake_luma,
            self.analyze_result(),
        )
        runner = self.runner()
        worker = AnalyzeWorker(
            self.operations,
            runner,
            worker_id=self.worker_id,
            config=self.config,
            sleep=lambda _seconds: _no_sleep(),
        )
        terminal = await worker.run_once()
        assert terminal is not None
        self.assertEqual(terminal.operation.status, "succeeded")
        self.assertEqual(self.adapter.create_calls, 1)

    async def test_worker_claims_application_check_update_and_reuses_analyzer(
        self,
    ) -> None:
        self.operations.operation = replace(
            self.operations.operation,
            kind="application.check-update",
            target_type="application",
            target_id=self.application_id,
            status="queued",
            lease_owner=None,
            lease_expires_at=None,
        )
        self.adapter = AutoCompletingAdapter(
            self.fake_luma.bind(ServicePrincipal("lae-worker", "service-secret")),
            self.fake_luma,
            self.analyze_result(),
        )
        runner = self.runner()
        worker = AnalyzeWorker(
            self.operations,
            runner,
            worker_id=self.worker_id,
            config=self.config,
            sleep=lambda _seconds: _no_sleep(),
        )
        terminal = await worker.run_once()
        assert terminal is not None
        self.assertEqual(terminal.operation.kind, "application.check-update")
        self.assertEqual(terminal.operation.status, "succeeded")
        self.assertEqual(self.adapter.create_calls, 1)
        assert terminal.operation.result is not None
        self.assertIn("sourceRevisionId", terminal.operation.result)
        self.assertEqual(
            terminal.operation.result["updateCheck"],
            self.update_checks.result.to_body(),
        )
        self.assertEqual(len(self.update_checks.calls), 1)

    async def test_check_update_without_closed_resolver_fails_safely(self) -> None:
        self.operations.operation = replace(
            self.operations.operation,
            kind="application.check-update",
            target_type="application",
            target_id=self.application_id,
        )
        runner = self.runner(update_checks=None)
        task_id = await self.submit(runner)
        await self.finish_luma(task_id)
        terminal = await runner.step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "failed")
        self.assertEqual(
            terminal.operation.error_code,
            "LAE_UPDATE_CHECK_RESULT_INVALID",
        )
        self.assertIsNone(terminal.operation.result)

    async def test_needs_configuration_and_deny_are_successful_analyses(self) -> None:
        for status in ("needs_configuration", "not_deployable"):
            with self.subTest(status=status):
                await self.asyncSetUp()
                runner = self.runner(recorder=FixedRecorder(status))
                task_id = await self.submit(runner)
                await self.finish_luma(task_id)
                terminal = await runner.step(self.operations.operation)
                self.assertEqual(terminal.operation.status, "succeeded")
                assert terminal.operation.result is not None
                self.assertEqual(terminal.operation.result["analysisStatus"], status)

    async def test_disconnect_resumes_from_persisted_cursor(self) -> None:
        task_id = await self.submit()
        first_state = await self.states.load(self.operation_id)
        assert first_state is not None
        self.assertEqual(first_state.luma_cursor, 1)
        self.fake_luma.start_task(task_id)
        self.fake_luma.emit_event(task_id, "analysis")
        self.adapter.fail_next_events = True
        waiting = await self.runner().step(self.operations.operation)
        self.assertEqual(waiting.status, StepStatus.WAITING)
        unchanged = await self.states.load(self.operation_id)
        assert unchanged is not None
        self.assertEqual(unchanged.luma_cursor, 1)
        await self.finish_luma(task_id)
        terminal = await self.runner().step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "succeeded")
        self.assertEqual(self.adapter.event_cursors[:3], [0, 1, 1])

    async def test_expired_cursor_resynchronizes_without_raw_event_replay(self) -> None:
        task_id = await self.submit()
        self.fake_luma.start_task(task_id)
        self.fake_luma.emit_event(task_id, "analysis")
        self.fake_luma.trim_events_before(task_id, 3)
        waiting = await self.runner().step(self.operations.operation)
        self.assertEqual(waiting.status, StepStatus.WAITING)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertEqual(state.luma_cursor, 3)
        warning = self.operations.events[-1]
        self.assertEqual(warning.level, "warning")
        self.assertEqual(warning.data, {"skippedThroughCursor": 3})
        self.fake_luma.complete_task(
            task_id, status="succeeded", result=self.analyze_result()
        )
        terminal = await self.runner().step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "succeeded")

    async def test_crash_before_task_checkpoint_replays_create_idempotently(self) -> None:
        failing = FailTaskCheckpointOnce(self.states)
        runner = self.runner(states=failing)
        with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
            await runner.step(self.operations.operation)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertIsNone(state.luma_task_id)
        waiting = await self.runner().step(self.operations.operation)
        self.assertEqual(waiting.status, StepStatus.WAITING)
        self.assertEqual(self.adapter.create_calls, 2)
        self.assertTrue(
            any(
                event.type == "builder.analyze.progress"
                and event.message == "Source analysis submitted to Luma"
                and event.data == {"replayed": True}
                for event in self.operations.events
            )
        )

    async def test_running_cancel_is_forwarded(self) -> None:
        task_id = await self.submit()
        self.fake_luma.start_task(task_id)
        self.operations.request_cancel()
        waiting = await self.runner().step(self.operations.operation)
        self.assertEqual(waiting.status, StepStatus.WAITING)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertTrue(state.cancel_forwarded)
        upstream = self.adapter.get_builder_task(
            LumaCallContext(
                self.tenant_id, self.application_id, self.operation_id
            ),
            task_id,
        )
        self.assertEqual(upstream.status, "cancel_requested")

    async def test_already_forwarded_cancel_fences_late_success(self) -> None:
        task_id = await self.submit()
        self.fake_luma.start_task(task_id)
        self.operations.request_cancel()
        waiting = await self.runner().step(self.operations.operation)
        self.assertEqual(waiting.status, StepStatus.WAITING)
        late = self.fake_luma.complete_task(
            task_id, status="succeeded", result=self.analyze_result()
        )
        self.assertEqual(late.status, "canceled")
        terminal = await self.runner().step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "canceled")
        self.assertIsNone(terminal.operation.result)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertIsNone(state.digest_references)
        self.assertIsNone(state.recording)

    async def test_cancel_racing_with_success_wins_final_heartbeat(self) -> None:
        task_id = await self.submit()
        await self.finish_luma(task_id)
        recorder = FixedRecorder("deployable")
        self.operations.cancel_on_heartbeat_call = self.operations.heartbeat_calls + 2
        terminal = await self.runner(recorder=recorder).step(
            self.operations.operation
        )
        self.assertEqual(terminal.operation.status, "canceled")
        self.assertIsNone(terminal.operation.result)
        self.assertEqual(recorder.calls, 0)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertTrue(state.cancel_forwarded)
        self.assertIsNone(state.digest_references)

    async def test_cancel_before_luma_task_does_not_create_one(self) -> None:
        self.operations.request_cancel()
        terminal = await self.runner().step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "canceled")
        self.assertEqual(self.adapter.create_calls, 0)

    async def test_lease_lost_stops_before_any_luma_side_effect(self) -> None:
        self.operations.lose_lease = True
        with self.assertRaises(LeaseLost):
            await self.runner().step(self.operations.operation)
        self.assertEqual(self.adapter.create_calls, 0)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertIsNone(state.luma_task_id)

    async def test_cross_tenant_context_fails_closed(self) -> None:
        foreign = replace(self.context, tenant_ref=new_id("ten"))
        terminal = await self.runner(context=foreign).step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "failed")
        self.assertEqual(
            terminal.operation.error_code, "LAE_ANALYZE_CONTEXT_INVALID"
        )
        self.assertEqual(self.adapter.create_calls, 0)

    async def test_uploaded_object_submits_only_digest_media_type_and_size(self) -> None:
        context = AnalyzeSourceContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            source_revision_ref=self.source_id,
            repository=None,
            object_digest="sha256:" + "9" * 64,
            object_media_type="application/zip",
            object_size_bytes=4096,
        )
        await self.runner(context=context).step(self.operations.operation)
        self.assertEqual(self.adapter.create_calls, 1)
        assert self.adapter.last_request is not None
        wire = self.adapter.last_request.to_wire()
        self.assertEqual(
            wire["sourceRef"],
            {
                "kind": "object",
                "digest": "sha256:" + "9" * 64,
                "mediaType": "application/zip",
                "sizeBytes": 4096,
            },
        )
        self.assertNotIn("url", json.dumps(wire, sort_keys=True).lower())

    def test_uploaded_object_context_requires_complete_immutable_descriptor(self) -> None:
        with self.assertRaises(AnalyzeContextInvalid):
            AnalyzeSourceContext(
                tenant_ref=self.tenant_id,
                application_ref=self.application_id,
                source_revision_ref=self.source_id,
                repository=None,
                object_digest="sha256:" + "9" * 64,
                object_media_type="application/zip",
                object_size_bytes=None,
            )

    def test_ssh_source_is_rejected_until_executor_support_exists(self) -> None:
        with self.assertRaises(AnalyzeContextInvalid):
            replace(
                self.context,
                repository="ssh://git@github.com/acme/application.git",
            )

    def test_operation_target_vocabulary_is_accepted_by_store_contract(self) -> None:
        command = CreateOperation(
            scope=TenantScope(self.tenant_id),
            principal=Principal("user", new_id("usr")),
            kind="source.analyze",
            target_type="source-revision",
            target_id=self.source_id,
            phase="source.analyze",
        )
        self.assertEqual(command.target_type, "source-revision")

    async def test_untrusted_builder_message_and_credential_ref_never_reach_events(
        self,
    ) -> None:
        self.adapter.inject_secret = True
        await self.submit()
        encoded = json.dumps(
            [asdict(event) for event in self.operations.events], sort_keys=True
        )
        self.assertNotIn("github_pat_", encoded)
        state = await self.states.load(self.operation_id)
        assert state is not None
        self.assertNotIn(state.credential_lease_id, encoded)
        self.assertIn("Source analysis progress updated", encoded)

    async def test_missing_checkpoint_fails_closed(self) -> None:
        empty = InMemoryAnalyzeStateStore()
        terminal = await self.runner(states=empty).step(self.operations.operation)
        self.assertEqual(terminal.operation.status, "failed")
        self.assertEqual(
            terminal.operation.error_code, "LAE_ANALYZE_CHECKPOINT_MISSING"
        )

    async def test_real_factory_defaults_to_postgres_state_and_recorder(self) -> None:
        runtime = build_worker_from_env(
            environ={
                "LAE_DATABASE_URL": (
                    "postgresql+asyncpg://lae:password@127.0.0.1:5432/unused"
                ),
                "LAE_LUMA_CONTROL_URL": "https://luma.example.test",
                "LAE_LUMA_CLUSTER_ID": "luma-primary",
                "LAE_LUMA_SERVICE_PRINCIPAL_ID": "lae-worker",
                "LAE_LUMA_SERVICE_TOKEN": "service-principal-secret",
                "LAE_WORKER_STATE_HMAC_KEY": base64.b64encode(b"k" * 32).decode(),
                "LAE_ANALYZER_IMAGE_DIGEST": (
                    "registry.internal/lae-agent@sha256:" + "a" * 64
                ),
            }
        )
        try:
            self.assertIsInstance(
                runtime.worker._runner._states, PostgresAnalyzeStateStore
            )
            self.assertIsInstance(
                runtime.worker._runner._recorder, PostgresAnalysisRecorder
            )
        finally:
            await runtime.close()

    def test_production_factory_fails_closed_without_secure_artifact_ingest(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError, "secure verified artifact-ingest recorder"
        ):
            build_worker_from_env(environ={"LAE_ENVIRONMENT": "production"})

    async def test_real_factory_wires_postgres_queue_and_https_adapter(self) -> None:
        runtime = build_worker_from_env(
            states=self.states,
            environ={
                "LAE_DATABASE_URL": (
                    "postgresql+asyncpg://lae:password@127.0.0.1:5432/unused"
                ),
                "LAE_LUMA_CONTROL_URL": "https://luma.example.test",
                "LAE_LUMA_SERVICE_PRINCIPAL_ID": "lae-worker",
                "LAE_LUMA_SERVICE_TOKEN": "service-principal-secret",
                "LAE_ANALYZER_IMAGE_DIGEST": (
                    "registry.internal/lae-agent@sha256:" + "a" * 64
                ),
            },
        )
        try:
            self.assertIsNotNone(runtime.worker)
            self.assertIsNotNone(runtime.engine)
        finally:
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
