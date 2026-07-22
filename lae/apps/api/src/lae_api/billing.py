from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Literal, Mapping

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lae_store import IdempotencyKeyReused, Principal, ResourceNotFound, TenantScope
from lae_store.billing import (
    BillingConfigurationError,
    BillingEventConflict,
    BillingOrderRecord,
    BillingPlanRecord,
    BillingSubscriptionRecord,
    BillingUnavailable,
    BillingUsageRecord,
    MockPaymentProvider,
    MockPricingCatalog,
    PaymentEventResult,
    PaymentProviderPort,
    PostgresBillingStore,
    ProviderPaymentEvent,
    decode_billing_key,
)
from lae_store.engine import create_session_factory


@dataclass(frozen=True, slots=True)
class BillingRuntime:
    store: Any
    provider: PaymentProviderPort
    environment: str


class BillingHttpError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutRequest(StrictModel):
    plan: Literal["lite", "pro", "ultra"]
    interval: Literal["monthly", "yearly"]
    provider: Literal["mock", "wechat_pay", "alipay"] | None = None


class MockCompleteRequest(StrictModel):
    eventId: str = Field(min_length=1, max_length=128)
    orderId: str = Field(min_length=1, max_length=64)
    merchantId: str = Field(min_length=1, max_length=128)
    plan: Literal["pro", "ultra"]
    interval: Literal["monthly", "yearly"]
    currency: str = Field(min_length=3, max_length=3)
    amountMinor: int = Field(gt=0, le=10**12)
    outcome: Literal["paid", "failed", "expired", "canceled"]
    occurredAt: datetime

    @field_validator("occurredAt")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurredAt must include a timezone")
        return value

    def provider_payload(self) -> dict[str, Any]:
        return {
            "eventId": self.eventId,
            "orderId": self.orderId,
            "merchantId": self.merchantId,
            "plan": self.plan,
            "interval": self.interval,
            "currency": self.currency,
            "amountMinor": self.amountMinor,
            "outcome": self.outcome,
            "occurredAt": self.occurredAt.astimezone(timezone.utc).isoformat(),
        }


class MockApproveRequest(StrictModel):
    """The browser may approve an order, but it may not report payment facts."""


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _no_store(response: JSONResponse) -> JSONResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _price_body(price: Any | None) -> dict[str, Any] | None:
    if price is None:
        return None
    return {"amountMinor": price.amount_minor, "currency": price.currency}


def _plan_body(plan: BillingPlanRecord) -> dict[str, Any]:
    is_free = plan.code == "lite"
    return {
        "code": plan.code,
        "version": plan.version,
        "limits": dict(plan.limits),
        "features": dict(plan.features),
        "pricing": {
            "mode": "free" if is_free else "mock-development-only",
            "commerciallyApproved": is_free,
            "monthly": _price_body(plan.monthly),
            "yearly": _price_body(plan.yearly),
        },
    }


def _subscription_body(subscription: BillingSubscriptionRecord) -> dict[str, Any]:
    return {
        "id": subscription.id,
        "plan": {
            "code": subscription.plan_code,
            "version": subscription.plan_version,
        },
        "interval": subscription.interval,
        "status": subscription.status,
        "provider": subscription.provider,
        "currentPeriodStart": _timestamp(subscription.current_period_start),
        "currentPeriodEnd": _timestamp(subscription.current_period_end),
        "cancelAtPeriodEnd": subscription.cancel_at_period_end,
        "limits": dict(subscription.limits),
        "features": dict(subscription.features),
    }


def _usage_body(usage: BillingUsageRecord) -> dict[str, Any]:
    return {
        "asOf": _timestamp(usage.as_of),
        "ledger": {
            "connected": True,
            "mode": "live-resource-counters",
            "billingImpact": False,
        },
        "plan": {
            "code": usage.subscription.plan_code,
            "version": usage.subscription.plan_version,
        },
        "counters": usage.counters,
        "notice": (
            "Resource counters are live. Development mock billing does not create "
            "real charges, but plan limits remain enforceable."
        ),
    }


