from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from ..cloudflare import sync_dns
from ..config import load_config
from ..errors import LumaError
from ..portainer import deploy_with_portainer
from ..render import render_stack, render_tailscale_route, route_path, stack_path
from ..service import VALID_REGIONS, ServiceSpec, load_service
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
    portainer_api_url = str(state.get("portainerApiUrl") or config.portainer.get("apiUrl") or "")
    portainer_endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
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
            "tokenConfigured": bool(os.environ.get(token_env) or token_env in secrets),
            "target": str(dns_target or ""),
            "ready": dns_provider == "cloudflare" and bool(zone_id) and bool(os.environ.get(token_env) or token_env in secrets) and bool(dns_target),
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
    }


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
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    labels = labels_for_region(region)
    label_swarm_node(node_name, labels)
    values: Dict[str, Any] = {"region": region, "status": "labeled", "labels": labels}
    if registered_name and registered_name != node_name:
        values["displayName"] = registered_name
    _remember_node(state, node_name, merge_from=registered_name, **values)
    save_state(state)
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "displayName": registered_name if registered_name and registered_name != node_name else node_name,
        "labels": labels,
        "message": f"Node labels applied: {node_name}",
    }


def handle_deployment(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
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
    steps.append({"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"})

    service, image_result = _deploy_step(steps, "Resolve image", lambda: resolve_service_image(config, service))
    target = _resolve_control_path(stack_path(config, service), config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stack_text = _deploy_step(steps, "Render stack", lambda: render_stack(config, service))
    stack_env = _deploy_step(steps, "Resolve stack secrets", lambda: _stack_env_for_text(stack_text))
    _deploy_step(steps, "Write stack", lambda: target.write_text(stack_text, encoding="utf-8"))
    written = [str(target)]
    dns_result = _deploy_step(steps, "Sync DNS", lambda: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service))
    webhook_result = _deploy_step(
        steps,
        "Deploy Portainer stack",
        lambda: "Portainer deploy skipped: --skip-webhook"
        if body.get("skipWebhook")
        else deploy_with_portainer(config, service, stack_text, state, stack_env=stack_env),
    )
    if service.exposure == "tailscale-relay":
        route_target = _resolve_control_path(route_path(config, service), config_path)
        route_target.parent.mkdir(parents=True, exist_ok=True)
        route_service = service
        relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host"))
        if body.get("skipWebhook") and not relay_is_explicit:
            _deploy_step(steps, "Write route", lambda: "Route skipped: --skip-webhook requires deploy to infer tailscale relay")
        else:
            if not body.get("skipWebhook"):
                route_service = _deploy_step(steps, "Resolve relay", lambda: resolve_tailscale_relay(service))
            _deploy_step(steps, "Write route", lambda: route_target.write_text(render_tailscale_route(config, route_service), encoding="utf-8"))
            written.append(str(route_target))
    probe_result = _deploy_step(
        steps,
        "Probe public route",
        lambda: "Public route probe skipped: --skip-webhook" if body.get("skipWebhook") else _probe_public_route(service),
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


def _deploy_step(steps: list[dict[str, str]], name: str, action: Any) -> Any:
    try:
        result = action()
    except LumaError as exc:
        steps.append({"name": name, "status": "fail", "message": str(exc)})
        raise LumaError(f"{name} failed: {exc}") from exc
    message = _step_message(result)
    steps.append({"name": name, "status": "ok", "message": message})
    return result


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
                "region": str(raw.get("region") or labels.get("region") or ""),
                "status": str(raw.get("status") or "registered"),
                "labels": {str(key): str(value) for key, value in labels.items()},
            }
        )
    return items


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
        spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        status = node.get("Status") if isinstance(node.get("Status"), dict) else {}
        manager_status = node.get("ManagerStatus") if isinstance(node.get("ManagerStatus"), dict) else {}
        labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
        items.append(
            {
                "id": str(node.get("ID") or "")[:12],
                "hostname": str(description.get("Hostname") or ""),
                "role": str(spec.get("Role") or ""),
                "availability": str(spec.get("Availability") or ""),
                "state": str(status.get("State") or ""),
                "addr": str(status.get("Addr") or ""),
                "region": str(labels.get("region") or ""),
                "ingress": str(labels.get("ingress") or ""),
                "leader": bool(manager_status.get("Leader")),
                "reachability": str(manager_status.get("Reachability") or ""),
                "labels": {str(key): str(value) for key, value in labels.items()},
            }
        )
    items.sort(key=lambda item: item.get("hostname") or item.get("id") or "")
    return {"available": True, "nodes": items}


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


def docker_request_raw(method: str, path: str) -> tuple[int, str]:
    conn = DockerSocketConnection()
    try:
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn.request(method, f"/v{api_version}" + path)
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
        if service.node and hostname != service.node:
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
            "addr": str(status.get("Addr") or ""),
        }
    return result


def resolve_service_image(config: Any, service: ServiceSpec) -> tuple[ServiceSpec, Dict[str, Any]]:
    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    for image in images:
        try:
            ensure_image_present(image)
            result = {
                "requested": service.image,
                "selected": image,
                "fallback": image != service.image,
            }
            return replace(service, image=image), result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("unable to pull service image; tried " + "; ".join(errors))


def ensure_image_present(image: str) -> None:
    encoded = urllib.parse.quote(image, safe="")
    status, _ = docker_request_raw("GET", f"/images/{encoded}/json")
    if status == 200:
        return
    from_image = urllib.parse.quote(image, safe="")
    status, raw = docker_request_raw("POST", f"/images/create?fromImage={from_image}")
    if status >= 400:
        raise LumaError(f"Docker pull failed with HTTP {status}: {raw.strip()}")
    if '"error"' in raw:
        raise LumaError(f"Docker pull failed: {raw.strip()}")


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


def label_swarm_node(node_name: str, labels: Dict[str, str]) -> None:
    nodes = docker_request("GET", "/nodes")
    if not isinstance(nodes, list):
        raise LumaError("Docker API returned invalid node list")
    match = None
    for node in nodes:
        description = node.get("Description") if isinstance(node, dict) else {}
        if isinstance(description, dict) and description.get("Hostname") == node_name:
            match = node
            break
    if not match:
        raise LumaError(f"swarm node not found: {node_name}")
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


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "LumaControl/0.1"

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "nodeJoinModel": "region-first",
                    "capabilities": ["node-region", "service-proxy"],
                },
            )
            return
        try:
            token = bearer_token(self.headers)
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_list(token))
                return
            if self.path == "/v1/status":
                self._json(200, handle_control_status(token))
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
            if self.path == "/v1/deployments":
                self._json(200, handle_deployment(token, body))
                return
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_set(token, body))
                return
            self._json(404, {"error": "not found"})
        except LumaError as exc:
            code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            self._json(code, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return

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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


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
