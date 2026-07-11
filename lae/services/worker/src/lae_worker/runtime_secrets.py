from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from lae_luma_adapter import (
    AdapterErrorCode,
    LumaAdapterError,
    RuntimeCallContext,
    RuntimeSecretRef,
    RuntimeServicePrincipal,
)
from lae_store import OperationRecord
from lae_store.environment_crypto import (
    EnvironmentCiphertext,
    EnvironmentCryptoError,
    EnvironmentKeyRing,
)
from lae_store.models import Application, ApplicationEnvironmentVariable

from .deployment import (
    DeploymentContext,
    RuntimeSecretsUnavailable,
)


@dataclass(frozen=True, slots=True)
class RuntimeSecretIssueBinding:
    tenant_ref: str
    application_ref: str
    operation_ref: str
    deployment_ref: str
    revision_ref: str
    service_key: str
    name: str
    environment_version: int


@dataclass(frozen=True, slots=True)
class RuntimeSecretPlaintext:
    """Ephemeral issuer input intentionally omitted from repr."""

    value: str = field(repr=False)


class EphemeralRuntimeSecretIssuer(Protocol):
    """Mint a short-lived, one-deployment Luma secret reference.

    Implementations may transmit ``plaintext`` only to the dedicated scoped
    Luma secret endpoint over authenticated TLS. They must not log, persist,
    retry-cache or include it in a build arg, image layer or exception.
    """

    async def issue(
        self,
        binding: RuntimeSecretIssueBinding,
        plaintext: RuntimeSecretPlaintext,
        *,
        ttl_seconds: int,
    ) -> RuntimeSecretRef: ...