def _order_body(order: BillingOrderRecord) -> dict[str, Any]:
    checkout = None
    if order.checkout_url is not None:
        checkout = {
            "url": order.checkout_url,
            "expiresAt": _timestamp(order.checkout_expires_at),
            "requiresUserAction": True,
        }
    return {
        "order": {
            "id": order.id,
            "status": order.status,
            "provider": order.provider,
            "plan": {"code": order.plan_code, "version": order.plan_version},
            "interval": order.interval,
            "price": {
                "amountMinor": order.amount_minor,
                "currency": order.currency,
                "pricingVersion": order.pricing_version,
                "commerciallyApproved": order.provider in {"wechat_pay", "alipay"},
            },
            "checkout": checkout,
            "paidSubscriptionId": order.paid_subscription_id,
            "statusChangedAt": _timestamp(order.status_changed_at),
            "createdAt": _timestamp(order.created_at),
        }
    }


def _payment_result_body(result: PaymentEventResult) -> dict[str, Any]:
    return {
        "accepted": result.processing_status == "accepted",
        "event": {
            "id": result.event_id,
            "processingStatus": result.processing_status,
            "reason": result.reason_code,
        },
        "order": {"id": result.order_id, "status": result.order_status},
        "subscriptionId": result.subscription_id,
    }


def billing_runtime_from_env(
    engine: Any,
    *,
    environment: str,
    environ: Mapping[str, str] | None = None,
) -> BillingRuntime:
    values = os.environ if environ is None else environ
    normalized_environment = environment.strip().lower()
    driver = values.get("LAE_BILLING_DRIVER", "disabled").strip().lower()
    pricing_raw = values.get("LAE_MOCK_PRICING_JSON", "") or values.get(
        "LAE_BILLING_PRICING_JSON", ""
    )
    if not pricing_raw:
        raise BillingConfigurationError("billing pricing JSON is required")
    pricing = MockPricingCatalog.parse(
        pricing_raw,
        environment=normalized_environment,
        allow_production=driver in {"china", "wechat_pay", "alipay"},
    )
    hash_key = decode_billing_key(
        values.get("LAE_BILLING_HMAC_KEY", ""), label="billing HMAC key"
    )
    if driver in {"mock", "development", "dev", "test"}:
        signing_key = decode_billing_key(
            values.get("LAE_MOCK_PAYMENT_SIGNING_KEY", ""),
            label="mock payment signing key",
        )
        if hash_key == signing_key:
            raise BillingConfigurationError(
                "billing storage and mock callback keys must be separated"
            )
        try:
            checkout_ttl_seconds = int(
                values.get("LAE_MOCK_CHECKOUT_TTL_SECONDS", "600")
            )
        except ValueError as exc:
            raise BillingConfigurationError("mock checkout TTL is invalid") from exc
        try:
            provider: PaymentProviderPort = MockPaymentProvider(
                merchant_id=values.get("LAE_MOCK_PAYMENT_MERCHANT_ID", ""),
                checkout_base_url=values.get("LAE_MOCK_CHECKOUT_BASE_URL", ""),
                signing_key=signing_key,
                checkout_ttl=timedelta(seconds=checkout_ttl_seconds),
            )
        except ValueError as exc:
            raise BillingConfigurationError(
                "mock payment configuration is invalid"
            ) from exc
    elif driver in {"china", "wechat_pay", "alipay"}:
        from lae_store.china_payments import china_payment_hub_from_env

        hub = china_payment_hub_from_env(values)
        if driver == "wechat_pay":
            provider = hub.select("wechat_pay")
        elif driver == "alipay":
            provider = hub.select("alipay")
        else:
            provider = hub
    else:
        raise BillingConfigurationError(f"billing driver {driver!r} is unsupported")
    store = PostgresBillingStore(
        create_session_factory(engine),
        pricing=pricing,
        provider=provider,
        hash_key=hash_key,
    )
    return BillingRuntime(
        store=store,
        provider=provider,
        environment=normalized_environment,
    )


