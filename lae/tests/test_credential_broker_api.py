from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT.parent))
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.app import create_app  # noqa: E402
from lae_api.credential_broker_api import (  # noqa: E402
    CREDENTIAL_REDEMPTION_MAX_BODY_BYTES,
    CREDENTIAL_REDEMPTION_PATH,
    CredentialBrokerRuntime,
    InternalBrokerToken,
    credential_broker_runtime_from_env,
)
from lae_store import (  # noqa: E402
    CREDENTIAL_REDEMPTION_REQUEST_SCHEMA,
    CREDENTIAL_REDEMPTION_RESULT_SCHEMA,
    CredentialLeaseRejected,
    CredentialRedemptionResult,
    new_id,
)
from luma.credential_broker import (  # noqa: E402
    CredentialLeaseBinding as LumaCredentialLeaseBinding,
    _validate_response as validate_luma_broker_response,
)


BROKER_TOKEN = "broker-service-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SECRET = "github_pat_broker_response_canary_123456789"


class ExplodingUserAuth:
    async def authenticate(self, _token):
        raise AssertionError("user auth must not run for the internal broker")

    async def authenticate_deploy_token(self, _token, *, request_ip):
        del request_ip
        raise AssertionError("deploy-token auth must not run for the internal broker")


class FakeBroker:
    def __init__(self) -> None:
        self.requests = []
        self.failure: Exception | None = None
        self.kind = "git-https"

    async def redeem(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return CredentialRedemptionResult(
            request,
            self.kind,
            expires_at=2_000_000_100,
            username="x-access-token" if self.kind == "git-https" else "",
            password=SECRET if self.kind == "git-https" else "",
        )


class CredentialBrokerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = FakeBroker()
        runtime = CredentialBrokerRuntime(
            self.broker, InternalBrokerToken(BROKER_TOKEN)
        )
        self.context = TestClient(
            create_app(
                auth_service=ExplodingUserAuth(),  # type: ignore[arg-type]
                credential_broker=runtime,
            ),
            base_url="https://lae.example.test",
        )
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    @staticmethod
    def body() -> dict[str, str]:
        return {
            "schemaVersion": CREDENTIAL_REDEMPTION_REQUEST_SCHEMA,
            "leaseId": new_id("lease"),
            "builderTaskId": "builder-" + "a" * 24,
            "externalOperationId": new_id("op"),
            "principalRef": "lae-worker",
            "tenantRef": new_id("ten"),
            "applicationRef": new_id("app"),
            "repository": "https://github.com/acme/private.git",
        }

    @staticmethod
    def headers(token: str = BROKER_TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_exact_luma_wire_contract_bypasses_user_auth_and_is_no_store(self) -> None:
        body = self.body()
        response = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers=self.headers(),
            json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        result = response.json()
        self.assertEqual(result["schemaVersion"], CREDENTIAL_REDEMPTION_RESULT_SCHEMA)
        for key in (
            "leaseId",
            "builderTaskId",
            "externalOperationId",
            "principalRef",
            "tenantRef",
            "applicationRef",
            "repository",
        ):
            self.assertEqual(result[key], body[key])
        self.assertEqual(result["kind"], "git-https")
        self.assertEqual(result["credential"]["password"], SECRET)
        luma_binding = LumaCredentialLeaseBinding(
            lease_id=body["leaseId"],
            builder_task_id=body["builderTaskId"],
            external_operation_id=body["externalOperationId"],
            principal_ref=body["principalRef"],
            tenant_ref=body["tenantRef"],
            application_ref=body["applicationRef"],
            repository=body["repository"],
        )
        decoded = validate_luma_broker_response(
            result,
            expected=luma_binding.request_body(),
            now=2_000_000_000,
        )
        self.assertEqual(decoded.password, SECRET)
        self.assertEqual(len(self.broker.requests), 1)
        self.assertNotIn(BROKER_TOKEN, repr(InternalBrokerToken(BROKER_TOKEN)))
        self.assertNotIn(SECRET, repr(CredentialRedemptionResult(
            self.broker.requests[0],
            "git-https",
            2_000_000_100,
            "x-access-token",
            SECRET,
        )))

    def test_anonymous_result_has_no_credential_member(self) -> None:
        self.broker.kind = "none"
        response = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers=self.headers(),
            json=self.body(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "none")
        self.assertNotIn("credential", response.json())

    def test_wrong_missing_or_duplicate_authorization_never_touches_broker(self) -> None:
        for headers in (
            {},
            self.headers("wrong-service-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            {"Authorization": "Basic " + BROKER_TOKEN},
            {"Authorization": "Bearer " + BROKER_TOKEN + ", Bearer second"},
        ):
            response = self.client.post(
                CREDENTIAL_REDEMPTION_PATH, headers=headers, json=self.body()
            )
            self.assertEqual(response.status_code, 401, response.text)
            self.assertNotIn(BROKER_TOKEN, response.text)
        self.assertEqual(self.broker.requests, [])

    def test_content_type_size_duplicate_and_extra_fields_fail_closed(self) -> None:
        wrong_type = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers={**self.headers(), "Content-Type": "text/plain"},
            content=json.dumps(self.body()),
        )
        self.assertEqual(wrong_type.status_code, 415)

        oversized = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers={**self.headers(), "Content-Type": "application/json"},
            content=b"{" + b"x" * CREDENTIAL_REDEMPTION_MAX_BODY_BYTES + b"}",
        )
        self.assertEqual(oversized.status_code, 413)

        duplicate = self.body()
        raw = json.dumps(duplicate)[:-1] + ',"leaseId":"lease_duplicate"}'
        duplicate_response = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers={**self.headers(), "Content-Type": "application/json"},
            content=raw,
        )
        self.assertEqual(duplicate_response.status_code, 400)

        extra = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers=self.headers(),
            json={**self.body(), "credential": {"password": SECRET}},
        )
        self.assertEqual(extra.status_code, 409)
        self.assertNotIn(SECRET, extra.text)
        self.assertEqual(self.broker.requests, [])

    def test_binding_and_internal_failures_never_echo_secret_url_or_body(self) -> None:
        body = self.body()
        self.broker.failure = CredentialLeaseRejected(
            "internal " + SECRET + " " + body["repository"]
        )
        unavailable = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers=self.headers(),
            json=body,
        )
        self.assertEqual(unavailable.status_code, 409)
        self.assertNotIn(SECRET, unavailable.text)
        self.assertNotIn(body["repository"], unavailable.text)

        self.broker.failure = RuntimeError("driver leaked " + SECRET)
        failed = self.client.post(
            CREDENTIAL_REDEMPTION_PATH,
            headers=self.headers(),
            json=body,
        )
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn(SECRET, failed.text)

    def test_token_env_and_safe_file_configuration_are_strict(self) -> None:
        env_runtime = credential_broker_runtime_from_env(
            object(),
            connection_key_ring=None,
            environment="production",
            environ={"LAE_CREDENTIAL_BROKER_TOKEN": BROKER_TOKEN},
        )
        self.assertTrue(env_runtime.token.matches(BROKER_TOKEN))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker.token"
            path.write_text(BROKER_TOKEN + "\n", encoding="utf-8")
            path.chmod(0o600)
            file_runtime = credential_broker_runtime_from_env(
                object(),
                connection_key_ring=None,
                environment="production",
                environ={"LAE_CREDENTIAL_BROKER_TOKEN_FILE": str(path)},
            )
            self.assertTrue(file_runtime.token.matches(BROKER_TOKEN))

            path.chmod(0o640)
            with self.assertRaises(ValueError):
                credential_broker_runtime_from_env(
                    object(),
                    connection_key_ring=None,
                    environment="production",
                    environ={"LAE_CREDENTIAL_BROKER_TOKEN_FILE": str(path)},
                )

        with self.assertRaises(ValueError):
            credential_broker_runtime_from_env(
                object(),
                connection_key_ring=None,
                environment="production",
                environ={},
            )
        with self.assertRaises(ValueError):
            credential_broker_runtime_from_env(
                object(),
                connection_key_ring=None,
                environment="production",
                environ={
                    "LAE_CREDENTIAL_BROKER_TOKEN": BROKER_TOKEN,
                    "LAE_CREDENTIAL_BROKER_TOKEN_FILE": "/secret/token",
                },
            )


if __name__ == "__main__":
    unittest.main()
