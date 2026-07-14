from __future__ import annotations

import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "staging_product_e2e.py"
SPEC = importlib.util.spec_from_file_location("staging_product_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class JsonClientResponseTest(unittest.TestCase):
    def test_retryable_gateway_html_is_an_api_failure(self) -> None:
        with self.assertRaises(MODULE.ApiFailure) as captured:
            MODULE.JsonClient._response(_Response(502, b"<html>bad gateway</html>"))

        self.assertEqual(captured.exception.status, 502)
        self.assertEqual(captured.exception.code, "LAE_API_INVALID_RESPONSE")
        self.assertTrue(captured.exception.retryable)

    def test_public_probe_accepts_plain_text_platform_health(self) -> None:
        with patch.object(
            MODULE.urllib.request,
            "urlopen",
            return_value=_Response(200, b"ok\n"),
        ):
            self.assertEqual(
                MODULE.public_probe("static.itool.tech", timeout_seconds=1), {}
            )


class _Headers:
    def get_all(self, name: str, default: list[str] | None = None) -> list[str]:
        if name.lower() != "set-cookie":
            return default or []
        return [
            "__Host-lae_session=session-test; Path=/; Secure; HttpOnly",
            "__Host-lae_csrf=csrf-test; Path=/; Secure",
        ]


class _PreviewClient:
    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        self.cookies: dict[str, str] = {}
        self.paths: list[str] = []

    def request(self, method: str, path: str, body=None, **kwargs):
        self.paths.append(path)
        if path == "/auth/preview":
            return MODULE.Response(
                201,
                {
                    "email": "preview@lae.invalid",
                    "purpose": self.purpose,
                    "magicToken": "lae_em_test",
                },
                _Headers(),
            )
        if path in {"/auth/login/verify", "/auth/email/verify"}:
            return MODULE.Response(200, {}, _Headers())
        if path == "/deploy-tokens":
            return MODULE.Response(
                201,
                {"plaintext": "lae_dt_test", "token": {"id": "token-test"}},
                _Headers(),
            )
        raise AssertionError(f"unexpected path: {path}")

    def remember_response_cookies(self, response) -> None:
        self.cookies = {
            "__Host-lae_session": "session-test",
            "__Host-lae_csrf": "csrf-test",
        }


class PreviewAuthenticationTest(unittest.TestCase):
    def test_preview_verification_follows_issued_purpose(self) -> None:
        for purpose, expected_path in (
            ("login", "/auth/login/verify"),
            ("register", "/auth/email/verify"),
        ):
            with self.subTest(purpose=purpose):
                client = _PreviewClient(purpose)
                token, token_id = MODULE.issue_preview_deploy_token(
                    client,
                    deadline=time.monotonic() + 2,
                )
                self.assertEqual(token, "lae_dt_test")
                self.assertEqual(token_id, "token-test")
                self.assertEqual(
                    client.paths,
                    ["/auth/preview", expected_path, "/deploy-tokens"],
                )

    def test_successful_invalid_json_is_an_acceptance_failure(self) -> None:
        with self.assertRaisesRegex(MODULE.AcceptanceFailure, "invalid JSON"):
            MODULE.JsonClient._response(_Response(200, b"not-json"))

    def test_retryable_non_object_json_is_an_api_failure(self) -> None:
        with self.assertRaises(MODULE.ApiFailure) as captured:
            MODULE.JsonClient._response(_Response(504, b"[]"))

        self.assertTrue(captured.exception.retryable)


class _BillingClient:
    def __init__(self, current: str) -> None:
        self.current = current
        self.calls: list[tuple[str, str, object, dict[str, object]]] = []

    def request(self, method: str, path: str, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if method == "GET" and path == "/billing/subscription":
            return MODULE.Response(
                200,
                {"subscription": {"plan": {"code": self.current}}},
                {},
            )
        if method == "POST" and path == "/billing/checkout-sessions":
            self.current = str(body["plan"])
            return MODULE.Response(
                201,
                {"order": {"id": "order-test", "provider": "mock"}},
                {},
            )
        if method == "POST" and path == "/billing/mock/orders/order-test/approve":
            return MODULE.Response(200, {"accepted": True}, {})
        raise AssertionError(f"unexpected request: {method} {path}")


class PreviewBillingTest(unittest.TestCase):
    def test_explicit_mock_upgrade_uses_session_checkout_and_verifies_subscription(self) -> None:
        client = _BillingClient("lite")
        MODULE.ensure_preview_mock_subscription(
            client,
            plan="ultra",
            deadline=time.monotonic() + 2,
        )

        self.assertEqual(
            [(method, path) for method, path, _body, _kwargs in client.calls],
            [
                ("GET", "/billing/subscription"),
                ("POST", "/billing/checkout-sessions"),
                ("POST", "/billing/mock/orders/order-test/approve"),
                ("GET", "/billing/subscription"),
            ],
        )
        checkout = client.calls[1]
        approval = client.calls[2]
        self.assertEqual(checkout[2], {"plan": "ultra", "interval": "monthly"})
        self.assertTrue(checkout[3]["csrf"])
        self.assertTrue(approval[3]["csrf"])
        self.assertTrue(checkout[3]["idempotency_key"].startswith("e2e-checkout-"))
        self.assertTrue(approval[3]["idempotency_key"].startswith("e2e-approve-"))

    def test_already_active_preview_plan_does_not_create_an_order(self) -> None:
        client = _BillingClient("ultra")
        MODULE.ensure_preview_mock_subscription(
            client,
            plan="ultra",
            deadline=time.monotonic() + 2,
        )

        self.assertEqual(
            [(method, path) for method, path, _body, _kwargs in client.calls],
            [("GET", "/billing/subscription")],
        )


if __name__ == "__main__":
    unittest.main()
