"""Luma CLI: common commands."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, TypeVar
from ..control.client import ControlClient  # noqa: F401  (used as common.ControlClient)
from ..control.context import load_context, load_current_context
from ..envfile import parse_env_file
from ..errors import LumaError


T = TypeVar("T")
OUTPUT_FORMATS = ("text", "json", "ndjson")
UPDATE_REEXEC_ENV = "LUMA_UPDATE_REEXECED"
UPDATE_DETACHED_ENV = "LUMA_UPDATE_DETACHED"


def _history_query(args: argparse.Namespace, *, detail: bool = False) -> Dict[str, Any]:
    limit = int(getattr(args, "limit", 50))
    if not 1 <= limit <= 100:
        raise LumaError("--limit must be between 1 and 100")
    query: Dict[str, Any] = {"limit": limit}
    cursor = str(getattr(args, "cursor", "") or "")
    if cursor:
        query["cursor"] = cursor
    if not detail:
        for key in ("app", "status", "source", "kind", "since", "until"):
            value = str(getattr(args, key, "") or "")
            if value:
                query[key] = value
    return query


def _print_history_expiry(record: Any) -> None:
    if not isinstance(record, dict) or not record.get("detailsExpiredAt"):
        return
    when = _format_epoch(int(record["detailsExpiredAt"]))
    days = int(record.get("detailsRetentionDays") or 0)
    policy = f" ({days}-day retention)" if days else ""
    print(f"Step log expired at {when}{policy}; the summary remains available.")


def _print_history_page(page: Any) -> None:
    if isinstance(page, dict) and page.get("hasMore") and page.get("nextCursor"):
        print(f"More records available. Continue with --cursor {page['nextCursor']}", file=sys.stderr)


def _configured_label(value: bool) -> str:
    return "configured" if value else "missing"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _status_value(value: object) -> str:
    text = str(value or "").strip()
    return text or "-"


def _print_key_values(title: str, rows: list[tuple[str, str]]) -> None:
    print()
    print(title)
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label.ljust(width)}  {value}")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  " + "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print("  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _output_format(args: argparse.Namespace) -> str:
    return str(getattr(args, "format", "text") or "text")


def _quiet(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "quiet", False))


def _context_warnings(args: argparse.Namespace) -> list[str]:
    warnings = getattr(args, "_luma_context_warnings", None)
    if not isinstance(warnings, list):
        warnings = []
        setattr(args, "_luma_context_warnings", warnings)
    return warnings


def _add_context_warning(args: argparse.Namespace, message: str) -> None:
    warnings = _context_warnings(args)
    if message not in warnings:
        warnings.append(message)


def _validation_context(args: argparse.Namespace) -> Dict[str, Any]:
    warnings = _context_warnings(args)
    context_used = bool(getattr(args, "_luma_context_used", False))
    return {
        "validationMode": "degraded" if warnings else ("cluster-aware" if context_used else "local"),
        "warnings": list(warnings),
    }


def _command_name(args: argparse.Namespace) -> str:
    command = str(getattr(args, "command", ""))
    if command in {"service", "context"}:
        return f"{command} {getattr(args, command + '_command', '')}".strip()
    if command == "secret":
        return f"secret {getattr(args, 'secret_command', '')}".strip()
    if command == "registry":
        return f"registry {getattr(args, 'registry_command', '')}".strip()
    if command == "git-provider":
        return f"git-provider {getattr(args, 'git_provider_command', '')}".strip()
    if command == "region":
        return f"region {getattr(args, 'region_command', '')}".strip()
    return command


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


def _print_json(payload: Dict[str, Any], *, file: Any = None) -> None:
    print(_json_dumps(payload), file=file, flush=True)


def _success_payload(args: argparse.Namespace, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "command": _command_name(args), "result": result}


def _error_payload(exc: LumaError, *, code: str = "luma_error") -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": str(exc)}}


def _print_success(args: argparse.Namespace, result: Dict[str, Any]) -> None:
    output_format = _output_format(args)
    if output_format == "json":
        _print_json(_success_payload(args, result))
    elif output_format == "ndjson":
        _print_json({"type": "result", "ok": True, "result": result})


def _print_structured_error(args: argparse.Namespace, exc: LumaError) -> bool:
    output_format = _output_format(args)
    if output_format == "json":
        _print_json(_error_payload(exc), file=sys.stderr)
        return True
    if output_format == "ndjson":
        _print_json({"type": "error", **_error_payload(exc)}, file=sys.stderr)
        return True
    return False


def _env_text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _arg_text(args: argparse.Namespace, name: str) -> str | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise LumaError(f"{name} must be true or false")


def _control_context(args: argparse.Namespace, *, require_token: bool) -> tuple[str, str, bool, str | None]:
    context_name = _arg_text(args, "control_context") or _env_text("LUMA_CONTROL_CONTEXT")
    def selected_context() -> Dict[str, Any]:
        return load_context(context_name) if context_name else load_current_context()

    control_url = _arg_text(args, "control_url") or _env_text("LUMA_CONTROL_URL")
    token = _arg_text(args, "token") or _env_text("LUMA_DEPLOY_TOKEN")
    resolve_ip = _arg_text(args, "resolve_ip") or _env_text("LUMA_RESOLVE_IP")
    env_insecure = _env_bool("LUMA_INSECURE")
    cli_insecure = bool(getattr(args, "insecure", False))
    insecure: bool | None = True if cli_insecure else env_insecure

    has_stateless_context = any(
        value is not None
        for value in (control_url, token, resolve_ip, env_insecure)
    ) or cli_insecure

    if not has_stateless_context:
        context = selected_context()
        return (
            str(context["endpoint"]),
            str(context["token"]),
            bool(context.get("insecure", False)),
            str(context["resolveIp"]) if context.get("resolveIp") else None,
        )

    context: Dict[str, Any] = {}
    if context_name or not control_url or (require_token and not token) or insecure is None:
        try:
            context = selected_context()
        except LumaError:
            if context_name:
                raise
            context = {}

    # A URL override must not silently send saved credentials or reuse a TLS/IP
    # override belonging to another cluster. Explicit CLI/environment values win.
    if control_url and str(context.get("endpoint") or "").rstrip("/") != control_url.rstrip("/"):
        context = {}

    if not control_url:
        control_url = str(context["endpoint"]) if context.get("endpoint") else None
    if not token:
        token = str(context["token"]) if context.get("token") else None
    if insecure is None:
        insecure = bool(context.get("insecure", False))
    if not resolve_ip and context.get("resolveIp"):
        resolve_ip = str(context["resolveIp"])

    if not control_url:
        raise LumaError("control URL is required; pass --control-url, set LUMA_CONTROL_URL, or run luma login")
    if require_token and not token:
        raise LumaError("management token is required; pass --token, set LUMA_DEPLOY_TOKEN, or run luma login")
    if not token:
        token = "health"
    return (
        control_url,
        token,
        bool(insecure),
        resolve_ip,
    )


def log(message: str) -> None:
    print(message, flush=True)


def _run_with_wait_heartbeat(action: Callable[[], T], *, timeout: int, interval: int = 30, emit: bool = True) -> T:
    if not emit:
        return action()
    done = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not done.wait(interval):
            elapsed = int(time.monotonic() - started)
            print(f"[wait] Control plane still working ({elapsed}s elapsed, timeout {timeout}s)", flush=True)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        done.set()
        thread.join(timeout=0.2)


def _print_deploy_step(step: Dict[str, Any]) -> None:
    status = str(step.get("status") or "ok")
    name = str(step.get("name") or "step")
    message = step.get("message")
    suffix = f": {message}" if message else ""
    print(f"[{status}] {name}{suffix}", flush=True)


def _format_epoch(value: int) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def prompt(default: str, label: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _deploy_env_secrets(path: Path | None, texts: list[str]) -> Dict[str, str] | None:
    if not path:
        return None
    if not path.exists():
        raise LumaError(f"deployment env file not found: {path}")
    values = parse_env_file(path)
    referenced: set[str] = set()
    for text in texts:
        referenced.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
    return {key: value for key, value in values.items() if key in referenced}


def _import_env_secrets(path: Path | None) -> Dict[str, str] | None:
    if not path:
        return None
    if not path.exists():
        raise LumaError(f"deployment env file not found: {path}")
    return parse_env_file(path)
