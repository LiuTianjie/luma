from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.upload_api import UploadApiService, register_upload_routes  # noqa: E402
from lae_store import (  # noqa: E402
    FakeUploadObjectStore,
    IdempotencyKeyReused,
    ResourceNotFound,
    UploadCompletionClaim,
    UploadRecord,
    UploadReservationResult,
    new_id,
)


class TestApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    credential_type: str
    credential_id: str


class RecordingUploadStore:
    def __init__(self) -> None:
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.records: dict[str, UploadRecord] = {}
        self.keys: dict[str, tuple[dict[str, Any], str]] = {}
        self.complete_keys: dict[str, str] = {}

    async def create(self, command: Any) -> UploadReservationResult:
        payload = command.hash_payload()
        existing = self.keys.get(command.idempotency_key)
        if existing is not None:
            previous, upload_id = existing
            if previous != payload:
                raise IdempotencyKeyReused("different request")
            return UploadReservationResult(self.records[upload_id], True)
        now = datetime.now(timezone.utc)
        upload_id = new_id("upl")
        record = UploadRecord(
            id=upload_id,
            application_id=command.application_id,
            operation_id=new_id("op"),
            source_revision_id=None,
            filename=command.filename,
            kind=command.kind,
            media_type=command.media_type,
            expected_bytes=command.expected_bytes,
            actual_bytes=None,
            expected_sha256=command.expected_sha256,
            actual_sha256=None,
            status="quarantine",
            cleanup_status="none",
            failure_code=None,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
            operation_status="queued",
            object_key=(
                f"tenants/{command.scope.tenant_id}/apps/{command.application_id}/"
                f"quarantine/{upload_id}/secret-object.html"
            ),
        )
        self.records[upload_id] = record
        self.keys[command.idempotency_key] = (payload, upload_id)
        return UploadReservationResult(record, False)

    async def get(self, scope: Any, upload_id: str) -> UploadRecord:
        record = self.records.get(upload_id)
        if record is None or not record.object_key.startswith(f"tenants/{scope.tenant_id}/"):
            raise ResourceNotFound("not found")
        return record

    async def claim_completion(self, command: Any) -> UploadCompletionClaim:
        record = await self.get(command.scope, command.upload_id)
        existing = self.complete_keys.get(record.id)
        if existing is not None:
            if existing != command.idempotency_key:
                raise IdempotencyKeyReused("different complete key")
            return UploadCompletionClaim(record, False, True)
        self.complete_keys[record.id] = command.idempotency_key
        verifying = replace(record, status="verifying")
        self.records[record.id] = verifying
        return UploadCompletionClaim(verifying, True, False)

    async def mark_scanning(
        self, scope: Any, upload_id: str, *, actual_bytes: int, actual_sha256: str
    ) -> UploadRecord:
        record = await self.get(scope, upload_id)
        updated = replace(
            record,
            status="scanning",
            actual_bytes=actual_bytes,
            actual_sha256=actual_sha256,
            operation_status="running",
        )
        self.records[upload_id] = updated
        return updated

    async def mark_failed(
        self, scope: Any, upload_id: str, *, failure_code: str
    ) -> UploadRecord:
        record = await self.get(scope, upload_id)
        updated = replace(
            record,
            status="failed",
            failure_code=failure_code,
            operation_status="failed",
        )
        self.records[upload_id] = updated
        return updated

    async def delete(self, scope: Any, upload_id: str) -> UploadRecord:
        record = await self.get(scope, upload_id)
        updated = replace(record, status="deleting", cleanup_status="deleting")
        self.records[upload_id] = updated
        return updated

    async def finish_delete(self, scope: Any, upload_id: str) -> UploadRecord:
        record = await self.get(scope, upload_id)
        updated = replace(record, status="deleted", cleanup_status="deleted")
        self.records[upload_id] = updated
        return updated


class StaticUploadApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RecordingUploadStore()
        self.objects = FakeUploadObjectStore()
        self.service = UploadApiService(self.store, self.objects)
        app = FastAPI()

        async def auth(
            request: Request,
            scope: str,
            *,
            csrf_header: str | None = None,
            mutation: bool,
        ) -> Principal:
            if scope != "sources:write":
                raise TestApiError(403, "LAE_FORBIDDEN", "Forbidden")
            authorization = request.headers.get("Authorization")
            session = request.cookies.get("session")
            if authorization == "Bearer valid":
                return Principal(self.store.tenant_id, "deploy_token", new_id("dtk"))
            if authorization == "Bearer other":
                return Principal(self.store.other_tenant_id, "deploy_token", new_id("dtk"))
            if authorization == "Bearer read-only":
                raise TestApiError(403, "LAE_FORBIDDEN", "Forbidden")
            if session == "valid":
                if mutation and csrf_header != "csrf-valid":
                    raise TestApiError(403, "LAE_CSRF_FAILED", "CSRF validation failed")
                return Principal(self.store.tenant_id, "session", new_id("ses"))
            raise TestApiError(401, "LAE_UNAUTHENTICATED", "Authentication is required")

        app.state.require_scoped_principal = auth

        @app.exception_handler(TestApiError)
        async def handle(_request: Request, exc: TestApiError) -> JSONResponse:
            return JSONResponse(
                {"error": {"code": exc.code, "message": exc.message}},
                status_code=exc.status,
            )

        register_upload_routes(app, lambda: self.service, TestApiError)
        self.context = TestClient(app, base_url="https://lae.example.test")
        self.client = self.context.__enter__()
        self.content = b"<!doctype html><html></html>"
        self.payload = {
            "applicationId": new_id("app"),
            "filename": "index.html",
            "mediaType": "text/html",
            "sizeBytes": len(self.content),
            "sha256": f"sha256:{hashlib.sha256(self.content).hexdigest()}",
        }

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": "Bearer valid"}

    def test_auth_scope_csrf_and_idempotency_are_enforced(self) -> None:
        headers = {"Idempotency-Key": "upload-create-auth"}
        unauthenticated = self.client.post("/v1/uploads", headers=headers, json=self.payload)
        self.assertEqual(unauthenticated.status_code, 401)
        forbidden = self.client.post(
            "/v1/uploads",
            headers={**headers, "Authorization": "Bearer read-only"},
            json=self.payload,
        )
        self.assertEqual(forbidden.status_code, 403)
        self.client.cookies.set("session", "valid")
        no_csrf = self.client.post("/v1/uploads", headers=headers, json=self.payload)
        self.assertEqual(no_csrf.status_code, 403)
        created = self.client.post(
            "/v1/uploads",
            headers={**headers, "X-CSRF-Token": "csrf-valid"},
            json=self.payload,
        )
        self.assertEqual(created.status_code, 201)

    def test_url_is_issued_once_and_never_leaks_from_status_complete_or_errors(self) -> None:
        headers = {**self.bearer, "Idempotency-Key": "upload-create-once"}
        created = self.client.post("/v1/uploads", headers=headers, json=self.payload)
        replay = self.client.post("/v1/uploads", headers=headers, json=self.payload)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        transfer = created.json()["transfer"]
        upload_id = created.json()["upload"]["id"]
        self.assertTrue(created.json()["uploadUrlIssued"])
        self.assertFalse(replay.json()["uploadUrlIssued"])
        self.assertNotIn("transfer", replay.json())
        self.assertEqual(created.headers["Cache-Control"], "no-store, max-age=0")
        self.objects.put_from_grant(
            transfer["url"], self.content, headers=transfer["headers"]
        )
        completed = self.client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers={**self.bearer, "Idempotency-Key": "upload-complete-once"},
            json={},
        )
        status = self.client.get(f"/v1/uploads/{upload_id}", headers=self.bearer)
        self.assertEqual(completed.status_code, 202)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(completed.json()["upload"]["status"], "scanning")
        for response in (replay, completed, status):
            serialized = response.text.lower()
            self.assertNotIn("upload.invalid", serialized)
            self.assertNotIn("secret-object", serialized)
            self.assertNotIn("quarantine/", serialized)
            self.assertNotIn("x-amz", serialized)

        foreign = self.client.get(
            f"/v1/uploads/{upload_id}",
            headers={"Authorization": "Bearer other"},
        )
        missing = self.client.get(
            f"/v1/uploads/{new_id('upl')}", headers=self.bearer
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())

    def test_invalid_type_missing_idempotency_and_delete_are_stable(self) -> None:
        invalid = dict(self.payload)
        invalid["filename"] = "site.tar.gz"
        invalid["mediaType"] = "application/gzip"
        rejected = self.client.post(
            "/v1/uploads",
            headers={**self.bearer, "Idempotency-Key": "upload-invalid-type"},
            json=invalid,
        )
        self.assertEqual(rejected.status_code, 400)
        missing = self.client.post("/v1/uploads", headers=self.bearer, json=self.payload)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "LAE_IDEMPOTENCY_REQUIRED")

        created = self.client.post(
            "/v1/uploads",
            headers={**self.bearer, "Idempotency-Key": "upload-delete-create"},
            json=self.payload,
        )
        upload_id = created.json()["upload"]["id"]
        no_key = self.client.delete(f"/v1/uploads/{upload_id}", headers=self.bearer)
        self.assertEqual(no_key.status_code, 400)
        deleted = self.client.delete(
            f"/v1/uploads/{upload_id}",
            headers={**self.bearer, "Idempotency-Key": "upload-delete"},
        )
        self.assertEqual(deleted.status_code, 202)
        self.assertEqual(deleted.json()["upload"]["status"], "deleted")


if __name__ == "__main__":
    unittest.main()
