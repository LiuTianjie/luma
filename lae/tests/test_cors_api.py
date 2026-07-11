from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-core" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_api.app import create_app  # noqa: E402
from lae_store.auth import AuthConfigurationError  # noqa: E402


class CorsApiTests(unittest.TestCase):
    origin = "https://lae-staging.itool.tech"

    def test_allowed_origin_preflight_supports_web_credentials_and_headers(self) -> None:
        with TestClient(create_app(cors_allowed_origins=[self.origin])) as client:
            response = client.options(
                "/v1/applications",
                headers={
                    "Origin": self.origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "content-type,idempotency-key,x-csrf-token,x-request-id"
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], self.origin)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")
        self.assertIn("POST", response.headers["access-control-allow-methods"])
        allowed_headers = response.headers["access-control-allow-headers"].lower()
        for header in (
            "content-type",
            "idempotency-key",
            "x-csrf-token",
            "x-request-id",
        ):
            self.assertIn(header, allowed_headers)
        self.assertEqual(response.headers["access-control-max-age"], "600")

    def test_actual_response_exposes_request_id_only_to_allowed_origin(self) -> None:
        app = create_app(cors_allowed_origins=self.origin)
        with TestClient(app) as client:
            allowed = client.get("/health/live", headers={"Origin": self.origin})
            denied = client.get(
                "/health/live", headers={"Origin": "https://attacker.example"}
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], self.origin)
        self.assertEqual(allowed.headers["access-control-allow-credentials"], "true")
        self.assertEqual(
            allowed.headers["access-control-expose-headers"], "X-Request-Id"
        )
        self.assertNotIn("access-control-allow-origin", denied.headers)

    def test_default_has_no_cross_origin_access(self) -> None:
        with TestClient(create_app(cors_allowed_origins=[])) as client:
            response = client.get("/health/live", headers={"Origin": self.origin})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_environment_accepts_an_explicit_comma_separated_allowlist(self) -> None:
        second_origin = "https://console.example.test:8443"
        with patch.dict(
            "os.environ",
            {"LAE_CORS_ALLOWED_ORIGINS": f" {self.origin}, {second_origin} "},
        ):
            app = create_app()
        with TestClient(app) as client:
            response = client.get(
                "/health/live", headers={"Origin": second_origin}
            )

        self.assertEqual(response.headers["access-control-allow-origin"], second_origin)

    def test_wildcard_and_non_origin_values_fail_closed(self) -> None:
        for value in (
            "*",
            "https://lae-staging.itool.tech/",
            "https://user@lae-staging.itool.tech",
        ):
            with self.subTest(value=value):
                with self.assertRaises(AuthConfigurationError):
                    create_app(cors_allowed_origins=value)


if __name__ == "__main__":
    unittest.main()
