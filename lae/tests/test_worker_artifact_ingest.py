from __future__ import annotations

import hashlib
import sys
import unittest
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

from lae_worker import (  # noqa: E402
    AnalysisDigestReferences,
    AnalyzeSourceContext,
    ArtifactDescriptor,
    ArtifactIngestCanceled,
    ArtifactIngestingAnalysisRecorder,
    ArtifactIntegrityError,
    ArtifactStorageUnavailable,
    ArtifactTransferBinding,
    ArtifactTransferUnavailable,
    InMemoryAnalysisArtifactCatalog,
    InMemoryArtifactTransferBroker,
    InMemoryS3CompatibleObjectStore,
    object_key_for,
)


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _descriptor(name: str, media_type: str, body: bytes) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        name=name,
        digest=f"sha256:{hashlib.sha256(body).hexdigest()}",
        media_type=media_type,
        size_bytes=len(body),
    )


class CancelOnChunk:
    def __init__(self, *, cancel_at: int) -> None:
        self.cancel_at = cancel_at
        self.calls = 0

    async def checkpoint(self, binding: ArtifactTransferBinding) -> None:
        del binding
        self.calls += 1
        if self.calls >= self.cancel_at:
            raise ArtifactIngestCanceled()


class FlakyHeadStore(InMemoryS3CompatibleObjectStore):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def head(self, key: str):
        if self.failures:
            self.failures -= 1
            raise ArtifactStorageUnavailable()
        return await super().head(key)


class TimeoutOnceBroker(InMemoryArtifactTransferBroker):
    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW, chunk_bytes=7)
        self.timed_out = False

    async def open_download(self, lease, binding):
        if not self.timed_out:
            self.timed_out = True
            raise TimeoutError()
        return await super().open_download(lease, binding)


class ArtifactIngestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.operation_id = "op_01J00000000000000000000001"
        self.task_id = "builder-task-00000001"
        self.context = AnalyzeSourceContext(
            tenant_ref="ten_01J00000000000000000000001",
            application_ref="app_01J00000000000000000000001",
            source_revision_ref="src_01J00000000000000000000001",
            repository="https://github.com/acme/example.git",
            ref="main",
        )
        self.bodies = {
            "evidence": b'{"schemaVersion":"lae.analysis-evidence/v1"}\n',
            "deploymentPlan": b'{"schemaVersion":"lae.deployment-plan/v1"}\n',
            "buildPlan": b'{"schemaVersion":"lae.build-plan-candidate/v1"}\n',
        }
        self.descriptors = (
            _descriptor(
                "evidence",
                "application/vnd.lae.evidence+json",
                self.bodies["evidence"],
            ),
            _descriptor(
                "deploymentPlan",
                "application/vnd.lae.deployment-plan+json",
                self.bodies["deploymentPlan"],
            ),
            _descriptor(
                "buildPlan",
                "application/vnd.lae.build-plan-candidate+json",
                self.bodies["buildPlan"],
            ),
        )
        by_name = {item.name: item for item in self.descriptors}
        self.references = AnalysisDigestReferences(
            resolved_commit="1" * 40,
            source_tree_digest="sha256:" + "2" * 64,
            source_snapshot_id="snapshot-analysis-1",
            source_snapshot_digest="sha256:" + "3" * 64,
            deployment_plan_digest=by_name["deploymentPlan"].digest,
            build_plan_digest=by_name["buildPlan"].digest,
            evidence_digest=by_name["evidence"].digest,
            policy_version="2026-07-11",
            artifacts=self.descriptors,
        )
        self.catalog = InMemoryAnalysisArtifactCatalog()
        self.broker = InMemoryArtifactTransferBroker(
            clock=lambda: NOW, chunk_bytes=7
        )
        self.object_store = InMemoryS3CompatibleObjectStore()
        self.bindings = tuple(
            ArtifactTransferBinding(
                tenant_ref=self.context.tenant_ref,
                application_ref=self.context.application_ref,
                operation_id=self.operation_id,
                builder_task_id=self.task_id,
                descriptor=descriptor,
            )
            for descriptor in self.descriptors
        )
        for binding in self.bindings:
            self.broker.register(binding, self.bodies[binding.descriptor.name])

    def recorder(self, **kwargs) -> ArtifactIngestingAnalysisRecorder:
        return ArtifactIngestingAnalysisRecorder(
            catalog=self.catalog,
            broker=self.broker,
            object_store=self.object_store,
            clock=lambda: NOW,
            **kwargs,
        )

    async def record(self, recorder=None):
        return await (recorder or self.recorder()).record(
            self.operation_id,
            self.context,
            self.references,
            builder_task_id=self.task_id,
        )

    async def test_streams_all_artifacts_and_only_then_marks_plan_stored(self) -> None:
        recording = await self.record()

        self.assertEqual(recording.artifact_state, "stored")
        self.assertTrue(recording.plan_stored)
        catalog_recording, states = self.catalog.state_for_test(
            self.operation_id, self.context
        )
        self.assertEqual(catalog_recording, recording)
        self.assertTrue(
            all(state.upload_status == "verified" for state in states.values())
        )
        for descriptor in self.descriptors:
            key = object_key_for(self.context.tenant_ref, descriptor)
            self.assertTrue(key.startswith(f"tenants/{self.context.tenant_ref}/"))
            self.assertEqual(
                self.object_store.read_for_test(key), self.bodies[descriptor.name]
            )

    async def test_crash_retry_is_idempotent_and_does_not_redownload(self) -> None:
        first = await self.record()
        issue_calls = self.broker.issue_calls
        put_calls = self.object_store.put_calls

        second = await self.record()

        self.assertEqual(second, first)
        self.assertEqual(self.broker.issue_calls, issue_calls)
        self.assertEqual(self.object_store.put_calls, put_calls)

    async def test_transient_broker_failure_gets_a_fresh_single_use_lease(self) -> None:
        first = self.bindings[0]
        self.broker.register(
            first,
            self.bodies[first.descriptor.name],
            failures_before_success=1,
        )

        recording = await self.record(self.recorder(max_attempts=2))

        self.assertTrue(recording.plan_stored)
        self.assertEqual(self.broker.issue_calls, 4)
        self.assertEqual(self.broker.open_calls, 4)

    async def test_transient_object_head_is_retried_without_download_lease(self) -> None:
        store = FlakyHeadStore(1)
        recorder = ArtifactIngestingAnalysisRecorder(
            catalog=self.catalog,
            broker=self.broker,
            object_store=store,
            clock=lambda: NOW,
            max_attempts=2,
        )

        recording = await self.record(recorder)

        self.assertTrue(recording.plan_stored)
        self.assertEqual(self.broker.issue_calls, 3)

    async def test_download_timeout_retries_with_a_new_lease(self) -> None:
        broker = TimeoutOnceBroker()
        for binding in self.bindings:
            broker.register(binding, self.bodies[binding.descriptor.name])
        recorder = ArtifactIngestingAnalysisRecorder(
            catalog=self.catalog,
            broker=broker,
            object_store=self.object_store,
            clock=lambda: NOW,
            max_attempts=2,
        )

        recording = await self.record(recorder)

        self.assertTrue(recording.plan_stored)
        self.assertEqual(broker.issue_calls, 4)

    async def test_digest_mismatch_never_commits_or_marks_analysis_stored(self) -> None:
        first = self.bindings[0]
        bad_body = b"x" * first.descriptor.size_bytes
        self.broker.register(first, bad_body)

        with self.assertRaises(ArtifactIntegrityError):
            await self.record()

        recording, states = self.catalog.state_for_test(
            self.operation_id, self.context
        )
        self.assertEqual(recording.artifact_state, "descriptor-only")
        self.assertFalse(recording.plan_stored)
        self.assertEqual(states[first.descriptor.name].upload_status, "failed")
        self.assertIsNone(
            await self.object_store.head(
                object_key_for(self.context.tenant_ref, first.descriptor)
            )
        )

    async def test_media_or_length_mismatch_fails_before_object_write(self) -> None:
        first = self.bindings[0]
        self.broker.register(
            first,
            self.bodies[first.descriptor.name],
            media_type="application/octet-stream",
        )

        with self.assertRaises(ArtifactIntegrityError):
            await self.record()

        self.assertEqual(self.object_store.put_calls, 0)

    async def test_cancel_during_stream_leaves_descriptor_only_and_no_object(
        self,
    ) -> None:
        guard = CancelOnChunk(cancel_at=3)

        with self.assertRaises(ArtifactIngestCanceled):
            await self.record(self.recorder(guard=guard))

        recording, states = self.catalog.state_for_test(
            self.operation_id, self.context
        )
        self.assertFalse(recording.plan_stored)
        self.assertEqual(recording.artifact_state, "descriptor-only")
        self.assertEqual(states["evidence"].upload_status, "failed")
        self.assertIsNone(
            await self.object_store.head(
                object_key_for(self.context.tenant_ref, self.descriptors[0])
            )
        )

    async def test_lease_is_exactly_bound_and_single_use(self) -> None:
        binding = self.bindings[0]
        lease = await self.broker.issue_download_lease(binding, ttl_seconds=60)
        foreign = ArtifactTransferBinding(
            tenant_ref="ten_01J00000000000000000000002",
            application_ref=binding.application_ref,
            operation_id=binding.operation_id,
            builder_task_id=binding.builder_task_id,
            descriptor=binding.descriptor,
        )

        with self.assertRaises(ArtifactTransferUnavailable):
            await self.broker.open_download(lease, foreign)
        download = await self.broker.open_download(lease, binding)
        self.assertNotIn(
            self.bodies[binding.descriptor.name].decode(), repr(download)
        )
        self.assertNotIn("http", repr(lease).lower())
        with self.assertRaises(ArtifactTransferUnavailable):
            await self.broker.open_download(lease, binding)

    async def test_expired_lease_and_oversized_descriptor_fail_closed(self) -> None:
        binding = self.bindings[0]
        lease = await self.broker.issue_download_lease(binding, ttl_seconds=60)
        expired = type(lease)(
            lease_id=lease.lease_id,
            binding=lease.binding,
            expires_at=NOW - timedelta(seconds=1),
        )
        with self.assertRaises(ArtifactTransferUnavailable):
            await self.broker.open_download(expired, binding)

        with self.assertRaises(ArtifactIntegrityError):
            ArtifactTransferBinding(
                tenant_ref=self.context.tenant_ref,
                application_ref=self.context.application_ref,
                operation_id=self.operation_id,
                builder_task_id=self.task_id,
                descriptor=ArtifactDescriptor(
                    name="evidence",
                    digest="sha256:" + "a" * 64,
                    media_type="application/vnd.lae.evidence+json",
                    size_bytes=16 * 1024 * 1024 + 1,
                ),
            )

    def test_object_key_is_derived_from_tenant_kind_and_digest_only(self) -> None:
        descriptor = self.descriptors[1]
        key = object_key_for(self.context.tenant_ref, descriptor)

        self.assertEqual(
            key,
            f"tenants/{self.context.tenant_ref}/analysis-artifacts/"
            f"deployment-plan/{descriptor.digest.replace(':', '/')}.json",
        )
        self.assertNotIn(self.operation_id, key)
        self.assertNotIn(self.task_id, key)


if __name__ == "__main__":
    unittest.main()
