from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import IdempotencyKeyReused, ResourceNotFound
from .ids import new_id, require_opaque_id
from .models import (
    Application,
    ApplicationRoute,
    ApplicationService,
    ApplicationVolume,
    BillingOrder,
    BillingPaymentEvent,
    Operation,
    PlanVersion,
    Subscription,
    Upload,
)
from .repositories import IdempotencyInput, Principal, TenantScope
from .tokens import keyed_request_hash, keyed_secret_hash

BillingInterval = Literal["monthly", "yearly"]
BillingOrderStatus = Literal["pending", "paid", "failed", "expired", "canceled"]
PaymentProcessingStatus = Literal["accepted", "rejected", "ignored"]

_CONFIG_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MERCHANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MOCK_ENVIRONMENTS = frozenset({"dev", "development", "test"})
_TERMINAL_ORDER_STATUSES = frozenset(
    {"paid", "failed", "expired", "canceled"}
)


class BillingConfigurationError(RuntimeError):
    """The mock billing boundary is absent or unsafe for this environment."""


class BillingEventConflict(RuntimeError):
    """A provider event identifier was replayed with different facts."""


class BillingUnavailable(RuntimeError):
    """A tenant has no usable subscription or plan version."""


@dataclass(frozen=True, slots=True)
class Price:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not 1 <= self.amount_minor <= 10**12:
            raise ValueError("price amount must be a positive integer in minor units")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("price currency must be a three-letter uppercase code")


@dataclass(frozen=True, slots=True)
class MockPricingCatalog:
    """Strict, explicitly non-commercial development/test pricing input."""

    version: str
    currency: str
    prices: Mapping[tuple[str, BillingInterval], Price] = field(repr=False)

    def __post_init__(self) -> None:
        if not _CONFIG_VERSION.fullmatch(self.version):
            raise ValueError("pricing version is invalid")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("pricing currency is invalid")
        expected = {
            (plan, interval)
            for plan in ("pro", "ultra")
            for interval in ("monthly", "yearly")
        }
        copied = dict(self.prices)
        if set(copied) != expected:
            raise ValueError("mock pricing must define pro/ultra monthly/yearly")
        for (plan, interval), price in copied.items():
            if plan not in {"pro", "ultra"} or interval not in {
                "monthly",
                "yearly",
            }:
                raise ValueError("mock pricing contains an unsupported plan")
            if price.currency != self.currency:
                raise ValueError("all mock prices must use the catalog currency")
        object.__setattr__(self, "prices", MappingProxyType(copied))

    @classmethod
    def parse(cls, raw: str, *, environment: str) -> MockPricingCatalog:
        if environment.strip().lower() not in _MOCK_ENVIRONMENTS:
            raise BillingConfigurationError(
                "mock billing is forbidden outside development/test"
            )
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BillingConfigurationError("mock pricing JSON is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "currency",
            "plans",
        }:
            raise BillingConfigurationError("mock pricing JSON has unknown fields")
        version = payload.get("version")
        currency = payload.get("currency")
        plans = payload.get("plans")
        if (
            not isinstance(version, str)
            or not isinstance(currency, str)
            or not isinstance(plans, dict)
            or set(plans) != {"pro", "ultra"}
        ):
            raise BillingConfigurationError("mock pricing JSON is incomplete")
        prices: dict[tuple[str, BillingInterval], Price] = {}
        try:
            for plan in ("pro", "ultra"):
                plan_prices = plans[plan]
                if not isinstance(plan_prices, dict) or set(plan_prices) != {
                    "monthly",
                    "yearly",
                }:
                    raise ValueError("plan prices are incomplete")
                for interval in ("monthly", "yearly"):
                    amount = plan_prices[interval]
                    prices[(plan, interval)] = Price(
                        amount_minor=amount,
                        currency=currency,
                    )
            return cls(version=version, currency=currency, prices=prices)
        except ValueError as exc:
            raise BillingConfigurationError("mock pricing JSON is unsafe") from exc

    def price(self, plan: str, interval: BillingInterval) -> Price:
        try:
            return self.prices[(plan, interval)]
        except KeyError as exc:
            raise ValueError("plan/interval is not purchasable") from exc


