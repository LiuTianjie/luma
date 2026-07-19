from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from lae_api.app import create_app
from lae_api.admin_api import (
    AdminAuthenticationError,
    AdminAuthenticator,
    admin_authenticator_from_env,
)


class _Store:
    def __init__(self) -> None:
        self.calls = []

    async def users(self, *, limit, offset):
        self.calls.append(("users", limit, offset))
        return {
            "users": [{"id": "usr_test", "email": "admin-view@example.test"}],
            "page": {"limit": limit, "offset": offset, "total": 1},
        }

    async def tenants(self, **kwargs):
        return {"tenants": [], "page": {**kwargs, "total": 0}}

    async def applications(self, **kwargs):
        return {"applications": [], "page": {**kwargs, "total": 0}}

    async def operations(self, **kwargs):
        return {"operations": [], "page": {**kwargs, "total": 0}}

    async def usage(self, **kwargs):
        return {"usage": [], "page": {**kwargs, "total": 0}}


class _ExplodingUserAuth:
    async def authenticate(self, _token):
        raise AssertionError("user auth must not run for admin API")

    async def authenticate_deploy_token(self, _token, *, request_ip):
        del request_ip
        raise AssertionError("deploy-token auth must not run for admin API")


def _app(token: str, store: _Store):
    return create_app(
        auth_service=_ExplodingUserAuth(),  # type: ignore[arg-type]
        admin_authenticator=AdminAuthenticator(token),
        admin_store=store,
    )


class AdminApiTests(unittest.TestCase):
    def test_authenticator_is_exact_constant_time_bearer_boundary(self) -> None:
        token = "admin-service-token-" + "a" * 32
        authenticator = AdminAuthenticator(token)
        authenticator.require(["Bearer " + token])
        for values in ([], ["bearer " + token], ["Bearer wrong"], ["Bearer " + token] * 2):
            with self.subTest(values=values), self.assertRaises(
                AdminAuthenticationError
            ):
                authenticator.require(values)

    def test_internal_routes_reject_user_tokens_and_never_cache(self) -> None:
        token = "admin-service-token-" + "b" * 32
        store = _Store()
        with TestClient(_app(token, store)) as client:
            self.assertEqual(client.get("/internal/v1/admin/users").status_code, 401)
            self.assertEqual(
                client.get(
                    "/internal/v1/admin/users",
                    headers={"Authorization": "Bearer user-deploy-token"},
                ).status_code,
                401,
            )
            response = client.get(
                "/internal/v1/admin/users?limit=25&offset=50",
                headers={"Authorization": "Bearer " + token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(store.calls, [("users", 25, 50)])
        rendered = response.text
        self.assertNotIn("deployToken", rendered)
        self.assertNotIn("credential", rendered)

    def test_production_style_factory_requires_private_regular_token_file(self) -> None:
        token = "admin-service-token-" + "c" * 32
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admin-token"
            path.write_text(token + "\n", encoding="utf-8")
            path.chmod(0o600)
            configured = admin_authenticator_from_env(
                {"LAE_ADMIN_API_TOKEN_FILE": str(path)}
            )
            configured.require(["Bearer " + token])
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                admin_authenticator_from_env({"LAE_ADMIN_API_TOKEN_FILE": str(path)})
        with self.assertRaises(ValueError):
            admin_authenticator_from_env({"LAE_ADMIN_API_TOKEN": token})


if __name__ == "__main__":
    unittest.main()