def create_billing_router(
    get_runtime: Callable[[], BillingRuntime],
    *,
    environment: str,
    mock_enabled: bool,
    china_enabled: bool = False,
) -> APIRouter:
    router = APIRouter()

    def runtime() -> BillingRuntime:
        try:
            return get_runtime()
        except BillingHttpError:
            raise
        except Exception as exc:  # runtime details never cross the public boundary
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            ) from exc

    def channel_provider(billing: BillingRuntime, channel: str) -> Any:
        provider = billing.provider
        select = getattr(provider, "select", None)
        if callable(select):
            try:
                return select(channel)
            except ValueError as exc:
                raise BillingHttpError(
                    503,
                    "LAE_BILLING_UNAVAILABLE",
                    "Payment channel is not configured",
                    retryable=False,
                ) from exc
        if getattr(provider, "code", None) != channel:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Payment channel is not configured",
                retryable=False,
            )
        return provider

    async def read_principal(request: Request) -> Any:
        return await request.app.state.require_scoped_principal(
            request,
            "apps:read",
            mutation=False,
        )

    @router.get("/v1/plans")
    async def list_plans() -> JSONResponse:
        try:
            plans = await runtime().store.list_plans()
        except BillingUnavailable as exc:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            ) from exc
        return _no_store(JSONResponse({"plans": [_plan_body(plan) for plan in plans]}))

    @router.get("/v1/usage")
    async def get_usage(request: Request) -> JSONResponse:
        principal = await read_principal(request)
        try:
            usage = await runtime().store.get_usage(TenantScope(principal.tenant_id))
        except BillingUnavailable as exc:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            ) from exc
        return _no_store(JSONResponse(_usage_body(usage)))

    @router.get("/v1/billing/subscription")
    async def get_subscription(request: Request) -> JSONResponse:
        principal = await read_principal(request)
        try:
            subscription = await runtime().store.get_subscription(
                TenantScope(principal.tenant_id)
            )
        except BillingUnavailable as exc:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            ) from exc
        return _no_store(JSONResponse({"subscription": _subscription_body(subscription)}))

    @router.post("/v1/billing/checkout-sessions", status_code=201)
    async def create_checkout_session(
        request: Request,
        payload: CheckoutRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        principal = await request.app.state.require_scoped_principal(
            request,
            "billing:checkout",
            csrf_header=x_csrf_token,
            mutation=True,
        )
        if idempotency_key is None:
            raise BillingHttpError(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        try:
            order = await runtime().store.create_checkout(
                scope=TenantScope(principal.tenant_id),
                principal=Principal(
                    "deploy-token"
                    if principal.credential_type == "deploy_token"
                    else "session",
                    principal.credential_id,
                ),
                plan_code=payload.plan,
                interval=payload.interval,
                idempotency_key=idempotency_key,
                provider_code=payload.provider,
            )
        except IdempotencyKeyReused as exc:
            raise BillingHttpError(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except BillingUnavailable as exc:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise BillingHttpError(
                400,
                "LAE_BILLING_PLAN_INVALID",
                "The selected plan or billing interval is not purchasable",
            ) from exc
        response = JSONResponse(_order_body(order), status_code=201)
        response.headers["Idempotency-Replayed"] = (
            "true" if order.replayed else "false"
        )
        return _no_store(response)

    @router.get("/v1/billing/orders/{order_id}")
    async def get_order(order_id: str, request: Request) -> JSONResponse:
        principal = await read_principal(request)
        try:
            order = await runtime().store.get_order(
                TenantScope(principal.tenant_id), order_id
            )
        except (ResourceNotFound, ValueError) as exc:
            raise BillingHttpError(
                404,
                "LAE_BILLING_ORDER_NOT_FOUND",
                "Billing order not found",
            ) from exc
        return _no_store(JSONResponse(_order_body(order)))

    if mock_enabled and environment.strip().lower() in {
        "dev",
        "development",
        "test",
    }:

        @router.post("/v1/billing/mock/orders/{order_id}/approve")
        async def approve_mock_order(
            order_id: str,
            request: Request,
            _payload: MockApproveRequest,
            idempotency_key: Annotated[
                str | None, Header(alias="Idempotency-Key")
            ] = None,
            x_csrf_token: Annotated[
                str | None, Header(alias="X-CSRF-Token")
            ] = None,
        ) -> JSONResponse:
            principal = await request.app.state.require_scoped_principal(
                request,
                "billing:checkout",
                csrf_header=x_csrf_token,
                mutation=True,
            )
            if principal.credential_type != "session":
                raise BillingHttpError(
                    403,
                    "LAE_MOCK_CHECKOUT_SESSION_REQUIRED",
                    "Mock checkout approval requires an interactive session",
                )
            if idempotency_key is None:
                raise BillingHttpError(
                    400,
                    "LAE_IDEMPOTENCY_REQUIRED",
                    "Idempotency-Key is required",
                )

            billing = runtime()
            try:
                # Resolve through the caller's tenant before constructing any
                # provider event. No client-supplied plan, price or merchant
                # fact is accepted by this endpoint.
                order = await billing.store.get_order(
                    TenantScope(principal.tenant_id), order_id
                )
            except (ResourceNotFound, ValueError) as exc:
                raise BillingHttpError(
                    404,
                    "LAE_BILLING_ORDER_NOT_FOUND",
                    "Billing order not found",
                ) from exc
            except BillingUnavailable as exc:
                raise BillingHttpError(
                    503,
                    "LAE_BILLING_UNAVAILABLE",
                    "Billing service is temporarily unavailable",
                    retryable=True,
                ) from exc

            if order.provider != "mock" or order.provider != billing.provider.code:
                raise BillingHttpError(
                    409,
                    "LAE_MOCK_CHECKOUT_ORDER_INVALID",
                    "The billing order is not a mock checkout",
                )
            # A paid order is admitted only so an identical request can replay
            # the already accepted provider event. A different key is rejected
            # below when the store reports an ignored terminal-order event.
            if order.status not in {"pending", "paid"}:
                raise BillingHttpError(
                    409,
                    "LAE_MOCK_CHECKOUT_ORDER_TERMINAL",
                    "The mock checkout can no longer be approved",
                )

            try:
                result = await billing.store.process_provider_event(
                    ProviderPaymentEvent(
                        idempotency_key=idempotency_key,
                        provider_event_id=idempotency_key,
                        path_order_id=order.id,
                        reported_order_id=order.id,
                        merchant_id=billing.provider.merchant_id,
                        plan_code=order.plan_code,
                        interval=order.interval,
                        currency=order.currency,
                        amount_minor=order.amount_minor,
                        outcome="paid",
                        # A deterministic server-owned timestamp keeps a retry
                        # byte-equivalent after the order changes to paid.
                        occurred_at=order.created_at,
                    )
                )
            except BillingEventConflict as exc:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_CONFLICT",
                    "Payment event conflicts with a previous delivery",
                ) from exc
            except BillingUnavailable as exc:
                raise BillingHttpError(
                    503,
                    "LAE_BILLING_UNAVAILABLE",
                    "Billing service is temporarily unavailable",
                    retryable=True,
                ) from exc
            except ValueError as exc:
                raise BillingHttpError(
                    400,
                    "LAE_IDEMPOTENCY_INVALID",
                    "Idempotency-Key is invalid",
                ) from exc

            if result.processing_status != "accepted":
                raise BillingHttpError(
                    409,
                    "LAE_MOCK_CHECKOUT_ORDER_TERMINAL",
                    "The mock checkout can no longer be approved",
                )
            response = JSONResponse(_payment_result_body(result))
            response.headers["Idempotency-Replayed"] = (
                "true" if result.replayed else "false"
            )
            return _no_store(response)

        @router.post("/v1/billing/mock/orders/{order_id}/complete")
        async def complete_mock_order(
            order_id: str,
            payload: MockCompleteRequest,
            idempotency_key: Annotated[
                str | None, Header(alias="Idempotency-Key")
            ] = None,
            signature: Annotated[
                str | None, Header(alias="X-LAE-Mock-Signature")
            ] = None,
        ) -> JSONResponse:
            if idempotency_key is None:
                raise BillingHttpError(
                    400,
                    "LAE_IDEMPOTENCY_REQUIRED",
                    "Idempotency-Key is required",
                )
            billing = runtime()
            provider_payload = payload.provider_payload()
            if signature is None or not billing.provider.verify_callback(
                provider_payload, signature
            ):
                raise BillingHttpError(
                    401,
                    "LAE_PAYMENT_EVENT_UNAUTHENTICATED",
                    "Payment event authentication failed",
                )
            try:
                result = await billing.store.process_provider_event(
                    ProviderPaymentEvent(
                        idempotency_key=idempotency_key,
                        provider_event_id=payload.eventId,
                        path_order_id=order_id,
                        reported_order_id=payload.orderId,
                        merchant_id=payload.merchantId,
                        plan_code=payload.plan,
                        interval=payload.interval,
                        currency=payload.currency,
                        amount_minor=payload.amountMinor,
                        outcome=payload.outcome,
                        occurred_at=payload.occurredAt,
                    )
                )
            except ResourceNotFound as exc:
                raise BillingHttpError(
                    404,
                    "LAE_BILLING_ORDER_NOT_FOUND",
                    "Billing order not found",
                ) from exc
            except BillingEventConflict as exc:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_CONFLICT",
                    "Payment event conflicts with a previous delivery",
                ) from exc
            except (BillingUnavailable, ValueError) as exc:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event was rejected",
                ) from exc
            response = JSONResponse(_payment_result_body(result))
            response.headers["Idempotency-Replayed"] = (
                "true" if result.replayed else "false"
            )
            return _no_store(response)

    if china_enabled:

        async def _china_webhook(
            *,
            channel: str,
            request: Request,
            signature_header: str,
        ) -> JSONResponse:
            billing = runtime()
            provider = channel_provider(billing, channel)
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise BillingHttpError(
                    400,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event payload is invalid",
                ) from exc
            if not isinstance(payload, dict):
                raise BillingHttpError(
                    400,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event payload is invalid",
                )
            signature = request.headers.get(signature_header) or request.headers.get(
                "X-LAE-China-Signature"
            )
            if signature is None or not provider.verify_callback(payload, signature):
                raise BillingHttpError(
                    401,
                    "LAE_PAYMENT_EVENT_UNAUTHENTICATED",
                    "Payment event authentication failed",
                )
            order_id = str(
                payload.get("out_trade_no")
                or payload.get("orderId")
                or payload.get("out_trade_no".upper())
                or ""
            )
            event_id = str(
                payload.get("transaction_id")
                or payload.get("trade_no")
                or payload.get("eventId")
                or ""
            )
            if not order_id or not event_id:
                raise BillingHttpError(
                    400,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event is missing order or event id",
                )
            try:
                order = await billing.store.get_order_by_id(order_id)
            except (ResourceNotFound, ValueError) as exc:
                raise BillingHttpError(
                    404,
                    "LAE_BILLING_ORDER_NOT_FOUND",
                    "Billing order not found",
                ) from exc
            if order.provider not in {"wechat_pay", "alipay"}:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event was rejected",
                )
            if order.provider != channel:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment channel does not match the order",
                )
            outcome_raw = str(
                payload.get("outcome")
                or payload.get("trade_status")
                or payload.get("trade_state")
                or "paid"
            ).lower()
            if outcome_raw in {"success", "trade_success", "paid", "success_pay"}:
                outcome = "paid"
            elif outcome_raw in {"closed", "canceled", "cancelled", "revoked"}:
                outcome = "canceled"
            elif outcome_raw in {"expired", "timeout"}:
                outcome = "expired"
            else:
                outcome = "failed"
            occurred_raw = payload.get("occurredAt") or payload.get("success_time")
            if isinstance(occurred_raw, str) and occurred_raw:
                try:
                    occurred_at = datetime.fromisoformat(
                        occurred_raw.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise BillingHttpError(
                        400,
                        "LAE_PAYMENT_EVENT_REJECTED",
                        "Payment event timestamp is invalid",
                    ) from exc
            else:
                occurred_at = order.created_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            try:
                result = await billing.store.process_provider_event(
                    ProviderPaymentEvent(
                        idempotency_key=event_id,
                        provider_event_id=event_id,
                        path_order_id=order.id,
                        reported_order_id=order.id,
                        merchant_id=provider.merchant_id,
                        plan_code=order.plan_code,
                        interval=order.interval,  # type: ignore[arg-type]
                        currency=order.currency,
                        amount_minor=order.amount_minor,
                        outcome=outcome,  # type: ignore[arg-type]
                        occurred_at=occurred_at,
                    )
                )
            except ResourceNotFound as exc:
                raise BillingHttpError(
                    404,
                    "LAE_BILLING_ORDER_NOT_FOUND",
                    "Billing order not found",
                ) from exc
            except BillingEventConflict as exc:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_CONFLICT",
                    "Payment event conflicts with a previous delivery",
                ) from exc
            except (BillingUnavailable, ValueError) as exc:
                raise BillingHttpError(
                    409,
                    "LAE_PAYMENT_EVENT_REJECTED",
                    "Payment event was rejected",
                ) from exc
            response = JSONResponse(_payment_result_body(result))
            response.headers["Idempotency-Replayed"] = (
                "true" if result.replayed else "false"
            )
            return _no_store(response)

        @router.post("/v1/billing/webhooks/wechat")
        async def wechat_webhook(request: Request) -> JSONResponse:
            return await _china_webhook(
                channel="wechat_pay",
                request=request,
                signature_header="Wechatpay-Signature",
            )

        @router.post("/v1/billing/webhooks/alipay")
        async def alipay_webhook(request: Request) -> JSONResponse:
            return await _china_webhook(
                channel="alipay",
                request=request,
                signature_header="X-Alipay-Signature",
            )

    return router


__all__ = [
    "BillingHttpError",
    "BillingRuntime",
    "billing_runtime_from_env",
    "create_billing_router",
]
