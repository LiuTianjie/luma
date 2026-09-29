"""Luma CLI: ``luma app`` commands for deployed applications."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict
from ..errors import LumaError
from . import common
from .common import _control_context, _format_epoch, _history_query, _output_format, _print_deploy_step, _print_history_expiry, _print_history_page, _print_json, _print_success, _print_table, _quiet, _run_with_wait_heartbeat


def cmd_app(args: argparse.Namespace) -> int:
    handlers = {
        "list": cmd_app_read,
        "show": cmd_app_read,
        "logs": cmd_app_read,
        "events": cmd_app_read,
        "history": cmd_app_history,
        "versions": cmd_app_versions,
        "rollback": cmd_app_rollback,
        "restart": cmd_app_restart,
        "remove": cmd_app_remove,
    }
    return handlers[args.app_command](args)


def _service_log_text(event: Dict[str, Any]) -> str:
    """Render each observed fragment separately so interleaved sources stay clear.

    Text output is a labelled diagnostic view, not a byte-exact log export.
    Continuation markers avoid joining fragments across sources or reconnects.
    """
    source = "/".join(str(event.get(key) or "?") for key in ("allocationId", "task", "stream"))
    if source == "?/?/?":
        source = "source unavailable"
    markers = "[continued] " if event.get("continued") else ""
    if event.get("partial"):
        markers += "[partial] "
    return f"[{source}] {markers}{event.get('line') or ''}"


def cmd_app_history(args: argparse.Namespace) -> int:
    record_id = str(args.record_id or "")
    if record_id and not args.kind:
        raise LumaError("--id requires --kind build or deployment")
    if record_id and any(getattr(args, key, "") for key in ("name", "status", "source", "since", "until")):
        raise LumaError("history detail accepts --id, --kind, --limit and --cursor; omit list filters")
    query = _history_query(args, detail=bool(record_id))
    if args.name:
        query["app"] = args.name
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    result = client.history_detail(args.kind, record_id, query=query) if record_id else client.history(query=query)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    if record_id:
        item = result.get("item") or {}
        print(f"{item.get('title') or record_id}: {item.get('status') or 'unknown'}")
        _print_history_expiry(item)
        for event in result.get("events") or []:
            if isinstance(event, dict):
                _print_deploy_step(event)
    else:
        items = result.get("items") or []
        if items:
            _print_table(["ID", "KIND", "APPLICATION", "SOURCE", "STATUS", "CREATED"], [
                [str(item.get(key) or "-") for key in ("id", "kind", "application", "source", "status")]
                + [_format_epoch(int(item.get("createdAt") or 0))] for item in items
            ])
        else:
            print("No matching history records.")
    _print_history_page(result.get("page"))
    return 0


def cmd_app_read(args: argparse.Namespace) -> int:
    operation = args.app_command
    if operation == "logs":
        if not 1 <= args.tail <= 500:
            raise LumaError("--tail must be between 1 and 500")
        if args.follow and _output_format(args) == "json":
            raise LumaError("--follow requires --format text or ndjson; omit --follow for a JSON snapshot")
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if operation in {"list", "show"}:
        snapshot = client.dashboard()
        services = [item for item in snapshot.get("services", []) if isinstance(item, dict)]
        if operation == "list":
            if args.region:
                services = [item for item in services if item.get("region") == args.region]
            if args.stack:
                services = [item for item in services if item.get("stack") == args.stack]
        else:
            services = [item for item in services if args.name in {
                item.get("fullName"), item.get("stack"), item.get("name")
            }]
            if not services:
                raise LumaError(f"application or service not found: {args.name}; run 'luma app list'")
        result = {"services": services, "updatedAt": snapshot.get("updatedAt")}
        if _output_format(args) != "text":
            _print_success(args, result)
        elif operation == "show":
            _print_services(services)
        elif not services:
            print("No matching services.")
        else:
            _print_table(["SERVICE", "STACK", "REGION", "STATUS", "REPLICAS"], [
                [str(item.get("fullName") or item.get("name") or "-"),
                 str(item.get("stack") or "-"), str(item.get("region") or "-"),
                 str(item.get("status") or "unknown"), f"{item.get('running', 0)}/{item.get('desired', 0)}"]
                for item in services
            ])
        return 0
    if operation == "events":
        result = client.service_events(args.name)
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            print(f"{result.get('service') or args.name}: {result.get('status') or 'unknown'}"
                  f" (allocation {result.get('allocId') or 'none'})")
            for event in result.get("events") or []:
                print(f"[{event.get('type') or event.get('source') or 'event'}] {event.get('message') or ''}")
            if not result.get("events"):
                print("No recent runtime events.")
        return 0
    if args.follow:
        try:
            for event in client.service_log_events(args.name, tail=args.tail, allocation=args.allocation, previous=args.previous):
                if _output_format(args) == "ndjson":
                    _print_json(event)
                    sys.stdout.flush()
                elif "line" in event:
                    print(_service_log_text(event), flush=True)
                elif event.get("status") in {"warning", "error", "reconnecting"}:
                    print(str(event.get("message") or event.get("error") or "Log stream warning"), file=sys.stderr)
                if event.get("status") == "error" or event.get("type") == "error":
                    return 1
        except KeyboardInterrupt:
            return 0
        return 0
    result = client.service_logs(args.name, tail=args.tail, allocation=args.allocation, previous=args.previous)
    if _output_format(args) != "text":
        _print_success(args, result)
    else:
        entries = result.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and "line" in entry:
                    print(_service_log_text(entry))
        else:
            for line in result.get("logs") or []:
                print(_service_log_text({"line": line}))
        for warning in result.get("warnings") or []:
            print(f"Warning: {warning}", file=sys.stderr)
    return 0


def cmd_app_remove(args: argparse.Namespace) -> int:
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    service_name = str(args.service).strip()
    if not service_name:
        raise LumaError("service name is required")
    if service_name.endswith((".yaml", ".yml")) or any(part in service_name for part in ("/", "\\")):
        raise LumaError("app remove expects an application name, not a manifest path")
    emit = _output_format(args) == "text" and not _quiet(args)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if emit:
        print(f"[start] Submit remove: {service_name}", flush=True)
    result = _run_with_wait_heartbeat(
        lambda: client.remove_service(
            name=service_name,
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            delete_storage=args.delete_storage,
            dry_run=args.dry_run,
            timeout=args.timeout,
        ),
        timeout=args.timeout,
        emit=_output_format(args) == "text" and not _quiet(args),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    for step in result.get("steps") or []:
        if emit and isinstance(step, dict):
            _print_deploy_step(step)
    action = "Remove dry run finished" if result.get("dryRun") else "Remove finished"
    print(f"[ok] {action}: {result.get('service') or result.get('deployment') or service_name}")
    if result.get("dns"):
        print(result["dns"])
    orchestrator_message = result.get("orchestrator")
    if orchestrator_message:
        print(orchestrator_message)
    if result.get("generatedFiles"):
        print(result["generatedFiles"])
    if result.get("storageCleanup"):
        print(result["storageCleanup"])
    return 0


def cmd_app_restart(args: argparse.Namespace) -> int:
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    stack = str(args.stack).strip()
    service_name = str(args.service or "").strip()
    if not stack:
        raise LumaError("application name is required")
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    mode = str(args.mode or "").strip()
    result = _run_with_wait_heartbeat(
        lambda: client.restart_application(stack=stack, service=service_name, mode=mode, timeout=args.timeout),
        timeout=args.timeout,
        emit=_output_format(args) == "text" and not _quiet(args),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    actual_mode = str(result.get("mode") or mode or ("task" if service_name else "recreate"))
    suffix = f"/{service_name}" if service_name else ""
    print(f"[ok] Restart finished: {stack}{suffix} ({actual_mode})")
    for item in result.get("restarted") or []:
        if isinstance(item, dict):
            task = str(item.get("task") or "*")
            alloc = str(item.get("allocId") or "")
            print(f"  {task} {alloc}".rstrip())
    return 0


def cmd_app_rollback(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    result = client.rollback_service(name=args.name, version=args.to_version)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(result.get("message") or f"Rolled back {args.name}")
    return 0


def cmd_app_versions(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    result = client.service_history(name=args.name)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    versions = result.get("versions") or []
    if not versions:
        print(f"No versions recorded for {args.name}")
        return 0
    rows = [
        [
            str(v.get("version")),
            "stable" if v.get("stable") else "-",
            str(v.get("image") or "-"),
        ]
        for v in versions
    ]
    _print_table(["version", "stable", "image"], rows)
    return 0


SHOW_FIELDS = (
    ("stack", "Application"), ("status", "Status"), ("region", "Region"), ("node", "Node"),
    ("exposure", "Exposure"), ("domain", "Domain"), ("image", "Image"),
)


def _print_services(services: list[Dict[str, Any]]) -> None:
    for index, item in enumerate(services):
        if index:
            print()
        print(str(item.get("fullName") or item.get("name") or "-"))
        for key, label in SHOW_FIELDS:
            value = item.get(key)
            if value not in (None, "", []):
                print(f"  {label + ':':<13}{value}")
        print(f"  {'Replicas:':<13}{item.get('running', 0)}/{item.get('desired', 0)}")
