from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
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
from lae_api.object_source_broker_api import (  # noqa: E402
    OBJECT_SOURCE_REDEMPTION_PATH,
    ObjectSourceBrokerRuntime,
    ObjectSourceBrokerService,
    object_source_broker_runtime_from_env,
)
from lae_api.credential_broker_api import InternalBrokerToken  # noqa: E402
from lae_store import (  # noqa: E402
    CredentialLeaseRejected,
    FakeUploadObjectStore,
    ObjectSourceDescriptor,
    ObjectSourceRedemptionClaim,
    ObjectSourceRedemptionRequest,
    ObjectSourceRedemptionResult,
    new_id,
)
from luma.credential_broker import (  # noqa: E402
    ObjectSourceLeaseBinding as LumaObjectSourceLeaseBinding,
    _validate_object_source_response as validate_luma_object_response,
)


BROKER_TOKEN = "object-broker-service-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SIGNED_URL = (
    "https://objects.example.test/lae-uploads/private/object.zip"
    "?X-Amz-Signature=signed-url-canary"
)
OBJECT_KEY = "tenants/hidden/apps/hidden/quarantine/private/object.zip"
DIGEST = "sha256:" + "a" * 64


class FakeBroker:
    def __init__(self) -> None:
        self.requests: list[ObjectSourceRedemptionRequest] = []
        self.failure: Exception | None = None

    async def redeem(
        self, request: ObjectSourceRedemptionRequest
    ) -> ObjectSourceRedemptionResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ObjectSourceRedemptionResult(
            request=request,
            expires_at=int(time.time()) + 60,
            allowed_host="objects.example.test",
            object_url=SIGNED_URL,
        )


class ExplodingUserAuth:
    async def authenticate(self, _token):
        raise AssertionError("user authentication must not run for object broker")

    async def authenticate_deploy_token(self, _token, *, request_ip):
        del request_ip
        raise AssertionError("deploy-token authentication must not run")


class ObjectSourceBrokerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = FakeBroker()
        runtime = ObjectSourceBrokerRuntime(
            self.broker, InternalBrokerToken(BROKER_TOKEN)
        )
        app = create_app(
            auth_service=ExplodingUserAuth(),  # type: ignore[arg-type]
            object_source_broker=runtime,
        )
        self.context = TestClient(app, base_url="https://lae.example.test")
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    @staticmethod
    def body() -> dict[str, object]:
        return {
            "schemaVersion": "luma.object-source-redemption/v1",
            "leaseId": new_id("lease"),
            "builderTaskId": "builder-" + "a" * 24,
            "externalOperationId": new_id("op"),
            "principalRef": "lae-builder",
            "tenantRef": new_id("ten"),
            "applicationRef": new_id("app"),
            "object": {
                "kind": "object",
                "digest": DIGEST,
                "mediaType": "application/zip",
                "sizeBytes": 4096,
            },
        }

    @staticmethod
    def headers(token: str = BROKER_TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_exact_luma_wire_is_no_store_and_preserves_every_binding(self) -> None:
        body = self.body()
        response = self.client.post(
            OBJECT_SOURCE_REDEMPTION_PATH,
            headers=self.headers(),
            json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        result = response.json()
        self.assertEqual(
            result["schemaVersion"],
            "luma.object-source-redemption-result/v1",
        )
        self.assertEqual(result["method"], "GET")
        self.assertEqual(result["allowedHost"], "objects.example.test")
        self.assertEqual(result["objectUrl"], SIGNED_URL)
        for key in (
            "leaseId",
            "builderTaskId",
            "externalOperationId",
            "principalRef",
            "tenantRef",
            "applicationRef",
            "object",
        ):
            self.assertEqual(result[key], body[key])

        binding = LumaObjectSourceLeaseBinding(
            lease_id=body["leaseId"],  # type: ignore[arg-type]
            builder_task_id=body["builderTaskId"],  # type: ignore[arg-type]
            external_operation_id=body["externalOperationId"],  # type: ignore[arg-type]
            principal_ref=body["principalRef"],  # type: ignore[arg-type]
            tenant_ref=body["tenantRef"],  # type: ignore[arg-type]
            application_ref=body["applicationRef"],  # type: ignore[arg-type]
            object_descriptor=body["object"],  # type: ignore[arg-type]
        )
        decoded = validate_luma_object_response(
            result,
            expected=binding.request_body(),
            now=int(time.time()),
        )
        self.assertEqual(decoded.object_url, SIGNED_URL)
        self.assertNotIn(SIGNED_URL, repr(decoded))
        self.assertNotIn(BROKER_TOKEN, repr(InternalBrokerToken(BROKER_TOKEN)))

    def test_auth_body_schema_and_failures_are_generic(self) -> None:
        for headers in (
            {},
            self.headers("wrong-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            {"Authorization": "Basic " + BROKER_TOKEN},
        ):
            response = self.client.post(
                OBJECT_SOURCE_REDEMPTION_PATH,
                headers=headers,
                json=self.body(),
            )
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(BROKER_TOKEN, response.text)
        self.assertEqual(self.broker.requests, [])

        body = self.body()
        raw = json.dumps(body)[:-1] + ',"leaseId":"lease_duplicate"}'
        duplicate = self.client.post(
            OBJECT_SOURCE_REDEMPTION_PATH,
            headers={**self.headers(), "Content-Type": "application/json"},
            content=raw,
        )
        self.assertEqual(duplicate.status_code, 400)

        extra = self.client.post(
            OBJECT_SOURCE_REDEMPTION_PATH,
            headers=self.headers(),
            json={**self.body(), "objectUrl": SIGNED_URL},
        )
        self.assertEqual(extra.status_code, 409)
        self.assertNotIn(SIGNED_URL, extra.text)

        self.broker.failure = CredentialLeaseRejected(
            "leaked " + OBJECT_KEY + " " + SIGNED_URL
        )
        rejected = self.client.post(
            OBJECT_SOURCE_REDEMPTION_PATH,
            headers=self.headers(),
            json=self.body(),
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertNotIn(OBJECT_KEY, rejected.text)
        self.assertNotIn(SIGNED_URL, rejected.text)

        self.broker.failure = RuntimeError("driver leaked " + OBJECT_KEY)
        unavailable = self.client.post(
            OBJECT_SOURCE_REDEMPTION_PATH,
            headers=self.headers(),
            json=self.body(),
        )
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn(OBJECT_KEY, unavailable.text)

    def test_factory_uses_safe_shared_token_file_and_api_s3_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "broker.token"
            token_path.write_text(BROKER_TOKEN + "\n", encoding="utf-8")
            token_path.chmod(0o600)
            runtime = object_source_broker_runtime_from_env(
                object(),
                environment="production",
                environ={
                    "LAE_CREDENTIAL_BROKER_TOKEN_FILE": str(token_path),
                    "LAE_UPLOAD_DRIVER": "s3",
                    "LAE_UPLOAD_S3_ENDPOINT": "https://objects.example.test",
                    "LAE_UPLOAD_S3_BUCKET": "lae-uploads",
                    "LAE_UPLOAD_S3_REGION": "us-east-1",
                    "LAE_UPLOAD_S3_ACCESS_KEY": "api-access-key",
                    "LAE_UPLOAD_S3_SECRET_KEY": "api-secret-key-value",
                },
            )
            self.assertTrue(runtime.token.matches(BROKER_TOKEN))
            self.assertNotIn(BROKER_TOKEN, repr(runtime))


class ObjectSourceBrokerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_key_is_signed_only_after_claim_and_never_rendered(self) -> None:
        request = ObjectSourceRedemptionRequest(
            lease_id=new_id("lease"),
            builder_task_id="builder-" + "b" * 24,
            external_operation_id=new_id("op"),
            principal_ref="lae-builder",
            tenant_ref=new_id("ten"),
            application_ref=new_id("app"),
            object=ObjectSourceDescriptor(DIGEST, "application/zip", 4),
        )

        class ClaimBroker:
            async def redeem(self, actual):
                self.actual = actual
                return ObjectSourceRedemptionClaim(
                    request=actual,
                    allowed_host="upload.invalid",
                    ttl_seconds=1,
                    object_key=OBJECT_KEY,
                )

        objects = FakeUploadObjectStore()
        objects.seed(OBJECT_KEY, b"data", "application/zip")
        broker = ClaimBroker()
        service = ObjectSourceBrokerService(
            broker, objects, allowed_host="upload.invalid"
        )
        result = await service.redeem(request)
        self.assertEqual(result.allowed_host, "upload.invalid")
        self.assertEqual(broker.actual, request)
        self.assertNotIn(OBJECT_KEY, repr(result))
        self.assertNotIn(result.object_url, repr(result))
        self.assertGreater(
            result.expires_at, int(datetime.now(timezone.utc).timestamp())
        )


if __name__ == "__main__":
    unittest.main()
