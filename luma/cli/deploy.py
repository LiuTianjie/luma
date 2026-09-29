"""Luma CLI: validate, deploy and compose commands."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict
from ..compose import (
    compose_route_path,
    compose_stack_path,
    init_compose_sidecar,
    load_compose_deployment,
    render_compose_routes,
    storage_summary,
)
from ..config import LumaConfig, load_config
from ..errors import LumaError
from ..render import render_tailscale_route, render_tcp_route, route_path, stack_path
from ..service import load_service, slugify
from . import common
from .builds import _workflow_finish, _workflow_prepare
from .common import _add_context_warning, _context_warnings, _control_context, _deploy_env_secrets, _output_format, _print_deploy_step, _print_json, _print_success, _quiet, _run_with_wait_heartbeat, _validation_context


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    _require_nomad_engine(_service_engine(config, service, args))
    from ..nomad_render import render_nomad_job

    rendered = render_nomad_job(config, service, resolve_secrets=False)
    target = stack_path(config, service)
    rendered_route = None
    if service.exposure == "tailscale-relay":
        rendered_route = render_tailscale_route(config, service)
    elif service.exposure == "tcp-relay":
        rendered_route = render_tcp_route(config, service)
    route_target = route_path(config, service) if rendered_route else None
    if _output_format(args) != "text":
        _print_success(args, _render_result(service, target, rendered, route_target, rendered_route, artifact_kind="job"))
        return 0
    if _quiet(args):
        print(f"Service valid: {service.name}")
        return 0
    print(f"Service valid: {service.name}")
    for label, detail in (
        ("Region", service.region),
        ("Exposure", service.exposure),
        ("Domain", service.domain),
        ("Image", service.image),
    ):
        if detail:
            print(f"  {label + ':':<10}{detail}")
    print(f"Next: luma deploy {args.service} --dry-run shows the Nomad job; luma deploy {args.service} deploys it.")
    return 0


def _service_summary(service: Any) -> Dict[str, Any]:
    return {
        "source": str(service.source),
        "name": service.name,
        "slug": service.slug,
        "image": service.image,
        "region": service.region,
        "node": service.node,
        "exposure": service.exposure,
        "serviceKind": service.service_kind,
        "public": service.public,
        "domain": service.domain,
        "port": service.port,
        "replicas": service.replicas,
    }


def _render_result(
    service: Any,
    target: Path,
    rendered: str,
    route_target: Path | None = None,
    rendered_route: str | None = None,
    *,
    artifact_kind: str = "stack",
) -> Dict[str, Any]:
    artifacts: list[Dict[str, Any]] = [{"kind": artifact_kind, "path": str(target), "content": rendered}]
    if route_target and rendered_route:
        artifacts.append({"kind": "route", "path": str(route_target), "content": rendered_route})
    return {"service": _service_summary(service), "artifacts": artifacts}


def _service_storage_context_for_local(args: argparse.Namespace, service: Any) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    if not getattr(service, "storage", None):
        return None, None
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    return storage_classes, node_records


def cmd_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    _require_nomad_engine(_service_engine(config, service, args))
    output_format = _output_format(args)
    quiet = _quiet(args) or output_format != "text"

    if args.dry_run:
        rendered_route = None
        from ..nomad_render import render_nomad_job

        rendered = render_nomad_job(config, service, resolve_secrets=False)
        target = stack_path(config, service)
        if service.exposure == "tailscale-relay":
            rendered_route = render_tailscale_route(config, service)
        elif service.exposure == "tcp-relay":
            rendered_route = render_tcp_route(config, service)
        route_target = route_path(config, service) if rendered_route else None
        result = _render_result(service, target, rendered, route_target, rendered_route, artifact_kind="job")
        result["dryRun"] = True
        result.update(_validation_context(args))
        if output_format != "text":
            _print_success(args, result)
            return 0
        if quiet:
            print(f"Dry run: {service.name}")
            return 0
        print(f"Dry run: Nomad job for {service.name} (not submitted)")
        for warning in _context_warnings(args):
            print(f"[warn] {warning}")
        print(rendered)
        if rendered_route and route_target:
            print(f"Dry run: Traefik route for {service.name}")
            print(rendered_route)
        return 0

    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")

    if not quiet:
        print(f"[start] Load deploy context: {args.service}", flush=True)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    if not quiet:
        print(f"[ok] Control endpoint: {endpoint}", flush=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    _workflow_prepare(args, client, name=service.name)
    if not quiet:
        print(f"[start] Submit deploy: {service.name} -> {service.region}/{service.exposure}", flush=True)
    manifest_text = args.service.read_text(encoding="utf-8")
    env_secrets = _deploy_env_secrets(args.deploy_env_file, [manifest_text])
    streamed = False
    result: Dict[str, Any] | None = None
    try:
        for event in client.deploy_events(
            manifest=manifest_text,
            source_name=str(args.service),
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            env_secrets=env_secrets,
            timeout=args.timeout,
        ):
            status = str(event.get("status") or "")
            if output_format == "ndjson":
                _print_json({"type": "event", **event})
            if status in {"start", "ok", "fail"}:
                if not quiet:
                    _print_deploy_step(event)
                if status == "fail":
                    raise LumaError(str(event.get("message") or "deploy failed"))
            elif status == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise LumaError("control API stream ended without a deploy result")
                result = payload
            streamed = True
    except LumaError as exc:
        if "control API error 404" not in str(exc):
            raise

    if result is None:
        if streamed:
            raise LumaError("control API stream ended without a deploy result")
        if not quiet:
            print(f"[start] Waiting for control plane response (timeout {args.timeout}s)", flush=True)
        result = _run_with_wait_heartbeat(
            lambda: client.deploy(
                manifest=manifest_text,
                source_name=str(args.service),
                skip_dns=args.skip_dns,
                skip_orchestrator=args.skip_orchestrator,
                env_secrets=env_secrets,
                timeout=args.timeout,
            ),
            timeout=args.timeout,
            emit=not quiet,
        )
        for step in result.get("steps") or []:
            if isinstance(step, dict):
                if output_format == "ndjson":
                    _print_json({"type": "event", **step})
                elif not quiet:
                    _print_deploy_step(step)
    _workflow_finish(args, client, result, name=service.name)
    if output_format != "text":
        _print_success(args, result)
        return 0
    print(f"[ok] Deploy finished: {result.get('service', service.name)}")
    if result.get("image"):
        image = result["image"]
        if image.get("fallback"):
            print(f"Image fallback: {image.get('requested')} -> {image.get('selected')}")
        else:
            print(f"Image ready: {image.get('selected')}")
    if result.get("dns"):
        print(result["dns"])
    orchestrator_message = result.get("orchestrator")
    if orchestrator_message:
        print(orchestrator_message)
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    if args.compose_command == "init":
        init_compose_sidecar(args.compose, args.output)
        print(f"Created {args.output}")
        print(f"Next: luma compose validate {args.output}")
        return 0
    if args.compose_command == "validate":
        return cmd_compose_validate(args)
    if args.compose_command == "deploy":
        return cmd_compose_deploy(args)
    raise LumaError(f"unknown compose command: {args.compose_command}")


def cmd_compose_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    deployment = load_compose_deployment(
        args.sidecar,
        storage_classes=_control_storage_classes_for_local(args),
        allow_build_services=bool(args.import_mode),
    )
    if args.import_mode:
        _inject_import_mode_placeholder_images(deployment)
    node_records = _control_node_records_for_local(args)
    _require_nomad_engine(_compose_engine(config, args))
    from ..nomad_render import render_compose_job

    stack = render_compose_job(
        config,
        deployment,
        resolve_secrets=False,
        node_records=node_records,
    )
    routes = render_compose_routes(config, deployment)
    result = {
        "deployment": _compose_summary(config, deployment, stack, routes, artifact_kind="job"),
        "storage": storage_summary(deployment, node_records=node_records),
        **_validation_context(args),
    }
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(f"Compose deployment valid: {deployment.name}")
    for warning in _context_warnings(args):
        print(f"[warn] {warning}")
    for warning in deployment.warnings:
        print(f"[warn] {warning}")
    return 0


def _inject_import_mode_placeholder_images(deployment: Any) -> None:
    services = deployment.compose.get("services") if isinstance(deployment.compose.get("services"), dict) else {}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        if service.get("image"):
            continue
        if service.get("build") is not None:
            service["image"] = f"luma-import-preview/{slugify(str(service_name))}:latest"


def cmd_compose_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    deployment = load_compose_deployment(args.sidecar, storage_classes=storage_classes)
    _require_nomad_engine(_compose_engine(config, args))
    output_format = _output_format(args)
    quiet = _quiet(args) or output_format != "text"
    if args.dry_run:
        from ..nomad_render import render_compose_job

        stack = render_compose_job(
            config,
            deployment,
            resolve_secrets=False,
            node_records=node_records,
        )
        routes = render_compose_routes(config, deployment)
        result = {
            "deployment": _compose_summary(config, deployment, stack, routes, artifact_kind="job"),
            "storage": storage_summary(deployment, node_records=node_records),
            "dryRun": True,
        }
        if output_format != "text":
            _print_success(args, result)
            return 0
        print(f"Dry run: Nomad job for {deployment.name} (not submitted)")
        print(stack)
        for service_name, route_text in routes.items():
            print(f"Dry run: Traefik route for {deployment.name}/{service_name}")
            print(route_text)
        for warning in deployment.warnings:
            print(f"[warn] {warning}")
        return 0
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    if not quiet:
        print(f"[start] Load compose deploy context: {args.sidecar}", flush=True)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    if not quiet:
        print(f"[ok] Control endpoint: {endpoint}", flush=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    _workflow_prepare(args, client, name=deployment.name)
    manifest_text, compose_text = _compose_request_text(args.sidecar, deployment)
    env_secrets = _deploy_env_secrets(args.deploy_env_file, [manifest_text, compose_text])
    streamed = False
    result: Dict[str, Any] | None = None
    try:
        for event in client.deploy_compose_events(
            manifest=manifest_text,
            compose_content=compose_text,
            source_name=str(args.sidecar),
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            env_secrets=env_secrets,
            timeout=args.timeout,
        ):
            status = str(event.get("status") or "")
            if output_format == "ndjson":
                _print_json({"type": "event", **event})
            if status in {"start", "ok", "fail"}:
                if not quiet:
                    _print_deploy_step(event)
                if status == "fail":
                    raise LumaError(str(event.get("message") or "compose deploy failed"))
            elif status == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise LumaError("control API stream ended without a compose deploy result")
                result = payload
            streamed = True
    except LumaError as exc:
        if "control API error 404" not in str(exc):
            raise
    if result is None:
        # If the stream produced events but ended without a `done` result, the
        # deploy already ran (or is running) on the manager — re-issuing it via
        # the non-streaming endpoint would silently deploy a SECOND time. Only
        # fall back when nothing streamed (old Control without the stream
        # endpoint -> 404 swallowed above). Mirrors cmd_deploy's native guard.
        if streamed:
            raise LumaError("control API stream ended without a compose deploy result")
        result = _run_with_wait_heartbeat(
            lambda: client.deploy_compose(
                manifest=manifest_text,
                compose_content=compose_text,
                source_name=str(args.sidecar),
                skip_dns=args.skip_dns,
                skip_orchestrator=args.skip_orchestrator,
                env_secrets=env_secrets,
                timeout=args.timeout,
            ),
            timeout=args.timeout,
            emit=not quiet,
        )
        for step in result.get("steps") or []:
            if isinstance(step, dict) and not quiet:
                _print_deploy_step(step)
    _workflow_finish(args, client, result, name=deployment.name)
    if output_format != "text":
        _print_success(args, result)
        return 0
    print(f"[ok] Compose deploy finished: {result.get('deployment', deployment.name)}")
    for warning in (result.get("storage") or {}).get("warnings") or []:
        print(f"[warn] {warning}")
    return 0


def _compose_summary(config: LumaConfig, deployment: Any, stack: str, routes: Dict[str, str], *, artifact_kind: str = "job") -> Dict[str, Any]:
    artifacts = [{"kind": artifact_kind, "path": str(compose_stack_path(config, deployment)), "content": stack}]
    for service_name, route_text in routes.items():
        artifacts.append({"kind": "route", "path": str(compose_route_path(config, deployment, service_name)), "content": route_text})
    return {
        "source": str(deployment.source),
        "name": deployment.name,
        "slug": deployment.slug,
        "compose": str(deployment.compose_path),
        "services": sorted(str(name) for name in deployment.compose.get("services", {}).keys()),
        "artifacts": artifacts,
        "warnings": deployment.warnings,
    }


def _compose_engine(config: LumaConfig, args: argparse.Namespace) -> str:
    return str(getattr(args, "engine", None) or config.defaults.get("engine") or "nomad")


def _service_engine(config: LumaConfig, service: Any, args: argparse.Namespace) -> str:
    return str(getattr(args, "engine", None) or getattr(service, "engine", "") or config.defaults.get("engine") or "nomad")


def _require_nomad_engine(engine: str) -> str:
    value = str(engine or "nomad").strip() or "nomad"
    if value != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    return value


def _compose_request_text(sidecar: Path, deployment: Any) -> tuple[str, str]:
    return sidecar.read_text(encoding="utf-8"), deployment.compose_path.read_text(encoding="utf-8")


def _control_storage_classes_for_local(args: argparse.Namespace, *, required: bool = False) -> Dict[str, Any] | None:
    try:
        setattr(args, "_luma_context_used", True)
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).list_storage()
    except LumaError as exc:
        if required:
            raise
        _add_context_warning(args, f"Storage classes were not loaded from Luma Control; validation is using local sidecar data only ({exc})")
        return None
    if not isinstance(result, dict):
        _add_context_warning(args, "Storage classes were not loaded from Luma Control; control API returned an invalid response")
        return None
    storage: Dict[str, Any] = {}
    for item in result.get("storageClasses") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        storage[name] = {key: value for key, value in item.items() if key != "name" and value not in ("", [], None)}
    return storage


def _control_node_records_for_local(args: argparse.Namespace, *, required: bool = False) -> Dict[str, Any] | None:
    try:
        setattr(args, "_luma_context_used", True)
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
    except LumaError as exc:
        if required:
            raise
        _add_context_warning(args, f"Node records were not loaded from Luma Control; placement/storage reachability checks are degraded ({exc})")
        return None
    node_items = ((result.get("nodes") or {}).get("items") if isinstance(result, dict) else []) or []
    records: Dict[str, Any] = {}
    for item in node_items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        records[str(item["name"])] = dict(item)
    return records
