from __future__ import annotations

import json
import os
import re
import stat
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import LumaError


BROKER_REQUEST_SCHEMA_VERSION = "luma.credential-redemption/v1"
BROKER_RESPONSE_SCHEMA_VERSION = "luma.credential-redemption-result/v1"
BROKER_MAX_TTL_SECONDS = 300
BROKER_MAX_RESPONSE_BYTES = 64 * 1024

_BINDING_FIELDS = frozenset(
    {
        "leaseId",
        "builderTaskId",
        "externalOperationId",
        "principalRef",
        "tenantRef",
        "applicationRef",
        "repository",
    }
)
_REQUEST_FIELDS = frozenset({"schemaVersion", *_BINDING_FIELDS})
_RESPONSE_FIELDS = frozenset({"schemaVersion", *_BINDING_FIELDS, "kind", "expiresAt", "credential"})
_NONE_RESPONSE_FIELDS = _RESPONSE_FIELDS - {"credential"}
_GIT_HTTPS_CREDENTIAL_FIELDS = frozenset({"username", "password"})

OBJECT_SOURCE_REQUEST_SCHEMA_VERSION = "luma.object-source-redemption/v1"
OBJECT_SOURCE_RESPONSE_SCHEMA_VERSION = "luma.object-source-redemption-result/v1"
OBJECT_SOURCE_MAX_TTL_SECONDS = 300
OBJECT_SOURCE_MAX_RESPONSE_BYTES = 64 * 1024
_OBJECT_DESCRIPTOR_FIELDS = frozenset({"kind", "digest", "mediaType", "sizeBytes"})
_OBJECT_BINDING_FIELDS = frozenset(
    {
        "leaseId",
        "builderTaskId",
        "externalOperationId",
        "principalRef",
        "tenantRef",
        "applicationRef",
        "object",
    }
)
_OBJECT_REQUEST_FIELDS = frozenset({"schemaVersion", *_OBJECT_BINDING_FIELDS})
_OBJECT_RESPONSE_FIELDS = frozenset(
    {
        "schemaVersion",
        *_OBJECT_BINDING_FIELDS,
        "method",
        "expiresAt",
        "objectUrl",
        "allowedHost",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OBJECT_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class CredentialBrokerError(LumaError):
    """A deliberately generic broker failure safe to handle without secrets."""


class ObjectSourceBrokerError(LumaError):
    """A generic object-source lease failure that never includes signed URLs."""


@dataclass(frozen=True)
class CredentialLeaseBinding:
    lease_id: str
    builder_task_id: str
    external_operation_id: str
    principal_ref: str
    tenant_ref: str
    application_ref: str
    repository: str

    def request_body(self) -> Dict[str, str]:
        body = {
            "schemaVersion": BROKER_REQUEST_SCHEMA_VERSION,
            "leaseId": self.lease_id,
            "builderTaskId": self.builder_task_id,
            "externalOperationId": self.external_operation_id,
            "principalRef": self.principal_ref,
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "repository": self.repository,
        }
        _validate_request_body(body)
        return body


@dataclass(frozen=True)
class RedeemedCredential:
    kind: str
    expires_at: int
    username: str = ""
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class ObjectSourceLeaseBinding:
    lease_id: str
    builder_task_id: str
    external_operation_id: str
    principal_ref: str
    tenant_ref: str
    application_ref: str
    object_descriptor: Mapping[str, Any]

    def request_body(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schemaVersion": OBJECT_SOURCE_REQUEST_SCHEMA_VERSION,
            "leaseId": self.lease_id,
            "builderTaskId": self.builder_task_id,
            "externalOperationId": self.external_operation_id,
            "principalRef": self.principal_ref,
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "object": dict(self.object_descriptor),
        }
        _validate_object_source_request(body)
        return body


@dataclass(frozen=True)
class RedeemedObjectSource:
    expires_at: int
    allowed_host: str
    object_url: str = field(repr=False)


@dataclass(frozen=True)
class _BrokerConfig:
    url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class _ObjectSourceBrokerConfig:
    url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float = 5.0


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def redeem_builder_credential(
    binding: CredentialLeaseBinding,
    *,
    now: int | None = None,
) -> RedeemedCredential:
    """Redeem an LAE credential lease without exposing the broker token.

    When no broker is configured, an anonymous lease is allowed only for a
    closed, credential-free HTTPS repository URL.  A configured broker is
    fail-closed: transport, schema, TTL, and binding errors all become the same
    generic exception so neither its response nor a credential reaches logs or
    durable Control state.
    """

    body = binding.request_body()
    current_time = int(now) if now is not None else _unix_time()
    config = _load_broker_config()
    if config is None:
        _validate_repository_url(binding.repository)
        return RedeemedCredential(kind="none", expires_at=current_time + BROKER_MAX_TTL_SECONDS)

    response = _post_redemption(config, body)
    try:
        # The broker computes its expiry after receiving the request. Validate
        # against the response time, not the pre-network timestamp, otherwise
        # crossing a one-second boundary can reject an exact max-TTL lease as
        # if it were 301 seconds long.
        validation_time = int(now) if now is not None else _unix_time()
        return _validate_response(response, expected=body, now=validation_time)
    except CredentialBrokerError:
        raise
    except Exception:
        raise CredentialBrokerError("credential broker redemption failed") from None


def redeem_builder_object_source(
    binding: ObjectSourceLeaseBinding,
    *,
    now: int | None = None,
) -> RedeemedObjectSource:
    """Redeem a descriptor-bound, short-lived HTTPS GET URL.

    Unlike anonymous public Git, object sources never have a no-broker
    fallback. The signed URL exists only in the returned in-memory value and
    every transport/schema/binding failure is collapsed to a fixed message.
    """

    body = binding.request_body()
    current_time = int(now) if now is not None else _unix_time()
    config = _load_object_source_broker_config()
    if config is None:
        raise ObjectSourceBrokerError("object source broker redemption failed")
    response = _post_object_source_redemption(config, body)
    try:
        validation_time = int(now) if now is not None else _unix_time()
        return _validate_object_source_response(
            response, expected=body, now=validation_time
        )
    except ObjectSourceBrokerError:
        raise
    except Exception:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None


def _load_object_source_broker_config(
    environ: Mapping[str, str] | None = None,
) -> _ObjectSourceBrokerConfig | None:
    env = os.environ if environ is None else environ
    raw_url = str(env.get("LUMA_OBJECT_SOURCE_BROKER_URL") or "").strip()
    object_token_file = str(
        env.get("LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE") or ""
    ).strip()
    shared_token_file = str(
        env.get("LUMA_CREDENTIAL_BROKER_TOKEN_FILE") or ""
    ).strip()
    token_file = object_token_file or shared_token_file
    test_mode = (
        str(env.get("LUMA_OBJECT_SOURCE_BROKER_TEST_MODE") or "").strip()
        == "1"
    )
    test_token = (
        str(env.get("LUMA_OBJECT_SOURCE_BROKER_TOKEN") or "")
        if test_mode
        else ""
    )
    if not raw_url:
        if object_token_file or test_token:
            raise ObjectSourceBrokerError(
                "object source broker redemption failed"
            )
        return None
    try:
        _validate_broker_url(raw_url)
        token = _read_broker_token(Path(token_file)) if token_file else test_token
    except Exception:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None
    if (
        not token
        or len(token) > 8192
        or any(character in token for character in ("\0", "\n", "\r"))
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    raw_timeout = str(
        env.get("LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS") or "5"
    ).strip()
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None
    if not 0.1 <= timeout_seconds <= 30.0:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    return _ObjectSourceBrokerConfig(
        url=raw_url,
        bearer_token=token,
        timeout_seconds=timeout_seconds,
    )


def _load_broker_config(environ: Mapping[str, str] | None = None) -> _BrokerConfig | None:
    env = os.environ if environ is None else environ
    raw_url = str(env.get("LUMA_CREDENTIAL_BROKER_URL") or "").strip()
    token_file = str(env.get("LUMA_CREDENTIAL_BROKER_TOKEN_FILE") or "").strip()
    test_mode = str(env.get("LUMA_CREDENTIAL_BROKER_TEST_MODE") or "").strip() == "1"
    test_token = str(env.get("LUMA_CREDENTIAL_BROKER_TOKEN") or "") if test_mode else ""

    if not raw_url:
        if token_file or test_token:
            raise CredentialBrokerError("credential broker redemption failed")
        return None
    _validate_broker_url(raw_url)

    try:
        token = _read_broker_token(Path(token_file)) if token_file else test_token
    except Exception:
        raise CredentialBrokerError("credential broker redemption failed") from None
    if not token or len(token) > 8192 or any(character in token for character in ("\0", "\n", "\r")):
        raise CredentialBrokerError("credential broker redemption failed")

    raw_timeout = str(env.get("LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS") or "5").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        raise CredentialBrokerError("credential broker redemption failed") from None
    if not 0.1 <= timeout_seconds <= 30.0:
        raise CredentialBrokerError("credential broker redemption failed")
    return _BrokerConfig(url=raw_url, bearer_token=token, timeout_seconds=timeout_seconds)


def _read_broker_token(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        raise CredentialBrokerError("credential broker redemption failed") from None
    if (
        not str(path)
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o007
    ):
        raise CredentialBrokerError("credential broker redemption failed")
    if metadata.st_size > 16 * 1024:
        raise CredentialBrokerError("credential broker redemption failed")
    return path.read_text(encoding="utf-8").strip()


def _post_redemption(config: _BrokerConfig, body: Dict[str, str]) -> Dict[str, Any]:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": "luma-control-credential-broker/1",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=config.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            raw = response.read(BROKER_MAX_RESPONSE_BYTES + 1)
    except Exception:
        # Never include HTTP response bodies, URLs, or transport exception text:
        # any of them may contain broker-controlled credential material.
        raise CredentialBrokerError("credential broker redemption failed") from None
    if status != 200 or content_type != "application/json" or len(raw) > BROKER_MAX_RESPONSE_BYTES:
        raise CredentialBrokerError("credential broker redemption failed")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        raise CredentialBrokerError("credential broker redemption failed") from None
    if not isinstance(decoded, dict):
        raise CredentialBrokerError("credential broker redemption failed")
    return decoded


def _post_object_source_redemption(
    config: _ObjectSourceBrokerConfig,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": "luma-control-object-source-broker/1",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=config.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            content_type = (
                str(response.headers.get("Content-Type") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            raw = response.read(OBJECT_SOURCE_MAX_RESPONSE_BYTES + 1)
    except Exception:
        # Broker errors, Location headers, and bodies can all contain a signed
        # URL. Never attach the transport exception as a cause.
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None
    if (
        status != 200
        or content_type != "application/json"
        or len(raw) > OBJECT_SOURCE_MAX_RESPONSE_BYTES
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None
    if not isinstance(decoded, dict):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    return decoded


def _validate_object_source_response(
    response: Dict[str, Any],
    *,
    expected: Dict[str, Any],
    now: int,
) -> RedeemedObjectSource:
    if (
        set(response) != _OBJECT_RESPONSE_FIELDS
        or response.get("schemaVersion")
        != OBJECT_SOURCE_RESPONSE_SCHEMA_VERSION
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    for field_name in _OBJECT_BINDING_FIELDS - {"object"}:
        if (
            not isinstance(response.get(field_name), str)
            or response[field_name] != expected[field_name]
        ):
            raise ObjectSourceBrokerError(
                "object source broker redemption failed"
            )
    if response.get("object") != expected.get("object"):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    if response.get("method") != "GET":
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    expires_at = response.get("expiresAt")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= now
        or expires_at > now + OBJECT_SOURCE_MAX_TTL_SECONDS
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    allowed_host = response.get("allowedHost")
    object_url = response.get("objectUrl")
    if (
        not isinstance(allowed_host, str)
        or allowed_host != allowed_host.lower()
        or _OBJECT_HOST.fullmatch(allowed_host) is None
        or not isinstance(object_url, str)
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    parsed = _parse_object_source_url(object_url)
    if parsed.hostname != allowed_host:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    return RedeemedObjectSource(
        expires_at=expires_at,
        allowed_host=allowed_host,
        object_url=object_url,
    )


def _validate_object_source_request(body: Dict[str, Any]) -> None:
    if (
        set(body) != _OBJECT_REQUEST_FIELDS
        or body.get("schemaVersion") != OBJECT_SOURCE_REQUEST_SCHEMA_VERSION
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    for field_name in _OBJECT_BINDING_FIELDS - {"object"}:
        value = body.get(field_name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(character in value for character in ("\0", "\n", "\r"))
        ):
            raise ObjectSourceBrokerError(
                "object source broker redemption failed"
            )
    descriptor = body.get("object")
    if not isinstance(descriptor, dict) or set(descriptor) != _OBJECT_DESCRIPTOR_FIELDS:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    if (
        descriptor.get("kind") != "object"
        or not isinstance(descriptor.get("digest"), str)
        or _SHA256.fullmatch(descriptor["digest"]) is None
        or descriptor.get("mediaType") not in {"text/html", "application/zip"}
        or isinstance(descriptor.get("sizeBytes"), bool)
        or not isinstance(descriptor.get("sizeBytes"), int)
        or not 1 <= descriptor["sizeBytes"] <= 536_870_912
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )


def _parse_object_source_url(value: str) -> urllib.parse.SplitResult:
    if (
        not value
        or len(value) > 8192
        or any(character in value for character in ("\0", "\n", "\r", " ", "\t"))
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ObjectSourceBrokerError(
            "object source broker redemption failed"
        )
    return parsed


def _validate_response(
    response: Dict[str, Any],
    *,
    expected: Dict[str, str],
    now: int,
) -> RedeemedCredential:
    kind = response.get("kind")
    expected_fields = _NONE_RESPONSE_FIELDS if kind == "none" else _RESPONSE_FIELDS
    if set(response) != expected_fields:
        raise CredentialBrokerError("credential broker redemption failed")
    if response.get("schemaVersion") != BROKER_RESPONSE_SCHEMA_VERSION:
        raise CredentialBrokerError("credential broker redemption failed")
    for field_name in _BINDING_FIELDS:
        if not isinstance(response.get(field_name), str) or response[field_name] != expected[field_name]:
            raise CredentialBrokerError("credential broker redemption failed")
    if kind not in {"none", "git-https"}:
        raise CredentialBrokerError("credential broker redemption failed")
    expires_at = response.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise CredentialBrokerError("credential broker redemption failed")
    if expires_at <= now or expires_at > now + BROKER_MAX_TTL_SECONDS:
        raise CredentialBrokerError("credential broker redemption failed")
    if kind == "none":
        return RedeemedCredential(kind="none", expires_at=expires_at)

    credential = response.get("credential")
    if not isinstance(credential, dict) or set(credential) != _GIT_HTTPS_CREDENTIAL_FIELDS:
        raise CredentialBrokerError("credential broker redemption failed")
    username = credential.get("username")
    password = credential.get("password")
    if (
        not isinstance(username, str)
        or not username
        or len(username) > 256
        or any(character in username for character in ("\0", "\n", "\r"))
        or not isinstance(password, str)
        or not password
        or len(password) > 8192
        or any(character in password for character in ("\0", "\n", "\r"))
    ):
        raise CredentialBrokerError("credential broker redemption failed")
    return RedeemedCredential(
        kind="git-https",
        expires_at=expires_at,
        username=username,
        password=password,
    )


def _validate_request_body(body: Dict[str, Any]) -> None:
    if set(body) != _REQUEST_FIELDS or body.get("schemaVersion") != BROKER_REQUEST_SCHEMA_VERSION:
        raise CredentialBrokerError("credential broker redemption failed")
    for field_name in _BINDING_FIELDS - {"repository"}:
        value = body.get(field_name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(character in value for character in ("\0", "\n", "\r"))
        ):
            raise CredentialBrokerError("credential broker redemption failed")
    repository = body.get("repository")
    if not isinstance(repository, str):
        raise CredentialBrokerError("credential broker redemption failed")
    _validate_repository_url(repository)


def _validate_repository_url(repository: str) -> None:
    if not repository or len(repository) > 2048 or any(
        character in repository for character in ("\0", "\n", "\r", " ", "\t")
    ):
        raise CredentialBrokerError("credential broker redemption failed")
    try:
        parsed = urllib.parse.urlsplit(repository)
        port = parsed.port
    except ValueError:
        raise CredentialBrokerError("credential broker redemption failed") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise CredentialBrokerError("credential broker redemption failed")


def _validate_broker_url(url: str) -> None:
    if len(url) > 2048 or any(character in url for character in ("\0", "\n", "\r", " ", "\t")):
        raise CredentialBrokerError("credential broker redemption failed")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise CredentialBrokerError("credential broker redemption failed") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise CredentialBrokerError("credential broker redemption failed")


def _unix_time() -> int:
    # Kept as a leaf for deterministic unit tests without patching time.time
    # globally across Control state-machine tests.
    import time

    return int(time.time())