@dataclass(frozen=True, slots=True)
class ProviderCheckout:
    url: str
    expires_at: datetime


class PaymentProviderPort(Protocol):
    """Minimal provider boundary; WeChat/Alipay adapters are not implemented."""

    code: str
    merchant_id: str

    def create_checkout(
        self, *, order_id: str, now: datetime
    ) -> ProviderCheckout: ...

    def verify_callback(self, payload: Mapping[str, Any], signature: str) -> bool: ...


def canonical_provider_payload(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("provider payload is not canonical JSON") from exc


def decode_billing_key(value: str, *, label: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise BillingConfigurationError(f"{label} is not valid base64") from exc
    if len(key) < 32:
        raise BillingConfigurationError(f"{label} must contain at least 256 bits")
    return key


@dataclass(frozen=True, slots=True)
class MockPaymentProvider:
    """Signed development/test adapter; it never talks to a real payment network."""

    merchant_id: str
    checkout_base_url: str
    signing_key: bytes = field(repr=False)
    checkout_ttl: timedelta = timedelta(minutes=10)
    code: str = "mock"

    def __post_init__(self) -> None:
        if not _MERCHANT_ID.fullmatch(self.merchant_id):
            raise ValueError("mock merchant id is invalid")
        if not isinstance(self.signing_key, bytes) or len(self.signing_key) < 32:
            raise ValueError("mock signing key must contain at least 256 bits")
        if not timedelta(minutes=2) <= self.checkout_ttl <= timedelta(hours=1):
            raise ValueError("mock checkout TTL must be between 2 and 60 minutes")
        parsed = urllib.parse.urlsplit(self.checkout_base_url)
        is_local_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if (
            (parsed.scheme != "https" and not is_local_http)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("mock checkout base URL must be safe HTTPS or localhost")
        object.__setattr__(self, "checkout_base_url", self.checkout_base_url.rstrip("/"))

    def create_checkout(self, *, order_id: str, now: datetime) -> ProviderCheckout:
        require_opaque_id(order_id, prefix="ord")
        return ProviderCheckout(
            url=f"{self.checkout_base_url}/orders/{order_id}",
            expires_at=now + self.checkout_ttl,
        )

    def verify_callback(self, payload: Mapping[str, Any], signature: str) -> bool:
        if not isinstance(signature, str) or not signature.startswith("v1="):
            return False
        supplied = signature[3:]
        if not re.fullmatch(r"[0-9a-f]{64}", supplied):
            return False
        expected = hmac.new(
            self.signing_key,
            canonical_provider_payload(payload),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(supplied, expected)


@dataclass(frozen=True, slots=True)
class BillingPlanRecord:
    code: str
    version: int
    limits: Mapping[str, Any]
    features: Mapping[str, Any]
    monthly: Price | None
    yearly: Price | None


@dataclass(frozen=True, slots=True)
class BillingSubscriptionRecord:
    id: str
    tenant_id: str
    plan_code: str
    plan_version: int
    interval: str
    status: str
    provider: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    limits: Mapping[str, Any]
    features: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BillingUsageRecord:
    subscription: BillingSubscriptionRecord
    as_of: datetime
    usage: Mapping[str, int] = field(default_factory=dict)

    @property
    def counters(self) -> dict[str, dict[str, int | None]]:
        limits = self.subscription.limits
        mapping = {
            "applications": "applications",
            "servicesPerApp": "servicesPerApp",
            "publicHttpRoutesPerApp": "publicHttpRoutesPerApp",
            "persistentVolumeBytes": "persistentVolumeBytes",
            "uploadBytes": "uploadBytes",
            "concurrentAnalyses": "concurrentAnalyses",
            "concurrentBuilds": "concurrentBuilds",
            "concurrentDeployments": "concurrentDeployments",
        }
        return {
            public: {
                "used": int(self.usage.get(public, 0)),
                "limit": value if isinstance(value, int) and not isinstance(value, bool) else None,
            }
            for public, key in mapping.items()
            if (value := limits.get(key)) is not None
        }


@dataclass(frozen=True, slots=True)
class BillingOrderRecord:
    id: str
    tenant_id: str
    provider: str
    plan_code: str
    plan_version: int
    interval: str
    currency: str
    amount_minor: int
    pricing_version: str
    status: str
    checkout_url: str | None
    checkout_expires_at: datetime
    status_changed_at: datetime
    paid_subscription_id: str | None
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ProviderPaymentEvent:
    idempotency_key: str = field(repr=False)
    provider_event_id: str
    path_order_id: str
    reported_order_id: str
    merchant_id: str
    plan_code: str
    interval: BillingInterval
    currency: str
    amount_minor: int
    outcome: Literal["paid", "failed", "expired", "canceled"]
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not _PROVIDER_EVENT_ID.fullmatch(self.provider_event_id):
            raise ValueError("provider event id is invalid")
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template="/v1/billing/mock/orders/{order_id}/complete",
            request_hash=b"\0" * 32,
        )
        if self.idempotency_key != self.provider_event_id:
            raise ValueError("mock idempotency key must equal provider event id")
        require_opaque_id(self.path_order_id, prefix="ord")
        require_opaque_id(self.reported_order_id, prefix="ord")
        if not _MERCHANT_ID.fullmatch(self.merchant_id):
            raise ValueError("provider merchant id is invalid")
        if self.plan_code not in {"pro", "ultra"}:
            raise ValueError("provider plan is invalid")
        if self.interval not in {"monthly", "yearly"}:
            raise ValueError("provider interval is invalid")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("provider currency is invalid")
        if isinstance(self.amount_minor, bool) or self.amount_minor <= 0:
            raise ValueError("provider amount is invalid")
        if self.outcome not in {"paid", "failed", "expired", "canceled"}:
            raise ValueError("provider outcome is invalid")
        if self.occurred_at.tzinfo is None:
            raise ValueError("provider timestamp must include a timezone")

    def hash_payload(self) -> dict[str, Any]:
        return {
            "eventId": self.provider_event_id,
            "orderId": self.reported_order_id,
            "merchantId": self.merchant_id,
            "plan": self.plan_code,
            "interval": self.interval,
            "currency": self.currency,
            "amountMinor": self.amount_minor,
            "outcome": self.outcome,
            "occurredAt": self.occurred_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PaymentEventResult:
    event_id: str
    order_id: str
    order_status: str
    processing_status: PaymentProcessingStatus
    reason_code: str
    subscription_id: str | None
    replayed: bool = False


async def _db_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    if value is None:  # pragma: no cover - PostgreSQL always returns a value
        raise RuntimeError("database clock is unavailable")
    return value


class PostgresBillingStore:
    """Tenant-fenced billing read model and signed mock fulfillment state."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        pricing: MockPricingCatalog,
        provider: PaymentProviderPort,
        hash_key: bytes,
    ) -> None:
        if provider.code != "mock":
            raise BillingConfigurationError(
                "only the explicit mock provider is implemented in this slice"
            )
        if not isinstance(hash_key, bytes) or len(hash_key) < 32:
            raise ValueError("billing hash key must contain at least 256 bits")
        self._sessions = sessions
        self.pricing = pricing
        self.provider = provider
        self._hash_key = hash_key

    async def list_plans(self) -> tuple[BillingPlanRecord, ...]:
        async with self._sessions() as session:
            now = await _db_now(session)
            rows = list(
                await session.scalars(
                    select(PlanVersion)
                    .where(PlanVersion.effective_at <= now)
                    .order_by(PlanVersion.code, PlanVersion.version.desc())
                )
            )
        latest: dict[str, PlanVersion] = {}
        for row in rows:
            latest.setdefault(row.code, row)
        records: list[BillingPlanRecord] = []
        for code in ("lite", "pro", "ultra"):
            row = latest.get(code)
            if row is None:
                raise BillingUnavailable("a required plan version is missing")
            records.append(
                BillingPlanRecord(
                    code=code,
                    version=row.version,
                    limits=MappingProxyType(dict(row.limits_json)),
                    features=MappingProxyType(dict(row.features_json)),
                    monthly=(
                        self.pricing.price(code, "monthly") if code != "lite" else None
                    ),
                    yearly=(
                        self.pricing.price(code, "yearly") if code != "lite" else None
                    ),
                )
            )
        return tuple(records)

    async def get_subscription(
        self, scope: TenantScope
    ) -> BillingSubscriptionRecord:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Subscription, PlanVersion)
                    .join(PlanVersion, PlanVersion.id == Subscription.plan_version_id)
                    .where(
                        Subscription.tenant_id == scope.tenant_id,
                        Subscription.status.in_(("active", "trialing", "past_due")),
                    )
                    .order_by(Subscription.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            raise BillingUnavailable("tenant subscription is unavailable")
        subscription, plan = row
        return BillingSubscriptionRecord(
            id=subscription.id,
            tenant_id=subscription.tenant_id,
            plan_code=plan.code,
            plan_version=plan.version,
            interval=subscription.interval,
            status=subscription.status,
            provider=subscription.provider,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            limits=MappingProxyType(dict(plan.limits_json)),
            features=MappingProxyType(dict(plan.features_json)),
        )

    async def get_usage(self, scope: TenantScope) -> BillingUsageRecord:
        subscription = await self.get_subscription(scope)
        async with self._sessions() as session:
            now = await _db_now(session)
            applications = int(
                await session.scalar(
                    select(func.count(Application.id)).where(
                        Application.tenant_id == scope.tenant_id,
                        Application.deleted_at.is_(None),
                    )
                )
                or 0
            )
            service_counts = (
                select(
                    ApplicationService.application_id.label("application_id"),
                    func.count(ApplicationService.id).label("used"),
                )
                .join(
                    Application,
                    (Application.tenant_id == ApplicationService.tenant_id)
                    & (Application.id == ApplicationService.application_id),
                )
                .where(
                    ApplicationService.tenant_id == scope.tenant_id,
                    ApplicationService.desired_state != "deleted",
                    Application.deleted_at.is_(None),
                )
                .group_by(ApplicationService.application_id)
                .subquery()
            )
            services_per_app = int(
                await session.scalar(select(func.max(service_counts.c.used))) or 0
            )
            route_counts = (
                select(
                    ApplicationRoute.application_id.label("application_id"),
                    func.count(ApplicationRoute.id).label("used"),
                )
                .join(
                    Application,
                    (Application.tenant_id == ApplicationRoute.tenant_id)
                    & (Application.id == ApplicationRoute.application_id),
                )
                .where(
                    ApplicationRoute.tenant_id == scope.tenant_id,
                    Application.deleted_at.is_(None),
                )
                .group_by(ApplicationRoute.application_id)
                .subquery()
            )
            routes_per_app = int(
                await session.scalar(select(func.max(route_counts.c.used))) or 0
            )
            persistent_volume_bytes = int(
                await session.scalar(
                    select(func.coalesce(func.sum(ApplicationVolume.requested_bytes), 0))
                    .join(
                        Application,
                        (Application.tenant_id == ApplicationVolume.tenant_id)
                        & (Application.id == ApplicationVolume.application_id),
                    )
                    .where(
                        ApplicationVolume.tenant_id == scope.tenant_id,
                        ApplicationVolume.status != "deleted",
                        Application.deleted_at.is_(None),
                    )
                )
                or 0
            )
            upload_bytes = int(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(Upload.actual_bytes, Upload.expected_bytes)
                            ),
                            0,
                        )
                    ).where(
                        Upload.tenant_id == scope.tenant_id,
                        Upload.deleted_at.is_(None),
                        Upload.status.notin_(("deleted", "expired")),
                    )
                )
                or 0
            )
            active_operations = dict(
                (
                    await session.execute(
                        select(Operation.kind, func.count(Operation.id))
                        .where(
                            Operation.tenant_id == scope.tenant_id,
                            Operation.status.in_(("queued", "running")),
                        )
                        .group_by(Operation.kind)
                    )
                ).all()
            )
        active_analyses = int(active_operations.get("source.analyze", 0))
        active_deployments = int(active_operations.get("deployment.create", 0))
        return BillingUsageRecord(
            subscription=subscription,
            as_of=now,
            usage=MappingProxyType(
                {
                    "applications": applications,
                    "servicesPerApp": services_per_app,
                    "publicHttpRoutesPerApp": routes_per_app,
                    "persistentVolumeBytes": persistent_volume_bytes,
                    "uploadBytes": upload_bytes,
                    "concurrentAnalyses": active_analyses,
                    "concurrentBuilds": active_deployments,
                    "concurrentDeployments": active_deployments,
                }
            ),
        )

    async def create_checkout(
        self,
        *,
        scope: TenantScope,
        principal: Principal,
        plan_code: str,
        interval: BillingInterval,
        idempotency_key: str,
    ) -> BillingOrderRecord:
        if plan_code == "lite":
            raise ValueError("Lite is not a paid checkout target")
        if plan_code not in {"pro", "ultra"} or interval not in {
            "monthly",
            "yearly",
        }:
            raise ValueError("plan/interval is not purchasable")
        IdempotencyInput(
            key=idempotency_key,
            method="POST",
            route_template="/v1/billing/checkout-sessions",
            request_hash=b"\0" * 32,
        )
        idempotency_hash = keyed_secret_hash(
            idempotency_key,
            self._hash_key,
            domain="lae.billing-idempotency-key.v1",
        )
        request_hash = keyed_request_hash(
            {"plan": plan_code, "interval": interval}, self._hash_key
        )
        try:
            return await self._create_checkout_once(
                scope=scope,
                principal=principal,
                plan_code=plan_code,
                interval=interval,
                idempotency_hash=idempotency_hash,
                request_hash=request_hash,
            )
        except IntegrityError as exc:
            # A concurrent request may win the unique idempotency scope.  The
            # winner is replayed only when its keyed request digest matches.
            try:
                return await self._replay_checkout(
                    scope=scope,
                    principal=principal,
                    idempotency_hash=idempotency_hash,
                    request_hash=request_hash,
                )
            except BillingUnavailable:
                raise exc

    async def _create_checkout_once(
        self,
        *,
        scope: TenantScope,
        principal: Principal,
        plan_code: str,
        interval: BillingInterval,
        idempotency_hash: bytes,
        request_hash: bytes,
    ) -> BillingOrderRecord:
        async with self._sessions() as session:
            async with session.begin():
                existing = await self._find_order_by_idempotency(
                    session, scope, principal, idempotency_hash
                )
                if existing is not None:
                    return self._replay_order(existing, request_hash)
                now = await _db_now(session)
                plan = await session.scalar(
                    select(PlanVersion)
                    .where(
                        PlanVersion.code == plan_code,
                        PlanVersion.effective_at <= now,
                    )
                    .order_by(PlanVersion.version.desc())
                    .limit(1)
                    .with_for_update()
                )
                if plan is None:
                    raise BillingUnavailable("purchasable plan version is missing")
                price = self.pricing.price(plan_code, interval)
                order_id = new_id("ord")
                checkout = self.provider.create_checkout(order_id=order_id, now=now)
                snapshot = {
                    "mode": "mock-development-only",
                    "commerciallyApproved": False,
                    "pricingVersion": self.pricing.version,
                    "plan": plan.code,
                    "planVersion": plan.version,
                    "interval": interval,
                    "currency": price.currency,
                    "amountMinor": price.amount_minor,
                }
                order = BillingOrder(
                    id=order_id,
                    tenant_id=scope.tenant_id,
                    principal_type=principal.type,
                    principal_id=principal.id,
                    provider=self.provider.code,
                    provider_merchant_id=self.provider.merchant_id,
                    plan_version_id=plan.id,
                    plan_code=plan.code,
                    interval=interval,
                    currency=price.currency,
                    amount_minor=price.amount_minor,
                    pricing_version=self.pricing.version,
                    pricing_snapshot=snapshot,
                    status="pending",
                    idempotency_key_hash=idempotency_hash,
                    request_hash=request_hash,
                    checkout_expires_at=checkout.expires_at,
                    status_changed_at=now,
                )
                session.add(order)
                await session.flush()
                return self._order_record(order, plan.version)

    async def _replay_checkout(
        self,
        *,
        scope: TenantScope,
        principal: Principal,
        idempotency_hash: bytes,
        request_hash: bytes,
    ) -> BillingOrderRecord:
        async with self._sessions() as session:
            order = await self._find_order_by_idempotency(
                session, scope, principal, idempotency_hash
            )
            if order is None:
                raise BillingUnavailable("idempotency winner is unavailable")
            return self._replay_order(order, request_hash)

    async def _find_order_by_idempotency(
        self,
        session: AsyncSession,
        scope: TenantScope,
        principal: Principal,
        idempotency_hash: bytes,
    ) -> BillingOrder | None:
        return await session.scalar(
            select(BillingOrder).where(
                BillingOrder.tenant_id == scope.tenant_id,
                BillingOrder.principal_type == principal.type,
                BillingOrder.principal_id == principal.id,
                BillingOrder.idempotency_key_hash == idempotency_hash,
            )
        )

    def _replay_order(
        self, order: BillingOrder, request_hash: bytes
    ) -> BillingOrderRecord:
        if not hmac.compare_digest(order.request_hash, request_hash):
            raise IdempotencyKeyReused("checkout idempotency key was reused")
        return replace(self._order_record(order), replayed=True)

    async def get_order(
        self, scope: TenantScope, order_id: str
    ) -> BillingOrderRecord:
        require_opaque_id(order_id, prefix="ord")
        async with self._sessions() as session:
            async with session.begin():
                order = await session.scalar(
                    select(BillingOrder)
                    .where(
                        BillingOrder.tenant_id == scope.tenant_id,
                        BillingOrder.id == order_id,
                    )
                    .with_for_update()
                )
                if order is None:
                    raise ResourceNotFound("billing order not found")
                now = await _db_now(session)
                if order.status == "pending" and order.checkout_expires_at <= now:
                    order.status = "expired"
                    order.status_changed_at = now
                    order.updated_at = now
                    await session.flush()
                return self._order_record(order)

    async def process_provider_event(
        self, command: ProviderPaymentEvent
    ) -> PaymentEventResult:
        payload_hash = keyed_request_hash(command.hash_payload(), self._hash_key)
        idempotency_hash = keyed_secret_hash(
            command.idempotency_key,
            self._hash_key,
            domain="lae.billing-provider-idempotency.v1",
        )
        try:
            return await self._process_provider_event_once(
                command, idempotency_hash, payload_hash
            )
        except IntegrityError as exc:
            try:
                return await self._replay_provider_event(
                    command, idempotency_hash, payload_hash
                )
            except BillingUnavailable:
                raise exc

    async def _process_provider_event_once(
        self,
        command: ProviderPaymentEvent,
        idempotency_hash: bytes,
        payload_hash: bytes,
    ) -> PaymentEventResult:
        async with self._sessions() as session:
            async with session.begin():
                existing = await self._find_payment_event(
                    session, command.provider_event_id, idempotency_hash
                )
                if existing is not None:
                    return self._replay_event(
                        existing, command, idempotency_hash, payload_hash
                    )
                order = await session.scalar(
                    select(BillingOrder)
                    .where(BillingOrder.id == command.path_order_id)
                    .with_for_update()
                )
                if order is None:
                    raise ResourceNotFound("billing order not found")
                # Recheck after the order lock so concurrent deliveries for the
                # same order cannot both pass the initial absence check.
                existing = await self._find_payment_event(
                    session, command.provider_event_id, idempotency_hash
                )
                if existing is not None:
                    return self._replay_event(
                        existing, command, idempotency_hash, payload_hash
                    )
                now = await _db_now(session)
                if order.status == "pending" and order.checkout_expires_at <= now:
                    order.status = "expired"
                    order.status_changed_at = now
                    order.updated_at = now

                mismatch = self._event_mismatch(order, command, now)
                if mismatch is not None:
                    processing: PaymentProcessingStatus = "rejected"
                    reason = mismatch
                elif order.status != "pending":
                    processing = "ignored"
                    reason = (
                        "already_paid" if order.status == "paid" else "order_terminal"
                    )
                else:
                    processing = "accepted"
                    reason = "accepted"
                    order.status_changed_at = now
                    order.updated_at = now
                    if command.outcome == "paid":
                        subscription_id = await self._activate_subscription(
                            session, order, now
                        )
                        order.paid_subscription_id = subscription_id
                    order.status = command.outcome

                event = BillingPaymentEvent(
                    id=new_id("pevt"),
                    tenant_id=order.tenant_id,
                    order_id=order.id,
                    provider=self.provider.code,
                    provider_principal_id=self.provider.merchant_id,
                    provider_event_id=command.provider_event_id,
                    idempotency_key_hash=idempotency_hash,
                    payload_hash=payload_hash,
                    reported_order_id=command.reported_order_id,
                    reported_merchant_id=command.merchant_id,
                    reported_plan_code=command.plan_code,
                    reported_interval=command.interval,
                    reported_currency=command.currency,
                    reported_amount_minor=command.amount_minor,
                    reported_outcome=command.outcome,
                    provider_occurred_at=command.occurred_at,
                    processing_status=processing,
                    reason_code=reason,
                    order_status_after=order.status,
                    subscription_id=order.paid_subscription_id,
                    processed_at=now,
                )
                session.add(event)
                await session.flush()
                return PaymentEventResult(
                    event_id=event.id,
                    order_id=order.id,
                    order_status=order.status,
                    processing_status=processing,
                    reason_code=reason,
                    subscription_id=order.paid_subscription_id,
                )

    def _event_mismatch(
        self,
        order: BillingOrder,
        command: ProviderPaymentEvent,
        now: datetime,
    ) -> str | None:
        comparisons = (
            (command.reported_order_id == order.id, "order_mismatch"),
            (command.merchant_id == order.provider_merchant_id, "merchant_mismatch"),
            (command.plan_code == order.plan_code, "plan_mismatch"),
            (command.interval == order.interval, "interval_mismatch"),
            (command.currency == order.currency, "currency_mismatch"),
            (command.amount_minor == order.amount_minor, "amount_mismatch"),
            (command.occurred_at <= now + timedelta(minutes=5), "future_event"),
            (
                command.occurred_at >= order.created_at - timedelta(minutes=1),
                "event_predates_order",
            ),
        )
        for matches, reason in comparisons:
            if not matches:
                return reason
        return None

    async def _activate_subscription(
        self, session: AsyncSession, order: BillingOrder, now: datetime
    ) -> str:
        active = list(
            await session.scalars(
                select(Subscription)
                .where(
                    Subscription.tenant_id == order.tenant_id,
                    Subscription.status.in_(("active", "trialing", "past_due")),
                )
                .with_for_update()
            )
        )
        for subscription in active:
            subscription.status = "canceled"
            subscription.cancel_at_period_end = False
            subscription.current_period_end = now
            subscription.updated_at = now
        # The partial unique index sees old active rows until they are flushed.
        await session.flush()
        subscription_id = new_id("sub")
        duration = timedelta(days=30 if order.interval == "monthly" else 365)
        session.add(
            Subscription(
                id=subscription_id,
                tenant_id=order.tenant_id,
                plan_version_id=order.plan_version_id,
                interval=order.interval,
                status="active",
                provider=self.provider.code,
                current_period_start=now,
                current_period_end=now + duration,
                cancel_at_period_end=False,
            )
        )
        await session.flush()
        return subscription_id

    async def _find_payment_event(
        self,
        session: AsyncSession,
        provider_event_id: str,
        idempotency_hash: bytes,
    ) -> BillingPaymentEvent | None:
        return await session.scalar(
            select(BillingPaymentEvent).where(
                BillingPaymentEvent.provider == self.provider.code,
                (
                    BillingPaymentEvent.provider_event_id == provider_event_id
                )
                | (
                    (
                        BillingPaymentEvent.provider_principal_id
                        == self.provider.merchant_id
                    )
                    & (
                        BillingPaymentEvent.idempotency_key_hash
                        == idempotency_hash
                    )
                ),
            )
        )

    async def _replay_provider_event(
        self,
        command: ProviderPaymentEvent,
        idempotency_hash: bytes,
        payload_hash: bytes,
    ) -> PaymentEventResult:
        async with self._sessions() as session:
            event = await self._find_payment_event(
                session, command.provider_event_id, idempotency_hash
            )
            if event is None:
                raise BillingUnavailable("provider event winner is unavailable")
            return self._replay_event(
                event, command, idempotency_hash, payload_hash
            )

    def _replay_event(
        self,
        event: BillingPaymentEvent,
        command: ProviderPaymentEvent,
        idempotency_hash: bytes,
        payload_hash: bytes,
    ) -> PaymentEventResult:
        if (
            event.order_id != command.path_order_id
            or event.provider_principal_id != self.provider.merchant_id
            or not hmac.compare_digest(
                event.idempotency_key_hash, idempotency_hash
            )
            or not hmac.compare_digest(event.payload_hash, payload_hash)
        ):
            raise BillingEventConflict(
                "provider event id was reused for different facts"
            )
        return PaymentEventResult(
            event_id=event.id,
            order_id=event.order_id,
            order_status=event.order_status_after,
            processing_status=event.processing_status,
            reason_code=event.reason_code,
            subscription_id=event.subscription_id,
            replayed=True,
        )

    def _order_record(
        self, order: BillingOrder, plan_version: int | None = None
    ) -> BillingOrderRecord:
        checkout_url = None
        if order.status == "pending":
            checkout_url = self.provider.create_checkout(
                order_id=order.id,
                now=order.checkout_expires_at - getattr(
                    self.provider, "checkout_ttl", timedelta(minutes=10)
                ),
            ).url
        if plan_version is None:
            snapshot_version = order.pricing_snapshot.get("planVersion")
            if isinstance(snapshot_version, int) and not isinstance(
                snapshot_version, bool
            ):
                plan_version = snapshot_version
            else:
                raise BillingUnavailable("order plan snapshot is invalid")
        return BillingOrderRecord(
            id=order.id,
            tenant_id=order.tenant_id,
            provider=order.provider,
            plan_code=order.plan_code,
            plan_version=plan_version,
            interval=order.interval,
            currency=order.currency,
            amount_minor=order.amount_minor,
            pricing_version=order.pricing_version,
            status=order.status,
            checkout_url=checkout_url,
            checkout_expires_at=order.checkout_expires_at,
            status_changed_at=order.status_changed_at,
            paid_subscription_id=order.paid_subscription_id,
            created_at=order.created_at,
        )


__all__ = [
    "BillingConfigurationError",
    "BillingEventConflict",
    "BillingOrderRecord",
    "BillingPlanRecord",
    "BillingSubscriptionRecord",
    "BillingUnavailable",
    "BillingUsageRecord",
    "MockPaymentProvider",
    "MockPricingCatalog",
    "PaymentEventResult",
    "PaymentProviderPort",
    "PostgresBillingStore",
    "Price",
    "ProviderPaymentEvent",
    "canonical_provider_payload",
    "decode_billing_key",
]
