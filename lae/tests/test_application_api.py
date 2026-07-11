from __future__ import annotations

import asyncio
import hmac
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from lae_api.application_api import (  # noqa: E402
    ApplicationApiService,
    application_service_from_env,
)
from lae_store import (  # noqa: E402
    ApplicationRecord,
    ApplicationSummary,
    EnvironmentKeyRing,
    EnvironmentMetadata,
    EnvironmentVariableMetadata,
    EnvironmentVersionConflict,
    DeploymentEnvironmentScopeInvalid,
    HttpRouteRecord,
    IdempotencyKeyReused,
    IdempotentCatalogResult,
    ResourceNotFound,
    PreparedEnvironmentVariable,
    ServiceRecord,
    TenantScope,
    VolumeRecord,
    new_id,
)
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)


class ApplicationAuth:
    def __init__(self) -> None:
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.deploy_tenant_id = self.tenant_id
        self.session_id = new_id("ses")
        self.deploy_token_id = new_id("dtk")
        self.scopes = frozenset({"apps:read", "apps:write"})

    async def authenticate(self, token: str) -> SessionPrincipal:
        if token != self.session_token:
            raise AuthRejected("invalid")
        return SessionPrincipal(
            session_id=self.session_id,
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            key_version=1,
            csrf_digest=b"c" * 32,
        )

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        del request_ip
        if token != self.deploy_token:
            raise AuthRejected("invalid")
        return DeployTokenPrincipal(
            token_id=self.deploy_token_id,
            token_prefix="0123456789",
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.deploy_tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=self.scopes,
        )

    def csrf_valid(
        self,
        _principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        return csrf_cookie == self.csrf_token and csrf_header == self.csrf_token


class RecordingApplicationCatalog:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.application_id = new_id("app")
        self.now = datetime.now(timezone.utc)
        self.environment_version = 0
        self.create_calls: list[object] = []
        self.patch_calls: list[object] = []
        self._idempotency: dict[tuple[str, str], tuple[bytes, dict[str, object]]] = {}

    def summary(self) -> ApplicationSummary:
        return ApplicationSummary(
            id=self.application_id,
            tenant_id=self.tenant_id,
            name="Stillwater",
            slug="stillwater",
            luma_name="lae-internal-must-not-escape",
            kind="pending",
            desired_state="running",
            observed_state="unknown",
            current_revision_id=None,
            current_deployment_id=None,
            environment_version=self.environment_version,
            created_at=self.now,
            updated_at=self.now,
        )

    def record(self) -> ApplicationRecord:
        return ApplicationRecord(
            application=self.summary(),
            services=(
                ServiceRecord(
                    id="svc_internal_web",
                    service_key="web",
                    role="http",
                    required=True,
                    desired_state="running",
                    observed_state="unknown",
                    current_image_digest=None,
                ),
            ),
            routes=(
                HttpRouteRecord(
                    id="rte_internal",
                    service_id="svc_internal_web",
                    hostname="0123456789abcdef0123456789abcdef.itool.tech",
                    is_primary=True,
                    container_port=8080,
                    status="pending",
                ),
            ),
            volumes=(
                VolumeRecord(
                    id="vol_internal",
                    volume_key="data",
                    requested_bytes=1024,
                    storage_policy="managed",
                    backup_policy="none",
                    delete_policy="retain",
                    status="pending",
                ),
            ),
            environment=self.environment(),
        )

    def environment(self) -> EnvironmentMetadata:
        variables = ()
        if self.patch_calls:
            command = self.patch_calls[-1]
            variables = tuple(
                EnvironmentVariableMetadata(
                    service_scope=item.service_scope,
                    name=item.name,
                    configured=True,
                    key_version=item.key_version,
                    is_sensitive=item.is_sensitive,
                    required=item.required,
                    source=item.source,
                    updated_at=self.now,
                )
                for item in command.set_values
            )
        return EnvironmentMetadata(self.environment_version, variables)

    def _replay(
        self, kind: str, idempotency: object, body: dict[str, object]
    ) -> IdempotentCatalogResult:
        key = (kind, idempotency.key)
        existing = self._idempotency.get(key)
        if existing is not None:
            if not hmac.compare_digest(existing[0], idempotency.request_hash):
                raise IdempotencyKeyReused("different request")
            return IdempotentCatalogResult(existing[1], replayed=True)
        self._idempotency[key] = (idempotency.request_hash, body)
        return IdempotentCatalogResult(body, replayed=False)

    async def create_application_draft_idempotent(
        self, command: object, *, principal: object, idempotency: object
    ) -> IdempotentCatalogResult:
        del principal
        self.create_calls.append(command)
        body = {
            "application": {
                "id": self.application_id,
                "name": command.name,
                "slug": command.slug,
                "kind": "pending",
                "desiredState": "running",
                "observedState": "unknown",
                "currentRevisionId": None,
                "currentDeploymentId": None,
                "environmentVersion": 0,
                "createdAt": self.now.isoformat().replace("+00:00", "Z"),
                "updatedAt": self.now.isoformat().replace("+00:00", "Z"),
            }
        }
        return self._replay("create", idempotency, body)

    async def list_applications(self, scope: object) -> tuple[ApplicationSummary, ...]:
        return (self.summary(),) if scope.tenant_id == self.tenant_id else ()

    async def get_application(
        self, scope: object, application_id: str
    ) -> ApplicationRecord:
        if scope.tenant_id != self.tenant_id or application_id != self.application_id:
            raise ResourceNotFound("missing")
        return self.record()

    async def get_environment(
        self, scope: object, application_id: str
    ) -> EnvironmentMetadata:
        if scope.tenant_id != self.tenant_id or application_id != self.application_id:
            raise ResourceNotFound("missing")
        return self.environment()

    async def patch_environment_idempotent(
        self, command: object, *, principal: object, idempotency: object
    ) -> IdempotentCatalogResult:
        del principal
        if (
            command.scope.tenant_id != self.tenant_id
            or command.application_id != self.application_id
        ):
            raise ResourceNotFound("missing")
        existing = self._idempotency.get(("patch", idempotency.key))
        if existing is not None:
            return self._replay("patch", idempotency, existing[1])
        if command.expected_version != self.environment_version:
            raise EnvironmentVersionConflict(
                expected=command.expected_version, actual=self.environment_version
            )
        self.patch_calls.append(command)
        self.environment_version += 1
        metadata = self.environment()
        body = {
            "environment": {
                "version": metadata.version,
                "variables": [
                    {
                        "serviceScope": item.service_scope,
                        "name": item.name,
                        "configured": True,
                        "sensitive": item.is_sensitive,
                        "required": item.required,
                        "source": item.source,
                        "updatedAt": self.now.isoformat().replace("+00:00", "Z"),
                    }
                    for item in metadata.variables
                ],
            }
        }
        return self._replay("patch", idempotency, body)


class ApplicationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = ApplicationAuth()
        self.catalog = RecordingApplicationCatalog(self.auth.tenant_id)
        self.service = ApplicationApiService(
            self.catalog,
            EnvironmentKeyRing(
                current_version=1,
                keys={1: b"e" * 32},
                checksum_key=b"c" * 32,
            ),
            idempotency_hash_key=b"i" * 32,
        )
        self.context = TestClient(
            create_app(self.auth, applications=self.service),
            base_url="https://lae.example.test",
        )
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.deploy_token}"}

    def test_create_requires_scope_idempotency_and_supports_safe_replay(self) -> None:
        payload = {"name": "Stillwater", "slug": "stillwater"}
        missing = self.client.post(
            "/v1/applications", headers=self.bearer, json=payload
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED")

        headers = {**self.bearer, "Idempotency-Key": "create-app-1"}
        created = self.client.post("/v1/applications", headers=headers, json=payload)
        replay = self.client.post("/v1/applications", headers=headers, json=payload)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(created.json(), replay.json())
        self.assertEqual(created.headers["idempotency-replayed"], "false")
        self.assertEqual(replay.headers["idempotency-replayed"], "true")
        self.assertEqual(created.json()["application"]["kind"], "pending")
        serialized = created.text.lower()
        self.assertNotIn("tenant", serialized)
        self.assertNotIn("luma", serialized)

        conflict = self.client.post(
            "/v1/applications",
            headers=headers,
            json={"name": "Changed", "slug": "changed"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "LAE_IDEMPOTENCY_KEY_REUSED")

        self.auth.scopes = frozenset({"apps:read"})
        forbidden = self.client.post(
            "/v1/applications",
            headers={**self.bearer, "Idempotency-Key": "scope-denied"},
            json=payload,
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_cookie_mutation_requires_csrf_even_with_same_bearer(self) -> None:
        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)
        headers = {
            **self.bearer,
            "Idempotency-Key": "session-create",
        }
        failed = self.client.post(
            "/v1/applications",
            headers=headers,
            json={"name": "Session", "slug": "session"},
        )
        self.assertEqual(failed.status_code, 403)
        headers["X-CSRF-Token"] = self.auth.csrf_token
        succeeded = self.client.post(
            "/v1/applications",
            headers=headers,
            json={"name": "Session", "slug": "session"},
        )
        self.assertEqual(succeeded.status_code, 201)

    def test_reads_are_tenant_fenced_and_strip_internal_catalog_fields(self) -> None:
        application_id = self.catalog.application_id
        detail = self.client.get(
            f"/v1/applications/{application_id}", headers=self.bearer
        )
        self.assertEqual(detail.status_code, 200)
        serialized = detail.text.lower()
        for forbidden in (
            self.auth.tenant_id.lower(),
            "lae-internal",
            "svc_internal",
            "rte_internal",
            "vol_internal",
            "ciphertext",
            "checksum",
            "keyversion",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(detail.json()["routes"][0]["serviceKey"], "web")
        for suffix, key in (
            ("services", "services"),
            ("routes", "routes"),
            ("volumes", "volumes"),
            ("environment", "environment"),
        ):
            response = self.client.get(
                f"/v1/applications/{application_id}/{suffix}", headers=self.bearer
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(key, response.json())

        self.auth.deploy_tenant_id = new_id("ten")
        hidden = self.client.get(
            f"/v1/applications/{application_id}", headers=self.bearer
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertNotIn(application_id, hidden.text)

    def test_environment_patch_encrypts_all_values_and_never_echoes_secret(
        self,
    ) -> None:
        secret = "never-echo-this-secret-value"
        path = f"/v1/applications/{self.catalog.application_id}/environment"
        headers = {**self.bearer, "Idempotency-Key": "environment-1"}
        payload = {
            "expectedVersion": 0,
            "set": {
                "*:API_KEY": {"value": secret, "sensitive": True},
                "*:NODE_ENV": {"value": "production", "sensitive": False},
            },
            "unset": [],
        }
        changed = self.client.patch(path, headers=headers, json=payload)
        replay = self.client.patch(path, headers=headers, json=payload)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(changed.json(), replay.json())
        self.assertEqual(changed.json()["environment"]["version"], 1)
        self.assertNotIn(secret, changed.text)
        self.assertNotIn("production", changed.text)
        self.assertNotIn(secret, repr(self.catalog.patch_calls[-1]))
        for encrypted in self.catalog.patch_calls[-1].set_values:
            self.assertNotIn(secret.encode(), encrypted.envelope_ciphertext)
            self.assertEqual(encrypted.key_version, 1)

        conflict = self.client.patch(
            path,
            headers=headers,
            json={
                **payload,
                "set": {"*:API_KEY": {"value": "different-secret"}},
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "LAE_IDEMPOTENCY_KEY_REUSED")
        stale = self.client.patch(
            path,
            headers={**self.bearer, "Idempotency-Key": "environment-stale"},
            json=payload,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.json()["error"]["code"], "LAE_ENVIRONMENT_VERSION_CONFLICT"
        )
        self.assertNotIn(secret, stale.text)

    def test_environment_validation_is_bounded_and_secret_safe(self) -> None:
        secret = "boundary-secret"
        response = self.client.patch(
            f"/v1/applications/{self.catalog.application_id}/environment",
            headers={**self.bearer, "Idempotency-Key": "invalid-env"},
            json={
                "expectedVersion": 0,
                "set": {"invalid-reference": {"value": secret}},
                "unset": [],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "LAE_INVALID_ARGUMENT")
        self.assertNotIn(secret, response.text)

    def test_plan_environment_patch_derives_flags_and_keeps_service_scopes(
        self,
    ) -> None:
        analysis_id = new_id("ana")
        digest = "sha256:" + "a" * 64
        secret = "postgres://plan-bound-secret"
        result = asyncio.run(
            self.service.patch_plan_environment(
                TenantScope(self.auth.tenant_id),
                type(
                    "Principal",
                    (),
                    {
                        "credential_type": "deploy_token",
                        "credential_id": self.auth.deploy_token_id,
                    },
                )(),
                self.catalog.application_id,
                analysis_id=analysis_id,
                expected_version=0,
                environment_schema_digest=digest,
                plan_service_keys=("web", "worker"),
                schema_environment=(
                    PreparedEnvironmentVariable(
                        "DATABASE_URL", ("web",), required=True, sensitive=True
                    ),
                ),
                values={
                    "web:DATABASE_URL": secret,
                    "worker:FEATURE_FLAG": "enabled",
                },
                unset=(),
                idempotency_key="plan-environment-1",
            )
        )
        self.assertEqual(result.response_body["environment"]["version"], 1)
        command = self.catalog.patch_calls[-1]
        self.assertEqual(command.plan_analysis_id, analysis_id)
        self.assertEqual(command.plan_environment_schema_digest, digest)
        self.assertEqual(command.plan_service_keys, ("web", "worker"))
        encrypted = {item.key: item for item in command.set_values}
        self.assertEqual(
            set(encrypted),
            {("web", "DATABASE_URL"), ("worker", "FEATURE_FLAG")},
        )
        self.assertTrue(encrypted[("web", "DATABASE_URL")].required)
        self.assertTrue(encrypted[("web", "DATABASE_URL")].is_sensitive)
        self.assertFalse(encrypted[("worker", "FEATURE_FLAG")].required)
        self.assertTrue(encrypted[("worker", "FEATURE_FLAG")].is_sensitive)
        self.assertEqual(
            {item.key for item in command.unset},
            {("*", "DATABASE_URL"), ("*", "FEATURE_FLAG")},
        )
        self.assertNotIn(secret, repr(command))

        with self.assertRaises(DeploymentEnvironmentScopeInvalid):
            asyncio.run(
                self.service.patch_plan_environment(
                    TenantScope(self.auth.tenant_id),
                    type(
                        "Principal",
                        (),
                        {
                            "credential_type": "deploy_token",
                            "credential_id": self.auth.deploy_token_id,
                        },
                    )(),
                    self.catalog.application_id,
                    analysis_id=analysis_id,
                    expected_version=1,
                    environment_schema_digest=digest,
                    plan_service_keys=("web", "worker"),
                    schema_environment=(
                        PreparedEnvironmentVariable(
                            "DATABASE_URL", ("web",), required=True
                        ),
                    ),
                    values={"worker:DATABASE_URL": "must-not-cross"},
                    unset=(),
                    idempotency_key="plan-environment-wrong-scope",
                )
            )

    def test_runtime_crypto_configuration_fails_readiness_closed(self) -> None:
        names = {
            "LAE_ENVIRONMENT_AEAD_KEY_VERSION",
            "LAE_ENVIRONMENT_AEAD_KEYS",
            "LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY",
            "LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY",
        }
        clean = {key: value for key, value in os.environ.items() if key not in names}
        with patch.dict(os.environ, clean, clear=True):
            with self.assertRaisesRegex(ValueError, "configuration is incomplete"):
                application_service_from_env(object())


if __name__ == "__main__":
    unittest.main()
