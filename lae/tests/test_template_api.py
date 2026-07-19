from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app
from lae_api.template_api import (
    TEMPLATES,
    TemplateApiService,
    TemplateLaunchRequest,
    TemplateSmokeAuthenticator,
)
from lae_store import ResourceNotFound, new_id
from lae_store.auth import DeployTokenPrincipal, SessionPrincipal


class _Applications:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, scope, principal, payload, key):
        self.calls.append((scope, principal, payload, key))
        return SimpleNamespace(
            response_body={
                "application": {
                    "id": "app_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "name": payload.name,
                    "slug": payload.slug,
                    "kind": "pending",
                }
            },
            replayed=True,
        )


class _Analyses:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, command):
        self.calls.append(command)
        return SimpleNamespace(
            public_body=lambda: {
                "analysis": {"id": "ana_template", "status": "queued"},
                "operation": {"id": "op_template", "status": "queued"},
                "links": {"analysis": "/v1/analyses/ana_template", "events": "/v1/operations/op_template/events"},
            },
            replayed=True,
        )


class _TemplateHealth:
    def __init__(self) -> None:
        self.states: dict[str, SimpleNamespace] = {}

    async def publication(self, template_id, template_version):
        return self.states.get(
            template_id,
            SimpleNamespace(
                template_id=template_id,
                template_version=template_version,
                published=True,
                consecutive_failures=0,
                last_status="unverified",
                last_error_code=None,
            ),
        )

    async def record(
        self,
        *,
        template_id,
        template_version,
        run_id,
        succeeded,
        error_code,
    ):
        del run_id
        state = SimpleNamespace(
            template_id=template_id,
            template_version=template_version,
            published=succeeded,
            consecutive_failures=0 if succeeded else 3,
            last_status="succeeded" if succeeded else "failed",
            last_error_code=error_code,
        )
        self.states[template_id] = state
        return state


class TemplateApiTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_is_versioned_pinned_and_agent_verified(self) -> None:
        self.assertGreaterEqual(len(TEMPLATES), 4)
        self.assertEqual(len({item.id for item in TEMPLATES}), len(TEMPLATES))
        for item in TEMPLATES:
            with self.subTest(template=item.id):
                self.assertRegex(item.id, r"^[a-z0-9][a-z0-9-]+$")
                self.assertRegex(item.version, r"^20[0-9]{2}\.[0-9]{2}\.[0-9]{2}-[0-9]+$")
                self.assertRegex(item.commit, r"^[0-9a-f]{40}$")
                self.assertRegex(item.repository, r"^https://github\.com/[^/]+/[^/]+\.git$")
                self.assertEqual(item.kind, "service")
                self.assertEqual(
                    item.public_body()["verification"]["status"], "agent-pass"
                )
                self.assertNotIn("repository", item.public_body())
        nextjs = next(item for item in TEMPLATES if item.id == "nextjs-docker")
        self.assertEqual(
            nextjs.subdirectory, "lae/e2e/fixtures/nextjs-standalone"
        )
        self.assertEqual(
            nextjs.commit, "a759c8606fdb21f793b0e8071c99491ca7ba52c8"
        )

    async def test_launch_reuses_normal_application_and_analysis_gates(self) -> None:
        applications = _Applications()
        analyses = _Analyses()
        service = TemplateApiService(lambda: applications, lambda: analyses)
        principal = SimpleNamespace(
            tenant_id="ten_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            credential_type="deploy_token",
            credential_id="dt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        first = TEMPLATES[0]
        body, replayed = await service.launch(
            principal=principal,
            template_id=first.id,
            payload=TemplateLaunchRequest(name="My Next App", slug="my-next-app"),
            idempotency_key="template-launch-1",
        )
        self.assertTrue(replayed)
        self.assertEqual(
            body["application"]["id"], "app_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        )
        self.assertEqual(body["analysis"]["id"], "ana_template")
        command = analyses.calls[0]
        self.assertEqual(command.repository, first.repository)
        self.assertEqual(command.ref, first.commit)
        self.assertEqual(
            command.application_id, "app_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        )
        self.assertEqual(command.public_protocols, ("http",))
        self.assertTrue(command.idempotency_key.startswith("template-analysis:"))
        self.assertTrue(applications.calls[0][3].startswith("template-app:"))
        self.assertEqual(
            applications.calls[0][3].removeprefix("template-app:"),
            command.idempotency_key.removeprefix("template-analysis:"),
        )

    async def test_unpublished_template_is_hidden_but_smoke_can_recover_it(self) -> None:
        health = _TemplateHealth()
        first = TEMPLATES[0]
        health.states[first.id] = SimpleNamespace(
            template_id=first.id,
            template_version=first.version,
            published=False,
            consecutive_failures=3,
            last_status="failed",
            last_error_code="LAE_TEMPLATE_ACCEPTANCE_FAILED",
        )
        service = TemplateApiService(
            lambda: _Applications(), lambda: _Analyses(), lambda: health
        )
        public = await service.list()
        self.assertNotIn(first.id, {item["id"] for item in public["templates"]})
        internal = await service.list(include_unpublished=True)
        selected = next(item for item in internal["templates"] if item["id"] == first.id)
        self.assertFalse(selected["publication"]["published"])
        principal = SimpleNamespace(
            tenant_id="ten_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            credential_type="deploy_token",
            credential_id="dt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        with self.assertRaises(ResourceNotFound):
            await service.launch(
                principal=principal,
                template_id=first.id,
                payload=TemplateLaunchRequest(name="Hidden", slug="hidden"),
                idempotency_key="hidden-public",
            )
        body, _ = await service.launch(
            principal=principal,
            template_id=first.id,
            payload=TemplateLaunchRequest(name="Recovery", slug="recovery"),
            idempotency_key="hidden-smoke",
            allow_unpublished=True,
        )
        self.assertEqual(body["template"]["id"], first.id)

    def test_smoke_result_endpoint_is_authenticated_and_fail_closed(self) -> None:
        token = "s" * 48
        health = _TemplateHealth()
        with TestClient(
            create_app(
                template_health=health,
                template_smoke_authenticator=TemplateSmokeAuthenticator(token),
            )
        ) as client:
            payload = {
                "runId": "tsm-route-1",
                "templateId": TEMPLATES[0].id,
                "version": TEMPLATES[0].version,
                "status": "failed",
                "errorCode": "LAE_TEMPLATE_ACCEPTANCE_FAILED",
            }
            rejected = client.post(
                "/internal/v1/template-smoke/results", json=payload
            )
            self.assertEqual(rejected.status_code, 401)
            accepted = client.post(
                "/internal/v1/template-smoke/results",
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertFalse(accepted.json()["published"])


class TemplateRouteWiringTests(unittest.TestCase):
    def test_public_catalog_and_launch_auth_csrf_scopes_and_idempotency(self) -> None:
        deploy_token = "lae_dt_0123456789_" + "T" * 43
        session_token = "lae_ss_v1_" + "S" * 43
        csrf = "lae_cs_" + "C" * 43
        user_id = new_id("usr")
        tenant_id = new_id("ten")

        class Auth:
            async def authenticate(self, value):
                if value != session_token:
                    raise AssertionError("unexpected session")
                return SessionPrincipal(
                    session_id=new_id("ses"),
                    user_id=user_id,
                    email="template@example.test",
                    tenant_id=tenant_id,
                    entitlement_code="lite",
                    key_version=1,
                    csrf_digest=b"c" * 32,
                )

            async def authenticate_deploy_token(self, value, *, request_ip):
                del request_ip
                if value != deploy_token:
                    raise AssertionError("unexpected deploy token")
                return DeployTokenPrincipal(
                    token_id=new_id("dtk"),
                    token_prefix="0123456789",
                    user_id=user_id,
                    email="template@example.test",
                    tenant_id=tenant_id,
                    entitlement_code="lite",
                    member_role="owner",
                    scopes=frozenset({"apps:write", "analyses:write"}),
                )

            def csrf_valid(
                self,
                _principal,
                *,
                csrf_cookie,
                csrf_header,
            ):
                return csrf_cookie == csrf and csrf_header == csrf

        applications = _Applications()
        analyses = _Analyses()
        with TestClient(
            create_app(
                auth_service=Auth(),  # type: ignore[arg-type]
                analysis_requests=analyses,
                applications=SimpleNamespace(
                    create=applications.create,
                ),
            ),
            base_url="https://lae.example.test",
        ) as client:
            catalog = client.get("/v1/templates")
            self.assertEqual(catalog.status_code, 200, catalog.text)
            self.assertGreaterEqual(len(catalog.json()["templates"]), 4)
            self.assertIn("public", catalog.headers["cache-control"])

            path = f"/v1/templates/{TEMPLATES[0].id}/launch"
            payload = {"name": "Template App", "slug": "template-app"}
            missing_idempotency = client.post(
                path,
                headers={"Authorization": "Bearer " + deploy_token},
                json=payload,
            )
            self.assertEqual(missing_idempotency.status_code, 400)

            bearer = client.post(
                path,
                headers={
                    "Authorization": "Bearer " + deploy_token,
                    "Idempotency-Key": "template-route-bearer",
                },
                json=payload,
            )
            self.assertEqual(bearer.status_code, 202, bearer.text)

            internal_region = client.post(
                path,
                headers={
                    "Authorization": "Bearer " + deploy_token,
                    "Idempotency-Key": "template-route-home",
                },
                json={**payload, "region": "home"},
            )
            self.assertEqual(internal_region.status_code, 400)
            self.assertEqual(
                internal_region.json()["error"]["code"],
                "LAE_INVALID_ARGUMENT",
            )

            client.cookies.set(SESSION_COOKIE, session_token, path="/")
            client.cookies.set(CSRF_COOKIE, csrf, path="/")
            no_csrf = client.post(
                path,
                headers={"Idempotency-Key": "template-route-session-no-csrf"},
                json=payload,
            )
            self.assertEqual(no_csrf.status_code, 403)
            session = client.post(
                path,
                headers={
                    "Idempotency-Key": "template-route-session",
                    "X-CSRF-Token": csrf,
                },
                json=payload,
            )
            self.assertEqual(session.status_code, 202, session.text)

        self.assertEqual(len(applications.calls), 2)
        self.assertEqual(len(analyses.calls), 2)


if __name__ == "__main__":
    unittest.main()
