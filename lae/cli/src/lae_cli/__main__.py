from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import math
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Callable, Sequence

from lae_contracts import validate_repository
from lae_core import VERSION

from .client import ApiClient
from .config import api_url, deploy_credential, token_is_configured
from .errors import CliError
from .upload import LocalUpload, open_local_upload, put_upload_transfer
from .watch import watch_operation


_RESOURCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{2,127}$")
_RESOURCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
_APP_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
_TOKEN_LIKE = re.compile(r"^lae_dt_[A-Za-z0-9_-]{20,}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_REGION = re.compile(r"^(?:cn|global)$")
_IDEMPOTENCY_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_UPLOAD_FAILURE = re.compile(r"^LAE_UPLOAD_[A-Z0-9_]{1,80}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ENV_VALUE_BYTES = 64 * 1024
_MAX_SOURCE_SECRET_BYTES = 4096
_BILLING_INTERVALS = {"month": "monthly", "year": "yearly"}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse normally repeats the rejected argv, which can leak a secret
        # accidentally placed on the command line. Emit only a stable error.
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Command arguments are invalid.", 2
        )


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _write(value: dict[str, Any], output_format: str) -> None:
    if output_format in {"json", "ndjson"}:
        print(_canonical(value))
        return
    for key, item in value.items():
        rendered = _canonical(item) if isinstance(item, (dict, list)) else item
        print(f"{key}: {rendered}")


def _write_error(error: CliError, output_format: str) -> None:
    if output_format in {"json", "ndjson"}:
        print(_canonical(error.to_dict()), file=sys.stderr)
    else:
        print(f"error[{error.code}]: {error.message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="lae")
    parser.add_argument("--format", choices=("text", "json", "ndjson"), default="text")
    parser.add_argument("--token-stdin", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    commands.add_parser("doctor")
    commands.add_parser("contracts-validate")
    commands.add_parser("login")
    commands.add_parser("whoami")

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--app", required=True)
    inspect.add_argument("--repo", required=True)
    inspect.add_argument("--ref", required=True)
    inspect.add_argument("--connection-id")
    inspect.add_argument("--subdirectory", default="")
    inspect.add_argument("--region", default="cn")
    inspect.add_argument("--no-wait", action="store_true")
    inspect.add_argument("--timeout", type=float, default=0)
    inspect.add_argument("--poll", type=float, default=1)
    inspect.add_argument("--idempotency-key", required=True)

    inspect_file = commands.add_parser("inspect-file")
    inspect_file.add_argument("--app", required=True)
    inspect_file.add_argument("--file", required=True)
    inspect_file.add_argument("--region", default="cn")
    inspect_file.add_argument("--no-wait", action="store_true")
    inspect_file.add_argument("--timeout", type=float, default=0)
    inspect_file.add_argument("--poll", type=float, default=1)
    inspect_file.add_argument("--transfer-timeout", type=float, default=600)
    inspect_file.add_argument("--idempotency-prefix", required=True)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("--app", required=True)
    deploy.add_argument("--analysis", required=True)
    deploy.add_argument("--environment-version", type=int, required=True)
    deploy.add_argument("--wait", action="store_true")
    deploy.add_argument("--timeout", type=float, default=0)
    deploy.add_argument("--poll", type=float, default=1)
    deploy.add_argument("--idempotency-key", required=True)

    operation = commands.add_parser("operation").add_subparsers(
        dest="operation_command", required=True
    )
    operation_show = operation.add_parser("show")
    operation_show.add_argument("operation_id")
    operation_watch = operation.add_parser("watch")
    operation_watch.add_argument("operation_id")
    operation_watch.add_argument("--after", type=int, default=0)
    operation_watch.add_argument("--timeout", type=float, default=0)
    operation_watch.add_argument("--poll", type=float, default=1)
    operation_cancel = operation.add_parser("cancel")
    operation_cancel.add_argument("operation_id")
    operation_cancel.add_argument("--idempotency-key")

    apps = commands.add_parser("apps").add_subparsers(
        dest="apps_command", required=True
    )
    apps.add_parser("list")
    apps_create = apps.add_parser("create")
    apps_create.add_argument("--name", required=True)
    apps_create.add_argument("--slug", required=True)
    apps_create.add_argument("--idempotency-key", required=True)
    apps_show = apps.add_parser("show")
    apps_show.add_argument("app")
    apps_logs = apps.add_parser("logs")
    apps_logs.add_argument("app")
    apps_logs.add_argument("--service")
    apps_logs.add_argument("--tail", type=int, default=120)
    apps_metrics = apps.add_parser("metrics")
    apps_metrics.add_argument("app")
    apps_metrics.add_argument("--service")
    apps_metrics.add_argument("--window", type=int, default=3600)
    for action in ("check-update", "suspend", "resume", "restart"):
        action_parser = apps.add_parser(action)
        action_parser.add_argument("app")
        action_parser.add_argument("--idempotency-key", required=True)
    apps_rollback = apps.add_parser("rollback")
    apps_rollback.add_argument("app")
    apps_rollback.add_argument("--deployment")
    apps_rollback.add_argument("--idempotency-key", required=True)
    apps_delete = apps.add_parser("delete")
    apps_delete.add_argument("app")
    apps_delete.add_argument("--yes", action="store_true")
    apps_delete.add_argument("--idempotency-key", required=True)

    environment = commands.add_parser("env").add_subparsers(
        dest="env_command", required=True
    )
    environment_list = environment.add_parser("list")
    environment_list.add_argument("app")
    environment_set = environment.add_parser("set")
    environment_set.add_argument("app")
    environment_set.add_argument("name")
    environment_set.add_argument("--service", default="*")
    environment_set.add_argument("--expected-version", type=int, required=True)
    environment_set.add_argument("--value-stdin", action="store_true", required=True)
    environment_set.add_argument("--non-sensitive", action="store_true")
    environment_set.add_argument("--idempotency-key", required=True)
    environment_unset = environment.add_parser("unset")
    environment_unset.add_argument("app")
    environment_unset.add_argument("name")
    environment_unset.add_argument("--service", default="*")
    environment_unset.add_argument("--expected-version", type=int, required=True)
    environment_unset.add_argument("--idempotency-key", required=True)

    config = commands.add_parser("config").add_subparsers(
        dest="config_command", required=True
    )
    config_show = config.add_parser("show")
    config_show.add_argument("--app", required=True)
    config_show.add_argument("--analysis", required=True)

    source_connections = commands.add_parser("source-connections").add_subparsers(
        dest="source_connections_command", required=True
    )
    source_connections.add_parser("list")
    source_connection_create = source_connections.add_parser("create")
    source_connection_create.add_argument(
        "--provider", choices=("github", "gitea", "generic"), required=True
    )
    source_connection_create.add_argument("--name", required=True)
    source_connection_create.add_argument("--base-url", required=True)
    source_connection_create.add_argument("--username")
    source_connection_create.add_argument(
        "--secret-stdin", action="store_true", required=True
    )
    source_connection_create.add_argument("--idempotency-key", required=True)
    source_connection_rotate = source_connections.add_parser("rotate")
    source_connection_rotate.add_argument("connection_id")
    source_connection_rotate.add_argument("--username")
    source_connection_rotate.add_argument(
        "--secret-stdin", action="store_true", required=True
    )
    source_connection_rotate.add_argument("--idempotency-key", required=True)
    source_connection_revoke = source_connections.add_parser("revoke")
    source_connection_revoke.add_argument("connection_id")
    source_connection_revoke.add_argument("--idempotency-key", required=True)

    uploads = commands.add_parser("uploads").add_subparsers(
        dest="uploads_command", required=True
    )
    upload_create = uploads.add_parser("create")
    upload_create.add_argument("--app", required=True)
    upload_create.add_argument("--file", required=True)
    upload_create.add_argument("--transfer-timeout", type=float, default=600)
    upload_create.add_argument("--idempotency-key", required=True)
    upload_show = uploads.add_parser("show")
    upload_show.add_argument("upload_id")
    upload_complete = uploads.add_parser("complete")
    upload_complete.add_argument("upload_id")
    upload_complete.add_argument("--idempotency-key", required=True)
    upload_delete = uploads.add_parser("delete")
    upload_delete.add_argument("upload_id")
    upload_delete.add_argument("--idempotency-key", required=True)

    templates = commands.add_parser("templates").add_subparsers(
        dest="templates_command", required=True
    )
    templates.add_parser("list")
    template_launch = templates.add_parser("launch")
    template_launch.add_argument("template_id")
    template_launch.add_argument("--name", required=True)
    template_launch.add_argument("--slug", required=True)
    template_launch.add_argument("--region", default="cn")
    template_launch.add_argument("--wait", action="store_true")
    template_launch.add_argument("--timeout", type=float, default=0)
    template_launch.add_argument("--poll", type=float, default=1)
    template_launch.add_argument("--idempotency-key", required=True)

    plans = commands.add_parser("plans").add_subparsers(
        dest="plans_command", required=True
    )
    plans.add_parser("list")
    billing = commands.add_parser("billing").add_subparsers(
        dest="billing_command", required=True
    )
    checkout = billing.add_parser("checkout")
    checkout.add_argument("--plan", choices=("lite", "pro", "ultra"), required=True)
    checkout.add_argument(
        "--interval", choices=tuple(_BILLING_INTERVALS), required=True
    )
    checkout.add_argument("--idempotency-key", required=True)
    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    """Allow safe global options before or after nested subcommands."""

    remaining: list[str] = []
    output_format: str | None = None
    token_stdin = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--format":
            if index + 1 >= len(argv):
                remaining.append(item)
            else:
                output_format = argv[index + 1]
                index += 1
        elif item.startswith("--format="):
            output_format = item.split("=", 1)[1]
        elif item == "--token-stdin":
            token_stdin = True
        else:
            remaining.append(item)
        index += 1
    prefix: list[str] = []
    if output_format is not None:
        prefix.extend(["--format", output_format])
    if token_stdin:
        prefix.append("--token-stdin")
    return prefix + remaining


def _format_hint(argv: Sequence[str]) -> str:
    for index, item in enumerate(argv):
        if item.startswith("--format="):
            value = item.split("=", 1)[1]
            return value if value in {"text", "json", "ndjson"} else "text"
        if item == "--format" and index + 1 < len(argv):
            value = argv[index + 1]
            return value if value in {"text", "json", "ndjson"} else "text"
    return "text"


def _contains_argv_secret(argv: Sequence[str]) -> bool:
    forbidden_options = {"--token", "--deploy-token", "--password", "--secret"}
    return any(
        item in forbidden_options
        or any(item.startswith(option + "=") for option in forbidden_options)
        or _TOKEN_LIKE.fullmatch(item) is not None
        for item in argv
    )


def _resource_id(value: str, label: str) -> str:
    if not _RESOURCE_ID.fullmatch(value):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", f"{label} is invalid.", 2)
    return value


def _protocol_resource_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_ID.fullmatch(value):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", f"LAE returned an invalid {label}.", 9
        )
    return value


def _repository(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Repository URL is invalid.", 2
        ) from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    blocked_host = (
        is_ip
        or hostname == "localhost"
        or "." not in hostname
        or hostname.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home.arpa")
        )
    )
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or blocked_host
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
        or len(value) > 2048
    ):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID",
            "Repository must be a credential-free HTTPS URL.",
            2,
        )
    return value


