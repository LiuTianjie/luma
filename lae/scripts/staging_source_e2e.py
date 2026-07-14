#!/usr/bin/env python3
"""Exercise LAE ZIP upload and private Git through the public product API.

The caller supplies a private repository and its credential. The credential is
read from an environment variable, sent only to LAE's source-connection API,
and never printed or persisted by this script. ZIP bytes use the one-time S3
PUT directly, without forwarding LAE authentication headers.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping

from staging_product_e2e import (
    AcceptanceFailure,
    ApiFailure,
    JsonClient,
    create_application,
    delete_application,
    deploy,
    emit,
    ensure_preview_mock_subscription,
    idempotency,
    issue_preview_deploy_token,
    operation_id,
    public_probe,
    request_with_retry,
    revoke_preview_deploy_token,
    watch_operation,
    analyze_git_source,
)


UPLOAD_TERMINAL_STATUSES = frozenset({"ready", "failed", "expired", "deleted"})


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _zip_fixture(root: Path) -> bytes:
    if not root.is_dir() or not (root / "index.html").is_file():
        raise AcceptanceFailure("ZIP fixture must contain a root index.html")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            archive.writestr(relative, path.read_bytes())
    content = output.getvalue()
    if not content:
        raise AcceptanceFailure("ZIP fixture is empty")
    return content


def _put_upload(transfer: Mapping[str, Any], content: bytes, *, timeout: float) -> None:
    if transfer.get("method") != "PUT":
        raise AcceptanceFailure("upload reservation did not use PUT")
    url = transfer.get("url")
    headers = transfer.get("headers")
    if not isinstance(url, str) or not isinstance(headers, dict):
        raise AcceptanceFailure("upload reservation is incomplete")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise AcceptanceFailure("upload reservation is not an HTTPS URL")
    safe_headers: dict[str, str] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or name.lower() in {"authorization", "cookie"}
            or "\r" in name + value
            or "\n" in name + value
        ):
            raise AcceptanceFailure("upload reservation contains unsafe headers")
        safe_headers[name] = value
    if safe_headers.get("Content-Length") != str(len(content)):
        raise AcceptanceFailure("upload reservation has an invalid content length")
    request = urllib.request.Request(
        url,
        data=content,
        headers={**safe_headers, "User-Agent": "lae-staging-source-e2e/1"},
        method="PUT",
    )
    opener = urllib.request.build_opener(_RejectRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(64 * 1024 + 1)
            if not 200 <= int(response.status) < 300:
                raise AcceptanceFailure("upload transfer returned a non-success status")
    except urllib.error.HTTPError as error:
        error.read(64 * 1024 + 1)
        raise AcceptanceFailure("upload transfer failed") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AcceptanceFailure("upload transfer is unavailable") from error


def _environment_version(client: JsonClient, application_id: str, *, deadline: float) -> int:
    body = request_with_retry(
        client,
        "GET",
        f"/applications/{application_id}/environment",
        deadline=deadline,
    ).body
    environment = body.get("environment")
    version = environment.get("version") if isinstance(environment, dict) else None
    if not isinstance(version, int):
        raise AcceptanceFailure("application environment version is missing")
    return version


def _assert_single_http(application: Mapping[str, Any]) -> str:
    services = application.get("services")
    routes = application.get("routes")
    volumes = application.get("volumes")
    if not isinstance(services, list) or len(services) != 1:
        raise AcceptanceFailure("static deployment must contain one service")
    if not isinstance(routes, list) or len(routes) != 1:
        raise AcceptanceFailure("static deployment must contain one public route")
    if not isinstance(volumes, list) or volumes:
        raise AcceptanceFailure("static deployment unexpectedly contains volumes")
    service = services[0]
    route = routes[0]
    if not isinstance(service, dict) or service.get("role") != "http":
        raise AcceptanceFailure("static deployment service is not HTTP")
    hostname = route.get("hostname") if isinstance(route, dict) else None
    if not isinstance(hostname, str) or not hostname.endswith(".itool.tech"):
        raise AcceptanceFailure("static deployment hostname is invalid")
    return hostname


def _wait_upload_ready(
    client: JsonClient,
    upload_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    last_status: str | None = None
    while True:
        body = request_with_retry(
            client, "GET", f"/uploads/{upload_id}", deadline=deadline
        ).body
        upload = body.get("upload")
        status = upload.get("status") if isinstance(upload, dict) else None
        if not isinstance(status, str):
            raise AcceptanceFailure("upload status response is incomplete")
        if status != last_status:
            emit("upload_status", uploadId=upload_id, status=status)
            last_status = status
        if status == "ready":
            return body
        if status in UPLOAD_TERMINAL_STATUSES:
            code = upload.get("failureCode") if isinstance(upload, dict) else None
            raise AcceptanceFailure(
                f"ZIP upload validation failed ({code or 'LAE_UPLOAD_FAILED'})"
            )
        if time.monotonic() >= deadline:
            raise AcceptanceFailure("ZIP upload validation timed out")
        time.sleep(1.0)


def _analyze_upload(
    client: JsonClient,
    *,
    application_id: str,
    upload_id: str,
    deadline: float,
) -> dict[str, Any]:
    created = request_with_retry(
        client,
        "POST",
        "/analyses",
        {
            "applicationId": application_id,
            "source": {"type": "upload", "uploadId": upload_id},
            "intent": {"region": "cn", "publicProtocols": ["http"]},
        },
        idempotency_key=idempotency("zip-analysis"),
        expected=frozenset({202}),
        deadline=deadline,
    ).body
    analysis = created.get("analysis")
    analysis_id = analysis.get("id") if isinstance(analysis, dict) else None
    if not isinstance(analysis_id, str):
        raise AcceptanceFailure("ZIP analysis creation response is incomplete")
    watch_operation(client, operation_id(created), deadline=deadline)
    return request_with_retry(
        client, "GET", f"/analyses/{analysis_id}", deadline=deadline
    ).body


def _deploy_analysis(
    client: JsonClient,
    *,
    application_id: str,
    analysis: Mapping[str, Any],
    deadline: float,
) -> str:
    analysis_id = analysis.get("id")
    if (
        analysis.get("status") != "deployable"
        or analysis.get("verdict") != "deployable"
        or analysis.get("planStored") is not True
        or not isinstance(analysis_id, str)
    ):
        blockers = analysis.get("blockers")
        codes = (
            [item.get("code") for item in blockers if isinstance(item, dict)]
            if isinstance(blockers, list)
            else []
        )
        raise AcceptanceFailure(f"source is not deployable ({json.dumps(codes)})")
    detail = deploy(
        client,
        application_id=application_id,
        analysis_id=analysis_id,
        environment_version=_environment_version(
            client, application_id, deadline=deadline
        ),
        deadline=deadline,
    )
    hostname = _assert_single_http(detail)
    public_probe(hostname, timeout_seconds=30)
    emit("source_route_verified", applicationId=application_id, hostname=hostname)
    return hostname


def _create_source_connection(
    client: JsonClient,
    *,
    provider: str,
    base_url: str,
    username: str | None,
    secret: str,
    deadline: float,
) -> str:
    payload: dict[str, Any] = {
        "provider": provider,
        "displayName": f"Private source E2E {uuid.uuid4().hex[:8]}",
        "baseUrl": base_url,
        "secret": secret,
    }
    if username:
        payload["username"] = username
    body = request_with_retry(
        client,
        "POST",
        "/source-connections",
        payload,
        idempotency_key=idempotency("source-connection"),
        expected=frozenset({201}),
        deadline=deadline,
    ).body
    connection = body.get("connection")
    connection_id = connection.get("id") if isinstance(connection, dict) else None
    if not isinstance(connection_id, str):
        raise AcceptanceFailure("source connection response is incomplete")
    emit("source_connection_created", connectionId=connection_id, provider=provider)
    return connection_id


def _restart_worker_when_operation_runs(
    client: JsonClient,
    operation: str,
    *,
    stack: str,
    deadline: float,
) -> None:
    while True:
        snapshot = request_with_retry(
            client, "GET", f"/operations/{operation}", deadline=deadline
        ).body
        status = snapshot.get("status")
        if status == "running":
            break
        if status in {"succeeded", "failed", "canceled"}:
            raise AcceptanceFailure("analysis completed before recovery injection")
        if time.monotonic() >= deadline:
            raise AcceptanceFailure("analysis did not start before recovery injection")
        time.sleep(0.5)
    restarted = subprocess.run(
        [
            "luma",
            "service",
            "restart",
            stack,
            "--service",
            "worker",
            "--mode",
            "task",
            "--timeout",
            "120",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=150,
        check=False,
    )
    if restarted.returncode != 0:
        raise AcceptanceFailure("Worker recovery injection failed")
    emit("worker_recovery_injected", operationId=operation)


def run(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    session: JsonClient | None = None
    token_id: str | None = None
    token = os.environ.get("LAE_DEPLOY_TOKEN")
    if not token:
        session = JsonClient(args.api_base, timeout_seconds=args.request_timeout)
        token, token_id = issue_preview_deploy_token(session, deadline=deadline)
        ensure_preview_mock_subscription(session, plan="pro", deadline=deadline)
    api = JsonClient(
        args.api_base,
        bearer_token=token,
        timeout_seconds=args.request_timeout,
    )
    private_secret = os.environ.get(args.private_git_token_env, "")
    if args.mode in {"both", "private-git"} and not private_secret:
        raise AcceptanceFailure("private Git credential environment is missing")

    upload_id: str | None = None
    zip_app: str | None = None
    connection_id: str | None = None
    private_app: str | None = None
    cleanup_failures: list[str] = []
    try:
        if args.mode in {"both", "zip"}:
            zip_app = create_application(
                api,
                name=f"ZIP source E2E {uuid.uuid4().hex[:8]}",
                slug=f"zip-source-e2e-{uuid.uuid4().hex[:12]}",
                deadline=deadline,
            )
            content = _zip_fixture(Path(args.zip_fixture))
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            created = request_with_retry(
                api,
                "POST",
                "/uploads",
                {
                    "applicationId": zip_app,
                    "filename": "static-site.zip",
                    "mediaType": "application/zip",
                    "sizeBytes": len(content),
                    "sha256": digest,
                },
                idempotency_key=idempotency("zip-upload"),
                expected=frozenset({201}),
                deadline=deadline,
            ).body
            upload = created.get("upload")
            upload_id = upload.get("id") if isinstance(upload, dict) else None
            transfer = created.get("transfer")
            if not isinstance(upload_id, str) or not isinstance(transfer, dict):
                raise AcceptanceFailure("ZIP upload reservation is incomplete")
            _put_upload(transfer, content, timeout=args.request_timeout)
            request_with_retry(
                api,
                "POST",
                f"/uploads/{upload_id}/complete",
                {},
                idempotency_key=idempotency("zip-complete"),
                expected=frozenset({202}),
                deadline=deadline,
            )
            _wait_upload_ready(api, upload_id, deadline=deadline)
            analysis = _analyze_upload(
                api,
                application_id=zip_app,
                upload_id=upload_id,
                deadline=deadline,
            )
            _deploy_analysis(
                api, application_id=zip_app, analysis=analysis, deadline=deadline
            )
            emit("zip_source_succeeded", applicationId=zip_app, uploadId=upload_id)

        if args.mode in {"both", "private-git"}:
            connection_id = _create_source_connection(
                api,
                provider=args.private_git_provider,
                base_url=args.private_git_base_url,
                username=args.private_git_username,
                secret=private_secret,
                deadline=deadline,
            )
            private_app = create_application(
                api,
                name=f"Private Git E2E {uuid.uuid4().hex[:8]}",
                slug=f"private-git-e2e-{uuid.uuid4().hex[:12]}",
                deadline=deadline,
            )
            analysis = analyze_git_source(
                api,
                application_id=private_app,
                repository=args.private_git_repository,
                ref=args.private_git_ref,
                subdirectory=args.private_git_subdirectory,
                connection_id=connection_id,
                on_operation_created=(
                    lambda operation: _restart_worker_when_operation_runs(
                        api,
                        operation,
                        stack=args.stack,
                        deadline=deadline,
                    )
                    if args.restart_worker_during_private_analysis
                    else None
                ),
                deadline=deadline,
            )
            _deploy_analysis(
                api, application_id=private_app, analysis=analysis, deadline=deadline
            )
            emit("private_git_source_succeeded", applicationId=private_app)

        emit("source_acceptance_succeeded", mode=args.mode)
    finally:
        cleanup_deadline = max(deadline, time.monotonic() + 300)
        for label, application_id in (
            ("private_git_application", private_app),
            ("zip_application", zip_app),
        ):
            if application_id is None:
                continue
            try:
                delete_application(api, application_id, deadline=cleanup_deadline)
            except Exception:
                cleanup_failures.append(label)
                emit("cleanup_failed", resource=label)
        if upload_id is not None:
            try:
                request_with_retry(
                    api,
                    "DELETE",
                    f"/uploads/{upload_id}",
                    idempotency_key=idempotency("zip-delete"),
                    expected=frozenset({202}),
                    deadline=cleanup_deadline,
                )
            except Exception:
                cleanup_failures.append("upload")
                emit("cleanup_failed", resource="upload")
        if connection_id is not None:
            try:
                request_with_retry(
                    api,
                    "DELETE",
                    f"/source-connections/{connection_id}",
                    idempotency_key=idempotency("connection-revoke"),
                    expected=frozenset({204}),
                    deadline=cleanup_deadline,
                )
            except Exception:
                cleanup_failures.append("source_connection")
                emit("cleanup_failed", resource="source_connection")
        if session is not None and token_id is not None:
            try:
                revoke_preview_deploy_token(session, token_id, deadline=cleanup_deadline)
            except Exception:
                cleanup_failures.append("deploy_token")
                emit("cleanup_failed", resource="deploy_token")
        if cleanup_failures:
            raise AcceptanceFailure(
                "source acceptance cleanup failed (" + ",".join(cleanup_failures) + ")"
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--api-base",
        default=os.environ.get("LAE_API_URL", "https://lae-api-staging.itool.tech/v1"),
    )
    result.add_argument("--mode", choices=("both", "zip", "private-git"), default="both")
    result.add_argument(
        "--zip-fixture",
        default=str(Path(__file__).parents[1] / "e2e" / "fixtures" / "static-site"),
    )
    result.add_argument(
        "--private-git-provider", choices=("github", "gitea", "generic"), default="github"
    )
    result.add_argument("--private-git-base-url", default="https://github.com")
    result.add_argument("--private-git-username")
    result.add_argument("--private-git-repository", default="")
    result.add_argument("--private-git-ref", default="main")
    result.add_argument("--private-git-subdirectory", default="")
    result.add_argument("--private-git-token-env", default="LAE_PRIVATE_GIT_TOKEN")
    result.add_argument("--stack", default="lae-platform-staging")
    result.add_argument("--restart-worker-during-private-analysis", action="store_true")
    result.add_argument("--timeout-seconds", type=float, default=1800)
    result.add_argument("--request-timeout", type=float, default=30)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.mode in {"both", "private-git"}:
        parsed = urllib.parse.urlsplit(args.private_git_repository)
        if parsed.scheme != "https" or parsed.hostname is None:
            emit("source_acceptance_failed", reason="private Git repository must use HTTPS")
            return 1
    try:
        run(args)
    except (AcceptanceFailure, ApiFailure, ValueError) as error:
        emit("source_acceptance_failed", reason=str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
