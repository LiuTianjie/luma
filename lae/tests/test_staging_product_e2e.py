from __future__ import annotations

import importlib.util
import io
import sys
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

    def test_successful_invalid_json_is_an_acceptance_failure(self) -> None:
        with self.assertRaisesRegex(MODULE.AcceptanceFailure, "invalid JSON"):
            MODULE.JsonClient._response(_Response(200, b"not-json"))

    def test_retryable_non_object_json_is_an_api_failure(self) -> None:
        with self.assertRaises(MODULE.ApiFailure) as captured:
            MODULE.JsonClient._response(_Response(504, b"[]"))

        self.assertTrue(captured.exception.retryable)


if __name__ == "__main__":
    unittest.main()