def _git_ref(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or len(value) > 255
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)
        or any(char in "\\~^:?*[" for char in value)
        or value in {"@", ".", ".."}
        or value.startswith(("/", "."))
        or value.endswith((".", "/"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(part.endswith(".lock") for part in parts)
    ):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", "Git ref is invalid.", 2)
    return value


def _subdirectory(value: str) -> str:
    parts = value.split("/") if value else []
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or len(value) > 512
    ):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", "Subdirectory is invalid.", 2)
    return value


def _environment_key(name: str, service: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Environment variable name is invalid.", 2
        )
    if service != "*":
        _resource_name(service, "service")
    return f"{service}:{name}"


def _resource_name(value: str, label: str) -> str:
    if not _RESOURCE_NAME.fullmatch(value):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", f"{label} is invalid.", 2)
    return value


def _application_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 160
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", "Application name is invalid.", 2)
    return value


def _application_slug(value: str) -> str:
    if not _APP_SLUG.fullmatch(value):
        raise CliError("LAE_CLI_ARGUMENT_INVALID", "Application slug is invalid.", 2)
    return value


def _source_display_name(value: str) -> str:
    if (
        value != value.strip()
        or not 1 <= len(value) <= 120
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Source connection name is invalid.", 2
        )
    return value


def _source_username(value: str) -> str:
    if (
        not 1 <= len(value) <= 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Source connection username is invalid.", 2
        )
    return value


