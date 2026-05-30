from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import tempfile
import urllib.parse
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from ..cloudflare import sync_dns
from ..config import load_config
from ..errors import LumaError
from ..portainer import deploy_with_portainer
from ..profiles import PROFILES
from ..render import render_stack, render_tailscale_route, route_path, stack_path
from ..service import ServiceSpec, load_service
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


def handle_node_register(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="join")
    node_name = str(body.get("nodeName") or "").strip()
    profile = str(body.get("profile") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not profile or not region:
        raise LumaError("nodeName, profile, and region are required")
    _remember_node(state, node_name, profile=profile, region=region, status="registered")
    save_state(state)
    return {
        "clusterId": state["clusterId"],
        "managerAddr": state.get("managerAddr", ""),
        "swarmJoinToken": state.get("swarmJoinToken", ""),
        "nodeName": node_name,
        "profile": profile,
        "region": region,
    }


def handle_node_label(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="join")
    node_name = str(body.get("nodeName") or "").strip()
    profile_name = str(body.get("profile") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not profile_name or not region:
        raise LumaError("nodeName, profile, and region are required")
    if profile_name not in PROFILES:
        raise LumaError(f"unknown profile: {profile_name}")
    labels = labels_for_profile(profile_name, region)
    label_swarm_node(node_name, labels)
    _remember_node(state, node_name, profile=profile_name, region=region, status="labeled", labels=labels)
    save_state(state)
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "labels": labels,
        "message": f"Node labels applied: {node_name}",
    }


def handle_deployment(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
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

    service, image_result = resolve_service_image(config, service)
    target = _resolve_control_path(stack_path(config, service), config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stack_text = render_stack(config, service)
    stack_env = _stack_env_for_text(stack_text)
    target.write_text(stack_text, encoding="utf-8")
    written = [str(target)]
    if service.exposure == "tailscale-relay":
        route_target = _resolve_control_path(route_path(config, service), config_path)
        route_target.parent.mkdir(parents=True, exist_ok=True)
        route_target.write_text(render_tailscale_route(config, service), encoding="utf-8")
        written.append(str(route_target))
    dns_result = None if body.get("skipDns") else sync_dns(config, service)
    webhook_result = None if body.get("skipWebhook") else deploy_with_portainer(config, service, stack_text, state, stack_env=stack_env)
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "written": written,
        "image": image_result,
        "dns": dns_result,
        "webhook": webhook_result,
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


def _valid_env_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def labels_for_profile(profile_name: str, region: str) -> Dict[str, str]:
    profile = PROFILES[profile_name]
    labels = {str(key): str(value) for key, value in profile.labels.items()}
    labels["region"] = region
    for role in profile.roles:
        labels[f"role.{role}"] = "true"
    return labels


def _remember_node(state: Dict[str, Any], node_name: str, **values: Any) -> None:
    nodes = state.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        state["nodes"] = nodes
    current = nodes.get(node_name)
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    nodes[node_name] = current


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
            self._json(200, {"ok": True})
            return
        try:
            token = bearer_token(self.headers)
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_list(token))
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
