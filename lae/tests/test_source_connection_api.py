from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from dataclasses import replace
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
from lae_api.source_connection_api import (  # noqa: E402
    SourceConnectionApiService,
    source_connection_service_from_env,
)
from lae_store import (  # noqa: E402
    IdempotencyKeyReused,
    ResourceNotFound,
    SourceConnectionKeyRing,
    SourceConnectionMutationResult,
    SourceConnectionRecord,
    new_id,
)
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)


class SourceConnectionAuth:
    def __init__(self) -> None:
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.deploy_tenant_id = self.tenant_id
        self.session_id = new_id("ses")
        self.deploy_token_id = new_id("dtk")
        self.deploy_scopes = frozenset({"sources:write"})

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
            scopes=self.deploy_scopes,
        )

    def csrf_valid(
        self,
        _principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        return csrf_cookie == self.csrf_token and csrf_header == self.csrf_token


class RecordingSourceConnectionStore:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.key_ring = SourceConnectionKeyRing(
            current_version=1,
            encryption_keys={1: b"a" * 32},
            hmac_keys={1: b"h" * 32},
        )
        self.now = datetime.now(timezone.utc)
        self.record: SourceConnectionRecord | None = None
        self.calls: list[object] = []
        self.idempotency: dict[tuple[str, str], tuple[object, dict[str, object]]] = {}

    def result(
        self,
        kind: str,
        command: object,
        body: dict[str, object],
    ) -> SourceConnectionMutationResult:
        key = (kind, command.idempotency_key)
        existing = self.idempotency.get(key)
        fingerprint = (
            getattr(command, "connection_id", None),
            getattr(command, "provider", None),
            getattr(command, "base_url", None),
            getattr(command, "username", None),
            getattr(command, "username_provided", None),
            getattr(command, "secret", None),
        )
        if existing is not None:
            if existing[0] != fingerprint:
                raise IdempotencyKeyReused("different request")
            return SourceConnectionMutationResult(existing[1], replayed=True)
        self.idempotency[key] = (fingerprint, body)
        return SourceConnectionMutationResult(body, replayed=False)

    async def create(self, command: object) -> SourceConnectionMutationResult:
        self.calls.append(command)
        if command.scope.tenant_id != self.tenant_id:
            raise ResourceNotFound("missing")
        if self.record is None:
            self.record = SourceConnectionRecord(
                id=new_id("conn"),
                provider=command.provider,
                display_name=command.display_name,
                base_url=command.base_url,
                allowed_host=command.allowed_host,
                username=command.username,
                credential_version=1,
                created_at=self.now,
                updated_at=self.now,
                last_used_at=None,
                revoked_at=None,
            )
        return self.result("create", command, {"connection": self.record.public_body()})

    async def list(self, scope: object) -> tuple[SourceConnectionRecord, ...]:
        if scope.tenant_id != self.tenant_id or self.record is None:
            return ()
        return (self.record,)

    async def rotate(self, command: object) -> SourceConnectionMutationResult:
        self.calls.append(command)
        if (
            command.scope.tenant_id != self.tenant_id
            or self.record is None
            or command.connection_id != self.record.id
            or self.record.revoked_at is not None
        ):
            raise ResourceNotFound("missing")
        username = (
            command.username if command.username_provided else self.record.username
        )
        self.record = replace(
            self.record,
            username=username,
            credential_version=self.record.credential_version + 1,
            updated_at=self.now,
        )
        return self.result("rotate", command, {"connection": self.record.public_body()})

    async def revoke(self, command: object) -> SourceConnectionMutationResult:
        self.calls.append(command)
        if (
            command.scope.tenant_id != self.tenant_id
            or self.record is None
            or command.connection_id != self.record.id
        ):
            raise ResourceNotFound("missing")
        self.record = replace(self.record, revoked_at=self.now, updated_at=self.now)
        return self.result("revoke", command, {"connection": self.record.public_body()})


class SourceConnectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = SourceConnectionAuth()
        self.store = RecordingSourceConnectionStore(self.auth.tenant_id)
        self.service = SourceConnectionApiService(self.store)  # type: ignore[arg-type]
        self.client_context = TestClient(
            create_app(
                auth_service=self.auth,  # type: ignore[arg-type]
                source_connections=self.service,
            ),
            base_url="https://lae.example.test",
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.deploy_token}"}

    def payload(self, **changes: object) -> dict[str, object]:
        body: dict[str, object] = {
            "provider": "gitea",
            "displayName": "Production Gitea",
            "baseUrl": "https://Git.Example.com:443/gitea/",
            "username": "deploy",
            "secret": "api-private-token-canary",
        }
        body.update(changes)
        return body

    def create(self, key: str = "source-create-1") -> object:
        return self.client.post(
            "/v1/source-connections",
            headers={**self.bearer, "Idempotency-Key": key},
            json=self.payload(),
        )

    def test_deploy_token_crud_idempotency_and_metadata_only_responses(self) -> None:
        created = self.create()
        replay = self.create()
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(created.json(), replay.json())
        self.assertEqual(created.headers["idempotency-replayed"], "false")
        self.assertEqual(replay.headers["idempotency-replayed"], "true")
        connection = created.json()["connection"]
        self.assertEqual(connection["baseUrl"], "https://git.example.com/gitea")
        self.assertEqual(connection["allowedHost"], "git.example.com")
        serialized = created.text.lower()
        for forbidden in (
            "api-private-token-canary",
            "ciphertext",
            "nonce",
            "checksum",
            self.auth.deploy_token.lower(),
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("api-private-token-canary", repr(self.store.calls[0]))

        listed = self.client.get("/v1/source-connections", headers=self.bearer)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["connections"], [connection])

        connection_id = connection["id"]
        rotated = self.client.post(
            f"/v1/source-connections/{connection_id}/rotate",
            headers={**self.bearer, "Idempotency-Key": "source-rotate-1"},
            json={"secret": "api-private-token-canary-v2"},
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["connection"]["credentialVersion"], 2)
        self.assertEqual(rotated.json()["connection"]["username"], "deploy")
        self.assertNotIn("api-private-token-canary-v2", rotated.text)
        self.assertNotIn("api-private-token-canary-v2", repr(self.store.calls[-1]))

        revoked = self.client.delete(
            f"/v1/source-connections/{connection_id}",
            headers={**self.bearer, "Idempotency-Key": "source-revoke-1"},
        )
        replayed_revoke = self.client.delete(
            f"/v1/source-connections/{connection_id}",
            headers={**self.bearer, "Idempotency-Key": "source-revoke-1"},
        )
        self.assertEqual(revoked.status_code, 204, revoked.text)
        self.assertEqual(replayed_revoke.status_code, 204, replayed_revoke.text)
        self.assertEqual(revoked.headers["idempotency-replayed"], "false")
        self.assertEqual(replayed_revoke.headers["idempotency-replayed"], "true")

    def test_scope_csrf_tenant_and_idempotency_guards(self) -> None:
        missing_key = self.client.post(
            "/v1/source-connections", headers=self.bearer, json=self.payload()
        )
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(
            missing_key.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED"
        )

        self.auth.deploy_scopes = frozenset({"apps:read"})
        forbidden = self.create("forbidden")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "LAE_FORBIDDEN")

        self.auth.deploy_scopes = frozenset({"sources:write"})
        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)
        no_csrf = self.client.post(
            "/v1/source-connections",
            headers={"Idempotency-Key": "session-no-csrf"},
            json=self.payload(),
        )
        self.assertEqual(no_csrf.status_code, 403)
        with_csrf = self.client.post(
            "/v1/source-connections",
            headers={
                "Idempotency-Key": "session-with-csrf",
                "X-CSRF-Token": self.auth.csrf_token,
            },
            json=self.payload(),
        )
        self.assertEqual(with_csrf.status_code, 201, with_csrf.text)

        self.client.cookies.clear()
        self.auth.deploy_tenant_id = new_id("ten")
        foreign_list = self.client.get("/v1/source-connections", headers=self.bearer)
        self.assertEqual(foreign_list.status_code, 200)
        self.assertEqual(foreign_list.json(), {"connections": []})
        foreign_rotate = self.client.post(
            f"/v1/source-connections/{with_csrf.json()['connection']['id']}/rotate",
            headers={**self.bearer, "Idempotency-Key": "foreign-rotate"},
            json={"secret": "foreign-secret-canary"},
        )
        self.assertEqual(foreign_rotate.status_code, 404)
        self.assertNotIn("foreign-secret-canary", foreign_rotate.text)

    def test_source_policy_and_secret_validation_never_echo_credentials(self) -> None:
        for index, changes in enumerate(
            (
                {"baseUrl": "http://git.example.com"},
                {"baseUrl": "https://user:password@git.example.com"},
                {"baseUrl": "https://git.example.com?token=private"},
                {"baseUrl": "https://127.0.0.1"},
                {"baseUrl": "https://localhost"},
                {"baseUrl": "https://git.internal"},
                {"secret": "line\nbreak"},
                {"provider": "github", "baseUrl": "https://git.example.com"},
            )
        ):
            rejected = self.client.post(
                "/v1/source-connections",
                headers={
                    **self.bearer,
                    "Idempotency-Key": f"source-policy-{index}",
                },
                json=self.payload(**changes),
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertEqual(
                rejected.json()["error"]["code"],
                "LAE_INVALID_SOURCE_CONNECTION",
            )
            for forbidden in ("password", "private", "line", "break"):
                self.assertNotIn(forbidden, rejected.text.lower())

    def test_missing_capability_fails_closed_without_affecting_api_liveness(
        self,
    ) -> None:
        context = TestClient(
            create_app(auth_service=self.auth),  # type: ignore[arg-type]
            base_url="https://lae.example.test",
        )
        with context as client:
            unavailable = client.get("/v1/source-connections", headers=self.bearer)
            live = client.get("/health/live")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.json()["error"]["code"],
            "LAE_SOURCE_CONNECTIONS_UNAVAILABLE",
        )
        self.assertEqual(live.status_code, 200)

    def test_runtime_key_configuration_is_independent_and_strict(self) -> None:
        encoded_aead = base64.b64encode(b"a" * 32).decode("ascii")
        encoded_hmac = base64.b64encode(b"h" * 32).decode("ascii")
        encoded_idempotency = base64.b64encode(b"i" * 32).decode("ascii")
        values = {
            "LAE_SOURCE_CONNECTION_KEY_VERSION": "1",
            "LAE_SOURCE_CONNECTION_AEAD_KEYS": json.dumps({"1": encoded_aead}),
            "LAE_SOURCE_CONNECTION_HMAC_KEYS": json.dumps({"1": encoded_hmac}),
            "LAE_SOURCE_CONNECTION_IDEMPOTENCY_HMAC_KEY": encoded_idempotency,
        }
        with patch.dict(os.environ, values, clear=False):
            service = source_connection_service_from_env(object())
        self.assertEqual(service.key_ring.current_version, 1)

        invalid = {**values, "LAE_SOURCE_CONNECTION_AEAD_KEYS": "{}"}
        with patch.dict(os.environ, invalid, clear=False):
            with self.assertRaises(ValueError):
                source_connection_service_from_env(object())

        missing = {key: "" for key in values}
        with patch.dict(os.environ, missing, clear=False):
            with self.assertRaises(ValueError):
                source_connection_service_from_env(object())


if __name__ == "__main__":
    unittest.main()
