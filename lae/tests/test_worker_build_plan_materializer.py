from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LAE_ROOT.parent
for relative in (
    "packages/contracts/src",
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))
sys.path.insert(0, str(REPO_ROOT))

from lae_store import (  # noqa: E402
    PrivateObjectDownload,
    PrivateObjectMetadata,
    StoredDeploymentPlanArtifact,
    new_id,
)
from lae_worker import (  # noqa: E402
    BuildPlanIntegrityError,
    HmacBuildCredentialLeaseIssuer,
    S3TrustedBuildPlanMaterializer,
    StoredBuildPlanArtifact,
)
from luma.builder_tasks import builder_plan_signature_payload  # noqa: E402


SNAPSHOT = "sha256:" + "c" * 64
COMMIT = "0123456789abcdef0123456789abcdef01234567"
POLICY = "2026-07-11"


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def candidate() -> dict[str, object]:
    return {
        "schemaVersion": "lae.build-plan-candidate/v1",
        "sourceSnapshotDigest": SNAPSHOT,
        "resolvedCommit": COMMIT,
        "policyVersion": POLICY,
        "builds": [
            {
                "key": "web",
                "context": ".",
                "dockerfile": "Dockerfile",
                "target": None,
                "platform": "linux/amd64",
                "buildArgNames": [],
                "secretMountNames": [],
                "dependsOnBuilds": [],
            }
        ],
        "externalImages": [
            {
                "key": "postgres",
                "ref": "postgres:17",
                "resolvedDigest": "sha256:" + "e" * 64,
                "platform": "linux/amd64",
            }
        ],
    }


def deployment_plan() -> dict[str, object]:
    return {
        "schemaVersion": "lae.deployment-plan/v1",
        "planId": "plan_materializer_test",
        "sourceRevisionId": "src_materializer_test",
        "sourceDigest": SNAPSHOT,
        "kind": "compose",
        "services": [
            {
                "key": "web",
                "role": "http",
                "image": {"source": "build", "buildKey": "web"},
                "command": "node server.js",
                "port": 8080,
                "healthcheck": {
                    "type": "http",
                    "path": "/healthz",
                    "intervalSeconds": 10,
                },
                "dependencies": ["postgres"],
                "environmentNames": ["DATABASE_URL"],
                "resources": {"cpu": "0.50", "memoryMiB": 512},
            },
            {
                "key": "postgres",
                "role": "datastore",
                "image": {"source": "external", "ref": "postgres:17"},
                "command": None,
                "dependencies": [],
                "environmentNames": [],
                "resources": {"cpu": "0.50", "memoryMiB": 1024},
            },
        ],
        "routes": [
            {
                "serviceKey": "web",
                "kind": "http",
                "primary": True,
                "hostnameRef": "domain_materializer_test",
                "containerPort": 8080,
                "healthPath": "/healthz",
            }
        ],
        "volumes": [
            {
                "key": "pg-data",
                "serviceKeys": ["postgres"],
                "mountPath": "/var/lib/postgresql/data",
                "class": "persistent",
                "requestedBytes": 1_073_741_824,
                "accessMode": "ReadWriteOnce",
                "backupPolicy": "plan-default",
                "deletePolicy": "retain",
            }
        ],
        "environment": [
            {
                "name": "DATABASE_URL",
                "scope": "runtime",
                "services": ["web"],
                "required": True,
                "sensitive": True,
                "public": False,
                "configured": True,
            }
        ],
        "warnings": [],
        "blockers": [],
        "policy": {"version": POLICY, "decision": "allow"},
    }


class MemoryObjectStore:
    def __init__(self, objects: dict[str, tuple[str, bytes]]) -> None:
        self.objects = objects

    async def get_stream(self, key: str, *, max_bytes: int):
        media_type, raw = self.objects[key]
        if len(raw) > max_bytes:
            raise AssertionError("test object exceeds bound")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()

        async def chunks():
            yield raw[:7]
            yield raw[7:]

        return PrivateObjectDownload(
            PrivateObjectMetadata(key, media_type, len(raw), digest),
            chunks(),
        )


class BuildPlanMaterializerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tenant = "ten_materializer_test"
        self.application = "app_materializer_test"
        self.operation = "op_materializer_test"
        self.revision = "rev_materializer_test"
        self.snapshot_id = "snapshot-materializer-test"
        self.signing_key = b"s" * 32
        self.lease_key = b"l" * 32
        self.candidate_raw = canonical(candidate())
        self.deployment_raw = canonical(deployment_plan())
        self.build_digest = "sha256:" + hashlib.sha256(self.candidate_raw).hexdigest()
        self.deployment_digest = "sha256:" + hashlib.sha256(
            self.deployment_raw
        ).hexdigest()
        self.build_key = (
            f"tenants/{self.tenant}/analysis-artifacts/build-plan-candidate/"
            f"sha256/{self.build_digest.removeprefix('sha256:')}.json"
        )
        self.deployment_key = (
            f"tenants/{self.tenant}/analysis-artifacts/deployment-plan/"
            f"sha256/{self.deployment_digest.removeprefix('sha256:')}.json"
        )
        self.store = MemoryObjectStore(
            {
                self.build_key: (
                    "application/vnd.lae.build-plan-candidate+json",
                    self.candidate_raw,
                ),
                self.deployment_key: (
                    "application/vnd.lae.deployment-plan+json",
                    self.deployment_raw,
                ),
            }
        )
        self.materializer = S3TrustedBuildPlanMaterializer(
            self.store,  # type: ignore[arg-type]
            signing_key_id="lae-plan-primary",
            signing_key=self.signing_key,
            lease_issuer=HmacBuildCredentialLeaseIssuer(self.lease_key),
        )

    def descriptors(self):
        return (
            StoredBuildPlanArtifact(
                new_id("art"),
                self.build_digest,
                "application/vnd.lae.build-plan-candidate+json",
                len(self.candidate_raw),
                self.build_key,
            ),
            StoredDeploymentPlanArtifact(
                artifact_id=new_id("art"),
                analysis_id=new_id("ana"),
                source_revision_id=new_id("src"),
                digest=self.deployment_digest,
                media_type="application/vnd.lae.deployment-plan+json",
                size_bytes=len(self.deployment_raw),
                storage_key=self.deployment_key,
                source_snapshot_digest=SNAPSHOT,
            ),
        )

    async def materialize(self):
        build, deployment = self.descriptors()
        return await self.materializer.materialize(
            build,
            deployment,
            tenant_ref=self.tenant,
            application_ref=self.application,
            operation_ref=self.operation,
            revision_ref=self.revision,
            source_snapshot_id=self.snapshot_id,
            source_snapshot_digest=SNAPSHOT,
            resolved_commit=COMMIT,
            policy_version=POLICY,
        )

    async def test_signs_exact_luma_payload_and_preserves_runtime_topology(self) -> None:
        trusted = await self.materialize()
        request = {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "build-plan",
            "externalOperationId": self.operation,
            "tenantRef": self.tenant,
            "applicationRef": self.application,
            "payload": {
                "sourceSnapshotId": self.snapshot_id,
                "sourceSnapshotDigest": SNAPSHOT,
                "signedBuildPlan": dict(trusted.signed_build_plan),
                "credentialLeaseId": trusted.credential_lease_id,
                "limits": {
                    "cpu": 2,
                    "memoryMiB": 2048,
                    "diskMiB": 4096,
                    "timeoutSeconds": 300,
                },
            },
        }
        expected = hmac.new(
            self.signing_key,
            builder_plan_signature_payload(request),
            hashlib.sha256,
        ).digest()
        supplied = base64.urlsafe_b64decode(
            trusted.signed_build_plan["signature"]["value"] + "="
        )
        self.assertEqual(supplied, expected)
        self.assertEqual(trusted.service_build_keys, {"web": "web", "postgres": "postgres"})
        self.assertEqual(trusted.services[0].command, "node server.js")
        self.assertEqual(trusted.services[0].dependencies, ("postgres",))
        self.assertEqual(trusted.services[0].cpu, "0.50")
        self.assertEqual(trusted.volumes[0].service_keys, ("postgres",))
        self.assertEqual(
            trusted.volumes[0].mount_path, "/var/lib/postgresql/data"
        )

    async def test_rejects_noncanonical_bytes_even_when_descriptor_matches(self) -> None:
        spaced = json.dumps(candidate(), sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(spaced).hexdigest()
        key = (
            f"tenants/{self.tenant}/analysis-artifacts/build-plan-candidate/"
            f"sha256/{digest.removeprefix('sha256:')}.json"
        )
        self.store.objects[key] = (
            "application/vnd.lae.build-plan-candidate+json",
            spaced,
        )
        build, deployment = self.descriptors()
        build = StoredBuildPlanArtifact(
            build.artifact_id,
            digest,
            build.media_type,
            len(spaced),
            key,
        )
        with self.assertRaises(BuildPlanIntegrityError):
            await self.materializer.materialize(
                build,
                deployment,
                tenant_ref=self.tenant,
                application_ref=self.application,
                operation_ref=self.operation,
                revision_ref=self.revision,
                source_snapshot_id=self.snapshot_id,
                source_snapshot_digest=SNAPSHOT,
                resolved_commit=COMMIT,
                policy_version=POLICY,
            )

    async def test_rejects_cross_snapshot_and_incomplete_topology(self) -> None:
        build, deployment = self.descriptors()
        with self.assertRaises(BuildPlanIntegrityError):
            await self.materializer.materialize(
                build,
                deployment,
                tenant_ref=self.tenant,
                application_ref=self.application,
                operation_ref=self.operation,
                revision_ref=self.revision,
                source_snapshot_id=self.snapshot_id,
                source_snapshot_digest="sha256:" + "d" * 64,
                resolved_commit=COMMIT,
                policy_version=POLICY,
            )

        changed = deployment_plan()
        changed["volumes"] = []
        raw = canonical(changed)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        key = (
            f"tenants/{self.tenant}/analysis-artifacts/deployment-plan/"
            f"sha256/{digest.removeprefix('sha256:')}.json"
        )
        self.store.objects[key] = (
            "application/vnd.lae.deployment-plan+json",
            raw,
        )
        changed_descriptor = StoredDeploymentPlanArtifact(
            artifact_id=deployment.artifact_id,
            analysis_id=deployment.analysis_id,
            source_revision_id=deployment.source_revision_id,
            digest=digest,
            media_type=deployment.media_type,
            size_bytes=len(raw),
            storage_key=key,
            source_snapshot_digest=SNAPSHOT,
        )
        # The artifact itself is valid; the context loader later rejects it
        # against the catalog volume. Ensure materialization never invents one.
        trusted = await self.materializer.materialize(
            build,
            changed_descriptor,
            tenant_ref=self.tenant,
            application_ref=self.application,
            operation_ref=self.operation,
            revision_ref=self.revision,
            source_snapshot_id=self.snapshot_id,
            source_snapshot_digest=SNAPSHOT,
            resolved_commit=COMMIT,
            policy_version=POLICY,
        )
        self.assertEqual(trusted.volumes, ())


if __name__ == "__main__":
    unittest.main()
