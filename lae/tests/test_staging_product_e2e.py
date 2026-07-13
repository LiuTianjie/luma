from __future__ import annotations

import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path


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


class JsonClientResponseTest(unittest.TestCase):
    def test_retryable_gateway_html_is_an_api_failure(self) -> None:
        with self.assertRaises(MODULE.ApiFailure) as captured:
            MODULE.JsonClient._response(_Response(502, b"<html>bad gateway</html>"))

        self.assertEqual(captured.exception.status, 502)
        self.assertEqual(captured.exception.code, "LAE_API_INVALID_RESPONSE")
        self.assertTrue(captured.exception.retryable)


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


if __name__ == "__main__":
    unittest.main()