def _source_base_url(value: str, provider: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Source connection base URL is invalid.", 2
        ) from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    blocked_host = (
        is_ip
        or hostname == "localhost"
        or "." not in hostname
        or hostname.endswith(
            (
                ".localhost",
                ".local",
                ".internal",
                ".lan",
                ".home.arpa",
                ".test",
                ".example",
                ".invalid",
            )
        )
    )
    if (
        parsed.scheme != "https"
        or not hostname
        or blocked_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.startswith("//")
        or "\\" in parsed.path
        or len(value) > 2048
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID",
            "Source connection base URL must be credential-free HTTPS.",
            2,
        )
    path = parsed.path.rstrip("/")
    host = hostname if ":" not in hostname else f"[{hostname}]"
    authority = host if port in {None, 443} else f"{host}:{port}"
    result = urllib.parse.urlunsplit(("https", authority, path, "", ""))
    if provider == "github" and result != "https://github.com":
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID",
            "GitHub connections must use https://github.com.",
            2,
        )
    return result


def _read_source_secret(*, token_from_stdin: bool) -> str:
    if token_from_stdin:
        raise CliError(
            "LAE_CLI_STDIN_CONFLICT",
            "Use LAE_DEPLOY_TOKEN in the environment when a Git secret is read from stdin.",
            2,
        )
    if getattr(sys.stdin, "isatty", lambda: False)():
        value = getpass.getpass("Git credential: ")
    else:
        value = sys.stdin.read(_MAX_SOURCE_SECRET_BYTES + 2)
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise CliError(
            "LAE_CLI_SOURCE_SECRET_INVALID", "The Git secret is invalid.", 2
        ) from exc
    if (
        not 1 <= len(encoded) <= _MAX_SOURCE_SECRET_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CliError(
            "LAE_CLI_SOURCE_SECRET_INVALID", "The Git secret is invalid.", 2
        )
    return value


def _idempotency_keys(prefix: str) -> dict[str, str]:
    if not _IDEMPOTENCY_PREFIX.fullmatch(prefix):
        raise CliError(
            "LAE_CLI_IDEMPOTENCY_KEY_INVALID",
            "The idempotency prefix has an invalid format.",
            2,
        )
    return {
        "create": f"{prefix}-upload-create",
        "complete": f"{prefix}-upload-complete",
        "analysis": f"{prefix}-analysis-create",
    }


def _read_environment_value(*, token_from_stdin: bool) -> str:
    if token_from_stdin:
        raise CliError(
            "LAE_CLI_STDIN_CONFLICT",
            "Use LAE_DEPLOY_TOKEN in the environment when the value is read from stdin.",
            2,
        )
    if getattr(sys.stdin, "isatty", lambda: False)():
        value = getpass.getpass("Environment value: ")
    else:
        value = sys.stdin.read(_MAX_ENV_VALUE_BYTES + 1)
    if len(value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
        raise CliError(
            "LAE_CLI_ENV_VALUE_TOO_LARGE", "Environment value is too large.", 2
        )
    return value


def _operation_ref(value: dict[str, Any]) -> tuple[str, str | None]:
    raw_operation = value.get("operation")
    operation_id = (
        raw_operation.get("id") if isinstance(raw_operation, dict) else None
    )
    raw_analysis = value.get("analysis")
    analysis_id = raw_analysis.get("id") if isinstance(raw_analysis, dict) else None
    operation_id = _protocol_resource_id(operation_id, "operation ID")
    if analysis_id is not None:
        analysis_id = _protocol_resource_id(analysis_id, "analysis ID")
    return operation_id, analysis_id


def _event_output(output_format: str):
    def output(event: dict[str, Any]) -> None:
        if output_format == "ndjson":
            print(_canonical(event), flush=True)
        elif output_format == "text":
            print(
                f"{event['cursor']} {event.get('phase', '-')} "
                f"{event.get('status', '-')} {event.get('message', '')}",
                file=sys.stderr,
                flush=True,
            )

    return output


def _watch(
    client: ApiClient,
    operation_id: str,
    *,
    after: int = 0,
    timeout: float = 0,
    poll: float = 1,
    output_format: str,
):
    return watch_operation(
        client,
        operation_id,
        after=after,
        timeout_seconds=timeout,
        poll_seconds=poll,
        on_event=_event_output(output_format),
    )


def _redact_environment_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_environment_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    forbidden = {
        "authorization",
        "credential",
        "credentials",
        "deploytoken",
        "plaintext",
        "secret",
        "secretvalue",
        "token",
        "value",
        "valueciphertext",
    }
    return {
        key: _redact_environment_payload(item)
        for key, item in value.items()
        if key.casefold() not in forbidden
    }


def _redact_source_connection_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_source_connection_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    forbidden = {
        "authorization",
        "checksum",
        "ciphertext",
        "cookie",
        "credential",
        "nonce",
        "password",
        "secret",
        "token",
    }

    def keep(key: str) -> bool:
        folded = key.casefold()
        if folded == "credentialversion":
            return True
        return folded not in forbidden and not any(
            marker in folded
            for marker in ("password", "secret", "token", "credential")
        )

    return {
        key: _redact_source_connection_payload(item)
        for key, item in value.items()
        if keep(key)
    }


def _redact_upload_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_upload_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    forbidden = {
        "authorization",
        "bucket",
        "cookie",
        "credential",
        "headers",
        "objectkey",
        "secret",
        "signature",
        "signedurl",
        "token",
        "transfer",
        "uploadurl",
        "url",
    }

    def keep(key: str) -> bool:
        folded = key.casefold()
        return (
            folded not in forbidden
            and not folded.endswith("url")
            and "signature" not in folded
            and not any(
                marker in folded
                for marker in ("password", "secret", "token", "credential")
            )
        )

    return {
        key: _redact_upload_payload(item)
        for key, item in value.items()
        if keep(key)
    }


def _upload_record(
    value: dict[str, Any],
    *,
    expected_id: str | None = None,
    source: LocalUpload | None = None,
) -> tuple[str, str]:
    raw_upload = value.get("upload")
    if not isinstance(raw_upload, dict):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid upload.", 9
        )
    upload_id = _protocol_resource_id(raw_upload.get("id"), "upload ID")
    if expected_id is not None and upload_id != expected_id:
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an inconsistent upload.", 9
        )
    status = raw_upload.get("status")
    if status not in {
        "quarantine",
        "verifying",
        "scanning",
        "ready",
        "failed",
        "deleting",
        "deleted",
        "expired",
    }:
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid upload status.", 9
        )
    if source is not None and (
        raw_upload.get("filename") != source.filename
        or raw_upload.get("mediaType") != source.media_type
        or raw_upload.get("expectedBytes") != source.size_bytes
        or raw_upload.get("sha256") != source.sha256
    ):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned inconsistent upload facts.", 9
        )
    return upload_id, status


