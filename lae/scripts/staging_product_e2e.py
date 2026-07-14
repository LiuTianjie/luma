#!/usr/bin/env python3
"""Run a secret-safe LAE staging product acceptance flow.

The flow uses the public API only. It can obtain an ephemeral deploy token from
the staging preview identity, or consume ``LAE_DEPLOY_TOKEN``. No credential or
environment value is written to stdout, persisted to disk, or included in an
exception message.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Mapping


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed", "canceled"})
DEPLOY_TOKEN_SCOPES = (
    "analyses:write",
    "apps:read",
    "apps:write",
    "deployments:write",
    "logs:read",
    "sources:write",
)


class AcceptanceFailure(RuntimeError):
    """A product assertion failed without exposing response internals."""


@dataclass(frozen=True, slots=True)
class ApiFailure(RuntimeError):
    status: int
    code: str
    retryable: bool

    def __str__(self) -> str:
        return f"API request failed ({self.status}, {self.code})"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict[str, Any]
    headers: Any


def emit(event: str, **data: object) -> None:
    print(
        json.dumps(
            {"event": event, **data},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


class JsonClient:
    def __init__(
        self,
        api_base: str,
        *,
        bearer_token: str | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        parsed = urllib.parse.urlsplit(api_base.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("api_base must be an absolute HTTP(S) URL")
        self.api_base = api_base.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, str | int] | None = None,
        idempotency_key: str | None = None,
        csrf: bool = False,
        expected: frozenset[int] = frozenset({200}),
    ) -> Response:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("path must be an absolute API path without a query")
        headers = {
            "Accept": "application/json",
            "User-Agent": "lae-staging-product-e2e/1",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in sorted(self.cookies.items())
            )
        if csrf:
            token = self.cookies.get("__Host-lae_csrf")
            if not token:
                raise AcceptanceFailure("session CSRF cookie is missing")
            headers["X-CSRF-Token"] = token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        encoded: bytes | None = None
        if body is not None:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        encoded_query = urllib.parse.urlencode(query or {}, safe="")
        request = urllib.request.Request(
            self.api_base + path + ("?" + encoded_query if encoded_query else ""),
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = self._response(response)
        except urllib.error.HTTPError as error:
            result = self._response(error)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiFailure(0, "LAE_API_UNAVAILABLE", True) from error
        if result.status not in expected:
            envelope = (
                result.body.get("error")
                if isinstance(result.body.get("error"), dict)
                else {}
            )
            code = envelope.get("code")
            safe_code = code if isinstance(code, str) else "LAE_API_REQUEST_FAILED"
            raise ApiFailure(
                result.status,
                safe_code,
                result.status in RETRYABLE_HTTP_STATUSES
                or envelope.get("retryable") is True,
            )
        return result

    @staticmethod
    def _response(stream: Any) -> Response:
        status = int(getattr(stream, "status", stream.code))
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AcceptanceFailure("API returned an oversized response")
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if status in RETRYABLE_HTTP_STATUSES:
                raise ApiFailure(status, "LAE_API_INVALID_RESPONSE", True) from error
            raise AcceptanceFailure("API returned invalid JSON") from error
        if not isinstance(decoded, dict):
            if status in RETRYABLE_HTTP_STATUSES:
                raise ApiFailure(status, "LAE_API_INVALID_RESPONSE", True)
            raise AcceptanceFailure("API returned a non-object response")
        return Response(status, decoded, stream.headers)

    def remember_response_cookies(self, response: Response) -> None:
        for header in response.headers.get_all("Set-Cookie", []):
            parsed = SimpleCookie()
            parsed.load(header)
            for name in ("__Host-lae_session", "__Host-lae_csrf"):
                if name in parsed:
                    self.cookies[name] = parsed[name].value


def request_with_retry(
    client: JsonClient,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    query: Mapping[str, str | int] | None = None,
    idempotency_key: str | None = None,
    csrf: bool = False,
    expected: frozenset[int] = frozenset({200}),
    deadline: float,
) -> Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.request(
                method,
                path,
                body,
                query=query,
                idempotency_key=idempotency_key,
                csrf=csrf,
                expected=expected,
            )
        except ApiFailure as error:
            if not error.retryable or time.monotonic() >= deadline:
                raise
            delay = min(8.0, 0.5 * (2 ** min(attempt - 1, 4)))
            emit(
                "api_retry",
                path=path,
                status=error.status,
                code=error.code,
                delaySeconds=delay,
            )
            time.sleep(delay)


def issue_preview_deploy_token(
    client: JsonClient, *, deadline: float
) -> tuple[str, str]:
    emit("auth_preview_start")
    preview = request_with_retry(
        client,
        "POST",
        "/auth/preview",
        {},
        expected=frozenset({201}),
        deadline=deadline,
    ).body
    email = preview.get("email")
    purpose = preview.get("purpose")
    magic_token = preview.get("magicToken")
    if (
        not isinstance(email, str)
        or purpose not in {"login", "register"}
        or not isinstance(magic_token, str)
    ):
        raise AcceptanceFailure("preview authentication response is incomplete")
    verify_path = (
        "/auth/login/verify"
        if purpose == "login"
        else "/auth/email/verify"
    )
    verified = request_with_retry(
        client,
        "POST",
        verify_path,
        {"email": email, "magicToken": magic_token},
        deadline=deadline,
    )
    client.remember_response_cookies(verified)
    if set(client.cookies) != {"__Host-lae_session", "__Host-lae_csrf"}:
        raise AcceptanceFailure("preview login did not establish a complete session")
    issued = request_with_retry(
        client,
        "POST",
        "/deploy-tokens",
        {
            "name": f"Staging E2E {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "scopes": list(DEPLOY_TOKEN_SCOPES),
        },
        csrf=True,
        expected=frozenset({201}),
        deadline=deadline,
    ).body
    token = issued.get("plaintext")
    record = issued.get("token")
    token_id = record.get("id") if isinstance(record, dict) else None
    if not isinstance(token, str) or not isinstance(token_id, str):
        raise AcceptanceFailure("deploy token issuance response is incomplete")
    emit("auth_preview_succeeded", tokenId=token_id)
    return token, token_id


def revoke_preview_deploy_token(
    client: JsonClient, token_id: str, *, deadline: float
) -> None:
    request_with_retry(
        client,
        "DELETE",
        f"/deploy-tokens/{token_id}",
        csrf=True,
        expected=frozenset({204}),
        deadline=deadline,
    )
    emit("deploy_token_revoked", tokenId=token_id)


def ensure_preview_mock_subscription(
    client: JsonClient,
    *,
    plan: str,
    deadline: float,
) -> None:
    """Explicitly upgrade only the reserved preview session via mock billing."""
    subscription = request_with_retry(
        client,
        "GET",
        "/billing/subscription",
        deadline=deadline,
    ).body.get("subscription")
    current = (
        subscription.get("plan", {}).get("code")
        if isinstance(subscription, dict)
        and isinstance(subscription.get("plan"), dict)
        else None
    )
    if not isinstance(current, str):
        raise AcceptanceFailure("preview subscription response is incomplete")
    if current == plan:
        emit("preview_subscription_ready", plan=plan, changed=False)
        return

    checkout = request_with_retry(
        client,
        "POST",
        "/billing/checkout-sessions",
        {"plan": plan, "interval": "monthly"},
        idempotency_key=idempotency("checkout"),
        csrf=True,
        expected=frozenset({201}),
        deadline=deadline,
    ).body.get("order")
    if not isinstance(checkout, dict):
        raise AcceptanceFailure("preview checkout response is incomplete")
    order_id = checkout.get("id")
    if not isinstance(order_id, str) or checkout.get("provider") != "mock":
        raise AcceptanceFailure("preview checkout is not a mock order")
    approved = request_with_retry(
        client,
        "POST",
        f"/billing/mock/orders/{order_id}/approve",
        {},
        idempotency_key=idempotency("approve"),
        csrf=True,
        deadline=deadline,
    ).body
    if approved.get("accepted") is not True:
        raise AcceptanceFailure("preview mock checkout was not accepted")
    refreshed = request_with_retry(
        client,
        "GET",
        "/billing/subscription",
        deadline=deadline,
    ).body.get("subscription")
    active = (
        refreshed.get("plan", {}).get("code")
        if isinstance(refreshed, dict) and isinstance(refreshed.get("plan"), dict)
        else None
    )
    if active != plan:
        raise AcceptanceFailure("preview subscription did not activate the selected plan")
    emit("preview_subscription_ready", plan=plan, changed=True, orderId=order_id)


def idempotency(prefix: str) -> str:
    return f"e2e-{prefix}-{uuid.uuid4().hex[:16]}"


def operation_id(response: Mapping[str, Any]) -> str:
    operation = response.get("operation")
    value = operation.get("id") if isinstance(operation, Mapping) else None
    if not isinstance(value, str):
        raise AcceptanceFailure("mutation response did not include an operation ID")
    return value


def watch_operation(
    client: JsonClient,
    operation: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    cursor = 0
    last_heartbeat = time.monotonic()
    while True:
        response = request_with_retry(
            client,
            "GET",
            f"/operations/{operation}/events",
            query={"after": cursor, "limit": 100},
            deadline=deadline,
        ).body
        events = response.get("events")
        if not isinstance(events, list):
            raise AcceptanceFailure("operation event page has an invalid shape")
        for item in events:
            if not isinstance(item, dict):
                raise AcceptanceFailure("operation event has an invalid shape")
            event_cursor = item.get("cursor")
            if not isinstance(event_cursor, int) or event_cursor <= cursor:
                raise AcceptanceFailure("operation event cursor is not monotonic")
            cursor = event_cursor
            emit(
                "operation_event",
                operationId=operation,
                cursor=cursor,
                type=item.get("type"),
                phase=item.get("phase"),
                status=item.get("status"),
                data=item.get("data") if isinstance(item.get("data"), dict) else {},
            )
        if response.get("terminal") is True:
            result = request_with_retry(
                client,
                "GET",
                f"/operations/{operation}",
                deadline=deadline,
            ).body
            status = result.get("status")
            if status not in TERMINAL_OPERATION_STATUSES:
                raise AcceptanceFailure("operation terminal snapshot is inconsistent")
            emit("operation_terminal", operationId=operation, status=status)
            if status != "succeeded":
                error = result.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                raise AcceptanceFailure(
                    f"operation {operation} failed ({code or 'LAE_OPERATION_FAILED'})"
                )
            return result
        now = time.monotonic()
        if now >= deadline:
            raise AcceptanceFailure(f"operation {operation} timed out at cursor {cursor}")
        if now - last_heartbeat >= 20:
            emit(
                "operation_waiting",
                operationId=operation,
                cursor=cursor,
                status=response.get("status"),
            )
            last_heartbeat = now
        time.sleep(1.5)


def create_application(
    client: JsonClient, *, name: str, slug: str, deadline: float
) -> str:
    response = request_with_retry(
        client,
        "POST",
        "/applications",
        {"name": name, "slug": slug},
        idempotency_key=idempotency("app"),
        expected=frozenset({201}),
        deadline=deadline,
    ).body
    application = response.get("application")
    application_id = application.get("id") if isinstance(application, dict) else None
    if not isinstance(application_id, str):
        raise AcceptanceFailure("application creation response is incomplete")
    emit("application_created", applicationId=application_id, slug=slug)
    return application_id


def analyze_git_source(
    client: JsonClient,
    *,
    application_id: str,
    repository: str,
    ref: str,
    subdirectory: str,
    deadline: float,
) -> dict[str, Any]:
    created = request_with_retry(
        client,
        "POST",
        "/analyses",
        {
            "applicationId": application_id,
            "source": {
                "type": "git",
                "repository": repository,
                "ref": ref,
                "subdirectory": subdirectory,
            },
            "intent": {"region": "cn", "publicProtocols": ["http"]},
        },
        idempotency_key=idempotency("analysis"),
        expected=frozenset({202}),
        deadline=deadline,
    ).body
    analysis = created.get("analysis")
    analysis_id = analysis.get("id") if isinstance(analysis, dict) else None
    if not isinstance(analysis_id, str):
        raise AcceptanceFailure("analysis creation response is incomplete")
    emit(
        "analysis_created",
        applicationId=application_id,
        analysisId=analysis_id,
        subdirectory=subdirectory,
    )
    watch_operation(client, operation_id(created), deadline=deadline)
    result = request_with_retry(
        client,
        "GET",
        f"/analyses/{analysis_id}",
        deadline=deadline,
    ).body
    emit(
        "analysis_terminal",
        analysisId=analysis_id,
        status=result.get("status"),
        verdict=result.get("verdict"),
        blockerCount=len(result.get("blockers", []))
        if isinstance(result.get("blockers"), list)
        else 0,
    )
    return result


def configure_required_environment(
    client: JsonClient,
    *,
    application_id: str,
    analysis_id: str,
    deadline: float,
) -> int:
    configuration = request_with_retry(
        client,
        "GET",
        f"/applications/{application_id}/analyses/{analysis_id}/configuration",
        deadline=deadline,
    ).body.get("configuration")
    if not isinstance(configuration, dict):
        raise AcceptanceFailure("deployment configuration is missing")
    schema_digest = configuration.get("environmentSchemaDigest")
    variables = configuration.get("environment")
    if not isinstance(schema_digest, str) or not isinstance(variables, list):
        raise AcceptanceFailure("deployment environment schema is incomplete")
    required = [item for item in variables if isinstance(item, dict) and item.get("required")]
    if not required:
        raise AcceptanceFailure("golden fixture did not request its required environment")
    values: dict[str, dict[str, str]] = {}
    for variable in required:
        references = variable.get("references")
        if not isinstance(references, list) or not references:
            raise AcceptanceFailure("required environment variable has no service reference")
        value = secrets.token_urlsafe(32)
        for reference in references:
            if not isinstance(reference, str):
                raise AcceptanceFailure("environment reference is invalid")
            values[reference] = {"value": value}
    environment = request_with_retry(
        client,
        "GET",
        f"/applications/{application_id}/environment",
        deadline=deadline,
    ).body.get("environment")
    current_version = environment.get("version") if isinstance(environment, dict) else None
    if not isinstance(current_version, int):
        raise AcceptanceFailure("application environment version is missing")
    patched = request_with_retry(
        client,
        "PATCH",
        f"/applications/{application_id}/analyses/{analysis_id}/environment",
        {
            "expectedVersion": current_version,
            "environmentSchemaDigest": schema_digest,
            "set": values,
            "unset": [],
        },
        idempotency_key=idempotency("env"),
        deadline=deadline,
    ).body
    metadata = patched.get("environment")
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if not isinstance(version, int) or version <= current_version:
        raise AcceptanceFailure("environment update did not advance its version")
    emit(
        "environment_configured",
        applicationId=application_id,
        requiredVariableCount=len(required),
        referenceCount=len(values),
        version=version,
    )
    return version


def deploy(
    client: JsonClient,
    *,
    application_id: str,
    analysis_id: str,
    environment_version: int,
    deadline: float,
) -> dict[str, Any]:
    created = request_with_retry(
        client,
        "POST",
        f"/applications/{application_id}/deployments",
        {"analysisId": analysis_id, "environmentVersion": environment_version},
        idempotency_key=idempotency("deploy"),
        expected=frozenset({202}),
        deadline=deadline,
    ).body
    deployment = created.get("deployment")
    deployment_id = deployment.get("id") if isinstance(deployment, dict) else None
    if not isinstance(deployment_id, str):
        raise AcceptanceFailure("deployment creation response is incomplete")
    emit(
        "deployment_created",
        applicationId=application_id,
        deploymentId=deployment_id,
    )
    watch_operation(client, operation_id(created), deadline=deadline)
    return request_with_retry(
        client,
        "GET",
        f"/applications/{application_id}",
        deadline=deadline,
    ).body


def assert_golden_topology(application: Mapping[str, Any]) -> tuple[str, ...]:
    services = application.get("services")
    routes = application.get("routes")
    volumes = application.get("volumes")
    if not isinstance(services, list) or not isinstance(routes, list) or not isinstance(volumes, list):
        raise AcceptanceFailure("application topology response is incomplete")
    service_roles = {
        item.get("key"): item.get("role") for item in services if isinstance(item, dict)
    }
    expected = {
        "web": "http",
        "admin": "http",
        "worker": "worker",
        "postgres": "datastore",
    }
    if service_roles != expected:
        raise AcceptanceFailure("deployed service topology differs from the golden fixture")
    if len(routes) != 2 or {item.get("serviceKey") for item in routes} != {"web", "admin"}:
        raise AcceptanceFailure("golden deployment did not create two HTTP routes")
    if len(volumes) != 2 or {item.get("key") for item in volumes} != {"app-data", "pg-data"}:
        raise AcceptanceFailure("golden deployment did not retain both named volumes")
    hostnames = tuple(
        sorted(
            item["hostname"]
            for item in routes
            if isinstance(item, dict) and isinstance(item.get("hostname"), str)
        )
    )
    if len(hostnames) != 2 or not all(name.endswith(".itool.tech") for name in hostnames):
        raise AcceptanceFailure("golden deployment route hostnames are invalid")
    emit(
        "topology_verified",
        serviceCount=len(services),
        routeCount=len(routes),
        volumeCount=len(volumes),
        hostnames=list(hostnames),
    )
    return hostnames


def lifecycle_action(
    client: JsonClient,
    *,
    application_id: str,
    action: str,
    deadline: float,
) -> dict[str, Any]:
    response = request_with_retry(
        client,
        "POST",
        f"/applications/{application_id}/actions/{action}",
        {},
        idempotency_key=idempotency(action.replace("-", "")),
        expected=frozenset({202}),
        deadline=deadline,
    ).body
    emit("lifecycle_requested", applicationId=application_id, action=action)
    return watch_operation(client, operation_id(response), deadline=deadline)


def public_probe(hostname: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://{hostname}/healthz",
        headers={"Accept": "application/json", "User-Agent": "lae-staging-product-e2e/1"},
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds, context=context) as response:
            raw = response.read(64 * 1024)
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if response.status != 200 or not isinstance(body, dict):
                raise AcceptanceFailure("public route returned an invalid health response")
            return body
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise AcceptanceFailure("public route probe failed") from error


def delete_application(
    client: JsonClient, application_id: str, *, deadline: float
) -> None:
    lifecycle_action(
        client,
        application_id=application_id,
        action="delete",
        deadline=deadline,
    )
    emit("application_deleted", applicationId=application_id)


def run(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    session_client: JsonClient | None = None
    ephemeral_token_id: str | None = None
    token = os.environ.get("LAE_DEPLOY_TOKEN")
    if not token:
        session_client = JsonClient(args.api_base, timeout_seconds=args.request_timeout)
        token, ephemeral_token_id = issue_preview_deploy_token(
            session_client, deadline=deadline
        )
        if args.mock_upgrade_preview:
            ensure_preview_mock_subscription(
                session_client,
                plan=args.mock_upgrade_preview,
                deadline=deadline,
            )
    elif args.mock_upgrade_preview:
        raise AcceptanceFailure(
            "--mock-upgrade-preview requires the reserved interactive preview session"
        )
    api = JsonClient(
        args.api_base,
        bearer_token=token,
        timeout_seconds=args.request_timeout,
    )
    suffix = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    golden_id: str | None = None
    negative_id: str | None = None
    succeeded = False
    try:
        golden_id = create_application(
            api,
            name=f"Compose product E2E {suffix}",
            slug=f"compose-product-e2e-{suffix}".lower(),
            deadline=deadline,
        )
        golden = analyze_git_source(
            api,
            application_id=golden_id,
            repository=args.repository,
            ref=args.ref,
            subdirectory=args.golden_subdirectory,
            deadline=deadline,
        )
        if (
            golden.get("status") != "needs_configuration"
            or golden.get("verdict") != "needs_input"
            or golden.get("planStored") is not True
        ):
            raise AcceptanceFailure(
                "golden Compose analysis did not return a stored configurable plan"
            )
        analysis_id = golden.get("id")
        if not isinstance(analysis_id, str):
            raise AcceptanceFailure("golden analysis ID is missing")
        environment_version = configure_required_environment(
            api,
            application_id=golden_id,
            analysis_id=analysis_id,
            deadline=deadline,
        )
        detail = deploy(
            api,
            application_id=golden_id,
            analysis_id=analysis_id,
            environment_version=environment_version,
            deadline=deadline,
        )
        initial_hostnames = assert_golden_topology(detail)
        probe_failures = []
        if args.public_probe != "skip":
            for hostname in initial_hostnames:
                try:
                    body = public_probe(hostname, timeout_seconds=args.request_timeout)
                    emit(
                        "public_route_verified",
                        hostname=hostname,
                        service=body.get("service"),
                    )
                except AcceptanceFailure:
                    probe_failures.append(hostname)
                    emit("public_route_probe_failed", hostname=hostname)
            if probe_failures and args.public_probe == "required":
                raise AcceptanceFailure("one or more public HTTP routes are unreachable")

        lifecycle_action(
            api, application_id=golden_id, action="restart", deadline=deadline
        )
        restarted = request_with_retry(
            api, "GET", f"/applications/{golden_id}", deadline=deadline
        ).body
        restarted_hostnames = assert_golden_topology(restarted)
        if restarted_hostnames != initial_hostnames:
            raise AcceptanceFailure("restart changed stable public hostnames")

        lifecycle_action(
            api, application_id=golden_id, action="suspend", deadline=deadline
        )
        suspended = request_with_retry(
            api, "GET", f"/applications/{golden_id}", deadline=deadline
        ).body
        summary = suspended.get("application")
        if not isinstance(summary, dict) or summary.get("desiredState") != "suspended":
            raise AcceptanceFailure("suspend did not update the desired state")
        lifecycle_action(
            api, application_id=golden_id, action="resume", deadline=deadline
        )
        resumed = request_with_retry(
            api, "GET", f"/applications/{golden_id}", deadline=deadline
        ).body
        summary = resumed.get("application")
        if not isinstance(summary, dict) or summary.get("desiredState") != "running":
            raise AcceptanceFailure("resume did not restore the desired state")
        update = lifecycle_action(
            api,
            application_id=golden_id,
            action="check-update",
            deadline=deadline,
        )
        update_check = update.get("updateCheck")
        if not isinstance(update_check, dict):
            raise AcceptanceFailure("update check did not return a structured result")
        candidate_analysis = update_check.get("candidateAnalysis")
        if (
            not isinstance(candidate_analysis, dict)
            or not isinstance(candidate_analysis.get("id"), str)
            or not candidate_analysis["id"].startswith("ana_")
            or candidate_analysis.get("verdict")
            not in {"deployable", "needs_input", "unsupported", "diagnostic_failed"}
        ):
            raise AcceptanceFailure("update check did not bind a candidate analysis")
        changes = update_check.get("changes")
        if (
            not isinstance(changes, dict)
            or not isinstance(changes.get("destructive"), bool)
            or not isinstance(changes.get("confirmations"), list)
        ):
            raise AcceptanceFailure("update check did not return a closed plan diff")
        for section in ("services", "routes", "volumes", "environment"):
            change_set = changes.get(section)
            if not isinstance(change_set, dict) or any(
                not isinstance(change_set.get(kind), list)
                for kind in ("added", "removed", "changed")
            ):
                raise AcceptanceFailure(
                    f"update check returned an invalid {section} diff"
                )
        if changes["destructive"] != bool(changes["confirmations"]):
            raise AcceptanceFailure("update check destructive diff is inconsistent")
        emit("lifecycle_verified", applicationId=golden_id)

        negative_id = create_application(
            api,
            name=f"Unsupported Compose E2E {suffix}",
            slug=f"unsupported-compose-e2e-{suffix}".lower(),
            deadline=deadline,
        )
        negative = analyze_git_source(
            api,
            application_id=negative_id,
            repository=args.repository,
            ref=args.ref,
            subdirectory=args.negative_subdirectory,
            deadline=deadline,
        )
        blockers = negative.get("blockers")
        if (
            negative.get("status") != "not_deployable"
            or negative.get("verdict") != "unsupported"
            or not isinstance(blockers, list)
            or not blockers
        ):
            raise AcceptanceFailure(
                "unsupported Compose analysis did not return actionable blockers"
            )
        emit(
            "unsupported_diagnosis_verified",
            applicationId=negative_id,
            blockerCount=len(blockers),
            blockerCodes=[
                item.get("code") for item in blockers if isinstance(item, dict)
            ],
        )
        delete_application(api, negative_id, deadline=deadline)
        negative_id = None
        succeeded = True
        emit(
            "acceptance_succeeded",
            goldenApplicationId=golden_id,
            goldenKept=args.keep_golden,
            publicProbeMode=args.public_probe,
            publicProbeFailures=probe_failures,
        )
    finally:
        cleanup_deadline = max(deadline, time.monotonic() + 180)
        if negative_id is not None:
            try:
                delete_application(api, negative_id, deadline=cleanup_deadline)
            except Exception:
                emit("cleanup_failed", resource="negative_application")
        if golden_id is not None and (not succeeded or not args.keep_golden):
            try:
                delete_application(api, golden_id, deadline=cleanup_deadline)
            except Exception:
                emit("cleanup_failed", resource="golden_application")
        if session_client is not None and ephemeral_token_id is not None:
            try:
                revoke_preview_deploy_token(
                    session_client,
                    ephemeral_token_id,
                    deadline=cleanup_deadline,
                )
            except Exception:
                emit("cleanup_failed", resource="deploy_token")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--api-base",
        default=os.environ.get("LAE_API_URL", "http://127.0.0.1:18080/v1"),
    )
    result.add_argument(
        "--repository", default="https://github.com/LiuTianjie/luma.git"
    )
    result.add_argument(
        "--ref",
        default="20005c959bc7591204cdd6ee0dd7a030d58a48cd",
        help="Immutable Git commit containing the acceptance fixtures.",
    )
    result.add_argument(
        "--golden-subdirectory",
        default="lae/e2e/fixtures/compose-two-http-volume",
    )
    result.add_argument(
        "--negative-subdirectory",
        default="lae/e2e/fixtures/compose-unsupported-host-access",
    )
    result.add_argument("--timeout-seconds", type=float, default=1800)
    result.add_argument("--request-timeout", type=float, default=30)
    result.add_argument(
        "--public-probe", choices=("required", "warn", "skip"), default="warn"
    )
    result.add_argument(
        "--mock-upgrade-preview",
        choices=("pro", "ultra"),
        help=(
            "Explicitly approve a staging-only mock checkout for the reserved "
            "preview identity; unavailable with LAE_DEPLOY_TOKEN."
        ),
    )
    result.add_argument("--keep-golden", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        run(args)
    except (AcceptanceFailure, ApiFailure, ValueError) as error:
        emit("acceptance_failed", reason=str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
