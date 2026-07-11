from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from lae_api.plan_resolver import (
    S3DeploymentPlanResolver,
    UnconfiguredBuildArtifactImageResolver,
)
from lae_store import (
    DeploymentPlanInvalid,
    PrivateObjectDownload,
    PrivateObjectMetadata,
    StoredDeploymentPlanArtifact,
)


ULID = "01J00000000000000000000000"
SOURCE_ID = f"src_{ULID}"


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def plan() -> dict[str, object]:
    return {
        "schemaVersion": "lae.deployment-plan/v1",
        "planId": "plan_01TEST",
        "sourceRevisionId": "src_SNAPSHOTDERIVED",
        "sourceDigest": "sha256:" + "c" * 64,
        "kind": "compose",
        "services": [
            {
                "key": "web",
                "role": "http",
                "image": {"source": "build", "buildKey": "web"},
                "command": None,
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
                "environmentNames": ["POSTGRES_PASSWORD"],
                "resources": {"cpu": "0.50", "memoryMiB": 1024},
            },
        ],
        "routes": [
            {
                "serviceKey": "web",
                "kind": "http",
                "primary": True,
                "hostnameRef": "domain_01TEST",
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
                "configured": False,
            },
            {
                "name": "POSTGRES_PASSWORD",
                "scope": "runtime",
                "services": ["postgres"],
                "required": True,
                "sensitive": True,
                "public": False,
                "configured": False,
            },
        ],
        "warnings": [],
        "blockers": [],
        "policy": {"version": "2026-07-11", "decision": "needs_configuration"},
    }


class FakeObjectStore:
    def __init__(self, key: str, body: bytes, media_type: str) -> None:
        self.key = key
        self.body = body
        self.media_type = media_type
        self.digest = "sha256:" + hashlib.sha256(body).hexdigest()

    async def get_stream(self, key: str, *, max_bytes: int):
        if key != self.key or len(self.body) > max_bytes:
            raise AssertionError("unexpected fake object request")

        async def chunks():
            for offset in range(0, len(self.body), 7):
                yield self.body[offset : offset + 7]

        return PrivateObjectDownload(
            metadata=PrivateObjectMetadata(
                key=key,
                media_type=self.media_type,
                size_bytes=len(self.body),
                digest=self.digest,
            ),
            chunks=chunks(),
        )


class TrustedImages:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_runtime_images(
        self, artifact, *, source_digest, requirements
    ):
        del artifact
        self.calls += 1
        if source_digest != "sha256:" + "c" * 64:
            raise AssertionError("wrong source digest")
        return {
            requirement.service_key: "sha256:"
            + (("a" if requirement.source == "build" else "b") * 64)
            for requirement in requirements
        }


class DeploymentPlanResolverTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, value: dict[str, object] | None = None, *, raw: bytes | None = None):
        body = raw if raw is not None else canonical(value or plan())
        key = "tenants/tenant-test/analysis-artifacts/deployment-plan/sha256/" + (
            hashlib.sha256(body).hexdigest()
        ) + ".json"
        store = FakeObjectStore(
            key, body, "application/vnd.lae.deployment-plan+json"
        )
        artifact = StoredDeploymentPlanArtifact(
            artifact_id=f"art_{ULID}",
            analysis_id=f"ana_{ULID}",
            source_revision_id=SOURCE_ID,
            digest=store.digest,
            media_type="application/vnd.lae.deployment-plan+json",
            size_bytes=len(body),
            storage_key=key,
            source_snapshot_digest="sha256:" + "c" * 64,
        )
        images = TrustedImages()
        return S3DeploymentPlanResolver(store, images), artifact, images, store

    async def test_resolves_verified_candidate_without_prebuilding_images(self) -> None:
        resolver, artifact, images, _store = self.fixture()
        prepared = await resolver.resolve(artifact)
        self.assertEqual(prepared.source_revision_id, SOURCE_ID)
        self.assertEqual(prepared.kind, "compose")
        self.assertEqual(
            {service.service_key: service.role for service in prepared.services},
            {"web": "http", "postgres": "datastore"},
        )
        self.assertIsNone(prepared.luma_manifest_digest)
        self.assertRegex(
            prepared.normalized_compose_digest or "", r"^sha256:[0-9a-f]{64}$"
        )
        self.assertRegex(
            prepared.environment_schema_digest, r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(prepared.volumes[0].backup_policy, "none")
        self.assertEqual(images.calls, 0)

        second = await resolver.resolve(artifact)
        self.assertEqual(second, prepared)

    async def test_admission_does_not_require_post_build_catalog(self) -> None:
        resolver, artifact, _images, store = self.fixture()
        unconfigured = S3DeploymentPlanResolver(
            store, UnconfiguredBuildArtifactImageResolver()
        )
        prepared = await unconfigured.resolve(artifact)
        self.assertIsNone(prepared.luma_manifest_digest)

        configuration = await unconfigured.resolve_configuration(artifact)
        public = configuration.public_body()
        self.assertEqual(
            [item["name"] for item in public["environment"]],
            ["DATABASE_URL", "POSTGRES_PASSWORD"],
        )
        self.assertEqual(
            public["serviceKeys"], ["web", "postgres"]
        )
        self.assertEqual(
            public["services"],
            [
                {
                    "key": "web",
                    "role": "http",
                    "dependencies": ["postgres"],
                    "resources": {"cpu": "0.50", "memoryMiB": 512},
                    "port": 8080,
                    "imageSource": "build",
                    "healthPath": "/healthz",
                },
                {
                    "key": "postgres",
                    "role": "datastore",
                    "dependencies": [],
                    "resources": {"cpu": "0.50", "memoryMiB": 1024},
                    "port": None,
                    "imageSource": "external",
                    "healthPath": None,
                },
            ],
        )
        self.assertEqual(
            public["routes"],
            [{"serviceKey": "web", "containerPort": 8080, "healthPath": "/healthz", "primary": True}],
        )
        self.assertEqual(public["volumes"][0]["mountPath"], "/var/lib/postgresql/data")
        encoded = json.dumps(public, sort_keys=True)
        for forbidden in (artifact.storage_key, artifact.artifact_id, "value", "ciphertext"):
            self.assertNotIn(forbidden, encoded)

    async def test_source_snapshot_binding_and_canonical_json_are_strict(self) -> None:
        wrong = plan()
        wrong["sourceDigest"] = "sha256:" + "d" * 64
        resolver, artifact, images, _store = self.fixture(wrong)
        with self.assertRaisesRegex(DeploymentPlanInvalid, "source snapshot"):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)

        noncanonical = json.dumps(plan(), indent=2).encode()
        resolver, artifact, images, _store = self.fixture(raw=noncanonical)
        with self.assertRaisesRegex(DeploymentPlanInvalid, "not canonical"):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)

    async def test_metadata_digest_size_and_media_are_reverified(self) -> None:
        resolver, artifact, _images, store = self.fixture()
        with self.assertRaisesRegex(DeploymentPlanInvalid, "metadata changed"):
            await resolver.resolve(replace(artifact, digest="sha256:" + "d" * 64))
        with self.assertRaisesRegex(DeploymentPlanInvalid, "metadata changed"):
            store.media_type = "application/json"
            await resolver.resolve(artifact)

    async def test_environment_and_route_semantics_fail_before_image_resolution(self) -> None:
        incomplete = plan()
        incomplete["environment"] = [incomplete["environment"][0]]
        resolver, artifact, images, _store = self.fixture(incomplete)
        with self.assertRaisesRegex(DeploymentPlanInvalid, "schema is incomplete"):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)

        route = plan()
        route["routes"][0]["containerPort"] = 9090
        resolver, artifact, images, _store = self.fixture(route)
        with self.assertRaisesRegex(DeploymentPlanInvalid, "route binding"):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)

        unsafe_image = plan()
        unsafe_image["services"][1]["image"]["ref"] = "http://127.0.0.1/image"
        resolver, artifact, images, _store = self.fixture(unsafe_image)
        with self.assertRaises(DeploymentPlanInvalid):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)

        cycle = plan()
        cycle["services"][1]["dependencies"] = ["web"]
        resolver, artifact, images, _store = self.fixture(cycle)
        with self.assertRaisesRegex(DeploymentPlanInvalid, "cycle"):
            await resolver.resolve(artifact)
        self.assertEqual(images.calls, 0)


if __name__ == "__main__":
    unittest.main()