class UnconfiguredEphemeralRuntimeSecretIssuer:
    async def issue(
        self,
        binding: RuntimeSecretIssueBinding,
        plaintext: RuntimeSecretPlaintext,
        *,
        ttl_seconds: int,
    ) -> RuntimeSecretRef:
        del binding, plaintext, ttl_seconds
        raise RuntimeSecretsUnavailable()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class HttpEphemeralRuntimeSecretIssuer:
    """Exchange one plaintext value for a scoped, consume-once Luma ref.

    The request uses the dedicated LAE runtime audience and all five immutable
    context headers. Redirects and ambient proxy configuration are disabled so
    neither the runtime bearer nor plaintext can be forwarded to another
    origin. Response bodies and transport exception text are never surfaced.
    """

    def __init__(
        self,
        endpoint: str,
        principal: RuntimeServicePrincipal,
        *,
        production: bool = True,
        timeout_seconds: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        clock: Any | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        schemes = {"https"} if production else {"http", "https"}
        if (
            parsed.scheme not in schemes
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 120
        ):
            raise ValueError("runtime secret endpoint configuration is invalid")
        self._endpoint = endpoint.rstrip("/")
        self._principal = principal
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if opener is None:
            handlers: list[Any] = [urllib.request.ProxyHandler({}), _NoRedirect()]
            if parsed.scheme == "https":
                handlers.append(
                    urllib.request.HTTPSHandler(
                        context=ssl_context or ssl.create_default_context()
                    )
                )
            opener = urllib.request.build_opener(*handlers)
        self._opener = opener

    async def issue(
        self,
        binding: RuntimeSecretIssueBinding,
        plaintext: RuntimeSecretPlaintext,
        *,
        ttl_seconds: int,
    ) -> RuntimeSecretRef:
        try:
            plaintext_size = len(plaintext.value.encode("utf-8"))
        except (AttributeError, UnicodeError):
            raise RuntimeSecretsUnavailable() from None
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 5 <= ttl_seconds <= 300
            or not isinstance(plaintext.value, str)
            or plaintext_size > 64 * 1024
        ):
            raise RuntimeSecretsUnavailable()
        try:
            context = RuntimeCallContext(
                tenant_ref=binding.tenant_ref,
                application_ref=binding.application_ref,
                operation_ref=binding.operation_ref,
                revision_ref=binding.revision_ref,
                deployment_ref=binding.deployment_ref,
            )
            body = {
                "schemaVersion": "luma.lae-runtime/v1",
                "serviceKey": binding.service_key,
                "name": binding.name,
                "plaintext": plaintext.value,
                "environmentVersion": binding.environment_version,
                "ttlSeconds": ttl_seconds,
            }
            idempotency_key = (
                f"lae:{binding.operation_ref}:runtime-secret:"
                f"{binding.service_key}:{binding.name}:v1"
            )
            value = await asyncio.to_thread(
                self._issue_sync,
                context,
                body,
                idempotency_key,
            )
            return self._parse_response(
                value,
                binding=binding,
                ttl_seconds=ttl_seconds,
            )
        except RuntimeSecretsUnavailable:
            raise
        except (LumaAdapterError, UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeSecretsUnavailable() from exc

    def _issue_sync(
        self,
        context: RuntimeCallContext,
        body: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            encoded = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise RuntimeSecretsUnavailable() from None
        request = urllib.request.Request(
            self._endpoint + "/v1/lae/runtime/secrets:issue",
            method="POST",
            data=encoded,
            headers={
                "Authorization": f"Bearer {self._principal.token}",
                "X-Luma-Principal-Audience": self._principal.audience,
                "Idempotency-Key": idempotency_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                **context.headers(),
            },
        )
        try:
            with self._opener.open(
                request, timeout=self._timeout_seconds
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read(64 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            code = (
                AdapterErrorCode.UNAUTHORIZED
                if exc.code in {401, 403}
                else AdapterErrorCode.INVALID_REQUEST
                if exc.code in {400, 409, 422}
                else AdapterErrorCode.UPSTREAM_UNAVAILABLE
            )
            raise LumaAdapterError(
                code,
                http_status=exc.code,
                retryable=exc.code in {429, 503} or exc.code >= 500,
            ) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            raise LumaAdapterError(
                AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=True
            ) from None
        if status not in {200, 201} or len(raw) > 64 * 1024:
            raise RuntimeSecretsUnavailable()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeSecretsUnavailable() from None
        if not isinstance(value, dict):
            raise RuntimeSecretsUnavailable()
        return value

    def _parse_response(
        self,
        value: dict[str, object],
        *,
        binding: RuntimeSecretIssueBinding,
        ttl_seconds: int,
    ) -> RuntimeSecretRef:
        if set(value) != {"schemaVersion", "replayed", "secret"} or value.get(
            "schemaVersion"
        ) != "luma.lae-runtime/v1" or not isinstance(value.get("replayed"), bool):
            raise RuntimeSecretsUnavailable()
        secret = value.get("secret")
        if not isinstance(secret, dict) or set(secret) != {
            "serviceKey",
            "name",
            "secretRef",
            "environmentVersion",
            "expiresAt",
        }:
            raise RuntimeSecretsUnavailable()
        expires_raw = secret.get("expiresAt")
        if not isinstance(expires_raw, str):
            raise RuntimeSecretsUnavailable()
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeSecretsUnavailable() from None
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= now
            or expires_at > now + timedelta(seconds=ttl_seconds + 5)
            or secret.get("serviceKey") != binding.service_key
            or secret.get("name") != binding.name
            or secret.get("environmentVersion") != binding.environment_version
        ):
            raise RuntimeSecretsUnavailable()
        try:
            return RuntimeSecretRef(
                service_key=binding.service_key,
                name=binding.name,
                secret_ref=secret["secretRef"],
                environment_version=binding.environment_version,
            )
        except (KeyError, TypeError, LumaAdapterError):
            raise RuntimeSecretsUnavailable() from None


class FakeEphemeralRuntimeSecretIssuer:
    """Non-persisting fake; retains only a one-way fingerprint for assertions."""

    def __init__(self) -> None:
        self.fingerprints: list[str] = []

    async def issue(
        self,
        binding: RuntimeSecretIssueBinding,
        plaintext: RuntimeSecretPlaintext,
        *,
        ttl_seconds: int,
    ) -> RuntimeSecretRef:
        if not 5 <= ttl_seconds <= 300:
            raise RuntimeSecretsUnavailable()
        fingerprint = hashlib.sha256(
            (
                binding.operation_ref
                + "\0"
                + binding.service_key
                + "\0"
                + binding.name
                + "\0"
                + plaintext.value
            ).encode()
        ).hexdigest()
        self.fingerprints.append(fingerprint)
        return RuntimeSecretRef(
            service_key=binding.service_key,
            name=binding.name,
            secret_ref="lsec_" + fingerprint[:24],
            environment_version=binding.environment_version,
        )


class PostgresRuntimeSecretProvider:
    """Decrypt environment rows just-in-time and immediately exchange for refs."""

    def __init__(
        self,
        sessions: Any,
        key_ring: EnvironmentKeyRing,
        issuer: EphemeralRuntimeSecretIssuer,
        *,
        ttl_seconds: int = 60,
    ) -> None:
        if not 5 <= ttl_seconds <= 300:
            raise ValueError("runtime secret TTL must be 5-300 seconds")
        self._sessions = sessions
        self._key_ring = key_ring
        self._issuer = issuer
        self._ttl_seconds = ttl_seconds

    async def issue_refs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
    ) -> tuple[RuntimeSecretRef, ...]:
        if not context.environment:
            return ()
        try:
            async with self._sessions() as session:
                application = await session.scalar(
                    select(Application).where(
                        Application.tenant_id == context.tenant_ref,
                        Application.id == context.application_ref,
                        Application.environment_version == context.environment_version,
                        Application.deleted_at.is_(None),
                    )
                )
                if application is None:
                    raise RuntimeSecretsUnavailable()
                rows = list(
                    await session.scalars(
                        select(ApplicationEnvironmentVariable).where(
                            ApplicationEnvironmentVariable.tenant_id
                            == context.tenant_ref,
                            ApplicationEnvironmentVariable.application_id
                            == context.application_ref,
                        )
                    )
                )
        except DBAPIError as exc:
            raise RuntimeSecretsUnavailable() from exc

        by_scope = {(row.service_scope, row.name): row for row in rows}
        refs: list[RuntimeSecretRef] = []
        try:
            for requirement in context.environment:
                row = by_scope.get((requirement.service_key, requirement.name))
                if row is None:
                    row = by_scope.get(("*", requirement.name))
                if row is None:
                    raise RuntimeSecretsUnavailable()
                plaintext_value = self._key_ring.decrypt(
                    EnvironmentCiphertext(
                        envelope=row.value_ciphertext,
                        checksum=row.value_checksum,
                        key_version=row.key_version,
                    ),
                    tenant_id=context.tenant_ref,
                    application_id=context.application_ref,
                    service_scope=row.service_scope,
                    name=row.name,
                )
                try:
                    binding = RuntimeSecretIssueBinding(
                        tenant_ref=context.tenant_ref,
                        application_ref=context.application_ref,
                        operation_ref=operation.id,
                        deployment_ref=context.deployment_ref,
                        revision_ref=context.revision_ref,
                        service_key=requirement.service_key,
                        name=requirement.name,
                        environment_version=context.environment_version,
                    )
                    ref = await self._issuer.issue(
                        binding,
                        RuntimeSecretPlaintext(plaintext_value),
                        ttl_seconds=self._ttl_seconds,
                    )
                finally:
                    # Best-effort lifetime minimization. Python strings cannot
                    # be zeroized, so the plaintext is never copied to any
                    # durable/model/event/log value in the first place.
                    plaintext_value = ""
                if (
                    ref.service_key != requirement.service_key
                    or ref.name != requirement.name
                    or ref.environment_version != context.environment_version
                ):
                    raise RuntimeSecretsUnavailable()
                refs.append(ref)
        except (EnvironmentCryptoError, UnicodeError, ValueError) as exc:
            raise RuntimeSecretsUnavailable() from exc
        return tuple(refs)


__all__ = [
    "EphemeralRuntimeSecretIssuer",
    "FakeEphemeralRuntimeSecretIssuer",
    "HttpEphemeralRuntimeSecretIssuer",
    "PostgresRuntimeSecretProvider",
    "RuntimeSecretIssueBinding",
    "RuntimeSecretPlaintext",
    "UnconfiguredEphemeralRuntimeSecretIssuer",
]