def _safe_configuration(value: Any) -> dict[str, Any]:
    raw = value.get("configuration") if isinstance(value, dict) else None
    if not isinstance(raw, dict):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned invalid configuration metadata.", 9
        )
    source_revision_id = raw.get("sourceRevisionId")
    if not isinstance(source_revision_id, str) or not _RESOURCE_ID.fullmatch(
        source_revision_id
    ):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned invalid configuration metadata.", 9
        )
    kind = raw.get("kind")
    service_keys = raw.get("serviceKeys")
    digest = raw.get("environmentSchemaDigest")
    environment = raw.get("environment")
    if (
        not isinstance(kind, str)
        or not 1 <= len(kind) <= 64
        or not isinstance(service_keys, list)
        or not 1 <= len(service_keys) <= 256
        or any(
            not isinstance(key, str) or not _RESOURCE_NAME.fullmatch(key)
            for key in service_keys
        )
        or len(service_keys) != len(set(service_keys))
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or not isinstance(environment, list)
        or len(environment) > 512
    ):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned invalid configuration metadata.", 9
        )
    known_services = set(service_keys)
    safe_environment: list[dict[str, Any]] = []
    for item in environment:
        if not isinstance(item, dict):
            raise CliError(
                "LAE_API_PROTOCOL_ERROR", "LAE returned invalid configuration metadata.", 9
            )
        name = item.get("name")
        item_services = item.get("serviceKeys")
        required = item.get("required")
        sensitive = item.get("sensitive")
        if (
            not isinstance(name, str)
            or not _ENV_NAME.fullmatch(name)
            or not isinstance(item_services, list)
            or not item_services
            or any(
                not isinstance(key, str) or key not in known_services
                for key in item_services
            )
            or len(item_services) != len(set(item_services))
            or not isinstance(required, bool)
            or not isinstance(sensitive, bool)
        ):
            raise CliError(
                "LAE_API_PROTOCOL_ERROR", "LAE returned invalid configuration metadata.", 9
            )
        safe_environment.append(
            {
                "name": name,
                "serviceKeys": item_services,
                "required": required,
                "sensitive": sensitive,
            }
        )
    return {
        "configuration": {
            "sourceRevisionId": source_revision_id,
            "kind": kind,
            "serviceKeys": service_keys,
            "environmentSchemaDigest": digest,
            "environment": safe_environment,
        }
    }


