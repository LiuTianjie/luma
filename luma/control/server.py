from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict

import yaml

from ..assets import asset_path
from ..cloudflare import delete_dns, sync_dns
from ..compose import (
    ComposeDeploymentSpec,
    compose_public_services,
    compose_route_path,
    compose_stack_path,
    load_compose_deployment,
    render_compose_routes,
    render_compose_stack,
    storage_summary,
)
from ..config import load_config
from ..errors import LumaError
from ..io import load_yaml
from ..portainer import deploy_with_portainer, remove_luma_portainer_registry, remove_stack, upsert_stack
from ..registry import (
    docker_registry_auth_header,
    image_uses_mutable_latest_tag,
    normalize_registry_host,
    registry_auth_for_image,
    registry_auth_matches_image,
)
from ..render import render_stack, render_tailscale_route, route_path, stack_path
from ..service import VALID_REGIONS, ServiceSpec, load_service
from ..storage import managed_storage_stacks
from .. import __version__
from .state import init_state, load_state, require_token, save_state


def bearer_token(headers: Any) -> str:
    value = headers.get("Authorization") or ""
    prefix = "Bearer "
    if not value.startswith(prefix):
        raise LumaError("missing bearer token")
    return value[len(prefix):].strip()


def handle_login_verify(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    return {"clusterId": state["clusterId"], "endpoint": state.get("domain", "")}


def require_control_node_token(state: Dict[str, Any], token: str) -> None:
    try:
        require_token(state, token, token_type="deploy")
        return
    except LumaError:
        pass
    require_token(state, token, token_type="join")


def handle_control_status(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    dns = config.dns
    dns_provider = str(dns.get("provider") or "not configured")
    token_env = str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    zone_id = os.environ.get(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID"))) or dns.get("zoneId")
    dns_target = dns.get("edgeTarget") or config.default_dns_target()
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    dns_token_configured = bool(os.environ.get(token_env) or token_env in secrets)
    dns_missing = _dns_missing_reasons(dns_provider, zone_id=zone_id, token_configured=dns_token_configured, token_env=token_env, target=dns_target)
    portainer_api_url = str(state.get("portainerApiUrl") or config.portainer.get("apiUrl") or "")
    portainer_endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    registered_nodes = _registered_nodes_summary(nodes)
    return {
        "clusterId": state["clusterId"],
        "version": __version__,
        "configPath": str(config_path),
        "dns": {
            "provider": dns_provider,
            "zone": str(dns.get("zone") or ""),
            "zoneIdConfigured": bool(zone_id),
            "tokenEnv": token_env,
            "tokenConfigured": dns_token_configured,
            "target": str(dns_target or ""),
            "ready": not dns_missing,
            "missing": dns_missing,
        },
        "portainer": {
            "apiUrl": _redact_url(portainer_api_url),
            "apiConfigured": bool(portainer_api_url),
            "endpointIdConfigured": bool(portainer_endpoint_id),
            "swarmIdConfigured": bool(swarm_id),
            "ready": bool(portainer_api_url and portainer_endpoint_id and swarm_id),
        },
        "nodes": {
            "registered": len(registered_nodes),
            "names": [item["name"] for item in registered_nodes],
            "items": registered_nodes,
        },
        "swarm": _swarm_nodes_summary(),
        "storage": {
            "storageClasses": _storage_classes_summary(state),
        },
    }


def handle_dashboard(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    dns = config.dns
    dns_provider = str(dns.get("provider") or "not configured")
    token_env = str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    zone_id = os.environ.get(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID"))) or dns.get("zoneId")
    dns_target = str(dns.get("edgeTarget") or config.default_dns_target() or "")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    dns_token_configured = bool(os.environ.get(token_env) or token_env in secrets)
    dns_missing = _dns_missing_reasons(dns_provider, zone_id=zone_id, token_configured=dns_token_configured, token_env=token_env, target=dns_target)
    portainer_endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    errors: list[str] = []

    raw_nodes = _dashboard_docker_list("/nodes", "nodes", errors)
    node_by_id = _dashboard_node_map(raw_nodes)
    registered_nodes = _registered_nodes_summary(state.get("nodes") if isinstance(state.get("nodes"), dict) else {})
    nodes = _dashboard_nodes(registered_nodes, raw_nodes)

    raw_services = _dashboard_docker_list("/services", "services", errors)
    raw_tasks = _dashboard_docker_list("/tasks", "tasks", errors)
    route_files = _dashboard_route_files(config, config_path, errors)
    services = _dashboard_services(raw_services, raw_tasks, node_by_id, route_files)
    traffic_paths = _dashboard_traffic_paths(services, route_files, dns_target)
    storage = _dashboard_storage(services, _storage_classes_summary(state))

    return {
        "cluster": {
            "id": str(state.get("clusterId") or ""),
            "version": __version__,
            "configPath": str(config_path),
        },
        "readiness": {
            "dns": {
                "ready": not dns_missing,
                "provider": dns_provider,
                "zone": str(dns.get("zone") or ""),
                "target": dns_target,
                "missing": dns_missing,
            },
            "portainer": {
                "ready": bool((state.get("portainerApiUrl") or config.portainer.get("apiUrl")) and portainer_endpoint_id and swarm_id),
                "apiConfigured": bool(state.get("portainerApiUrl") or config.portainer.get("apiUrl")),
                "endpointConfigured": bool(portainer_endpoint_id),
            },
            "swarm": {
                "available": bool(raw_nodes),
            },
        },
        "nodes": nodes,
        "services": [_public_dashboard_service(item) for item in services],
        "trafficPaths": traffic_paths,
        "storage": storage,
        "errors": errors,
    }


def _dns_missing_reasons(dns_provider: str, *, zone_id: object, token_configured: bool, token_env: str, target: object) -> list[str]:
    missing: list[str] = []
    if dns_provider != "cloudflare":
        missing.append("provider")
    if not zone_id:
        missing.append("zoneId")
    if not token_configured:
        missing.append(f"token:{token_env}")
    if not target:
        missing.append("target")
    return missing


def handle_node_register(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="join")
    node_name = str(body.get("nodeName") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    _remember_node(state, node_name, region=region, status="registered")
    save_state(state)
    return {
        "clusterId": state["clusterId"],
        "managerAddr": state.get("managerAddr", ""),
        "swarmJoinToken": state.get("swarmJoinToken", ""),
        "nodeName": node_name,
        "region": region,
    }


def handle_node_label(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="join")
    node_name = str(body.get("nodeName") or "").strip()
    registered_name = str(body.get("registeredName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    tailscale_ip = str(body.get("tailscaleIP") or "").strip()
    tailscale_name = str(body.get("tailscaleName") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    luma_name = registered_name or node_name
    labels = labels_for_node(region, luma_name=luma_name, node_id=node_id)
    label_swarm_node(node_name, labels, node_id=node_id)
    values: Dict[str, Any] = {
        "region": region,
        "status": "labeled",
        "labels": labels,
        "displayName": luma_name,
        "swarmHostname": node_name,
    }
    if node_id:
        values["swarmNodeId"] = node_id
    if tailscale_ip:
        values["tailscaleIP"] = tailscale_ip
    if tailscale_name:
        values["tailscaleName"] = tailscale_name
    _remember_node(state, luma_name, **values)
    save_state(state)
    return {
        "clusterId": state["clusterId"],
        "nodeName": luma_name,
        "swarmHostname": node_name,
        "swarmNodeId": node_id,
        "displayName": luma_name,
        "tailscaleIP": tailscale_ip,
        "tailscaleName": tailscale_name,
        "labels": labels,
        "message": f"Node labels applied: {luma_name}",
    }


def handle_node_unregister(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_control_node_token(state, token)
    node_name = str(body.get("nodeName") or "").strip()
    if not node_name:
        raise LumaError("nodeName is required")
    nodes = state.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
        state["nodes"] = nodes
    removed = nodes.pop(node_name, None)
    if removed is None:
        for key, value in list(nodes.items()):
            if isinstance(value, dict) and value.get("displayName") == node_name:
                removed = nodes.pop(key)
                break
    swarm_result = remove_swarm_node(node_name, removed if isinstance(removed, dict) else None)
    save_state(state)
    registered_removed = bool(removed)
    swarm_removed = bool(swarm_result.get("removed"))
    if registered_removed and swarm_removed:
        message = f"Node removed: {node_name}; Swarm node removed: {swarm_result.get('nodeId')}"
    elif registered_removed:
        message = f"Node removed: {node_name}; {swarm_result.get('message')}"
    elif swarm_removed:
        message = f"Node not registered: {node_name}; Swarm node removed: {swarm_result.get('nodeId')}"
    else:
        message = f"Node not registered: {node_name}; {swarm_result.get('message')}"
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "removed": registered_removed or swarm_removed,
        "registeredRemoved": registered_removed,
        "swarmRemoved": swarm_removed,
        "swarmNodeId": str(swarm_result.get("nodeId") or ""),
        "message": message,
    }


def handle_deployment(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    manifest = body.get("manifest")
    source_name = str(body.get("sourceName") or "service.yaml")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        service = load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)
    parse_step = {"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)

    registry_auth = _registry_auth_for_service(state, service)
    service, image_result = _deploy_step(
        steps,
        "Resolve image",
        lambda: resolve_service_image(config, service, registry_auth=registry_auth),
        progress=progress,
    )
    service = _deploy_step(steps, "Resolve node pin", lambda: resolve_service_node_pin(service, state), progress=progress)
    target = _resolve_control_path(stack_path(config, service), config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stack_text = _deploy_step(steps, "Render stack", lambda: render_stack(config, service), progress=progress)
    stack_env = _deploy_step(steps, "Resolve stack secrets", lambda: _stack_env_for_text(stack_text), progress=progress)
    _deploy_step(steps, "Write stack", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
    written = [str(target)]
    dns_result = _deploy_step(steps, "Sync DNS", lambda: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service), progress=progress)
    webhook_result = _deploy_step(
        steps,
        "Deploy Portainer stack",
        lambda: "Portainer deploy skipped: --skip-webhook"
        if body.get("skipWebhook")
        else deploy_with_portainer(config, service, stack_text, state, stack_env=stack_env, registry_auth=registry_auth),
        progress=progress,
    )
    if service.exposure == "tailscale-relay":
        route_target = _resolve_control_path(route_path(config, service), config_path)
        route_target.parent.mkdir(parents=True, exist_ok=True)
        route_service = service
        relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host"))
        if body.get("skipWebhook") and not relay_is_explicit:
            _deploy_step(steps, "Write route", lambda: "Route skipped: --skip-webhook requires deploy to infer tailscale relay", progress=progress)
        else:
            if not body.get("skipWebhook"):
                route_service = _deploy_step(steps, "Resolve relay", lambda: resolve_tailscale_relay(service), progress=progress)
            _deploy_step(steps, "Write route", lambda: route_target.write_text(render_tailscale_route(config, route_service), encoding="utf-8"), progress=progress)
            written.append(str(route_target))
    probe_result = _deploy_step(
        steps,
        "Probe public route",
        lambda: "Public route probe skipped: --skip-webhook" if body.get("skipWebhook") else _probe_public_route(service),
        progress=progress,
    )
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "written": written,
        "image": image_result,
        "dns": dns_result,
        "webhook": webhook_result,
        "probe": probe_result,
        "steps": steps,
    }


def handle_deployment_preview(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    manifest = body.get("manifest")
    source_name = str(body.get("sourceName") or "service.yaml")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        service = load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)
    service = resolve_service_node_pin(service, state)
    stack_text = render_stack(config, service)
    stack_env = _stack_env_for_text(stack_text)
    artifacts = [
        {
            "kind": "stack",
            "path": str(stack_path(config, service)),
            "content": stack_text,
        }
    ]
    if service.exposure == "tailscale-relay":
        artifacts.append(
            {
                "kind": "route",
                "path": str(route_path(config, service)),
                "content": render_tailscale_route(config, service),
            }
        )
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "summary": {
            "name": service.name,
            "image": service.image,
            "region": service.region,
            "node": service.node or "",
            "exposure": service.exposure,
            "domain": service.domain or "",
            "port": service.port,
            "replicas": service.replicas,
            "proxy": service.proxy,
            "secrets": [item["name"] for item in stack_env],
        },
        "artifacts": artifacts,
        "warnings": [],
    }


def handle_service_remove(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    manifest = body.get("manifest")
    source_name = str(body.get("sourceName") or "service.yaml")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        service = load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)
    parse_step = {"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)

    dry_run = bool(body.get("dryRun"))
    stack_target = _generated_stack_remove_target(config, service, config_path)
    route_target = _resolve_control_path(route_path(config, service), config_path) if service.exposure == "tailscale-relay" else None
    files = [str(stack_target)]
    if route_target:
        files.append(str(route_target))

    dns_result = _deploy_step(
        steps,
        "Delete DNS",
        lambda: "DNS skipped: --skip-dns"
        if body.get("skipDns")
        else (_planned_delete_dns_message(service) if dry_run else delete_dns(config, service)),
        progress=progress,
    )
    portainer_result = _deploy_step(
        steps,
        "Remove Portainer stack",
        lambda: "Portainer remove skipped: --skip-portainer"
        if body.get("skipPortainer")
        else (_planned_remove_message("Portainer stack would be removed", service.slug) if dry_run else remove_stack(config, service, state)),
        progress=progress,
    )
    files_result = _deploy_step(
        steps,
        "Delete generated files",
        lambda: _planned_remove_message("Generated files would be removed", ", ".join(files))
        if dry_run
        else _remove_generated_files(stack_target, route_target),
        progress=progress,
    )
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_result,
        "portainer": portainer_result,
        "generatedFiles": files_result,
        "dryRun": dry_run,
        "steps": steps,
    }


SYSTEM_STACKS = {"traefik", "portainer", "egress", "luma-control"}


def handle_application_restart(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    stack = str(body.get("stack") or "").strip()
    service_name = str(body.get("service") or "").strip()
    if not stack:
        raise LumaError("stack is required")
    if _is_system_stack(stack):
        raise LumaError(f"system stack cannot be restarted from application management: {stack}")
    services = docker_request("GET", "/services")
    if not isinstance(services, list):
        raise LumaError("Docker API returned invalid service list")
    targets = []
    for service in services:
        if not isinstance(service, dict):
            continue
        spec = service.get("Spec") if isinstance(service.get("Spec"), dict) else {}
        full_name = str(spec.get("Name") or service.get("ID") or "")
        item_stack, item_service = _split_swarm_service_name(full_name)
        if item_stack != stack:
            continue
        if service_name and item_service != service_name:
            continue
        service_id = str(service.get("ID") or "")
        if service_id:
            targets.append({"id": service_id, "name": full_name, "service": item_service})
    if not targets:
        suffix = f"/{service_name}" if service_name else ""
        raise LumaError(f"application service not found: {stack}{suffix}")
    restarted = []
    for target in targets:
        restarted.append(_force_update_service(str(target["id"]), str(target["name"])))
    return {
        "clusterId": state["clusterId"],
        "stack": stack,
        "service": service_name,
        "restarted": restarted,
    }


def _is_system_stack(stack: str) -> bool:
    return stack in SYSTEM_STACKS or stack.startswith("luma-storage")


def _force_update_service(service_id: str, display_name: str) -> Dict[str, Any]:
    inspected = docker_request("GET", f"/services/{urllib.parse.quote(service_id, safe='')}")
    if not isinstance(inspected, dict):
        raise LumaError(f"Docker API returned invalid service detail: {display_name}")
    version = inspected.get("Version", {}).get("Index")
    spec = inspected.get("Spec")
    if not version or not isinstance(spec, dict):
        raise LumaError(f"Docker API returned invalid service spec: {display_name}")
    task_template = spec.setdefault("TaskTemplate", {})
    if not isinstance(task_template, dict):
        raise LumaError(f"Docker API returned invalid task template: {display_name}")
    task_template["ForceUpdate"] = int(task_template.get("ForceUpdate") or 0) + 1
    docker_request(
        "POST",
        f"/services/{urllib.parse.quote(service_id, safe='')}/update?version={version}",
        spec,
    )
    return {"id": service_id, "name": display_name, "forceUpdate": task_template["ForceUpdate"]}


def handle_compose_deployment(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    parse_step = {"name": "Parse compose deployment", "status": "ok", "message": f"{deployment.name} ({len(deployment.compose.get('services', {}))} services)"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    _emit_compose_warnings(steps, progress, deployment)

    target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stack_text = _deploy_step(
        steps,
        "Render compose stack",
        lambda: render_compose_stack(
            config,
            deployment,
            node_id_resolver=_compose_node_id_resolver(state),
            node_records=_state_nodes(state),
        ),
        progress=progress,
    )
    _deploy_step(steps, "Check storage migration", lambda: _guard_compose_storage_switch(target, stack_text, deployment), progress=progress)
    stack_env = _deploy_step(steps, "Resolve stack secrets", lambda: _stack_env_for_text(stack_text), progress=progress)
    _deploy_step(steps, "Write compose stack", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
    written = [str(target)]

    dns_results: list[str] = []
    for service in compose_public_services(deployment):
        service_spec = _compose_service_as_service_spec(deployment, service)
        dns_results.append(
            _deploy_step(
                steps,
                f"Sync DNS {service.name}",
                lambda service_spec=service_spec: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service_spec),
                progress=progress,
            )
        )

    portainer_result = _deploy_step(
        steps,
        "Deploy Portainer stack",
        lambda: "Portainer deploy skipped: --skip-webhook"
        if body.get("skipWebhook")
        else upsert_stack(
            config,
            _stack_service_spec(deployment),
            stack_text,
            state,
            missing_webhook_env="PORTAINER_WEBHOOK_URL",
            stack_env=stack_env,
            registry_auth=None,
        ),
        progress=progress,
    )

    route_texts = render_compose_routes(config, deployment)
    for service_name, route_text in route_texts.items():
        route_target = _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
        route_target.parent.mkdir(parents=True, exist_ok=True)
        _deploy_step(steps, f"Write route {service_name}", lambda route_target=route_target, route_text=route_text: route_target.write_text(route_text, encoding="utf-8"), progress=progress)
        written.append(str(route_target))

    probe_results: list[str] = []
    for service in compose_public_services(deployment):
        service_spec = _compose_service_as_service_spec(deployment, service)
        probe_results.append(
            _deploy_step(
                steps,
                f"Probe public route {service.name}",
                lambda service_spec=service_spec: "Public route probe skipped: --skip-webhook" if body.get("skipWebhook") else _probe_public_route(service_spec),
                progress=progress,
            )
        )
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "written": written,
        "dns": dns_results,
        "webhook": portainer_result,
        "probe": probe_results,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "steps": steps,
    }


def handle_compose_deployment_preview(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    stack_text = render_compose_stack(
        config,
        deployment,
        node_id_resolver=_compose_node_id_resolver(state),
        node_records=_state_nodes(state),
    )
    target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
    storage_guard = _guard_compose_storage_switch(target, stack_text, deployment)
    _stack_env_for_text(stack_text)
    route_texts = render_compose_routes(config, deployment)
    artifacts = [
        {
            "kind": "stack",
            "path": str(compose_stack_path(config, deployment)),
            "content": stack_text,
        }
    ]
    for service_name, route_text in route_texts.items():
        artifacts.append(
            {
                "kind": "route",
                "path": str(compose_route_path(config, deployment, service_name)),
                "content": route_text,
            }
        )
    services = []
    for service_name, service in sorted(deployment.services.items()):
        services.append(
            {
                "name": service_name,
                "region": service.region or deployment.region,
                "node": service.node or "",
                "exposure": service.exposure,
                "domain": service.domain or "",
                "port": service.port,
                "publishPort": service.publish_port,
                "replicas": service.replicas,
                "proxy": service.proxy,
            }
        )
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "summary": {
            "name": deployment.name,
            "region": deployment.region,
            "services": services,
            "storageGuard": storage_guard,
        },
        "artifacts": artifacts,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "warnings": deployment.warnings,
    }


def handle_compose_remove(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    parse_step = {"name": "Parse compose deployment", "status": "ok", "message": deployment.name}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    dry_run = bool(body.get("dryRun"))
    stack_target = _resolve_control_path(compose_stack_path(config, deployment), config_path).parent
    route_targets = [
        _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
        for service_name, service in deployment.services.items()
        if service.exposure == "tailscale-relay"
    ]
    files = [str(stack_target), *[str(path) for path in route_targets]]
    dns_results = []
    for service in compose_public_services(deployment):
        service_spec = _compose_service_as_service_spec(deployment, service)
        dns_results.append(
            _deploy_step(
                steps,
                f"Delete DNS {service.name}",
                lambda service_spec=service_spec: "DNS skipped: --skip-dns"
                if body.get("skipDns")
                else (_planned_delete_dns_message(service_spec) if dry_run else delete_dns(config, service_spec)),
                progress=progress,
            )
        )
    portainer_result = _deploy_step(
        steps,
        "Remove Portainer stack",
        lambda: "Portainer remove skipped: --skip-portainer"
        if body.get("skipPortainer")
        else (_planned_remove_message("Portainer stack would be removed", deployment.slug) if dry_run else remove_stack(config, _stack_service_spec(deployment), state)),
        progress=progress,
    )
    files_result = _deploy_step(
        steps,
        "Delete generated files",
        lambda: _planned_remove_message("Generated files would be removed", ", ".join(files))
        if dry_run
        else _remove_generated_files(stack_target, *route_targets),
        progress=progress,
    )
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_results,
        "portainer": portainer_result,
        "generatedFiles": files_result,
        "dryRun": dry_run,
        "steps": steps,
    }


def handle_storage_apply(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    _deploy_step(
        steps,
        "Resolve storage endpoints",
        lambda: render_compose_stack(config, deployment, node_records=_state_nodes(state)),
        progress=progress,
    )
    stacks = managed_storage_stacks(deployment)
    written: list[str] = []
    applied: list[str] = []
    for stack in stacks:
        target = _resolve_control_path(config.stack_root / "storage" / stack.name / "stack.yml", config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _deploy_step(steps, f"Write storage {stack.storage_class}", lambda target=target, stack=stack: target.write_text(stack.content, encoding="utf-8"), progress=progress)
        written.append(str(target))
        result = _deploy_step(
            steps,
            f"Deploy storage {stack.storage_class}",
            lambda stack=stack: upsert_stack(
                config,
                _storage_stack_service_spec(stack.name),
                stack.content,
                state,
                missing_webhook_env="PORTAINER_WEBHOOK_URL",
                stack_env=[],
                registry_auth=None,
            ),
            progress=progress,
        )
        applied.append(result)
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "written": written,
        "applied": applied,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "steps": steps,
    }


def handle_storage_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    return {"storageClasses": _storage_classes_summary(state)}


def handle_storage_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or "").strip()
    if not name:
        raise LumaError("storage class name is required")
    provider = str(body.get("provider") or "nfs").strip()
    if provider != "nfs":
        raise LumaError("storage provider must be: nfs")
    mode = "external" if bool(body.get("external")) else "managed"
    item = {
        "provider": provider,
        "mode": mode,
        "node": str(body.get("node") or "").strip(),
        "endpoint": str(body.get("endpoint") or "").strip(),
        "path": str(body.get("path") or "").strip(),
        "mountOptions": str(body.get("mountOptions") or "nfsvers=4,rw").strip(),
        "regions": [str(value) for value in body.get("regions") or [] if str(value)],
        "nodes": [str(value) for value in body.get("nodes") or [] if str(value)],
        "updatedAt": int(time.time()),
    }
    _validate_storage_class_record(name, item, state)
    storage_classes = state.setdefault("storageClasses", {})
    if not isinstance(storage_classes, dict):
        storage_classes = {}
        state["storageClasses"] = storage_classes
    storage_classes[name] = {key: value for key, value in item.items() if value not in ("", [], None)}
    save_state(state)
    return {"name": name, "saved": True, "storageClass": _public_storage_class(name, storage_classes[name])}


def handle_storage_remove(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or "").strip()
    if not name:
        raise LumaError("storage class name is required")
    storage_classes = state.get("storageClasses") if isinstance(state.get("storageClasses"), dict) else {}
    removed = bool(storage_classes.pop(name, None))
    state["storageClasses"] = storage_classes
    save_state(state)
    return {"name": name, "removed": removed}


def _load_compose_request(body: Dict[str, Any], source_name: str) -> ComposeDeploymentSpec:
    manifest = body.get("manifest")
    compose_content = body.get("composeContent")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    if not isinstance(compose_content, str) or not compose_content.strip():
        raise LumaError("composeContent is required")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sidecar_name = Path(source_name).name or "luma.compose.yml"
        sidecar_path = root / sidecar_name
        sidecar_path.write_text(manifest, encoding="utf-8")
        sidecar_data = load_yaml(sidecar_path)
        compose_value = sidecar_data.get("compose", "docker-compose.yml")
        if not isinstance(compose_value, str) or not compose_value.strip():
            raise LumaError("compose deployment requires string field: compose")
        compose_path = _safe_compose_upload_path(sidecar_path, compose_value)
        compose_path.parent.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(compose_content, encoding="utf-8")
        state = load_state()
        return load_compose_deployment(
            sidecar_path,
            storage_classes=_state_storage_classes(state),
            allow_sidecar_storage_classes=False,
        )


def _safe_compose_upload_path(sidecar_path: Path, compose_value: str) -> Path:
    compose_path = Path(compose_value)
    if compose_path.is_absolute() or ".." in compose_path.parts or not compose_path.name:
        raise LumaError("compose path must be a relative path without .. when deploying through Luma Control")
    return sidecar_path.parent / compose_path


def _emit_compose_warnings(
    steps: list[dict[str, str]],
    progress: Callable[[dict[str, str]], None] | None,
    deployment: ComposeDeploymentSpec,
) -> None:
    for warning in deployment.warnings:
        step = {"name": "Compose warning", "status": "ok", "message": warning}
        steps.append(step)
        _emit_progress(progress, step)


def _compose_node_id_resolver(state: Dict[str, Any]) -> Callable[[str], str | None]:
    def resolve(node_name: str) -> str | None:
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        record = _node_record_for_name(nodes, node_name)
        if not record:
            names = ", ".join(sorted(str(name) for name in nodes)) or "none"
            raise LumaError(f"unknown Luma node: {node_name}. Registered nodes: {names}")
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
        if not node_id:
            raise LumaError(f"Luma node {node_name} has no Swarm NodeID; rerun luma node join on that node")
        return node_id

    return resolve


def _state_nodes(state: Dict[str, Any]) -> Dict[str, Any]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    return {str(name): dict(value) for name, value in nodes.items() if isinstance(value, dict)}


def _compose_service_as_service_spec(deployment: ComposeDeploymentSpec, service: Any) -> ServiceSpec:
    return ServiceSpec(
        source=deployment.source,
        name=f"{deployment.slug}-{service.name}",
        image="compose",
        region=service.region or deployment.region,
        node=service.node,
        public=service.exposure != "none",
        exposure=service.exposure,
        domain=service.domain,
        port=service.port,
        publish_port=service.publish_port,
        replicas=service.replicas or 1,
        relay=service.relay,
        tunnel=service.tunnel,
        proxy=service.proxy,
    )


def _stack_service_spec(deployment: ComposeDeploymentSpec) -> ServiceSpec:
    return ServiceSpec(source=deployment.source, name=deployment.name, image="compose", region=deployment.region)


def _storage_stack_service_spec(stack_name: str) -> ServiceSpec:
    return ServiceSpec(source=Path("storage"), name=stack_name, image="storage", region="cn")


def _guard_compose_storage_switch(target: Path, stack_text: str, deployment: ComposeDeploymentSpec) -> str:
    if not target.exists():
        return "No previous compose stack"
    previous = _safe_yaml_mapping(target.read_text(encoding="utf-8"))
    current = _safe_yaml_mapping(stack_text)
    previous_volumes = previous.get("volumes") if isinstance(previous.get("volumes"), dict) else {}
    current_volumes = current.get("volumes") if isinstance(current.get("volumes"), dict) else {}
    changed = []
    for name, spec in deployment.volumes.items():
        if not spec.storage_class:
            continue
        if spec.initialize == "empty" or spec.adopted:
            continue
        before = previous_volumes.get(name)
        after = current_volumes.get(name)
        if before != after:
            changed.append(name)
    if changed:
        raise LumaError(
            "storage backend changed for "
            + ", ".join(sorted(changed))
            + "; run luma storage migrate and set adopted: true after verification, or set initialize: empty for a fresh volume"
        )
    return "Storage backend unchanged"


def _safe_yaml_mapping(text: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid generated YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError("generated YAML must be a mapping")
    return data


def _state_storage_classes(state: Dict[str, Any]) -> Dict[str, Any]:
    storage_classes = state.get("storageClasses") if isinstance(state.get("storageClasses"), dict) else {}
    return {str(name): dict(value) for name, value in storage_classes.items() if isinstance(value, dict)}


def _storage_classes_summary(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [_public_storage_class(name, value) for name, value in sorted(_state_storage_classes(state).items())]


def _public_storage_class(name: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": str(name),
        "provider": str(value.get("provider") or ""),
        "mode": str(value.get("mode") or "external"),
        "node": str(value.get("node") or ""),
        "endpoint": str(value.get("endpoint") or ""),
        "path": str(value.get("path") or ""),
        "mountOptions": str(value.get("mountOptions") or ""),
        "regions": [str(item) for item in value.get("regions") or []],
        "nodes": [str(item) for item in value.get("nodes") or []],
        "updatedAt": value.get("updatedAt") or "",
    }


def _validate_storage_class_record(name: str, item: Dict[str, Any], state: Dict[str, Any]) -> None:
    if item["mode"] == "external":
        if not item.get("endpoint"):
            raise LumaError(f"external storage class {name} requires endpoint")
        if not item.get("regions"):
            raise LumaError(f"external storage class {name} requires at least one region")
        if item.get("node") or item.get("path"):
            raise LumaError(f"external storage class {name} cannot set node or path")
    if item["mode"] == "managed":
        if not item.get("node") or not item.get("path"):
            raise LumaError(f"managed storage class {name} requires node and path")
        if item.get("endpoint"):
            raise LumaError(f"managed storage class {name} cannot set endpoint")
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        node_name = str(item.get("node") or "")
        record = _node_record_for_name(nodes, node_name)
        if not record:
            _adopt_swarm_node_for_storage(state, node_name)
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            record = _node_record_for_name(nodes, node_name)
        if not record:
            names = ", ".join(sorted(str(key) for key in nodes)) or "none"
            raise LumaError(f"managed storage class {name} references unknown Luma node: {item.get('node')}. Registered nodes: {names}")
        _ensure_storage_node_swarm_label(node_name, record)
    regions = item.get("regions") if isinstance(item.get("regions"), list) else []
    for region in regions:
        if region not in VALID_REGIONS:
            raise LumaError(f"storage class {name} region must be one of {sorted(VALID_REGIONS)}")


def handle_secret_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    return {"secrets": sorted(str(key) for key in secrets)}


def handle_secret_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or "").strip()
    value = body.get("value")
    if not _valid_env_name(name):
        raise LumaError("secret name must be a valid environment variable name")
    if value is None or str(value) == "":
        raise LumaError("secret value is required")
    secrets = state.setdefault("secrets", {})
    if not isinstance(secrets, dict):
        secrets = {}
        state["secrets"] = secrets
    secrets[name] = str(value)
    save_state(state)
    return {"name": name, "saved": True}


def handle_registry_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    items = []
    for host, item in sorted(registries.items()):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "host": str(host),
                "serverAddress": str(item.get("serverAddress") or host),
                "username": str(item.get("username") or ""),
                "configured": bool(item.get("username") and item.get("password")),
            }
        )
    return {"registries": items}


def handle_registry_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    host = normalize_registry_host(str(body.get("host") or body.get("serverAddress") or ""))
    username = str(body.get("username") or "").strip()
    password = body.get("password")
    if not username:
        raise LumaError("registry username is required")
    if password is None or str(password) == "":
        raise LumaError("registry password is required")
    registries = state.setdefault("registries", {})
    if not isinstance(registries, dict):
        registries = {}
        state["registries"] = registries
    registries[host] = {
        "serverAddress": host,
        "username": username,
        "password": str(password),
        "updatedAt": int(time.time()),
    }
    save_state(state)
    return {"host": host, "username": username, "saved": True}


def handle_registry_remove(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    host = normalize_registry_host(str(body.get("host") or ""))
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    removed = bool(registries.pop(host, None))
    state["registries"] = registries
    save_state(state)
    portainer_removed = False
    warning = None
    try:
        config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
        config = load_config(config_path)
        portainer_removed = remove_luma_portainer_registry(config, state, host)
    except LumaError as exc:
        warning = f"Portainer registry cleanup failed: {exc}"
    result: Dict[str, Any] = {"host": host, "removed": removed, "portainerRegistryRemoved": portainer_removed}
    if warning:
        result["warning"] = warning
    return result


def _planned_remove_message(prefix: str, target: str) -> str:
    return f"{prefix}: {target}"


def _planned_delete_dns_message(service: ServiceSpec) -> str:
    if not service.public:
        return "DNS skipped: service is not public"
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"
    return _planned_remove_message("DNS would be deleted", service.domain or service.name)


def _generated_stack_remove_target(config: Any, service: ServiceSpec, config_path: Path) -> Path:
    target = _resolve_control_path(stack_path(config, service), config_path)
    if service.stack_path:
        return target
    return target.parent


def _remove_generated_files(stack_target: Path, *route_targets: Path | None) -> str:
    removed = []
    missing = []
    for target in [stack_target, *route_targets]:
        if target is None:
            continue
        result = _remove_path(target)
        if result:
            removed.append(str(target))
        else:
            missing.append(str(target))
    if removed and missing:
        return f"Generated files removed: {', '.join(removed)}; not found: {', '.join(missing)}"
    if removed:
        return f"Generated files removed: {', '.join(removed)}"
    return f"Generated files not found: {', '.join(missing)}"


def _remove_path(path: Path) -> bool:
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.exists():
        path.unlink()
        return True
    return False


def _resolve_control_path(path: Path, config_path: Path) -> Path:
    if path.is_absolute():
        return path
    return config_path.resolve().parent / path


def _apply_state_secrets(state: Dict[str, Any]) -> None:
    secrets = state.get("secrets") or {}
    if not isinstance(secrets, dict):
        return
    for key, value in secrets.items():
        if value is None:
            continue
        os.environ[str(key)] = str(value)


def _registry_auth_for_service(state: Dict[str, Any], service: ServiceSpec) -> Dict[str, str] | None:
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    return registry_auth_for_image(registries, service.image)


def _stack_env_for_text(stack_text: str) -> list[dict[str, str]]:
    names = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", stack_text)))
    env: list[dict[str, str]] = []
    missing = []
    for name in names:
        value = os.environ.get(name)
        if value is None:
            missing.append(name)
        else:
            env.append({"name": name, "value": value})
    if missing:
        raise LumaError("missing deployment secrets: " + ", ".join(missing) + ". Run: luma secret set <NAME>")
    return env


def _probe_public_route(service: ServiceSpec) -> str:
    if service.exposure not in {"cn-edge", "external-edge"}:
        return "Public route probe skipped: service is not exposed through Traefik"
    if not service.domain:
        return "Public route probe skipped: service has no domain"
    url = f"https://{service.domain}/"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "luma-control-route-probe"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _probe_status_message(url, int(resp.status))
    except urllib.error.HTTPError as exc:
        return _probe_status_message(url, int(exc.code))
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        return f"Public route probe inconclusive: {url} ({exc})"


def _probe_status_message(url: str, status: int) -> str:
    if status == 404:
        return f"Public route reachable: {url} -> HTTP 404 (the app may not serve /)"
    return f"Public route reachable: {url} -> HTTP {status}"


def _deploy_step(steps: list[dict[str, str]], name: str, action: Any, *, progress: Callable[[dict[str, str]], None] | None = None) -> Any:
    _emit_progress(progress, {"name": name, "status": "start", "message": "started"})
    try:
        result = action()
    except LumaError as exc:
        step = {"name": name, "status": "fail", "message": str(exc)}
        steps.append(step)
        _emit_progress(progress, step)
        raise LumaError(f"{name} failed: {exc}") from exc
    message = _step_message(result)
    step = {"name": name, "status": "ok", "message": message}
    steps.append(step)
    _emit_progress(progress, step)
    return result


def _emit_progress(progress: Callable[[dict[str, str]], None] | None, event: dict[str, str]) -> None:
    if progress:
        progress(event)


def _step_message(result: Any) -> str:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        image = result[1]
        selected = image.get("selected")
        requested = image.get("requested")
        if selected and requested and selected != requested:
            return f"{requested} -> {selected}"
        if selected:
            return str(selected)
    if isinstance(result, str):
        return result
    if isinstance(result, int):
        return "written"
    return "ok"


def _redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    if not parsed.netloc:
        return value
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _valid_env_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def labels_for_region(region: str) -> Dict[str, str]:
    return {"region": region}


def labels_for_node(region: str, *, luma_name: str, node_id: str = "") -> Dict[str, str]:
    labels = labels_for_region(region)
    labels["luma.node.name"] = luma_name
    if node_id:
        labels["luma.node.id"] = node_id
    return labels


def resolve_service_node_pin(service: ServiceSpec, state: Dict[str, Any]) -> ServiceSpec:
    if not service.node:
        return service
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, service.node)
    if not record:
        names = ", ".join(sorted(str(name) for name in nodes)) or "none"
        raise LumaError(f"unknown Luma node: {service.node}. Registered nodes: {names}")
    region = str(record.get("region") or "")
    if region and region != service.region:
        raise LumaError(f"Luma node {service.node} is in region {region}, not {service.region}")
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
    if not node_id:
        raise LumaError(f"Luma node {service.node} has no Swarm NodeID; rerun luma node join on that node")
    return replace(service, node_id=node_id)


def _node_record_for_name(nodes: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    direct = nodes.get(name)
    if isinstance(direct, dict):
        return direct
    for value in nodes.values():
        if isinstance(value, dict) and value.get("displayName") == name:
            return value
    return None


def _adopt_swarm_node_for_storage(state: Dict[str, Any], node_name: str) -> None:
    node_name = str(node_name or "").strip()
    if not node_name:
        return
    try:
        nodes = docker_request("GET", "/nodes")
    except LumaError:
        return
    if not isinstance(nodes, list):
        return
    match = _match_luma_swarm_node(nodes, node_name=node_name)
    if not match:
        return
    spec = match.get("Spec") if isinstance(match.get("Spec"), dict) else {}
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    description = match.get("Description") if isinstance(match.get("Description"), dict) else {}
    manager_status = match.get("ManagerStatus") if isinstance(match.get("ManagerStatus"), dict) else {}
    hostname = str(description.get("Hostname") or "")
    swarm_node_id = str(match.get("ID") or "").strip()
    luma_name = str(node_name or labels.get("luma.node.name") or hostname or swarm_node_id[:12]).strip()
    region = str(labels.get("region") or "").strip()
    values: Dict[str, Any] = {
        "status": "adopted",
        "displayName": luma_name,
        "swarmHostname": hostname,
        "labels": {str(key): str(value) for key, value in labels.items()},
    }
    if region:
        values["region"] = region
    if swarm_node_id:
        values["swarmNodeId"] = swarm_node_id
    if spec.get("Role"):
        values["swarmRole"] = str(spec.get("Role") or "")
    if manager_status:
        values["swarmManager"] = True
    _remember_node(state, luma_name, **values)
    if node_name != luma_name and not _node_record_for_name(_state_nodes(state), node_name):
        _remember_node(state, node_name, **values)


def _ensure_storage_node_swarm_label(node_name: str, record: Dict[str, Any]) -> None:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    if labels.get("luma.node.name") == node_name:
        return
    node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
    hostname = str(record.get("swarmHostname") or "").strip()
    if not node_id and not hostname:
        return
    region = str(record.get("region") or labels.get("region") or "").strip()
    if not region:
        raise LumaError(f"managed storage node {node_name} has no region; rerun luma update manager or luma node join")
    applied = labels_for_node(region, luma_name=node_name, node_id=node_id)
    try:
        label_swarm_node(hostname or node_name, applied, node_id=node_id)
    except LumaError as exc:
        raise LumaError(f"managed storage node {node_name} could not be labeled for storage placement: {exc}") from exc
    merged = {str(key): str(value) for key, value in labels.items()}
    merged.update(applied)
    record["labels"] = merged
    record["status"] = str(record.get("status") or "labeled")
    if node_id:
        record["swarmNodeId"] = node_id


def _remember_node(state: Dict[str, Any], node_name: str, *, merge_from: str = "", **values: Any) -> None:
    nodes = state.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        state["nodes"] = nodes
    current = nodes.pop(merge_from, None) if merge_from and merge_from != node_name else nodes.get(node_name)
    if not isinstance(current, dict):
        current = {}
    existing = nodes.get(node_name)
    if isinstance(existing, dict):
        current.update(existing)
    current.update(values)
    nodes[node_name] = current


def _registered_nodes_summary(nodes: Dict[str, Any]) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for name in sorted(str(key) for key in nodes):
        raw = nodes.get(name)
        if not isinstance(raw, dict):
            raw = {}
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        items.append(
            {
                "name": name,
                "displayName": str(raw.get("displayName") or name),
                "swarmHostname": str(raw.get("swarmHostname") or ""),
                "swarmNodeId": str(raw.get("swarmNodeId") or labels.get("luma.node.id") or ""),
                "region": str(raw.get("region") or labels.get("region") or ""),
                "tailscaleIP": str(raw.get("tailscaleIP") or ""),
                "tailscaleName": str(raw.get("tailscaleName") or ""),
                "status": str(raw.get("status") or "registered"),
                "labels": {str(key): str(value) for key, value in labels.items()},
            }
        )
    return items


def _dashboard_docker_list(path: str, label: str, errors: list[str]) -> list[Dict[str, Any]]:
    try:
        value = docker_request("GET", path)
    except LumaError as exc:
        errors.append(f"Docker {label} unavailable: {exc}")
        return []
    if not isinstance(value, list):
        errors.append(f"Docker {label} response was not a list")
        return []
    return [item for item in value if isinstance(item, dict)]


def _dashboard_node_map(raw_nodes: list[Dict[str, Any]]) -> dict[str, Dict[str, Any]]:
    result: dict[str, Dict[str, Any]] = {}
    for node in raw_nodes:
        node_id = str(node.get("ID") or "")
        if not node_id:
            continue
        result[node_id] = _swarm_node_summary_item(node)
    return result


def _dashboard_nodes(registered_nodes: list[Dict[str, Any]], raw_nodes: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    merged: dict[str, Dict[str, Any]] = {}
    for node in registered_nodes:
        name = str(node.get("name") or "")
        if not name:
            continue
        merged.setdefault(name, {})["registered"] = node
    for raw_node in raw_nodes:
        node = _swarm_node_summary_item(raw_node)
        name = str(node.get("lumaNode") or node.get("hostname") or node.get("id") or "")
        if not name:
            continue
        merged.setdefault(name, {})["swarm"] = node

    rows: list[Dict[str, Any]] = []
    for name in sorted(merged):
        registered = merged[name].get("registered") if isinstance(merged[name].get("registered"), dict) else {}
        swarm = merged[name].get("swarm") if isinstance(merged[name].get("swarm"), dict) else {}
        display = str(registered.get("displayName") or swarm.get("hostname") or name)
        rows.append(
            {
                "name": name,
                "displayName": display,
                "swarmHostname": str(registered.get("swarmHostname") or swarm.get("hostname") or ""),
                "swarmNodeId": str(registered.get("swarmNodeId") or swarm.get("lumaNodeId") or swarm.get("id") or ""),
                "region": str(registered.get("region") or swarm.get("region") or ""),
                "role": str(swarm.get("role") or ""),
                "state": str(swarm.get("state") or "missing"),
                "availability": str(swarm.get("availability") or ""),
                "leader": bool(swarm.get("leader")),
            }
        )
    return rows


def _dashboard_route_files(config: Any, config_path: Path, errors: list[str]) -> dict[str, Dict[str, Any]]:
    routes_root = _resolve_control_path(config.routes_root, config_path)
    if not routes_root.exists():
        return {}
    result: dict[str, Dict[str, Any]] = {}
    try:
        files = sorted([*routes_root.glob("*.yml"), *routes_root.glob("*.yaml")])
    except OSError as exc:
        errors.append(f"Route files unavailable: {exc}")
        return result
    for path in files:
        try:
            data = load_yaml(path)
            route = _dashboard_route_file(path.stem, data)
            if route:
                result[path.stem] = route
        except (LumaError, OSError) as exc:
            errors.append(f"Route file {path.name} unreadable: {exc}")
    return result


def _dashboard_route_file(route_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    http = data.get("http") if isinstance(data.get("http"), dict) else {}
    routers = http.get("routers") if isinstance(http.get("routers"), dict) else {}
    services = http.get("services") if isinstance(http.get("services"), dict) else {}
    domain = ""
    for router in routers.values():
        if not isinstance(router, dict):
            continue
        domain = _host_from_rule(str(router.get("rule") or ""))
        if domain:
            break
    upstreams: list[str] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        load_balancer = service.get("loadBalancer") if isinstance(service.get("loadBalancer"), dict) else {}
        servers = load_balancer.get("servers") if isinstance(load_balancer.get("servers"), list) else []
        for server in servers:
            if isinstance(server, dict) and server.get("url"):
                upstreams.append(str(server["url"]))
    if not domain and not upstreams:
        return {}
    return {"id": route_id, "domain": domain, "upstreams": upstreams}


def _dashboard_services(
    raw_services: list[Dict[str, Any]],
    raw_tasks: list[Dict[str, Any]],
    node_by_id: dict[str, Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
) -> list[Dict[str, Any]]:
    tasks_by_service: dict[str, list[Dict[str, Any]]] = {}
    for task in raw_tasks:
        service_id = str(task.get("ServiceID") or "")
        if service_id:
            tasks_by_service.setdefault(service_id, []).append(task)

    services: list[Dict[str, Any]] = []
    for service in raw_services:
        item = _dashboard_service(service, tasks_by_service.get(str(service.get("ID") or ""), []), node_by_id, route_files)
        if item:
            services.append(item)

    cloudflare_tunnel_stacks = {item["stack"] for item in services if item.get("name") == "cloudflared" and item.get("stack")}
    for item in services:
        if item.get("stack") in cloudflare_tunnel_stacks and item.get("name") != "cloudflared" and item.get("exposure") == "none":
            item["exposure"] = "cloudflare-tunnel"
    services.sort(key=lambda item: (str(item.get("stack") or ""), str(item.get("name") or "")))
    return services


def _dashboard_service(
    service: Dict[str, Any],
    tasks: list[Dict[str, Any]],
    node_by_id: dict[str, Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    spec = service.get("Spec") if isinstance(service.get("Spec"), dict) else {}
    full_name = str(spec.get("Name") or service.get("ID") or "")
    if not full_name:
        return {}
    stack, name = _split_swarm_service_name(full_name)
    template = spec.get("TaskTemplate") if isinstance(spec.get("TaskTemplate"), dict) else {}
    container = template.get("ContainerSpec") if isinstance(template.get("ContainerSpec"), dict) else {}
    placement = template.get("Placement") if isinstance(template.get("Placement"), dict) else {}
    constraints = placement.get("Constraints") if isinstance(placement.get("Constraints"), list) else []
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    route = _traefik_route_from_labels(labels)
    route_id = stack or name
    route_file = route_files.get(route_id) or route_files.get(name)
    counts, task_nodes = _dashboard_task_counts(tasks, node_by_id)
    desired = _service_desired_replicas(spec, counts)
    region = _constraint_value(constraints, "node.labels.region")
    exposure = "none"
    if route.get("domain"):
        exposure = "external-edge" if region == "global" else "cn-edge"
    elif route_file:
        exposure = "tailscale-relay"
    return {
        "stack": stack,
        "name": name,
        "fullName": full_name,
        "image": str(container.get("Image") or ""),
        "desired": desired,
        "running": counts["running"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "nodes": task_nodes,
        "region": region,
        "node": _constraint_value(constraints, "node.labels.luma.node.name") or _constraint_value(constraints, "node.labels.luma.node.id") or _constraint_value(constraints, "node.hostname"),
        "exposure": exposure,
        "routeId": route_id,
        "domain": str(route.get("domain") or (route_file or {}).get("domain") or ""),
        "targetPort": str(route.get("port") or ""),
        "network": str(route.get("network") or ""),
        "health": _service_health(desired, counts),
        "storage": _storage_from_labels(labels),
        "diagnostics": _service_diagnostics(desired, counts, labels),
        "_routeFile": route_file or {},
    }


def _public_dashboard_service(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _storage_from_labels(labels: Dict[str, Any]) -> list[Dict[str, str]]:
    volumes: dict[str, Dict[str, str]] = {}
    for key, value in labels.items():
        label = str(key)
        raw_value = str(value)
        if label.startswith("luma.storage."):
            name = label.removeprefix("luma.storage.")
            volumes.setdefault(name, {"name": name})["kind"] = raw_value
        elif label.startswith("luma.storageClass."):
            name = label.removeprefix("luma.storageClass.")
            volumes.setdefault(name, {"name": name})["storageClass"] = raw_value
        elif label.startswith("luma.storageEndpoint."):
            name = label.removeprefix("luma.storageEndpoint.")
            volumes.setdefault(name, {"name": name})["endpoint"] = raw_value
        elif label.startswith("luma.storagePath."):
            name = label.removeprefix("luma.storagePath.")
            volumes.setdefault(name, {"name": name})["networkPath"] = raw_value
        elif label.startswith("luma.storageNode."):
            name = label.removeprefix("luma.storageNode.")
            volumes.setdefault(name, {"name": name})["node"] = raw_value
    return [volumes[name] for name in sorted(volumes)]


def _service_diagnostics(desired: int, counts: Dict[str, int], labels: Dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    if desired > 0 and counts["running"] == 0:
        diagnostics.append("No running tasks")
    if counts["failed"] > 0:
        diagnostics.append(f"{counts['failed']} failed task(s)")
    if counts["pending"] > 0:
        diagnostics.append(f"{counts['pending']} pending task(s)")
    for volume in _storage_from_labels(labels):
        if volume.get("kind") == "unmanaged":
            diagnostics.append(f"Volume {volume['name']} is unmanaged by Luma storage")
    return diagnostics


def _dashboard_storage(services: list[Dict[str, Any]], storage_classes: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    volumes: dict[str, Dict[str, Any]] = {}
    warnings: list[str] = []
    for service in services:
        service_name = str(service.get("fullName") or service.get("name") or "")
        for item in service.get("storage") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            volume = volumes.setdefault(
                name,
                {
                    "name": name,
                    "kind": str(item.get("kind") or "unmanaged"),
                    "storageClass": str(item.get("storageClass") or ""),
                    "node": str(item.get("node") or ""),
                    "endpoint": str(item.get("endpoint") or ""),
                    "networkPath": str(item.get("networkPath") or ""),
                    "services": [],
                },
            )
            volume["services"].append(service_name)
            if volume["kind"] == "unmanaged":
                warning = f"Volume {name} is unmanaged by Luma; rescheduling may use node-local data"
                if warning not in warnings:
                    warnings.append(warning)
    return {"storageClasses": storage_classes or [], "volumes": list(volumes.values()), "warnings": warnings}


def _split_swarm_service_name(name: str) -> tuple[str, str]:
    if "_" not in name:
        return "", name
    stack, service = name.split("_", 1)
    return stack, service


def _service_desired_replicas(spec: Dict[str, Any], counts: Dict[str, int]) -> int:
    mode = spec.get("Mode") if isinstance(spec.get("Mode"), dict) else {}
    replicated = mode.get("Replicated") if isinstance(mode.get("Replicated"), dict) else {}
    replicas = replicated.get("Replicas")
    if isinstance(replicas, int):
        return replicas
    total = counts["running"] + counts["pending"] + counts["failed"]
    return total or 1


def _dashboard_task_counts(tasks: list[Dict[str, Any]], node_by_id: dict[str, Dict[str, Any]]) -> tuple[Dict[str, int], list[str]]:
    current_tasks = [task for task in tasks if str(task.get("DesiredState") or "") == "running"]
    if not current_tasks:
        current_tasks = tasks
    counts = {"running": 0, "failed": 0, "pending": 0}
    nodes: list[str] = []
    for task in current_tasks:
        status = task.get("Status") if isinstance(task.get("Status"), dict) else {}
        state = str(status.get("State") or "").lower()
        if state == "running":
            counts["running"] += 1
        elif state in {"failed", "rejected"}:
            counts["failed"] += 1
        elif state and state not in {"shutdown", "complete", "remove"}:
            counts["pending"] += 1
        node_id = str(task.get("NodeID") or "")
        node = node_by_id.get(node_id)
        hostname = str((node or {}).get("hostname") or "")
        if hostname and state not in {"failed", "rejected", "shutdown", "complete", "remove"} and hostname not in nodes:
            nodes.append(hostname)
    return counts, nodes


def _service_health(desired: int, counts: Dict[str, int]) -> str:
    if desired > 0 and counts["running"] >= desired and counts["failed"] == 0 and counts["pending"] == 0:
        return "running"
    if counts["running"] > 0:
        return "degraded"
    if counts["pending"] > 0:
        return "pending"
    if counts["failed"] > 0:
        return "failed"
    return "unknown"


def _constraint_value(constraints: list[Any], key: str) -> str:
    for constraint in constraints:
        text = str(constraint)
        match = re.search(rf"{re.escape(key)}\s*==\s*([^\s]+)", text)
        if match:
            return match.group(1)
    return ""


def _traefik_route_from_labels(labels: Dict[str, Any]) -> Dict[str, str]:
    route: Dict[str, str] = {}
    for key, value in labels.items():
        label_key = str(key)
        label_value = str(value)
        if label_key.startswith("traefik.http.routers.") and label_key.endswith(".rule") and not route.get("domain"):
            host = _host_from_rule(label_value)
            if host:
                route["domain"] = host
        elif label_key.endswith(".loadbalancer.server.port") and not route.get("port"):
            route["port"] = label_value
        elif label_key == "traefik.swarm.network":
            route["network"] = label_value
    return route


def _host_from_rule(rule: str) -> str:
    for pattern in (r"Host\(`([^`]+)`\)", r'Host\("([^"]+)"\)', r"Host\('([^']+)'\)", r"Host\(([^),]+)\)"):
        match = re.search(pattern, rule)
        if match:
            return match.group(1).strip(" `\"'")
    return ""


def _dashboard_traffic_paths(
    services: list[Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
    dns_target: str,
) -> list[Dict[str, Any]]:
    paths: list[Dict[str, Any]] = []
    for service in services:
        route_id = str(service.get("routeId") or service.get("name") or "")
        exposure = str(service.get("exposure") or "none")
        nodes = [str(node) for node in service.get("nodes", []) if node]
        service_target = str(service.get("name") or "")
        if service.get("targetPort"):
            service_target = f"{service_target}:{service['targetPort']}"
        if exposure in {"cn-edge", "external-edge"}:
            segments = ["Cloudflare DNS", dns_target or "DNS target missing", "Traefik", service_target]
            segments.extend(nodes or ["no running tasks"])
        elif exposure == "tailscale-relay":
            route_file = service.get("_routeFile") if isinstance(service.get("_routeFile"), dict) else route_files.get(route_id, {})
            upstreams = [str(item) for item in route_file.get("upstreams", [])] if isinstance(route_file, dict) else []
            segments = ["Cloudflare DNS", dns_target or "DNS target missing", "Traefik", "Tailscale"]
            segments.extend(upstreams or nodes or ["upstream unresolved"])
        elif exposure == "cloudflare-tunnel":
            segments = ["Cloudflare", "cloudflared", service_target]
            segments.extend(nodes or ["no running tasks"])
        else:
            segments = ["client/internal", service_target]
            segments.extend(nodes or ["no running tasks"])
        paths.append(
            {
                "id": route_id,
                "kind": exposure,
                "domain": str(service.get("domain") or ""),
                "segments": segments,
            }
        )
    return paths


def _swarm_node_summary_item(node: Dict[str, Any]) -> Dict[str, Any]:
    spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
    description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
    status = node.get("Status") if isinstance(node.get("Status"), dict) else {}
    manager_status = node.get("ManagerStatus") if isinstance(node.get("ManagerStatus"), dict) else {}
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    return {
        "id": str(node.get("ID") or "")[:12],
        "hostname": str(description.get("Hostname") or ""),
        "role": str(spec.get("Role") or ""),
        "availability": str(spec.get("Availability") or ""),
        "state": str(status.get("State") or ""),
        "addr": str(status.get("Addr") or ""),
        "region": str(labels.get("region") or ""),
        "lumaNode": str(labels.get("luma.node.name") or ""),
        "lumaNodeId": str(labels.get("luma.node.id") or ""),
        "ingress": str(labels.get("ingress") or ""),
        "leader": bool(manager_status.get("Leader")),
        "reachability": str(manager_status.get("Reachability") or ""),
        "labels": {str(key): str(value) for key, value in labels.items()},
    }


def _swarm_nodes_summary() -> Dict[str, Any]:
    try:
        nodes = docker_request("GET", "/nodes")
    except LumaError as exc:
        return {"available": False, "error": str(exc), "nodes": []}
    if not isinstance(nodes, list):
        return {"available": False, "error": "Docker API returned invalid node list", "nodes": []}
    items: list[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        items.append(_swarm_node_summary_item(node))
    items.sort(key=lambda item: item.get("hostname") or item.get("id") or "")
    return {"available": True, "nodes": items}


def remove_swarm_node(node_name: str, record: Dict[str, Any] | None = None) -> Dict[str, Any]:
    nodes = docker_request("GET", "/nodes")
    if not isinstance(nodes, list):
        raise LumaError("Docker API returned invalid node list")
    match = _match_luma_swarm_node(nodes, node_name=node_name, record=record)
    if not match:
        return {"removed": False, "message": "Swarm node not found"}
    node_id = str(match.get("ID") or "")
    if not node_id:
        raise LumaError("Docker API returned invalid node id")
    spec = match.get("Spec") if isinstance(match.get("Spec"), dict) else {}
    manager_status = match.get("ManagerStatus") if isinstance(match.get("ManagerStatus"), dict) else {}
    role = str(spec.get("Role") or "")
    if role == "manager" or manager_status:
        raise LumaError(f"refusing to remove Swarm manager node: {node_name}")
    docker_request("DELETE", f"/nodes/{urllib.parse.quote(node_id, safe='')}?force=1")
    return {"removed": True, "nodeId": node_id, "message": f"Swarm node removed: {node_id}"}


def _match_luma_swarm_node(
    nodes: list[Any],
    *,
    node_name: str,
    record: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    record = record or {}
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    wanted_ids = {
        str(value).strip()
        for value in [
            node_name,
            record.get("swarmNodeId"),
            labels.get("luma.node.id"),
        ]
        if str(value or "").strip()
    }
    wanted_names = {
        str(value).strip()
        for value in [
            node_name,
            record.get("displayName"),
            record.get("swarmHostname"),
            labels.get("luma.node.name"),
        ]
        if str(value or "").strip()
    }
    matches: list[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("ID") or "")
        spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
        node_labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        candidate_ids = {node_id, str(node_labels.get("luma.node.id") or "")}
        candidate_names = {
            str(description.get("Hostname") or ""),
            str(node_labels.get("luma.node.name") or ""),
        }
        if any(_node_id_matches(candidate, wanted) for candidate in candidate_ids for wanted in wanted_ids):
            matches.append(node)
            continue
        if wanted_names.intersection(candidate_names):
            matches.append(node)
    unique: dict[str, Dict[str, Any]] = {str(node.get("ID") or ""): node for node in matches if str(node.get("ID") or "")}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        raise LumaError(f"multiple Swarm nodes match {node_name}; remove by exact Swarm NodeID")
    return None


def _node_id_matches(candidate: str, wanted: str) -> bool:
    candidate = candidate.strip()
    wanted = wanted.strip()
    return bool(candidate and wanted and (candidate == wanted or candidate.startswith(wanted) or wanted.startswith(candidate)))


class DockerSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_request(method: str, path: str, body: Dict[str, Any] | None = None) -> Any:
    conn = DockerSocketConnection()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn.request(method, f"/v{api_version}" + path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
    except OSError as exc:
        raise LumaError("Docker socket unavailable to Luma Control") from exc
    finally:
        conn.close()
    if response.status >= 400:
        raise LumaError(f"Docker API error {response.status}: {raw}")
    if not raw:
        return None
    return json.loads(raw)


def docker_request_raw(method: str, path: str, *, headers: Dict[str, str] | None = None) -> tuple[int, str]:
    conn = DockerSocketConnection()
    try:
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn.request(method, f"/v{api_version}" + path, headers=headers or {})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, raw
    except OSError as exc:
        raise LumaError("Docker socket unavailable to Luma Control") from exc
    finally:
        conn.close()


def resolve_tailscale_relay(service: ServiceSpec) -> ServiceSpec:
    if service.exposure != "tailscale-relay":
        return service
    if service.relay.get("url") or service.relay.get("host"):
        return service
    upstream_urls = _swarm_task_upstream_urls(service)
    relay = dict(service.relay)
    relay["urls"] = upstream_urls
    return replace(service, relay=relay)


def _swarm_task_upstream_urls(service: ServiceSpec) -> list[str]:
    port = int(service.publish_port or service.port or 0)
    if port < 1:
        raise LumaError("tailscale-relay requires a valid port")
    deadline = time.monotonic() + 60
    last_count = 0
    while True:
        urls, running_count = _running_task_upstream_urls(service, port)
        if running_count >= service.replicas and urls:
            return urls
        last_count = running_count
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise LumaError(
        f"tailscale-relay service {service.slug} has {last_count}/{service.replicas} running tasks; "
        "wait for the service to become ready or check luma status"
    )


def _running_task_upstream_urls(service: ServiceSpec, port: int) -> tuple[list[str], int]:
    node_by_id = _swarm_node_map()
    service_name = f"{service.slug}_{service.slug}"
    filters = urllib.parse.quote(json.dumps({"service": {service_name: True}, "desired-state": {"running": True}}), safe="")
    tasks = docker_request("GET", f"/tasks?filters={filters}")
    if not isinstance(tasks, list):
        raise LumaError("Docker API returned invalid task list")
    urls: list[str] = []
    running_count = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("Status") if isinstance(task.get("Status"), dict) else {}
        if str(status.get("State") or "") != "running":
            continue
        node_id = str(task.get("NodeID") or "")
        node = node_by_id.get(node_id)
        if not node:
            continue
        hostname = str(node.get("hostname") or "")
        if service.node_id and node_id != service.node_id:
            continue
        if service.node and not service.node_id and str(node.get("lumaNode") or hostname) != service.node:
            continue
        region = str(node.get("region") or "")
        if region != service.region:
            raise LumaError(f"service task is on node {hostname or node_id} in region {region or '-'}, not {service.region}")
        addr = str(node.get("addr") or "")
        if not addr:
            raise LumaError(f"node {hostname or node_id} has no reachable Docker node address")
        running_count += 1
        url = f"http://{addr}:{port}"
        if url not in urls:
            urls.append(url)
    return urls, running_count


def _swarm_node_map() -> dict[str, dict[str, str]]:
    nodes = docker_request("GET", "/nodes")
    if not isinstance(nodes, list):
        raise LumaError("Docker API returned invalid node list")
    result: dict[str, dict[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("ID") or "")
        if not node_id:
            continue
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
        labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
        status = node.get("Status") if isinstance(node.get("Status"), dict) else {}
        result[node_id] = {
            "hostname": str(description.get("Hostname") or ""),
            "region": str(labels.get("region") or ""),
            "lumaNode": str(labels.get("luma.node.name") or ""),
            "lumaNodeId": str(labels.get("luma.node.id") or ""),
            "addr": str(status.get("Addr") or ""),
        }
    return result


def resolve_service_image(
    config: Any,
    service: ServiceSpec,
    *,
    registry_auth: Dict[str, str] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    for image in images:
        image_registry_auth = registry_auth if registry_auth_matches_image(registry_auth, image) else None
        try:
            force_pull = image_uses_mutable_latest_tag(image)
            resolved_image = ensure_image_present(image, registry_auth=image_registry_auth, force_pull=force_pull)
            deploy_image = resolved_image or image
            result = {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": force_pull,
            }
            return replace(service, image=deploy_image), result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("unable to pull service image; tried " + "; ".join(errors))


def ensure_image_present(
    image: str,
    *,
    registry_auth: Dict[str, str] | None = None,
    force_pull: bool = False,
) -> str | None:
    encoded = urllib.parse.quote(image, safe="")
    if not force_pull:
        status, _ = docker_request_raw("GET", f"/images/{encoded}/json")
        if status == 200:
            return None
    from_image = urllib.parse.quote(image, safe="")
    headers = {}
    auth_header = docker_registry_auth_header(registry_auth)
    if auth_header:
        headers["X-Registry-Auth"] = auth_header
    status, raw = docker_request_raw("POST", f"/images/create?fromImage={from_image}", headers=headers)
    if status >= 400:
        raise LumaError(f"Docker pull failed with HTTP {status}: {raw.strip()}")
    if '"error"' in raw:
        raise LumaError(f"Docker pull failed: {raw.strip()}")
    if force_pull:
        return _image_repo_digest(image)
    return None


def _image_repo_digest(image: str) -> str:
    encoded = urllib.parse.quote(image, safe="")
    status, raw = docker_request_raw("GET", f"/images/{encoded}/json")
    if status >= 400:
        raise LumaError(f"Docker image inspect failed with HTTP {status}: {raw.strip()}")
    try:
        details = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise LumaError(f"Docker image inspect returned invalid JSON: {raw.strip()}") from exc
    digests = details.get("RepoDigests")
    if not isinstance(digests, list):
        digests = []
    repository = _image_repository(image)
    for digest in digests:
        if not isinstance(digest, str) or "@sha256:" not in digest:
            continue
        digest_repository = digest.split("@", 1)[0]
        if digest_repository == repository:
            return digest
    for digest in digests:
        if isinstance(digest, str) and "@sha256:" in digest:
            return digest
    raise LumaError(f"Docker pull succeeded but no repo digest was found for {image}; use a pinned digest or fixed tag")


def _image_repository(image: str) -> str:
    image_ref = image.split("@", 1)[0]
    slash = image_ref.rfind("/")
    colon = image_ref.rfind(":")
    if colon > slash:
        return image_ref[:colon]
    return image_ref


def _fallback_images(config: Any, image: str) -> list[str]:
    if _has_registry(image):
        return []
    mirrors = config.defaults.get("imageMirrors") or [
        "docker.1panel.live",
        "docker.1ms.run",
        "docker.m.daocloud.io",
    ]
    if not isinstance(mirrors, list):
        return []
    return [f"{mirror}/{image}" for mirror in mirrors if isinstance(mirror, str) and mirror]


def _has_registry(image: str) -> bool:
    first = image.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


def label_swarm_node(node_name: str, labels: Dict[str, str], *, node_id: str = "") -> None:
    deadline = time.monotonic() + 60
    match = None
    while True:
        nodes = docker_request("GET", "/nodes")
        if not isinstance(nodes, list):
            raise LumaError("Docker API returned invalid node list")
        match = _match_swarm_node(nodes, node_name=node_name, node_id=node_id)
        if match or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not match:
        target = node_id or node_name
        raise LumaError(f"swarm node not found: {target}")
    node_id = match["ID"]
    inspected = docker_request("GET", f"/nodes/{urllib.parse.quote(node_id, safe='')}")
    version = inspected.get("Version", {}).get("Index")
    spec = inspected.get("Spec")
    if not version or not isinstance(spec, dict):
        raise LumaError(f"Docker API returned invalid node spec: {node_name}")
    current_labels = spec.get("Labels") or {}
    if not isinstance(current_labels, dict):
        current_labels = {}
    spec["Labels"] = {**current_labels, **labels}
    docker_request("POST", f"/nodes/{urllib.parse.quote(node_id, safe='')}/update?version={version}", spec)


def _match_swarm_node(nodes: list[Any], *, node_name: str, node_id: str = "") -> Dict[str, Any] | None:
    if node_id:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            candidate = str(node.get("ID") or "")
            if candidate == node_id or candidate.startswith(node_id) or node_id.startswith(candidate):
                return node
    matches: list[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        if description.get("Hostname") == node_name:
            matches.append(node)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LumaError(f"multiple swarm nodes have hostname {node_name}; rerun node join with a CLI that sends Swarm NodeID")
    return None


DASHBOARD_ASSETS = {
    "/dashboard/": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/dashboard/app.js": ("dashboard/app.js", "application/javascript; charset=utf-8"),
    "/dashboard/styles.css": ("dashboard/styles.css", "text/css; charset=utf-8"),
}


def _dashboard_asset(path: str) -> tuple[bytes, str]:
    if path not in DASHBOARD_ASSETS:
        raise LumaError("dashboard asset not found")
    relative_path, content_type = DASHBOARD_ASSETS[path]
    return asset_path(relative_path).read_bytes(), content_type


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "LumaControl/0.1"

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path == "/dashboard":
            self.send_response(308)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        if parsed_path.startswith("/dashboard/"):
            try:
                body, content_type = _dashboard_asset(parsed_path)
                cache_control = "no-store" if parsed_path == "/dashboard/" else "public, max-age=60"
                self._bytes(200, body, content_type, cache_control=cache_control)
            except (LumaError, OSError):
                self._json(404, {"error": "not found"})
            return
        if parsed_path == "/v1/health":
            self._json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "nodeJoinModel": "region-first",
                    "capabilities": ["node-region", "service-proxy", "dashboard", "service-remove"],
                },
            )
            return
        try:
            token = bearer_token(self.headers)
            if parsed_path == "/v1/registries":
                self._json(200, handle_registry_list(token))
                return
            if parsed_path == "/v1/secrets":
                self._json(200, handle_secret_list(token))
                return
            if parsed_path == "/v1/storage":
                self._json(200, handle_storage_list(token))
                return
            if parsed_path == "/v1/status":
                self._json(200, handle_control_status(token))
                return
            if parsed_path == "/v1/dashboard":
                self._json(200, handle_dashboard(token))
                return
        except LumaError as exc:
            code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            self._json(code, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            token = bearer_token(self.headers)
            if self.path == "/v1/auth/login/verify":
                self._json(200, handle_login_verify(token))
                return
            if self.path == "/v1/nodes/register":
                self._json(200, handle_node_register(token, body))
                return
            if self.path == "/v1/nodes/label":
                self._json(200, handle_node_label(token, body))
                return
            if self.path == "/v1/nodes/unregister":
                self._json(200, handle_node_unregister(token, body))
                return
            if self.path == "/v1/deployments":
                self._json(200, handle_deployment(token, body))
                return
            if self.path == "/v1/deployments/preview":
                self._json(200, handle_deployment_preview(token, body))
                return
            if self.path == "/v1/deployments/stream":
                self._stream_deployment(token, body)
                return
            if self.path == "/v1/compose-deployments":
                self._json(200, handle_compose_deployment(token, body))
                return
            if self.path == "/v1/compose-deployments/preview":
                self._json(200, handle_compose_deployment_preview(token, body))
                return
            if self.path == "/v1/compose-deployments/stream":
                self._stream_compose_deployment(token, body)
                return
            if self.path == "/v1/compose-deployments/remove":
                self._json(200, handle_compose_remove(token, body))
                return
            if self.path == "/v1/storage/apply":
                self._json(200, handle_storage_apply(token, body))
                return
            if self.path == "/v1/storage":
                self._json(200, handle_storage_set(token, body))
                return
            if self.path == "/v1/storage/remove":
                self._json(200, handle_storage_remove(token, body))
                return
            if self.path == "/v1/services/remove":
                self._json(200, handle_service_remove(token, body))
                return
            if self.path == "/v1/applications/restart":
                self._json(200, handle_application_restart(token, body))
                return
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_set(token, body))
                return
            if self.path == "/v1/registries":
                self._json(200, handle_registry_set(token, body))
                return
            if self.path == "/v1/registries/remove":
                self._json(200, handle_registry_remove(token, body))
                return
            self._json(404, {"error": "not found"})
        except LumaError as exc:
            code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            self._json(code, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _stream_deployment(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_deployment(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc)})
        except Exception as exc:
            emit({"status": "fail", "message": str(exc)})

    def _stream_compose_deployment(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_compose_deployment(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc)})
        except Exception as exc:
            emit({"status": "fail", "message": str(exc)})

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise LumaError("request body must be a JSON object")
        return data

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._bytes(status, raw, "application/json")

    def _bytes(self, status: int, body: bytes, content_type: str, *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), ControlHandler)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="luma-control")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--domain", required=True)
    init.add_argument("--cluster-id")
    init.add_argument("--overwrite", action="store_true")
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--host", default="0.0.0.0")
    serve_cmd.add_argument("--port", type=int, default=int(os.environ.get("LUMA_CONTROL_PORT", "8080")))
    args = parser.parse_args(argv)
    if args.command == "init":
        state = init_state(domain=args.domain, cluster_id=args.cluster_id, overwrite=args.overwrite)
        print(f"Cluster: {state['clusterId']}")
        print(f"Deploy token: {state['deployToken']}")
        print(f"Join token: {state['joinToken']}")
        return 0
    if args.command == "serve":
        serve(args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
