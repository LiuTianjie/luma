from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_luma_adapter import RuntimeServicePrincipal  # noqa: E402
from lae_worker import (  # noqa: E402
    HttpEphemeralRuntimeSecretIssuer,
    RuntimeSecretIssueBinding,
    RuntimeSecretPlaintext,
    RuntimeSecretsUnavailable,
)


NOW = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)


class Response:
    status = 201

    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class Opener:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body
        self.requests = []

    def open(self, request, *, timeout):
        del timeout
        self.requests.append(request)
        return Response(self.body)


class RedirectOpener:
    def open(self, request, *, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            307,
            "redirect",
            {"Location": "https://attacker.invalid/steal"},
            io.BytesIO(b""),
        )


class RuntimeSecretIssuerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.binding = RuntimeSecretIssueBinding(
            tenant_ref="ten_runtime_secret",
            application_ref="app_runtime_secret",
            operation_ref="op_runtime_secret",
            deployment_ref="dep_runtime_secret",
            revision_ref="rev_runtime_secret",
            service_key="web",
            name="DATABASE_URL",
            environment_version=7,
        )
        self.plaintext = "postgres://secret-canary@db/app"
        self.response = {
            "schemaVersion": "luma.lae-runtime/v1",
            "replayed": False,
            "secret": {
                "serviceKey": "web",
                "name": "DATABASE_URL",
                "secretRef": "lsec_abcdefghijklmnopqrstuvwxyz",
                "environmentVersion": 7,
                "expiresAt": (NOW + timedelta(seconds=60)).isoformat(),
            },
        }

    async def test_posts_closed_bound_request_with_dedicated_audience(self) -> None:
        opener = Opener(self.response)
        issuer = HttpEphemeralRuntimeSecretIssuer(
            "https://luma.runtime.internal",
            RuntimeServicePrincipal("lae-runtime", "runtime-token-canary"),
            opener=opener,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        result = await issuer.issue(
            self.binding,
            RuntimeSecretPlaintext(self.plaintext),
            ttl_seconds=60,
        )
        self.assertEqual(result.secret_ref, "lsec_abcdefghijklmnopqrstuvwxyz")
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/v1/lae/runtime/secrets:issue"))
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-luma-principal-audience"], "luma-lae-runtime")
        self.assertEqual(headers["x-lae-tenant-id"], self.binding.tenant_ref)
        self.assertEqual(headers["x-lae-deployment-id"], self.binding.deployment_ref)
        self.assertEqual(
            headers["idempotency-key"],
            "lae:op_runtime_secret:runtime-secret:web:DATABASE_URL:v1",
        )
        self.assertEqual(
            set(json.loads(request.data)),
            {
                "schemaVersion",
                "serviceKey",
                "name",
                "plaintext",
                "environmentVersion",
                "ttlSeconds",
            },
        )
        self.assertNotIn(self.plaintext, repr(issuer) + repr(result))

    async def test_redirect_and_mismatched_or_expired_response_fail_closed(self) -> None:
        issuer = HttpEphemeralRuntimeSecretIssuer(
            "https://luma.runtime.internal",
            RuntimeServicePrincipal("lae-runtime", "runtime-token-canary"),
            opener=RedirectOpener(),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        with self.assertRaises(RuntimeSecretsUnavailable) as caught:
            await issuer.issue(
                self.binding,
                RuntimeSecretPlaintext(self.plaintext),
                ttl_seconds=60,
            )
        self.assertNotIn(self.plaintext, str(caught.exception))

        bad = json.loads(json.dumps(self.response))
        bad["secret"]["serviceKey"] = "admin"
        issuer = HttpEphemeralRuntimeSecretIssuer(
            "https://luma.runtime.internal",
            RuntimeServicePrincipal("lae-runtime", "runtime-token-canary"),
            opener=Opener(bad),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        with self.assertRaises(RuntimeSecretsUnavailable):
            await issuer.issue(
                self.binding,
                RuntimeSecretPlaintext(self.plaintext),
                ttl_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