def _get_configuration(
    client: ApiClient, application_id: str, analysis_id: str
) -> dict[str, Any]:
    return _safe_configuration(
        client.get(
            f"/applications/{application_id}/analyses/{analysis_id}/configuration"
        )
    )


def _create_and_transfer_upload(
    client: ApiClient,
    *,
    application_id: str,
    file_path: str,
    idempotency_key: str,
    transfer_timeout: float,
) -> tuple[dict[str, Any], str]:
    if not math.isfinite(transfer_timeout) or not 1 <= transfer_timeout <= 3600:
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Transfer timeout is invalid.", 2
        )
    with open_local_upload(file_path) as source:
        created = client.post(
            "/uploads",
            source.request_body(application_id),
            idempotency_key=idempotency_key,
        )
        upload_id, _ = _upload_record(created, source=source)
        transfer = created.get("transfer")
        if not isinstance(transfer, dict):
            raise CliError(
                "LAE_UPLOAD_TRANSFER_UNAVAILABLE",
                "The upload reservation did not include a usable one-time transfer.",
                9,
                details={"uploadId": upload_id},
            )
        try:
            put_upload_transfer(
                transfer,
                source,
                timeout_seconds=transfer_timeout,
            )
        except CliError as error:
            raise CliError(
                error.code,
                error.message,
                error.exit_code,
                retryable=error.retryable,
                details={"uploadId": upload_id},
            ) from None
    safe = _redact_upload_payload(created)
    if not isinstance(safe, dict):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid upload.", 9
        )
    safe["transferCompleted"] = True
    return safe, upload_id


def _upload_status_output(output_format: str) -> Callable[[dict[str, Any]], None]:
    def output(event: dict[str, Any]) -> None:
        if output_format == "ndjson":
            print(_canonical(event), flush=True)
        elif output_format == "text":
            print(
                f"upload {event['uploadId']} {event['status']}",
                file=sys.stderr,
                flush=True,
            )

    return output


def _wait_upload_ready(
    client: ApiClient,
    upload_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (
        not math.isfinite(timeout_seconds)
        or not math.isfinite(poll_seconds)
        or timeout_seconds < 0
        or not 0 <= poll_seconds <= 60
    ):
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Upload watch arguments are invalid.", 2
        )
    started = monotonic()
    previous_status: str | None = None
    while True:
        response = client.get(f"/uploads/{upload_id}")
        _, status = _upload_record(response, expected_id=upload_id)
        if status != previous_status and on_status is not None:
            on_status(
                {"type": "upload.status", "uploadId": upload_id, "status": status}
            )
        previous_status = status
        if status == "ready":
            safe = _redact_upload_payload(response)
            if not isinstance(safe, dict):
                raise CliError(
                    "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid upload.", 9
                )
            return safe
        if status in {"failed", "deleting", "deleted", "expired"}:
            raw_upload = response.get("upload")
            failure_code = (
                raw_upload.get("failureCode") if isinstance(raw_upload, dict) else None
            )
            details: dict[str, Any] = {"uploadId": upload_id, "status": status}
            if isinstance(failure_code, str) and _UPLOAD_FAILURE.fullmatch(failure_code):
                details["failureCode"] = failure_code
            raise CliError(
                "LAE_UPLOAD_SCAN_FAILED",
                "The static artifact did not pass upload validation.",
                7,
                details=details,
            )
        if timeout_seconds and monotonic() - started >= timeout_seconds:
            raise CliError(
                "LAE_UPLOAD_WATCH_TIMEOUT",
                "The upload is still being validated; resume with its upload ID.",
                9,
                retryable=True,
                details={"uploadId": upload_id, "status": status},
            )
        sleeper(poll_seconds)


def _client(args: argparse.Namespace) -> ApiClient:
    return ApiClient(
        api_url(),
        deploy_credential(from_stdin=bool(args.token_stdin)),
    )


