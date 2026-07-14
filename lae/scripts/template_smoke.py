#!/usr/bin/env python3
"""Exercise every published LAE template through the public product API.

This is the executable half of the template publication gate.  It intentionally
uses the same application, analysis, deployment, route and deletion endpoints
as a tenant.  A scheduler may provide ``LAE_DEPLOY_TOKEN``; staging can instead
use the reserved preview identity and an ephemeral token that is revoked during
cleanup.
"""

from __future__ import annotations

import argparse
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

from staging_product_e2e import (
    AcceptanceFailure,
    ApiFailure,
    JsonClient,
    configure_required_environment,
    delete_application,
    deploy,
    emit,
    idempotency,
    issue_preview_deploy_token,
    operation_id,
    request_with_retry,
    revoke_preview_deploy_token,
    watch_operation,
)


def _environment_version(
    client: JsonClient, application_id: str, *, deadline: float
) -> int:
    body = request_with_retry(
        client,
        "GET",
        f"/applications/{application_id}/environment",
        deadline=deadline,
    ).body.get("environment")
    version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(version, int) or version < 0:
        raise AcceptanceFailure("template application environment is incomplete")
    return version


def _probe_route(hostname: str, *, deadline: float, timeout_seconds: float) -> int:
    context = ssl.create_default_context()
    last_status = 0
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"https://{hostname}/",
            headers={
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "User-Agent": "lae-template-smoke/1",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_seconds, context=context
            ) as response:
                response.read(64 * 1024)
                last_status = int(response.status)
                if 200 <= last_status < 400:
                    return last_status
        except urllib.error.HTTPError as error:
            last_status = int(error.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            last_status = 0
        time.sleep(2)
    raise AcceptanceFailure(
        f"template public route did not become ready (status {last_status})"
    )


def _template_ids(
    client: JsonClient, selected: set[str] | None, *, deadline: float
) -> list[tuple[str, str]]:
    body = request_with_retry(
        client, "GET", "/templates", deadline=deadline
    ).body.get("templates")
    if not isinstance(body, list) or not body:
        raise AcceptanceFailure("template catalog is empty")
    catalog: list[tuple[str, str]] = []
    for item in body:
        template_id = item.get("id") if isinstance(item, dict) else None
        version = item.get("version") if isinstance(item, dict) else None
        if not isinstance(template_id, str) or not isinstance(version, str):
            raise AcceptanceFailure("template catalog item is incomplete")
        if selected is None or template_id in selected:
            catalog.append((template_id, version))
    missing = set() if selected is None else selected - {item[0] for item in catalog}
    if missing:
        raise AcceptanceFailure("requested template is not available")
    return catalog


def _launch(
    client: JsonClient,
    template_id: str,
    *,
    suffix: str,
    deadline: float,
) -> tuple[str, str, str]:
    launched = request_with_retry(
        client,
        "POST",
        f"/templates/{template_id}/launch",
        {
            "name": f"Template smoke {template_id} {suffix}",
            "slug": f"template-smoke-{template_id}-{suffix}".lower(),
            "region": "cn",
        },
        idempotency_key=idempotency(f"template-{template_id}"),
        expected=frozenset({202}),
        deadline=deadline,
    ).body
    application = launched.get("application")
    analysis = launched.get("analysis")
    application_id = (
        application.get("id") if isinstance(application, Mapping) else None
    )
    analysis_id = analysis.get("id") if isinstance(analysis, Mapping) else None
    if not isinstance(application_id, str) or not isinstance(analysis_id, str):
        raise AcceptanceFailure("template launch response is incomplete")
    return application_id, analysis_id, operation_id(launched)


def smoke_one(
    client: JsonClient,
    *,
    template_id: str,
    version: str,
    timeout_seconds: float,
    request_timeout: float,
    keep_failed: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    suffix = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    application_id: str | None = None
    succeeded = False
    emit("template_smoke_started", templateId=template_id, version=version)
    try:
        application_id, analysis_id, analysis_operation = _launch(
            client, template_id, suffix=suffix, deadline=deadline
        )
        watch_operation(client, analysis_operation, deadline=deadline)
        analysis = request_with_retry(
            client, "GET", f"/analyses/{analysis_id}", deadline=deadline
        ).body
        verdict = analysis.get("verdict")
        status = analysis.get("status")
        if verdict not in {"deployable", "needs_input"}:
            raise AcceptanceFailure(
                f"template analysis rejected ({verdict or status or 'unknown'})"
            )
        environment_version = _environment_version(
            client, application_id, deadline=deadline
        )
        if verdict == "needs_input":
            environment_version = configure_required_environment(
                client,
                application_id=application_id,
                analysis_id=analysis_id,
                deadline=deadline,
            )
        application = deploy(
            client,
            application_id=application_id,
            analysis_id=analysis_id,
            environment_version=environment_version,
            deadline=deadline,
        )
        routes = application.get("routes")
        hostnames = sorted(
            item["hostname"]
            for item in routes
            if isinstance(item, dict) and isinstance(item.get("hostname"), str)
        ) if isinstance(routes, list) else []
        if not hostnames:
            raise AcceptanceFailure("template deployment has no public HTTP route")
        probes: list[dict[str, Any]] = []
        for hostname in hostnames:
            status_code = _probe_route(
                hostname,
                deadline=deadline,
                timeout_seconds=request_timeout,
            )
            probes.append({"hostname": hostname, "status": status_code})
            emit(
                "template_route_verified",
                templateId=template_id,
                hostname=hostname,
                status=status_code,
            )
        succeeded = True
        emit(
            "template_smoke_succeeded",
            templateId=template_id,
            version=version,
            routeCount=len(probes),
        )
        return {
            "templateId": template_id,
            "version": version,
            "status": "succeeded",
            "routes": probes,
        }
    finally:
        if application_id is not None and (succeeded or not keep_failed):
            try:
                delete_application(client, application_id, deadline=deadline)
            except Exception:
                emit(
                    "template_cleanup_failed",
                    templateId=template_id,
                    applicationId=application_id,
                )


def run(args: argparse.Namespace) -> None:
    session_client: JsonClient | None = None
    token_id: str | None = None
    token = os.environ.get("LAE_DEPLOY_TOKEN")
    report_token = os.environ.get("LAE_TEMPLATE_SMOKE_REPORT_TOKEN", "").strip()
    if not report_token:
        raise AcceptanceFailure("template smoke report token is not configured")
    bootstrap_deadline = time.monotonic() + min(args.timeout_seconds, 300)
    if not token:
        session_client = JsonClient(
            args.api_base, timeout_seconds=args.request_timeout
        )
        token, token_id = issue_preview_deploy_token(
            session_client, deadline=bootstrap_deadline
        )
    report_client = JsonClient(
        args.report_api_base,
        bearer_token=report_token,
        timeout_seconds=args.request_timeout,
    )
    smoke_headers = {"X-LAE-Template-Smoke-Token": report_token}
    client = JsonClient(
        args.api_base,
        bearer_token=token,
        request_headers=smoke_headers,
        timeout_seconds=args.request_timeout,
    )
    selected = None
    if args.templates:
        selected = {value.strip() for value in args.templates.split(",") if value.strip()}
        if not selected:
            raise AcceptanceFailure("--templates did not contain a template ID")
    catalog = _template_ids(client, selected, deadline=bootstrap_deadline)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    run_id = "tsm-" + uuid.uuid4().hex

    def report(
        template_id: str,
        version: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        body: dict[str, str] = {
            "runId": run_id,
            "templateId": template_id,
            "version": version,
            "status": "succeeded" if succeeded else "failed",
        }
        if error_code is not None:
            body["errorCode"] = error_code
        request_with_retry(
            report_client,
            "POST",
            "/internal/v1/template-smoke/results",
            body,
            deadline=time.monotonic() + min(args.timeout_seconds, 300),
        )

    try:
        for template_id, version in catalog:
            try:
                result = smoke_one(
                    client,
                    template_id=template_id,
                    version=version,
                    timeout_seconds=args.timeout_seconds,
                    request_timeout=args.request_timeout,
                    keep_failed=args.keep_failed,
                )
            except (AcceptanceFailure, ApiFailure) as error:
                error_code = (
                    "LAE_TEMPLATE_ACCEPTANCE_FAILED"
                    if isinstance(error, AcceptanceFailure)
                    else "LAE_TEMPLATE_API_FAILED"
                )
                report(
                    template_id,
                    version,
                    succeeded=False,
                    error_code=error_code,
                )
                failures.append(
                    {
                        "templateId": template_id,
                        "version": version,
                        "error": str(error),
                    }
                )
                emit(
                    "template_smoke_failed",
                    templateId=template_id,
                    version=version,
                    error=str(error),
                )
                if args.fail_fast:
                    break
            else:
                # Reporting is part of scheduler control-plane health. A
                # reporting outage must abort the run, never be rewritten as
                # a false template failure that could trigger auto-unpublish.
                report(template_id, version, succeeded=True)
                results.append(result)
        emit(
            "template_smoke_complete",
            checked=len(results) + len(failures),
            succeeded=len(results),
            failed=len(failures),
        )
        if failures:
            raise AcceptanceFailure(
                f"{len(failures)} published template smoke check(s) failed"
            )
    finally:
        if session_client is not None and token_id is not None:
            revoke_preview_deploy_token(
                session_client,
                token_id,
                deadline=time.monotonic() + 60,
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--api-base", default="https://lae-api-staging.itool.tech/v1"
    )
    result.add_argument(
        "--report-api-base",
        default=os.environ.get(
            "LAE_TEMPLATE_SMOKE_REPORT_API_BASE", "https://lae-api-staging.itool.tech"
        ),
    )
    result.add_argument(
        "--templates",
        help="comma-separated template IDs; omitted means every catalog template",
    )
    result.add_argument("--timeout-seconds", type=float, default=2400)
    result.add_argument("--request-timeout", type=float, default=30)
    result.add_argument("--fail-fast", action="store_true")
    result.add_argument("--keep-failed", action="store_true")
    return result


if __name__ == "__main__":
    try:
        run(parser().parse_args())
    except (AcceptanceFailure, ApiFailure, ValueError) as error:
        emit("template_smoke_aborted", error=str(error))
        raise SystemExit(1) from error
