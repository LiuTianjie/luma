from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store import (  # noqa: E402
    IdempotencyKeyReused,
    Principal,
    ResourceNotFound,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.auth import DEFAULT_DEPLOY_TOKEN_SCOPES  # noqa: E402
from lae_store.billing import (  # noqa: E402
    BillingEventConflict,
    MockPaymentProvider,
    MockPricingCatalog,
    PaymentEventResult,
    PostgresBillingStore,
    Price,
    ProviderPaymentEvent,
)
from lae_store.models import (  # noqa: E402
    BillingOrder,
    BillingPaymentEvent,
    PlanVersion,
    Subscription,
    Tenant,
    TenantMember,
    User,
)

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"


def _alembic_config() -> Config:
    assert Config is not None
    config = Config(str(LAE_ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(LAE_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", DSN)
    return config


def _upgrade() -> None:
    assert command is not None
    command.upgrade(_alembic_config(), "head")


def _downgrade() -> None:
    assert command is not None
    command.downgrade(_alembic_config(), "base")


@unittest.skipUnless(
    DSN and DDL_ALLOWED and command is not None,
    "set LAE_TEST_POSTGRES_DSN and LAE_TEST_POSTGRES_ALLOW_DDL=1 for real PostgreSQL",
)
class BillingPostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.hash_key = b"billing-integration-hash-key-material"
        self.signing_key = b"billing-integration-signing-material"
        self.pricing = MockPricingCatalog(
            version="integration-v1",
            currency="CNY",
            prices={
                ("pro", "monthly"): Price(9900, "CNY"),
                ("pro", "yearly"): Price(99000, "CNY"),
                ("ultra", "monthly"): Price(29900, "CNY"),
                ("ultra", "yearly"): Price(299000, "CNY"),
            },
        )
        self.provider = MockPaymentProvider(
            merchant_id="integration-mock-merchant",
            checkout_base_url="https://checkout.integration.invalid",
            signing_key=self.signing_key,
        )
        self.store = PostgresBillingStore(
            self.sessions,
            pricing=self.pricing,
            provider=self.provider,
            hash_key=self.hash_key,
        )
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.session_id = new_id("ses")
        self.other_session_id = new_id("ses")
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    User(
                        id=self.user_id,
                        email=f"{self.user_id.lower()}@example.test",
                        status="active",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        Tenant(
                            id=self.tenant_id,
                            type="personal",
                            name="Billing Tenant",
                            slug=f"billing-{self.tenant_id.lower()}",
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="organization",
                            name="Other Billing Tenant",
                            slug=f"billing-{self.other_tenant_id.lower()}",
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        TenantMember(
                            tenant_id=self.tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                        TenantMember(
                            tenant_id=self.other_tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                    ]
                )
                lite_plan_id = await session.scalar(
                    select(PlanVersion.id).where(
                        PlanVersion.code == "lite", PlanVersion.version == 1
                    )
                )
                assert lite_plan_id is not None
                session.add_all(
                    [
                        Subscription(
                            id=new_id("sub"),
                            tenant_id=tenant_id,
                            plan_version_id=lite_plan_id,
                            interval="none",
                            status="active",
                            provider="system",
                        )
                        for tenant_id in (self.tenant_id, self.other_tenant_id)
                    ]
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    def event(
        self,
        order,
        *,
        event_id: str,
        outcome: str = "paid",
        amount_minor: int | None = None,
    ) -> ProviderPaymentEvent:
        return ProviderPaymentEvent(
            idempotency_key=event_id,
            provider_event_id=event_id,
            path_order_id=order.id,
            reported_order_id=order.id,
            merchant_id=self.provider.merchant_id,
            plan_code=order.plan_code,
            interval=order.interval,
            currency=order.currency,
            amount_minor=amount_minor or order.amount_minor,
            outcome=outcome,
            occurred_at=datetime.now(timezone.utc),
        )

    async def test_server_pricing_idempotency_tenant_fence_and_zero_usage(self) -> None:
        plans = await self.store.list_plans()
        self.assertEqual([plan.code for plan in plans], ["lite", "pro", "ultra"])
        self.assertEqual(plans[1].monthly.amount_minor, 9900)
        self.assertFalse("billing:checkout" in DEFAULT_DEPLOY_TOKEN_SCOPES)

        scope = TenantScope(self.tenant_id)
        principal = Principal("session", self.session_id)
        created = await self.store.create_checkout(
            scope=scope,
            principal=principal,
            plan_code="pro",
            interval="monthly",
            idempotency_key="integration-checkout-1",
        )
        replayed = await self.store.create_checkout(
            scope=scope,
            principal=principal,
            plan_code="pro",
            interval="monthly",
            idempotency_key="integration-checkout-1",
        )
        self.assertEqual(created.id, replayed.id)
        self.assertTrue(replayed.replayed)
        self.assertEqual(created.amount_minor, 9900)
        self.assertTrue(created.checkout_url.startswith("https://"))
        with self.assertRaises(IdempotencyKeyReused):
            await self.store.create_checkout(
                scope=scope,
                principal=principal,
                plan_code="ultra",
                interval="monthly",
                idempotency_key="integration-checkout-1",
            )

        # Idempotency authority includes tenant and principal; another session
        # may safely use the same opaque key without colliding.
        another = await self.store.create_checkout(
            scope=scope,
            principal=Principal("session", self.other_session_id),
            plan_code="pro",
            interval="monthly",
            idempotency_key="integration-checkout-1",
        )
        self.assertNotEqual(created.id, another.id)

        concurrent = await asyncio.gather(
            *[
                self.store.create_checkout(
                    scope=scope,
                    principal=principal,
                    plan_code="ultra",
                    interval="yearly",
                    idempotency_key="concurrent-checkout",
                )
                for _ in range(2)
            ]
        )
        self.assertEqual(concurrent[0].id, concurrent[1].id)
        self.assertEqual(sum(item.replayed for item in concurrent), 1)
        with self.assertRaises(ResourceNotFound):
            await self.store.get_order(
                TenantScope(self.other_tenant_id), created.id
            )

        usage = await self.store.get_usage(scope)
        self.assertEqual(usage.subscription.plan_code, "lite")
        self.assertTrue(all(value["used"] == 0 for value in usage.counters.values()))
        async with self.sessions() as session:
            persisted = await session.get(BillingOrder, created.id)
        assert persisted is not None
        self.assertEqual(persisted.pricing_snapshot["amountMinor"], 9900)
        self.assertFalse(persisted.pricing_snapshot["commerciallyApproved"])
        self.assertNotEqual(persisted.idempotency_key_hash, b"integration-checkout-1")

    async def test_signed_event_fulfillment_replay_mismatch_and_out_of_order(self) -> None:
        scope = TenantScope(self.tenant_id)
        order = await self.store.create_checkout(
            scope=scope,
            principal=Principal("session", self.session_id),
            plan_code="pro",
            interval="yearly",
            idempotency_key="paid-order",
        )
        paid_command = self.event(order, event_id="provider-paid-1")
        paid = await self.store.process_provider_event(paid_command)
        replayed = await self.store.process_provider_event(paid_command)
        self.assertEqual(paid.processing_status, "accepted")
        self.assertEqual(paid.order_status, "paid")
        self.assertIsNotNone(paid.subscription_id)
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.subscription_id, paid.subscription_id)
        with self.assertRaises(BillingEventConflict):
            await self.store.process_provider_event(
                self.event(
                    order,
                    event_id="provider-paid-1",
                    amount_minor=order.amount_minor + 1,
                )
            )

        # A later contradictory event is durably ignored; it cannot roll back
        # the paid order or create a second active subscription.
        late_failure = await self.store.process_provider_event(
            self.event(order, event_id="provider-failed-late", outcome="failed")
        )
        self.assertEqual(late_failure.processing_status, "ignored")
        self.assertEqual(late_failure.reason_code, "already_paid")
        self.assertEqual(late_failure.order_status, "paid")
        subscription = await self.store.get_subscription(scope)
        self.assertEqual(subscription.plan_code, "pro")
        self.assertEqual(subscription.interval, "yearly")

        # A signed-but-mismatched amount is recorded as rejected and replayed
        # consistently, without changing the original Lite subscription.
        mismatch_order = await self.store.create_checkout(
            scope=TenantScope(self.other_tenant_id),
            principal=Principal("session", self.other_session_id),
            plan_code="ultra",
            interval="monthly",
            idempotency_key="mismatch-order",
        )
        mismatch_command = self.event(
            mismatch_order,
            event_id="provider-mismatch-1",
            amount_minor=mismatch_order.amount_minor + 7,
        )
        rejected = await self.store.process_provider_event(mismatch_command)
        rejected_replay = await self.store.process_provider_event(mismatch_command)
        self.assertEqual(rejected.processing_status, "rejected")
        self.assertEqual(rejected.reason_code, "amount_mismatch")
        self.assertTrue(rejected_replay.replayed)
        other_subscription = await self.store.get_subscription(
            TenantScope(self.other_tenant_id)
        )
        self.assertEqual(other_subscription.plan_code, "lite")

        async with self.sessions() as session:
            active_count = await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.tenant_id == self.tenant_id,
                    Subscription.status.in_(("active", "trialing", "past_due")),
                )
            )
            event_count = await session.scalar(
                select(func.count(BillingPaymentEvent.id))
            )
        self.assertEqual(active_count, 1)
        self.assertEqual(event_count, 3)


if __name__ == "__main__":
    unittest.main()