def _run(args: argparse.Namespace) -> int:
    if args.command == "version":
        _write({"component": "lae-cli", "version": VERSION}, args.format)
        return 0
    if args.command == "contracts-validate":
        _write(validate_repository(), args.format)
        return 0
    if args.command == "doctor":
        _write(
            {
                "apiUrl": api_url(),
                "authentication": "configured" if token_is_configured() else "missing",
                "component": "lae-cli",
                "contracts": validate_repository()["status"],
                "status": "ok",
                "version": VERSION,
            },
            args.format,
        )
        return 0

    if (
        args.command == "source-connections"
        and args.source_connections_command in {"create", "rotate"}
        and args.token_stdin
    ):
        raise CliError(
            "LAE_CLI_STDIN_CONFLICT",
            "Use LAE_DEPLOY_TOKEN in the environment when a Git secret is read from stdin.",
            2,
        )

    client = _client(args)
    if args.command == "login":
        result = client.post("/auth/token/verify")
        _write(
            {
                "authentication": "verified",
                "credentialStored": False,
                "principal": result,
                "next": "Keep using --token-stdin or LAE_DEPLOY_TOKEN; the CLI does not persist plaintext tokens.",
            },
            args.format,
        )
        return 0
    if args.command == "whoami":
        _write(client.get("/me"), args.format)
        return 0
    if args.command == "config":
        app = _resource_id(args.app, "application ID")
        analysis = _resource_id(args.analysis, "analysis ID")
        _write(_get_configuration(client, app, analysis), args.format)
        return 0
    if args.command == "source-connections":
        if args.source_connections_command == "list":
            _write(
                _redact_source_connection_payload(client.get("/source-connections")),
                args.format,
            )
            return 0
        if args.source_connections_command == "create":
            body: dict[str, Any] = {
                "provider": args.provider,
                "displayName": _source_display_name(args.name),
                "baseUrl": _source_base_url(args.base_url, args.provider),
                "secret": _read_source_secret(token_from_stdin=False),
            }
            if args.username is not None:
                body["username"] = _source_username(args.username)
            result = client.post(
                "/source-connections",
                body,
                idempotency_key=args.idempotency_key,
            )
            _write(_redact_source_connection_payload(result), args.format)
            return 0
        connection_id = _resource_id(
            args.connection_id, "source connection ID"
        )
        if args.source_connections_command == "rotate":
            body = {"secret": _read_source_secret(token_from_stdin=False)}
            if args.username is not None:
                body["username"] = _source_username(args.username)
            result = client.post(
                f"/source-connections/{connection_id}/rotate",
                body,
                idempotency_key=args.idempotency_key,
            )
            _write(_redact_source_connection_payload(result), args.format)
            return 0
        client.delete(
            f"/source-connections/{connection_id}",
            idempotency_key=args.idempotency_key,
        )
        _write(
            {"connectionId": connection_id, "revoked": True},
            args.format,
        )
        return 0
    if args.command == "uploads":
        if args.uploads_command == "create":
            app = _resource_id(args.app, "application ID")
            created, _ = _create_and_transfer_upload(
                client,
                application_id=app,
                file_path=args.file,
                idempotency_key=args.idempotency_key,
                transfer_timeout=args.transfer_timeout,
            )
            _write(created, args.format)
            return 0
        upload_id = _resource_id(args.upload_id, "upload ID")
        if args.uploads_command == "show":
            _write(
                _redact_upload_payload(client.get(f"/uploads/{upload_id}")),
                args.format,
            )
            return 0
        if args.uploads_command == "complete":
            result = client.post(
                f"/uploads/{upload_id}/complete",
                {},
                idempotency_key=args.idempotency_key,
            )
            _upload_record(result, expected_id=upload_id)
            _write(_redact_upload_payload(result), args.format)
            return 0
        result = client.delete(
            f"/uploads/{upload_id}",
            idempotency_key=args.idempotency_key,
        )
        _upload_record(result, expected_id=upload_id)
        _write(_redact_upload_payload(result), args.format)
        return 0
    if args.command == "templates":
        if args.templates_command == "list":
            _write(client.get("/templates"), args.format)
            return 0
        if not _REGION.fullmatch(args.region):
            raise CliError("LAE_CLI_ARGUMENT_INVALID", "Region is invalid.", 2)
        if (
            not math.isfinite(args.timeout)
            or not math.isfinite(args.poll)
            or args.timeout < 0
            or not 0 <= args.poll <= 60
        ):
            raise CliError(
                "LAE_CLI_ARGUMENT_INVALID", "Template watch arguments are invalid.", 2
            )
        template_id = _resource_name(args.template_id, "template ID")
        created = client.post(
            f"/templates/{template_id}/launch",
            {
                "name": _application_name(args.name),
                "slug": _application_slug(args.slug),
                "region": args.region,
            },
            idempotency_key=args.idempotency_key,
        )
        if not args.wait:
            _write(created, args.format)
            return 0
        operation_id, analysis_id = _operation_ref(created)
        watched = _watch(
            client,
            operation_id,
            timeout=args.timeout,
            poll=args.poll,
            output_format=args.format,
        )
        if analysis_id is None:
            result = watched.operation.get("result")
            analysis_id = result.get("analysisId") if isinstance(result, dict) else None
            analysis_id = _protocol_resource_id(analysis_id, "analysis ID")
        analysis = client.get(f"/analyses/{analysis_id}")
        application = created.get("application")
        application_id = (
            application.get("id") if isinstance(application, dict) else None
        )
        application_id = _protocol_resource_id(application_id, "application ID")
        result = {
            "template": created.get("template"),
            "application": application,
            "analysis": analysis,
            "operation": {
                "id": operation_id,
                "status": watched.status,
                "cursor": watched.cursor,
            },
        }
        if analysis.get("status") == "needs_configuration":
            result.update(_get_configuration(client, application_id, analysis_id))
        if args.format == "ndjson":
            print(_canonical({"type": "template.analysis.terminal", **result}), flush=True)
        else:
            _write(result, args.format)
        if watched.status != "succeeded":
            return 7
        if analysis.get("status") == "needs_configuration":
            return 4
        if analysis.get("status") == "not_deployable":
            return 5
        if analysis.get("status") == "diagnostic_failed":
            return 9
        return 0 if analysis.get("status") == "deployable" else 7
    if args.command == "inspect-file":
        if not _REGION.fullmatch(args.region):
            raise CliError("LAE_CLI_ARGUMENT_INVALID", "Region is invalid.", 2)
        if (
            not math.isfinite(args.timeout)
            or not math.isfinite(args.poll)
            or args.timeout < 0
            or not 0 <= args.poll <= 60
        ):
            raise CliError(
                "LAE_CLI_ARGUMENT_INVALID", "Upload watch arguments are invalid.", 2
            )
        if (
            not math.isfinite(args.transfer_timeout)
            or not 1 <= args.transfer_timeout <= 3600
        ):
            raise CliError(
                "LAE_CLI_ARGUMENT_INVALID", "Transfer timeout is invalid.", 2
            )
        app = _resource_id(args.app, "application ID")
        keys = _idempotency_keys(args.idempotency_prefix)
        _created_upload, upload_id = _create_and_transfer_upload(
            client,
            application_id=app,
            file_path=args.file,
            idempotency_key=keys["create"],
            transfer_timeout=args.transfer_timeout,
        )
        completed = client.post(
            f"/uploads/{upload_id}/complete",
            {},
            idempotency_key=keys["complete"],
        )
        _upload_record(completed, expected_id=upload_id)
        ready = _wait_upload_ready(
            client,
            upload_id,
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
            on_status=_upload_status_output(args.format),
        )
        created = client.post(
            "/analyses",
            {
                "applicationId": app,
                "source": {"type": "upload", "uploadId": upload_id},
                "intent": {"region": args.region, "publicProtocols": ["http"]},
            },
            idempotency_key=keys["analysis"],
        )
        operation_id, analysis_id = _operation_ref(created)
        if args.no_wait:
            result = {
                "upload": ready.get("upload"),
                "analysis": created.get("analysis"),
                "operation": created.get("operation"),
            }
            _write(result, args.format)
            return 0
        watched = _watch(
            client,
            operation_id,
            timeout=args.timeout,
            poll=args.poll,
            output_format=args.format,
        )
        if watched.status != "succeeded":
            _write(
                {
                    "uploadId": upload_id,
                    "operationId": operation_id,
                    "status": watched.status,
                    "cursor": watched.cursor,
                },
                args.format,
            )
            return 7
        if analysis_id is None:
            operation_result = watched.operation.get("result")
            analysis_id = (
                operation_result.get("analysisId")
                if isinstance(operation_result, dict)
                else None
            )
            analysis_id = _protocol_resource_id(analysis_id, "analysis ID")
        analysis = client.get(f"/analyses/{analysis_id}")
        analysis_status = analysis.get("status")
        result = {
            "upload": ready.get("upload"),
            "analysis": analysis,
            "operation": {
                "id": operation_id,
                "status": watched.status,
                "cursor": watched.cursor,
            },
        }
        if analysis_status == "needs_configuration":
            result.update(_get_configuration(client, app, analysis_id))
        if args.format == "ndjson":
            print(_canonical({"type": "analysis.terminal", **result}), flush=True)
        else:
            _write(result, args.format)
        if analysis_status == "needs_configuration":
            return 4
        if analysis_status == "not_deployable":
            return 5
        if analysis_status == "diagnostic_failed":
            return 9
        if analysis_status in {"failed", "expired"}:
            return 7
        if analysis_status != "deployable":
            raise CliError(
                "LAE_API_PROTOCOL_ERROR",
                "LAE returned an inconsistent terminal analysis.",
                9,
            )
        return 0
    if args.command == "inspect":
        if not _REGION.fullmatch(args.region):
            raise CliError("LAE_CLI_ARGUMENT_INVALID", "Region is invalid.", 2)
        app = _resource_id(args.app, "application ID")
        source: dict[str, Any] = {
            "type": "git",
            "repository": _repository(args.repo),
            "ref": _git_ref(args.ref),
            "subdirectory": _subdirectory(args.subdirectory),
        }
        if args.connection_id:
            source["connectionId"] = _resource_id(
                args.connection_id, "source connection ID"
            )
        created = client.post(
            "/analyses",
            {
                "applicationId": app,
                "source": source,
                "intent": {"region": args.region, "publicProtocols": ["http"]},
            },
            idempotency_key=args.idempotency_key,
        )
        operation_id, analysis_id = _operation_ref(created)
        if args.no_wait:
            _write(created, args.format)
            return 0
        watched = _watch(
            client,
            operation_id,
            timeout=args.timeout,
            poll=args.poll,
            output_format=args.format,
        )
        if watched.status != "succeeded":
            _write(
                {"operationId": operation_id, "status": watched.status}, args.format
            )
            return 7
        if analysis_id is None:
            result = watched.operation.get("result")
            analysis_id = result.get("analysisId") if isinstance(result, dict) else None
            analysis_id = _protocol_resource_id(analysis_id, "analysis ID")
        analysis = client.get(f"/analyses/{analysis_id}")
        analysis_status = analysis.get("status")
        result = {
            "analysis": analysis,
            "operation": {
                "id": operation_id,
                "status": watched.status,
                "cursor": watched.cursor,
            },
        }
        if analysis_status == "needs_configuration":
            result.update(_get_configuration(client, app, analysis_id))
        if args.format == "ndjson":
            print(_canonical({"type": "analysis.terminal", **result}), flush=True)
        else:
            _write(result, args.format)
        if analysis_status == "needs_configuration":
            return 4
        if analysis_status == "not_deployable":
            return 5
        if analysis_status == "diagnostic_failed":
            return 9
        if analysis_status in {"failed", "expired"}:
            return 7
        if analysis_status != "deployable":
            raise CliError(
                "LAE_API_PROTOCOL_ERROR",
                "LAE returned an inconsistent terminal analysis.",
                9,
            )
        return 0
    if args.command == "deploy":
        app = _resource_id(args.app, "application ID")
        analysis = _resource_id(args.analysis, "analysis ID")
        if args.environment_version < 0:
            raise CliError(
                "LAE_CLI_ARGUMENT_INVALID", "Environment version is invalid.", 2
            )
        body: dict[str, Any] = {
            "analysisId": analysis,
            "environmentVersion": args.environment_version,
        }
        created = client.post(
            f"/applications/{app}/deployments",
            body,
            idempotency_key=args.idempotency_key,
        )
        operation_id, _ = _operation_ref(created)
        if not args.wait:
            _write(created, args.format)
            return 0
        watched = _watch(
            client,
            operation_id,
            timeout=args.timeout,
            poll=args.poll,
            output_format=args.format,
        )
        summary = {
            "operationId": operation_id,
            "status": watched.status,
            "cursor": watched.cursor,
            "operation": watched.operation,
        }
        if args.format == "ndjson":
            print(_canonical({"type": "deployment.terminal", **summary}), flush=True)
        else:
            _write(summary, args.format)
        return 0 if watched.status == "succeeded" else 8
    if args.command == "operation":
        operation_id = _resource_id(args.operation_id, "operation ID")
        if args.operation_command == "show":
            _write(client.get(f"/operations/{operation_id}"), args.format)
            return 0
        if args.operation_command == "cancel":
            _write(
                client.post(
                    f"/operations/{operation_id}/cancel",
                    idempotency_key=args.idempotency_key,
                ),
                args.format,
            )
            return 0

        watched = watch_operation(
            client,
            operation_id,
            after=args.after,
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
            on_event=_event_output(args.format),
        )
        summary = {
            "operationId": watched.operation_id,
            "status": watched.status,
            "cursor": watched.cursor,
            "operation": watched.operation,
        }
        if args.format != "ndjson":
            _write(summary, args.format)
        else:
            print(_canonical({"type": "operation.terminal", **summary}), flush=True)
        return 0 if watched.status == "succeeded" else 7

    if args.command == "apps":
        if args.apps_command == "list":
            _write(client.get("/applications"), args.format)
            return 0
        if args.apps_command == "create":
            _write(
                client.post(
                    "/applications",
                    {
                        "name": _application_name(args.name),
                        "slug": _application_slug(args.slug),
                    },
                    idempotency_key=args.idempotency_key,
                ),
                args.format,
            )
            return 0
        app = _resource_id(args.app, "application ID")
        if args.apps_command == "show":
            _write(client.get(f"/applications/{app}"), args.format)
            return 0
        if args.apps_command == "logs":
            if not 1 <= args.tail <= 500:
                raise CliError(
                    "LAE_CLI_ARGUMENT_INVALID", "Log tail must be 1-500.", 2
                )
            query: dict[str, object] = {"tail": args.tail}
            if args.service:
                query["service"] = _resource_name(args.service, "service")
            _write(
                client.get(f"/applications/{app}/logs", query=query), args.format
            )
            return 0
        if args.apps_command == "metrics":
            if not 60 <= args.window <= 604800:
                raise CliError(
                    "LAE_CLI_ARGUMENT_INVALID",
                    "Metrics window must be 60-604800 seconds.",
                    2,
                )
            query = {"window": args.window}
            if args.service:
                query["service"] = _resource_name(args.service, "service")
            _write(
                client.get(f"/applications/{app}/metrics", query=query),
                args.format,
            )
            return 0
        action = args.apps_command
        if action == "delete" and not args.yes:
            raise CliError(
                "LAE_CLI_CONFIRMATION_REQUIRED",
                "Application deletion requires explicit --yes confirmation.",
                2,
            )
        body = (
            {"deploymentId": _resource_id(args.deployment, "deployment ID")}
            if action == "rollback" and args.deployment
            else {}
        )
        result = client.post(
            f"/applications/{app}/actions/{action}",
            body,
            idempotency_key=args.idempotency_key,
        )
        _write(result, args.format)
        return 0
    if args.command == "env":
        app = _resource_id(args.app, "application ID")
        if args.env_command == "list":
            _write(
                _redact_environment_payload(
                    client.get(f"/applications/{app}/environment")
                ),
                args.format,
            )
            return 0
        key = _environment_key(args.name, args.service)
        if args.expected_version < 0:
            raise CliError(
                "LAE_CLI_ARGUMENT_INVALID", "Environment version is invalid.", 2
            )
        if args.env_command == "set":
            value = _read_environment_value(token_from_stdin=bool(args.token_stdin))
            body = {
                "expectedVersion": args.expected_version,
                "set": {
                    key: {"value": value, "sensitive": not args.non_sensitive}
                },
                "unset": [],
            }
        else:
            body = {
                "expectedVersion": args.expected_version,
                "set": {},
                "unset": [key],
            }
        response = client.patch(
            f"/applications/{app}/environment",
            body,
            idempotency_key=args.idempotency_key,
        )
        _write(_redact_environment_payload(response), args.format)
        return 0
    if args.command == "plans":
        _write(client.get("/plans"), args.format)
        return 0
    if args.command == "billing":
        body: dict[str, Any] = {
            "plan": args.plan,
            "interval": _BILLING_INTERVALS[args.interval],
        }
        result = client.post(
            "/billing/checkout-sessions",
            body,
            idempotency_key=args.idempotency_key,
        )
        _write(result, args.format)
        return 0
    raise CliError("LAE_CLI_ARGUMENT_INVALID", "Unknown command.", 2)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if _contains_argv_secret(raw_arguments):
        error = CliError(
            "LAE_CLI_SECRET_IN_ARGUMENT",
            "Pass secrets through stdin or the environment, never command arguments.",
            2,
        )
        _write_error(error, _format_hint(raw_arguments))
        return error.exit_code
    try:
        args = _parser().parse_args(_normalize_global_options(raw_arguments))
    except CliError as error:
        _write_error(error, _format_hint(raw_arguments))
        return error.exit_code
    try:
        return _run(args)
    except CliError as error:
        _write_error(error, args.format)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
