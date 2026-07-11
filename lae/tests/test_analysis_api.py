from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from lae_store import (  # noqa: E402
    AnalysisRequestRecord,
    CreateAnalysisRequest,
    CreateUploadAnalysis,
    IdempotencyKeyReused,
    Principal,
    ResourceNotFound,
    SourceConnectionHostMismatch,
    SourceConnectionUnavailable,
    TenantScope,
    canonical_https_repository,
    canonical_subdirectory,
    new_id,
)
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)


class AnalysisAuthService:
    def __init__(self) -> None:
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.deploy_tenant_id = self.tenant_id
        self.session_id = new_id("ses")
        self.deploy_token_id = new_id("dtk")
        self.deploy_scopes = frozenset({"analyses:write"})

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


class RecordingAnalysisRequestStore:
    def __init__(self) -> None:
        self.calls: list[CreateAnalysisRequest] = []
        self._requests: dict[str, tuple[dict[str, object], AnalysisRequestRecord]] = {}
        self.not_found_application_id: str | None = None
        self.connection_error: Exception | None = None

    async def create(self, command: CreateAnalysisRequest) -> AnalysisRequestRecord:
        self.calls.append(command)
        if command.connection_id is not None and self.connection_error is not None:
            raise self.connection_error
        if command.application_id == self.not_found_application_id:
            raise ResourceNotFound("application not found")
        payload = command.hash_payload()
        existing = self._requests.get(command.idempotency_key)
        if existing is not None:
            previous, record = existing
            if previous != payload:
                raise IdempotencyKeyReused("different request")
            return replace(record, replayed=True)
        record = AnalysisRequestRecord(
            analysis_id=new_id("ana"),
            operation_id=new_id("op"),
            source_revision_id=new_id("src"),
            application_id=command.application_id,
            analysis_status="queued",
            operation_status="queued",
            replayed=False,
        )
        self._requests[command.idempotency_key] = (payload, record)
        return record


class AnalysisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = AnalysisAuthService()
        self.store = RecordingAnalysisRequestStore()
        self.application_id = new_id("app")
        self.client_context = TestClient(
            create_app(self.auth, self.store),
            base_url="https://lae.example.test",
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def payload(self, **source_changes: object) -> dict[str, object]:
        source: dict[str, object] = {
            "type": "git",
            "repository": "https://GitHub.com:443/acme/example.git/",
            "ref": "main",
            "subdirectory": "services/web",
        }
        source.update(source_changes)
        return {
            "applicationId": self.application_id,
            "source": source,
            "intent": {"region": "cn", "publicProtocols": ["http"]},
        }

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.deploy_token}"}

    def test_bearer_create_and_idempotency_replay_return_only_public_contract(
        self,
    ) -> None:
        headers = {**self.bearer, "Idempotency-Key": "analysis-create-1"}
        created = self.client.post("/v1/analyses", headers=headers, json=self.payload())
        replayed = self.client.post("/v1/analyses", headers=headers, json=self.payload())
        self.assertEqual(created.status_code, 202)
        self.assertEqual(replayed.status_code, 202)
        self.assertEqual(created.json(), replayed.json())
        self.assertEqual(created.headers["idempotency-replayed"], "false")
        self.assertEqual(replayed.headers["idempotency-replayed"], "true")
        self.assertEqual(created.json()["analysis"]["status"], "queued")
        self.assertEqual(created.json()["operation"]["status"], "queued")
        serialized = created.text.lower()
        for forbidden in (
            "credential",
            "lease",
            "luma",
            "image",
            "repository",
            self.auth.deploy_token.lower(),
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            self.store.calls[0].repository,
            "https://github.com/acme/example.git",
        )
        self.assertEqual(self.store.calls[0].principal.type, "deploy-token")

    def test_public_region_contract_rejects_internal_home_at_every_admission_layer(
        self,
    ) -> None:
        payload = self.payload()
        payload["intent"] = {"region": "home", "publicProtocols": ["http"]}
        rejected = self.client.post(
            "/v1/analyses",
            headers={**self.bearer, "Idempotency-Key": "home-is-internal"},
            json=payload,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(
            rejected.json()["error"]["code"], "LAE_INVALID_ARGUMENT"
        )
        self.assertEqual(self.store.calls, [])

        scope = TenantScope(self.auth.tenant_id)
        principal = Principal("deploy-token", self.auth.deploy_token_id)
        with self.assertRaisesRegex(ValueError, "region is invalid"):
            CreateAnalysisRequest(
                scope=scope,
                principal=principal,
                application_id=self.application_id,
                repository="https://github.com/acme/example.git",
                ref="main",
                subdirectory="",
                region="home",
                public_protocols=("http",),
                idempotency_key="store-home-git",
            )
        with self.assertRaisesRegex(ValueError, "region is invalid"):
            CreateUploadAnalysis(
                scope=scope,
                principal=principal,
                application_id=self.application_id,
                upload_id=new_id("upl"),
                region="home",
                public_protocols=("http",),
                idempotency_key="store-home-upload",
            )

    def test_scope_csrf_and_dual_credential_guards_close_all_auth_paths(self) -> None:
        headers = {**self.bearer, "Idempotency-Key": "analysis-auth-1"}
        self.auth.deploy_scopes = frozenset({"apps:read"})
        forbidden = self.client.post("/v1/analyses", headers=headers, json=self.payload())
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "LAE_FORBIDDEN")

        self.auth.deploy_scopes = frozenset({"analyses:write"})
        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)
        no_csrf = self.client.post(
            "/v1/analyses",
            headers={"Idempotency-Key": "analysis-auth-2"},
            json=self.payload(),
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(no_csrf.json()["error"]["code"], "LAE_CSRF_FAILED")
        with_csrf = self.client.post(
            "/v1/analyses",
            headers={
                "Idempotency-Key": "analysis-auth-2",
                "X-CSRF-Token": self.auth.csrf_token,
            },
            json=self.payload(),
        )
        self.assertEqual(with_csrf.status_code, 202)
        self.assertEqual(self.store.calls[-1].principal.type, "session")

        self.auth.deploy_tenant_id = new_id("ten")
        confused = self.client.post(
            "/v1/analyses",
            headers={
                **self.bearer,
                "Idempotency-Key": "analysis-auth-3",
                "X-CSRF-Token": self.auth.csrf_token,
            },
            json=self.payload(),
        )
        self.assertEqual(confused.status_code, 401)
        self.assertEqual(confused.json()["error"]["code"], "LAE_UNAUTHENTICATED")

    def test_idempotency_conflict_missing_key_and_source_policy_are_stable(self) -> None:
        missing = self.client.post(
            "/v1/analyses", headers=self.bearer, json=self.payload()
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED")

        headers = {**self.bearer, "Idempotency-Key": "analysis-conflict-1"}
        first = self.client.post("/v1/analyses", headers=headers, json=self.payload())
        conflict = self.client.post(
            "/v1/analyses",
            headers=headers,
            json=self.payload(ref="release"),
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["error"]["code"], "LAE_IDEMPOTENCY_KEY_REUSED"
        )

        for source in (
            {"repository": "http://github.com/acme/example.git"},
            {"repository": "https://user:password@github.com/acme/example.git"},
            {"repository": "https://github.com/acme/example.git?token=hidden"},
            {"repository": "https://127.0.0.1/acme/example.git"},
            {"repository": "https://[::1]/acme/example.git"},
            {"repository": "https://localhost/acme/example.git"},
            {"repository": "https://git/acme/example.git"},
            {"repository": "https://git.service.local/acme/example.git"},
            {"repository": "https://git.service.internal/acme/example.git"},
            {"repository": "https://git.service.lan/acme/example.git"},
            {"repository": "https://git.home.arpa/acme/example.git"},
            {"subdirectory": "../escape"},
        ):
            rejected = self.client.post(
                "/v1/analyses",
                headers={
                    **self.bearer,
                    "Idempotency-Key": f"reject-{len(self.store.calls)}",
                },
                json=self.payload(**source),
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertEqual(
                rejected.json()["error"]["code"], "LAE_UNSUPPORTED_SOURCE"
            )
            self.assertNotIn("password", rejected.text.lower())
            self.assertNotIn("hidden", rejected.text.lower())

        connection_id = new_id("conn")
        private_source = self.client.post(
            "/v1/analyses",
            headers={
                **self.bearer,
                "Idempotency-Key": "private-source-connection",
            },
            json=self.payload(connectionId=connection_id),
        )
        self.assertEqual(private_source.status_code, 202, private_source.text)
        self.assertEqual(self.store.calls[-1].connection_id, connection_id)

        internal = {
            **self.payload(),
            "credentialLeaseId": "lease_internal_only",
            "lumaPrincipalId": "must-not-be-public",
        }
        rejected_internal = self.client.post(
            "/v1/analyses",
            headers={**self.bearer, "Idempotency-Key": "reject-internal-fields"},
            json=internal,
        )
        self.assertEqual(rejected_internal.status_code, 400)
        self.assertEqual(
            rejected_internal.json()["error"]["code"], "LAE_INVALID_ARGUMENT"
        )
        self.assertNotIn("lease_internal_only", rejected_internal.text)

    def test_existing_application_is_required_and_foreign_ids_are_not_disclosed(
        self,
    ) -> None:
        missing_payload = self.payload()
        del missing_payload["applicationId"]
        missing = self.client.post(
            "/v1/analyses",
            headers={**self.bearer, "Idempotency-Key": "missing-app"},
            json=missing_payload,
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(
            missing.json()["error"]["code"], "LAE_APPLICATION_REQUIRED"
        )

        foreign_application_id = new_id("app")
        self.store.not_found_application_id = foreign_application_id
        hidden = self.client.post(
            "/v1/analyses",
            headers={**self.bearer, "Idempotency-Key": "foreign-app"},
            json={**self.payload(), "applicationId": foreign_application_id},
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json()["error"]["code"], "LAE_NOT_FOUND")
        self.assertNotIn(foreign_application_id, hidden.text)

    def test_private_connection_capability_and_host_errors_are_stable(self) -> None:
        connection_id = new_id("conn")
        headers = {**self.bearer, "Idempotency-Key": "private-host-mismatch"}
        self.store.connection_error = SourceConnectionHostMismatch("internal")
        mismatch = self.client.post(
            "/v1/analyses",
            headers=headers,
            json=self.payload(connectionId=connection_id),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(
            mismatch.json()["error"]["code"],
            "LAE_SOURCE_CONNECTION_HOST_MISMATCH",
        )
        self.assertNotIn(connection_id, mismatch.text)

        self.store.connection_error = SourceConnectionUnavailable("internal")
        unavailable = self.client.post(
            "/v1/analyses",
            headers={**self.bearer, "Idempotency-Key": "private-unavailable"},
            json=self.payload(connectionId=connection_id),
        )
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.json()["error"]["code"],
            "LAE_SOURCE_CONNECTIONS_UNAVAILABLE",
        )
        self.assertTrue(unavailable.json()["error"]["retryable"])
        self.assertNotIn(connection_id, unavailable.text)

    def test_canonical_source_helpers_reject_escape_and_non_https(self) -> None:
        self.assertEqual(
            canonical_https_repository("https://EXAMPLE.com:443/acme/app.git/"),
            "https://example.com/acme/app.git",
        )
        self.assertEqual(canonical_subdirectory("services/web/"), "services/web")
        for value in (
            "git@example.com:acme/app.git",
            "ssh://git@example.com/acme/app.git",
            "https://user:secret@example.com/acme/app.git",
            "https://127.0.0.1/acme/app.git",
            "https://[::1]/acme/app.git",
            "https://localhost/acme/app.git",
            "https://git/acme/app.git",
            "https://git.local/acme/app.git",
            "https://git.internal/acme/app.git",
            "https://git.lan/acme/app.git",
            "https://home.arpa/acme/app.git",
            "https://git.home.arpa/acme/app.git",
            "https://git.test/acme/app.git",
        ):
            with self.assertRaises(ValueError):
                canonical_https_repository(value)
        with self.assertRaises(ValueError):
            canonical_subdirectory("services/../secret")
        with self.assertRaises(ValueError):
            canonical_subdirectory("/absolute/path")


if __name__ == "__main__":
    unittest.main()
