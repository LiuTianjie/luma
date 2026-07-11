from __future__ import annotations

import hashlib
import hmac
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from lae_api.billing import BillingRuntime  # noqa: E402
from lae_store import Principal, ResourceNotFound, TenantScope, new_id  # noqa: E402
from lae_store.auth import (  # noqa: E402
    AuthRejected,
    DeployTokenPrincipal,
    SessionPrincipal,
)
from lae_store.billing import (  # noqa: E402
    BillingConfigurationError,
    BillingOrderRecord,
    BillingPlanRecord,
    BillingSubscriptionRecord,
    BillingUsageRecord,
    MockPaymentProvider,
    MockPricingCatalog,
    PaymentEventResult,
    Price,
    ProviderPaymentEvent,
    canonical_provider_payload,
)


class BillingAuthService:
    def __init__(self) -> None:
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.session_id = new_id("ses")
        self.token_id = new_id("dtk")
        self.token_scopes = frozenset({"apps:read"})

    async def authenticate(self, token: str) -> SessionPrincipal:
        if token != self.session_token:
            raise AuthRejected("invalid")
        return SessionPrincipal(
            session_id=self.session_id,
            user_id=self.user_id,
            email="billing@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            key_version=1,
            csrf_digest=b"c" * 32,
        )

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        del request_ip
        if token != self.deploy_token:
            raise AuthRejected("invalid")
        return DeployTokenPrincipal(
            token_id=self.token_id,
            token_prefix="0123456789",
            user_id=self.user_id,
            email="billing@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=self.token_scopes,
        )

    def csrf_valid(
        self,
        _principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        return csrf_cookie == self.csrf_token and csrf_header == self.csrf_token


class RecordingBillingStore:
    def __init__(self, tenant_id: str, provider: MockPaymentProvider) -> None:
        self.tenant_id = tenant_id
        self.provider = provider
        self.order_id = new_id("ord")
        self.subscription_id = new_id("sub")
        self.calls: list[tuple[TenantScope, Principal, str, str, str]] = []
        self.events: list[ProviderPaymentEvent] = []
        self.replayed = False

    async def list_plans(self):
        limits = {"applications": 3, "persistentVolumeBytes": 2_147_483_648}
        features = {"privateGit": True}
        return (
            BillingPlanRecord("lite", 1, limits, features, None, None),
            BillingPlanRecord(
                "pro",
                1,
                limits,
                features,
                Price(9900, "CNY"),
                Price(99000, "CNY"),
            ),
            BillingPlanRecord(
                "ultra",
                1,
                limits,
                features,
                Price(29900, "CNY"),
                Price(299000, "CNY"),
            ),
        )

    def subscription(self, tenant_id: str) -> BillingSubscriptionRecord:
        return BillingSubscriptionRecord(
            id=self.subscription_id,
            tenant_id=tenant_id,
            plan_code="lite",
            plan_version=1,
            interval="none",
            status="active",
            provider="internal",
            current_period_start=None,
            current_period_end=None,
            cancel_at_period_end=False,
            limits={"applications": 3, "persistentVolumeBytes": 2_147_483_648},
            features={"privateGit": True},
        )

    async def get_subscription(self, scope: TenantScope):
        return self.subscription(scope.tenant_id)

    async def get_usage(self, scope: TenantScope):
        return BillingUsageRecord(
            subscription=self.subscription(scope.tenant_id),
            as_of=datetime.now(timezone.utc),
        )

    def order(self, tenant_id: str, *, replayed: bool = False) -> BillingOrderRecord:
        now = datetime.now(timezone.utc)
        return BillingOrderRecord(
            id=self.order_id,
            tenant_id=tenant_id,
            provider="mock",
            plan_code="pro",
            plan_version=1,
            interval="monthly",
            currency="CNY",
            amount_minor=9900,
            pricing_version="test-v1",
            status="pending",
            checkout_url=f"https://checkout.example.test/orders/{self.order_id}",
            checkout_expires_at=now + timedelta(minutes=10),
            status_changed_at=now,
            paid_subscription_id=None,
            created_at=now,
            replayed=replayed,
        )

    async def create_checkout(
        self,
        *,
        scope: TenantScope,
        principal: Principal,
        plan_code: str,
        interval: str,
        idempotency_key: str,
    ):
        if plan_code == "lite":
            raise ValueError("lite")
        self.calls.append((scope, principal, plan_code, interval, idempotency_key))
        replay = self.replayed
        self.replayed = True
        return self.order(scope.tenant_id, replayed=replay)

    async def get_order(self, scope: TenantScope, order_id: str):
        if scope.tenant_id != self.tenant_id or order_id != self.order_id:
            raise ResourceNotFound("missing")
        return self.order(scope.tenant_id)

    async def process_provider_event(self, command: ProviderPaymentEvent):
        self.events.append(command)
        replayed = len(self.events) > 1
        accepted = command.amount_minor == 9900 and command.reported_order_id == self.order_id
        return PaymentEventResult(
            event_id=new_id("pevt"),
            order_id=self.order_id,
            order_status="paid" if accepted else "pending",
            processing_status="accepted" if accepted else "rejected",
            reason_code="accepted" if accepted else "amount_mismatch",
            subscription_id=self.subscription_id if accepted else None,
            replayed=replayed,
        )


class BillingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = BillingAuthService()
        self.signing_key = b"billing-api-test-signing-key-32-bytes"
        self.provider = MockPaymentProvider(
            merchant_id="lae-mock-merchant",
            checkout_base_url="https://checkout.example.test",
            signing_key=self.signing_key,
        )
        self.store = RecordingBillingStore(self.auth.tenant_id, self.provider)
        runtime = BillingRuntime(self.store, self.provider, "test")
        self.context = TestClient(
            create_app(
                self.auth,
                billing_runtime=runtime,
                billing_environment="test",
            ),
            base_url="https://lae.example.test",
        )
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.deploy_token}"}

    def use_session(self) -> None:
        self.client.cookies.set(SESSION_COOKIE, self.auth.session_token)
        self.client.cookies.set(CSRF_COOKIE, self.auth.csrf_token)

    def test_plan_usage_and_subscription_are_explicitly_non_billing_placeholders(self) -> None:
        plans = self.client.get("/v1/plans")
        self.assertEqual(plans.status_code, 200)
        self.assertEqual([p["code"] for p in plans.json()["plans"]], ["lite", "pro", "ultra"])
        self.assertEqual(
            plans.json()["plans"][1]["pricing"]["mode"],
            "mock-development-only",
        )
        self.assertFalse(
            plans.json()["plans"][1]["pricing"]["commerciallyApproved"]
        )

        usage = self.client.get("/v1/usage", headers=self.bearer)
        subscription = self.client.get(
            "/v1/billing/subscription", headers=self.bearer
        )
        self.assertEqual(usage.status_code, 200)
        self.assertFalse(usage.json()["ledger"]["connected"])
        self.assertFalse(usage.json()["ledger"]["billingImpact"])
        self.assertTrue(
            all(item["used"] == 0 for item in usage.json()["counters"].values())
        )
        self.assertEqual(subscription.json()["subscription"]["plan"]["code"], "lite")

    def test_checkout_requires_explicit_scope_or_session_csrf_and_never_accepts_amount(self) -> None:
        headers = {**self.bearer, "Idempotency-Key": "checkout-1"}
        forbidden = self.client.post(
            "/v1/billing/checkout-sessions",
            headers=headers,
            json={"plan": "pro", "interval": "monthly"},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.auth.token_scopes = frozenset({"apps:read", "billing:checkout"})
        created = self.client.post(
            "/v1/billing/checkout-sessions",
            headers=headers,
            json={"plan": "pro", "interval": "monthly"},
        )
        replay = self.client.post(
            "/v1/billing/checkout-sessions",
            headers=headers,
            json={"plan": "pro", "interval": "monthly"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.headers["idempotency-replayed"], "false")
        self.assertEqual(replay.headers["idempotency-replayed"], "true")
        self.assertTrue(created.json()["order"]["checkout"]["requiresUserAction"])
        self.assertEqual(created.json()["order"]["price"]["amountMinor"], 9900)
        self.assertNotIn(self.auth.deploy_token, created.text)

        injected_amount = self.client.post(
            "/v1/billing/checkout-sessions",
            headers={**self.bearer, "Idempotency-Key": "checkout-amount"},
            json={"plan": "pro", "interval": "monthly", "amount": 1},
        )
        self.assertEqual(injected_amount.status_code, 400)

        self.client.cookies.clear()
        self.use_session()
        missing_csrf = self.client.post(
            "/v1/billing/checkout-sessions",
            headers={"Idempotency-Key": "checkout-session"},
            json={"plan": "pro", "interval": "monthly"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        session_created = self.client.post(
            "/v1/billing/checkout-sessions",
            headers={
                "Idempotency-Key": "checkout-session",
                "X-CSRF-Token": self.auth.csrf_token,
            },
            json={"plan": "pro", "interval": "monthly"},
        )
        self.assertEqual(session_created.status_code, 201)
        self.assertEqual(self.store.calls[-1][1].type, "session")

    def test_lite_checkout_order_tenant_fence_and_signed_one_time_mock_completion(self) -> None:
        self.auth.token_scopes = frozenset({"apps:read", "billing:checkout"})
        lite = self.client.post(
            "/v1/billing/checkout-sessions",
            headers={**self.bearer, "Idempotency-Key": "lite-checkout"},
            json={"plan": "lite", "interval": "monthly"},
        )
        self.assertEqual(lite.status_code, 400)

        missing = self.client.get(
            f"/v1/billing/orders/{new_id('ord')}", headers=self.bearer
        )
        self.assertEqual(missing.status_code, 404)

        now = datetime.now(timezone.utc)
        payload = {
            "eventId": "mock-event-1",
            "orderId": self.store.order_id,
            "merchantId": self.provider.merchant_id,
            "plan": "pro",
            "interval": "monthly",
            "currency": "CNY",
            "amountMinor": 9900,
            "outcome": "paid",
            "occurredAt": now.isoformat(),
        }
        url = f"/v1/billing/mock/orders/{self.store.order_id}/complete"
        unauthenticated = self.client.post(
            url,
            headers={"Idempotency-Key": payload["eventId"]},
            json=payload,
        )
        self.assertEqual(unauthenticated.status_code, 401)
        signature_payload = dict(payload)
        signature_payload["occurredAt"] = now.astimezone(timezone.utc).isoformat()
        signature = "v1=" + hmac.new(
            self.signing_key,
            canonical_provider_payload(signature_payload),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Idempotency-Key": payload["eventId"],
            "X-LAE-Mock-Signature": signature,
        }
        completed = self.client.post(url, headers=headers, json=payload)
        replayed = self.client.post(url, headers=headers, json=payload)
        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.json()["accepted"])
        self.assertEqual(replayed.headers["idempotency-replayed"], "true")

    def test_production_does_not_register_mock_completion_route(self) -> None:
        runtime = BillingRuntime(self.store, self.provider, "production")
        with TestClient(
            create_app(
                self.auth,
                billing_runtime=runtime,
                billing_environment="production",
            ),
            base_url="https://lae.example.test",
        ) as production:
            response = production.post(
                f"/v1/billing/mock/orders/{self.store.order_id}/complete",
                json={},
            )
        self.assertEqual(response.status_code, 404)

    def test_billing_readiness_is_capability_scoped_and_driver_is_explicit(self) -> None:
        with TestClient(
            create_app(
                self.auth,
                analysis_requests=object(),
                public_resources=object(),
                applications=object(),
                billing_environment="production",
                billing_driver="disabled",
            ),
            base_url="https://lae.example.test",
        ) as production:
            ready = production.get("/health/ready")
            plans = production.get("/v1/plans")
            mock = production.post(
                f"/v1/billing/mock/orders/{self.store.order_id}/complete",
                json={},
            )
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(plans.status_code, 503)
        self.assertEqual(plans.json()["error"]["code"], "LAE_BILLING_UNAVAILABLE")
        self.assertEqual(mock.status_code, 404)

        runtime = BillingRuntime(self.store, self.provider, "staging")
        staging_app = create_app(
            self.auth,
            analysis_requests=object(),
            public_resources=object(),
            applications=object(),
            billing_runtime=runtime,
            billing_environment="staging",
            billing_driver="mock",
        )
        with TestClient(
            staging_app,
            base_url="https://lae.example.test",
        ) as staging:
            self.assertEqual(staging.get("/health/ready").status_code, 200)
            self.assertEqual(staging.get("/v1/plans").status_code, 200)
            mock_route = staging.post(
                f"/v1/billing/mock/orders/{self.store.order_id}/complete",
                json={},
            )
            self.assertNotEqual(mock_route.status_code, 404)

    def test_mock_pricing_is_strict_and_fails_closed_in_production(self) -> None:
        raw = json.dumps(
            {
                "version": "dev-test-1",
                "currency": "CNY",
                "plans": {
                    "pro": {"monthly": 9900, "yearly": 99000},
                    "ultra": {"monthly": 29900, "yearly": 299000},
                },
            }
        )
        parsed = MockPricingCatalog.parse(raw, environment="staging")
        self.assertEqual(parsed.price("pro", "monthly").amount_minor, 9900)
        with self.assertRaises(BillingConfigurationError):
            MockPricingCatalog.parse(raw, environment="production")
        with self.assertRaises(BillingConfigurationError):
            MockPricingCatalog.parse(
                raw[:-1] + ',"unexpected":true}', environment="development"
            )


if __name__ == "__main__":
    unittest.main()
