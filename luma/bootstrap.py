from __future__ import annotations

import json
import ipaddress
import os
import secrets
import shlex
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Union

from .assets import asset_path, asset_text
from .config import LumaConfig, NodeConfig
from .cloudflare import sync_control_dns
from .egress import minimal_mihomo_config_from_url
from .errors import LumaError
from .io import dump_yaml
from .local import LocalExecutor
from .profiles import PROFILES, Profile
from .remote import RemoteExecutor


ROOT = "/opt/luma"
DEFAULT_TRAEFIK_IMAGE = "docker.1panel.live/library/traefik:v3.6"
DEFAULT_PORTAINER_IMAGE = "docker.1panel.live/portainer/portainer-ce:2.21.5"
DEFAULT_PORTAINER_AGENT_IMAGE = "docker.1panel.live/portainer/agent:2.21.5"
DEFAULT_EGRESS_IMAGE = "docker.1panel.live/metacubex/mihomo:latest"
DEFAULT_CONTROL_IMAGE = "ghcr.io/liutianjie/luma-control:latest"
DEFAULT_PORTAINER_API_URL = "https://127.0.0.1:9443/api"
PORTAINER_IMAGE_FALLBACKS = [
    "docker.m.daocloud.io/portainer/portainer-ce:2.21.5",
    "docker.1ms.run/portainer/portainer-ce:2.21.5",
    "portainer/portainer-ce:2.21.5",
]
PORTAINER_AGENT_IMAGE_FALLBACKS = [
    "docker.m.daocloud.io/portainer/agent:2.21.5",
    "docker.1ms.run/portainer/agent:2.21.5",
    "portainer/agent:2.21.5",
]
Progress = Callable[[str], None]


def _emit(emit: Progress | None, message: str) -> None:
    if emit:
        emit(message)


def _step(results: list[str], emit: Progress | None, title: str, action: Callable[[], str | list[str]], *, fix: str | None = None) -> None:
    _emit(emit, f"[start] {title}")
    try:
        output = action()
    except Exception:
        _emit(emit, f"[fail] {title}")
        if fix:
            _emit(emit, f"  Fix: {fix}")
        raise
    lines = output if isinstance(output, list) else [output]
    for line in lines:
        results.append(line)
        _emit(emit, f"[ok] {line}")


Executor = Union[RemoteExecutor, LocalExecutor]


def _deploy_stack(remote: Executor, local_stack: Path, stack_name: str) -> str:
    if not local_stack.exists():
        raise LumaError(f"stack file not found: {local_stack}")
    remote_tmp = f"/tmp/luma-{stack_name}-stack.yml"
    remote.upload(local_stack, remote_tmp)
    _docker(remote, f"docker stack deploy --resolve-image never -c {shlex.quote(remote_tmp)} {shlex.quote(stack_name)}")
    return f"Stack deployed: {stack_name}"


def _deploy_stack_text(remote: Executor, stack_text: str, stack_name: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
        fh.write(stack_text)
        local_stack = Path(fh.name)
    try:
        return _deploy_stack(remote, local_stack, stack_name)
    finally:
        local_stack.unlink(missing_ok=True)


def _core_image(config: LumaConfig, key: str, env_name: str, default: str) -> str:
    images = config.defaults.get("images") or {}
    if isinstance(images, dict) and images.get(key):
        return str(images[key])
    return os.environ.get(env_name, default)


def _core_image_candidates(config: LumaConfig, key: str, env_name: str, default: str, fallbacks: list[str]) -> list[str]:
    selected = _core_image(config, key, env_name, default)
    override = os.environ.get(env_name)
    images = config.defaults.get("images") or {}
    if override or (isinstance(images, dict) and images.get(key)):
        return [selected]
    candidates = [selected, *fallbacks]
    return list(dict.fromkeys(candidates))


def _wait_service_ready(remote: Executor, service_name: str, *, timeout_seconds: int = 120) -> str:
    attempts = max(1, timeout_seconds // 2)
    _docker(
        remote,
        "set -euo pipefail; "
        f"service={shlex.quote(service_name)}; "
        f"for i in $(seq 1 {attempts}); do "
        "replicas=$(docker service ls --filter name=\"$service\" --format '{{.Name}} {{.Replicas}}' | awk -v s=\"$service\" '$1 == s {print $2; exit}'); "
        "if [ -n \"$replicas\" ]; then "
        "running=${replicas%%/*}; desired=${replicas##*/}; "
        "[ \"$running\" = \"$desired\" ] && [ \"$desired\" != \"0\" ] && exit 0; "
        "fi; "
        "sleep 2; "
        "done; "
        "docker service ps \"$service\" --no-trunc; "
        "exit 1"
    )
    return f"Service ready: {service_name}"


def _deploy_egress_stack(remote: Executor, config: LumaConfig) -> str:
    image = _egress_image(config)
    stack_text = asset_text("stacks/core/egress-gateway/stack.yml")
    stack_text = stack_text.replace(f"${{LUMA_EGRESS_IMAGE:-{DEFAULT_EGRESS_IMAGE}}}", image)
    return _deploy_stack_text(remote, stack_text, "egress")


def _egress_image(config: LumaConfig) -> str:
    return _core_image(
        config,
        "egressGateway",
        "LUMA_EGRESS_IMAGE",
        DEFAULT_EGRESS_IMAGE,
    )


def _traefik_image(config: LumaConfig) -> str:
    return _core_image(config, "traefik", "LUMA_TRAEFIK_IMAGE", DEFAULT_TRAEFIK_IMAGE)


def _traefik_ports(config: LumaConfig) -> tuple[int, int]:
    ports = config.defaults.get("ports") or {}
    if not isinstance(ports, dict):
        ports = {}
    http_port = int(ports.get("traefikHttp") or ports.get("http") or 80)
    https_port = int(ports.get("traefikHttps") or ports.get("https") or 443)
    return http_port, https_port


def _acme_email(config: LumaConfig) -> str:
    dns = config.dns
    zone = str(dns.get("zone") or "").strip()
    return (
        os.environ.get("TRAEFIK_ACME_EMAIL")
        or str(config.defaults.get("acmeEmail") or "").strip()
        or str(dns.get("acmeEmail") or "").strip()
        or (f"admin@{zone}" if zone and zone != "example.com" else "admin@example.com")
    )


def _portainer_image(config: LumaConfig) -> str:
    return _core_image(config, "portainer", "LUMA_PORTAINER_IMAGE", DEFAULT_PORTAINER_IMAGE)


def _portainer_image_candidates(config: LumaConfig) -> list[str]:
    return _core_image_candidates(config, "portainer", "LUMA_PORTAINER_IMAGE", DEFAULT_PORTAINER_IMAGE, PORTAINER_IMAGE_FALLBACKS)


def _portainer_agent_image(config: LumaConfig) -> str:
    return _core_image(config, "portainerAgent", "LUMA_PORTAINER_AGENT_IMAGE", DEFAULT_PORTAINER_AGENT_IMAGE)


def _portainer_agent_image_candidates(config: LumaConfig) -> list[str]:
    return _core_image_candidates(
        config,
        "portainerAgent",
        "LUMA_PORTAINER_AGENT_IMAGE",
        DEFAULT_PORTAINER_AGENT_IMAGE,
        PORTAINER_AGENT_IMAGE_FALLBACKS,
    )


def _control_image(config: LumaConfig) -> str:
    return _core_image(
        config,
        "lumaControl",
        "LUMA_CONTROL_IMAGE",
        DEFAULT_CONTROL_IMAGE,
    )


def _pull_image(remote: Executor, image: str) -> str:
    exists = _last_command_value(_docker(remote, f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1 && echo yes || echo no"))
    if exists == "yes":
        return f"Image already present: {image}"
    _docker(remote, f"docker pull {shlex.quote(image)}")
    return f"Image pulled: {image}"


def _pull_first_available(remote: Executor, images: list[str]) -> tuple[str, str]:
    errors = []
    for image in images:
        try:
            return image, _pull_image(remote, image)
        except Exception as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("failed to pull any candidate image:\n" + "\n".join(errors))


def _deploy_traefik(remote: Executor, config: LumaConfig) -> list[str]:
    image = _traefik_image(config)
    http_port, https_port = _traefik_ports(config)
    stack_text = asset_text("stacks/core/traefik/stack.yml")
    stack_text = stack_text.replace("traefik:v3.6", image)
    stack_text = stack_text.replace("admin@example.com", _acme_email(config))
    stack_text = stack_text.replace("published: 80", f"published: {http_port}")
    stack_text = stack_text.replace("published: 443", f"published: {https_port}")
    return [
        _pull_image(remote, image),
        _deploy_stack_text(remote, stack_text, "traefik"),
        _wait_service_ready(remote, "traefik_traefik"),
    ]


def _deploy_portainer(remote: Executor, config: LumaConfig) -> list[str]:
    agent_image, agent_result = _pull_first_available(remote, _portainer_agent_image_candidates(config))
    portainer_image, portainer_result = _pull_first_available(remote, _portainer_image_candidates(config))
    stack_text = asset_text("stacks/core/portainer/stack.yml")
    stack_text = stack_text.replace("portainer/portainer-ce:2.21.5", portainer_image)
    stack_text = stack_text.replace("portainer/agent:2.21.5", agent_image)
    return [
        agent_result,
        portainer_result,
        _deploy_stack_text(remote, stack_text, "portainer"),
        _wait_service_ready(remote, "portainer_portainer"),
        _wait_service_ready(remote, "portainer_agent"),
    ]


def _reset_portainer_state(remote: Executor) -> str:
    _docker(
        remote,
        "set -euo pipefail; "
        "docker stack rm portainer >/dev/null 2>&1 || true; "
        "for i in $(seq 1 60); do "
        "services=$(docker service ls --filter label=com.docker.stack.namespace=portainer --format '{{.Name}}'); "
        "[ -z \"$services\" ] && break; "
        "sleep 2; "
        "done; "
        "services=$(docker service ls --filter label=com.docker.stack.namespace=portainer --format '{{.Name}}'); "
        "[ -z \"$services\" ]; "
        "if docker volume inspect portainer_portainer_data >/dev/null 2>&1; then "
        "containers=$(docker ps -aq --filter volume=portainer_portainer_data); "
        "if [ -n \"$containers\" ]; then docker rm -f $containers >/dev/null; fi; "
        "docker volume rm portainer_portainer_data >/dev/null; "
        "fi",
    )
    return "Portainer state reset"


def deploy_control_stack(remote: Executor, config: LumaConfig, domain: str) -> list[str]:
    image = _control_image(config)
    _ensure_control_image(remote, image)
    stack_text = asset_text("stacks/core/luma-control/stack.yml")
    stack_text = stack_text.replace("${LUMA_CONTROL_DOMAIN:-luma.local}", domain)
    stack_text = stack_text.replace(f"${{LUMA_CONTROL_IMAGE:-{DEFAULT_CONTROL_IMAGE}}}", image)
    return [
        _deploy_stack_text(remote, stack_text, "luma-control"),
        _force_update_service_image(remote, "luma-control_luma-control", image),
        _wait_service_ready(remote, "luma-control_luma-control"),
    ]


def _force_update_service_image(remote: Executor, service: str, image: str) -> str:
    _docker(
        remote,
        "set -euo pipefail; "
        f"docker service inspect {shlex.quote(service)} >/dev/null 2>&1; "
        f"docker service update --image {shlex.quote(image)} --force {shlex.quote(service)} >/dev/null",
    )
    return f"Service image refreshed: {service} -> {image}"


def _ensure_control_image(remote: Executor, image: str) -> str:
    image_arg = shlex.quote(image)
    if "/" in image:
        try:
            _docker(remote, f"docker pull {image_arg} >/dev/null 2>&1")
            return f"Control image pulled: {image}"
        except Exception:
            exists = _last_command_value(_docker(remote, f"docker image inspect {image_arg} >/dev/null 2>&1 && echo yes || echo no"))
            if exists == "yes":
                return f"Control image already present: {image}"
    status = _last_command_value(
        _docker(
            remote,
            "set -euo pipefail; "
            f"if docker image inspect {image_arg} >/dev/null 2>&1; then "
            "echo present; "
            f"elif docker pull {image_arg} >/dev/null 2>&1; then "
            "echo pulled; "
            "else "
            "echo build; "
            "fi",
        )
    )
    if status == "present":
        return f"Control image already present: {image}"
    if status == "pulled":
        return f"Control image pulled: {image}"
    dockerfile = asset_path("Dockerfile.control")
    pyproject = asset_path("pyproject.toml")
    readme = asset_path("README.md")
    package_dir = Path(__file__).resolve().parent
    remote.run("rm -rf /tmp/luma-control-build; mkdir -p /tmp/luma-control-build")
    remote.upload(dockerfile, "/tmp/luma-Dockerfile.control")
    remote.upload(pyproject, "/tmp/luma-control-build/pyproject.toml")
    remote.upload(readme, "/tmp/luma-control-build/README.md")
    remote.upload(package_dir, "/tmp/luma-control-build/luma")
    _docker(
        remote,
        "set -euo pipefail; "
        "cp /tmp/luma-Dockerfile.control /tmp/luma-control-build/Dockerfile.control; "
        f"docker build -f /tmp/luma-control-build/Dockerfile.control -t {shlex.quote(image)} /tmp/luma-control-build",
    )
    return f"Control image built: {image}"


def install_control_config(remote: Executor, config: LumaConfig, node: NodeConfig | None = None) -> str:
    content = Path(config.path).read_text(encoding="utf-8") if config.path else dump_yaml(_bootstrap_config(config, node))
    return remote.write_secret(content, f"{ROOT}/luma.yaml", mode="644")


def _bootstrap_config(config: LumaConfig, node: NodeConfig | None = None) -> dict[str, object]:
    raw = dict(config.raw)
    raw.setdefault("project", "luma")
    providers = raw.setdefault("providers", {})
    if isinstance(providers, dict):
        providers.setdefault("portainer", {"webhookUrlEnv": "PORTAINER_WEBHOOK_URL"})
    defaults = raw.setdefault("defaults", {})
    if isinstance(defaults, dict):
        defaults.setdefault("exposure", "cn-edge")
        defaults.setdefault("stackRoot", "stacks")
        defaults.setdefault("routesRoot", "routes")
        defaults.setdefault("publicNetwork", "public")
        defaults.setdefault("egressNetwork", "egress")
        defaults.setdefault("entrypoint", "websecure")
        defaults.setdefault("certResolver", "letsencrypt")
    nodes = raw.setdefault("nodes", {})
    if node and isinstance(nodes, dict) and node.name not in nodes:
        nodes[node.name] = {
            "host": node.host,
            "publicIp": node.public_ip,
            "region": node.region,
            "roles": list(node.roles),
        }
    raw.setdefault("git", {"autoCommit": False, "autoPush": False, "commitMessage": "deploy {name} to {region}"})
    return raw


def install_control_state(remote: Executor, state: dict[str, object]) -> str:
    content = json.dumps(state, indent=2, sort_keys=True) + "\n"
    return remote.write_secret(content, f"{ROOT}/control/control.json", mode="600")


def initialize_portainer(remote: Executor, state: dict[str, object]) -> str:
    username = str(
        os.environ.get("LUMA_PORTAINER_ADMIN_USERNAME")
        or state.get("portainerAdminUsername")
        or "admin"
    )
    password = str(
        os.environ.get("LUMA_PORTAINER_ADMIN_PASSWORD")
        or state.get("portainerAdminPassword")
        or f"{secrets.token_urlsafe(24)}A1!"
    )
    api_url = str(state.get("portainerApiUrl") or DEFAULT_PORTAINER_API_URL)
    state["portainerAdminUsername"] = username
    state["portainerAdminPassword"] = password
    state["portainerApiUrl"] = api_url

    status, payload = _portainer_request(api_url, "GET", "/users/admin/check")
    if status == 303 and isinstance(payload, dict) and payload.get("message") == "Administrator initialization timeout":
        _docker(remote, "docker service update --force portainer_portainer >/dev/null")
        _wait_service_ready(remote, "portainer_portainer")
        status, payload = _portainer_request(api_url, "GET", "/users/admin/check")
    if status == 404:
        status, payload = _portainer_request(
            api_url,
            "POST",
            "/users/admin/init",
            {"Username": username, "Password": password},
        )
    if status not in {200, 204}:
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise LumaError(f"Portainer admin initialization failed: HTTP {status} {detail}")
    status, payload = _portainer_request(api_url, "POST", "/auth", {"Username": username, "Password": password})
    if status != 200 or not isinstance(payload, dict) or not payload.get("jwt"):
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise LumaError(
            f"Portainer authentication failed: HTTP {status} {detail}. "
            "If Portainer was already initialized, set LUMA_PORTAINER_ADMIN_PASSWORD to the existing admin password "
            "or reset the Portainer admin password, then rerun bootstrap manager."
        )
    jwt = str(payload["jwt"])
    status, payload = _portainer_request(api_url, "GET", "/endpoints", token=jwt)
    if status == 200 and isinstance(payload, list) and not payload:
        status, payload = _portainer_form_request(
            api_url,
            "POST",
            "/endpoints",
            {
                "Name": "luma-local",
                "EndpointCreationType": "2",
                "URL": "tcp://tasks.agent:9001",
                "TLS": "true",
                "TLSSkipVerify": "true",
                "TLSSkipClientVerify": "true",
            },
            token=jwt,
        )
        if status in {200, 201} and isinstance(payload, dict):
            payload = [payload]
        elif status == 409:
            status, payload = _portainer_request(api_url, "GET", "/endpoints", token=jwt)
    if status != 200 or not isinstance(payload, list) or not payload:
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise LumaError(f"Portainer endpoint discovery failed: HTTP {status} {detail}")
    endpoint = payload[0]
    if not isinstance(endpoint, dict) or not endpoint.get("Id"):
        raise LumaError("Portainer returned an invalid endpoint")
    state["portainerEndpointId"] = int(endpoint["Id"])
    state["portainerEndpointName"] = str(endpoint.get("Name") or endpoint["Id"])
    return "Portainer initialized"


def bind_portainer_credentials(remote: Executor, config: LumaConfig, state: dict[str, object]) -> str | list[str]:
    try:
        return initialize_portainer(remote, state)
    except LumaError:
        return [
            _reset_portainer_state(remote),
            *_deploy_portainer(remote, config),
            initialize_portainer(remote, state),
        ]


def _portainer_request(
    api_url: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    token: str | None = None,
) -> tuple[int, object | None]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url.rstrip("/") + path, data=data, method=method, headers=headers)
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: object | None = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


def _portainer_form_request(
    api_url: str,
    method: str,
    path: str,
    form: dict[str, str],
    token: str | None = None,
) -> tuple[int, object | None]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url.rstrip("/") + path, data=data, method=method, headers=headers)
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: object | None = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


def configure_dns(remote: Executor) -> str:
    if _is_darwin(remote):
        return "DNS resolver configuration skipped on macOS"
    remote.sudo(
        "set -euo pipefail; "
        "if command -v resolvectl >/dev/null 2>&1 && systemctl list-unit-files systemd-resolved.service >/dev/null 2>&1; then "
        "install -d -m 755 /etc/systemd/resolved.conf.d; "
        "cat > /etc/systemd/resolved.conf.d/luma.conf <<'EOF'\n"
        "[Resolve]\n"
        "DNS=223.5.5.5 119.29.29.29 1.1.1.1\n"
        "FallbackDNS=8.8.8.8 9.9.9.9\n"
        "Domains=~.\n"
        "EOF\n"
        "systemctl restart systemd-resolved || true; "
        "iface=$(ip route show default | awk '{print $5; exit}'); "
        "if [ -n \"$iface\" ]; then "
        "resolvectl dns \"$iface\" 223.5.5.5 119.29.29.29 1.1.1.1 || true; "
        "resolvectl domain \"$iface\" '~.' || true; "
        "fi; "
        "fi"
    )
    return "DNS resolvers configured"


def install_docker(remote: Executor) -> str:
    if _is_darwin(remote):
        remote.run("command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1")
        return "Docker available"
    if remote.sudo_result("command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1").code == 0:
        return "Docker available"
    remote.sudo(
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "command -v apt-get >/dev/null 2>&1 || { "
        "echo 'automatic Docker installation currently supports apt-based Linux only; install Docker manually and rerun luma node join' >&2; "
        "exit 1; "
        "}; "
        "for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do "
        "[ -f \"$file\" ] || continue; "
        "sed -i "
        "-e 's#http://mirrors.ivolces.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' "
        "-e 's#https://mirrors.ivolces.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' "
        "\"$file\"; "
        "done; "
        "apt-get update; "
        "apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml; "
        "systemctl enable --now containerd || true; "
        "systemctl enable --now docker.socket || true; "
        "systemctl reset-failed docker || true; "
        "systemctl restart docker || systemctl start docker; "
        "docker info >/dev/null"
    )
    return "Docker installed"


def setup_tailscale(node: NodeConfig, *, authkey: str | None = None, executor: Executor | None = None) -> list[str]:
    remote = executor or RemoteExecutor(node)
    authkey = authkey or os.environ.get("TAILSCALE_AUTHKEY")
    hostname = str(node.raw.get("tailscaleHostname") or f"luma-{node.name}")
    results = []
    if _is_darwin(remote):
        if remote.run_result("command -v tailscale >/dev/null 2>&1").code != 0:
            results.append("Tailscale skipped on macOS: install Tailscale app if this node needs private routing")
            return results
        status = remote.run_result("tailscale status >/dev/null 2>&1")
        if status.code == 0:
            results.append("Tailscale already logged in")
        elif authkey:
            _run_tailscale_up(remote.run_result, authkey, hostname)
            results.append(f"Tailscale connected: {hostname}")
        else:
            results.append("Tailscale installed but not connected on macOS")
        return results
    remote.sudo(
        "set -euo pipefail; "
        "if ! command -v tailscale >/dev/null 2>&1; then "
        "curl -fsSL https://tailscale.com/install.sh | sh; "
        "fi; "
        "systemctl enable --now tailscaled"
    )
    results.append("Tailscale installed")
    status = remote.sudo_result("tailscale status >/dev/null 2>&1")
    if status.code == 0:
        results.append("Tailscale already logged in")
        return results
    if not authkey:
        results.append("Tailscale login skipped: set TAILSCALE_AUTHKEY and run luma tailscale connect " + node.name)
        return results
    _run_tailscale_up(lambda command: remote.sudo_result(f"set -euo pipefail; {command}"), authkey, hostname)
    results.append(f"Tailscale connected: {hostname}")
    return results


def _run_tailscale_up(run_result: Callable[[str], Any], authkey: str, hostname: str) -> None:
    result = run_result(_tailscale_up_command(authkey, hostname))
    if result.code == 0:
        return
    if _tailscale_up_requires_reset(result.output):
        result = run_result(_tailscale_up_command(authkey, hostname, reset=True))
        if result.code == 0:
            return
    output = _redact_tailscale_authkey(str(result.output), authkey).strip()
    raise LumaError(f"tailscale up failed:\n{output}")


def _tailscale_up_command(authkey: str, hostname: str, *, reset: bool = False) -> str:
    reset_flag = "--reset " if reset else ""
    return (
        "tailscale up "
        f"{reset_flag}"
        f"--authkey {shlex.quote(authkey)} "
        f"--hostname {shlex.quote(hostname)} "
        "--accept-dns=false "
        "--accept-routes"
    )


def _tailscale_up_requires_reset(output: str) -> bool:
    return "changing settings via 'tailscale up' requires mentioning all" in output


def _redact_tailscale_authkey(output: str, authkey: str) -> str:
    return output.replace(authkey, "<redacted>")


def configure_firewall(remote: Executor, *, http_port: int = 80, https_port: int = 443, allow_portainer: bool = True) -> str:
    if _is_darwin(remote):
        return "Firewall configuration skipped on macOS"
    commands = [
        "ufw --force enable",
        "ufw allow OpenSSH",
        f"ufw allow {int(http_port)}/tcp",
        f"ufw allow {int(https_port)}/tcp",
        "ufw allow 2377/tcp",
        "ufw allow 7946/tcp",
        "ufw allow 7946/udp",
        "ufw allow 4789/udp",
        "ufw deny 7890/tcp",
        "ufw deny 7890/udp",
    ]
    if allow_portainer:
        commands.append("ufw allow 9443/tcp")
    remote.sudo("set -euo pipefail; " + "; ".join(commands))
    return "Firewall configured"


def ensure_swarm(remote: Executor, node: NodeConfig) -> str:
    advertise = node.raw.get("swarmAdvertiseAddr") or _tailscale_ip(remote) or node.raw.get("advertiseAddr") or node.public_ip
    init = "docker swarm init"
    if advertise:
        init += f" --advertise-addr {shlex.quote(str(advertise))}"
        init += f" --listen-addr {shlex.quote(_listen_addr(str(advertise)))}"
    force_new_cluster = "docker swarm init --force-new-cluster"
    if advertise:
        force_new_cluster += f" --advertise-addr {shlex.quote(str(advertise))}"
        force_new_cluster += f" --listen-addr {shlex.quote(_listen_addr(str(advertise)))}"
    _docker(
        remote,
        "set -euo pipefail; "
        'state="$(docker info --format \'{{.Swarm.LocalNodeState}}\')"; '
        'node_addr="$(docker info --format \'{{.Swarm.NodeAddr}}\' 2>/dev/null || true)"; '
        'control="$(docker info --format \'{{.Swarm.ControlAvailable}}\' 2>/dev/null || true)"; '
        'manager_addr="$(docker node inspect self --format \'{{.ManagerStatus.Addr}}\' 2>/dev/null || true)"; '
        f'if [ "$state" = "inactive" ]; then {init}; '
        f"elif [ \"$control\" = \"true\" ] && [ -n {shlex.quote(str(advertise or ''))} ] && "
        f"{{ [ \"$node_addr\" != {shlex.quote(str(advertise or ''))} ] || [ \"$manager_addr\" != {shlex.quote(_listen_addr(str(advertise)) if advertise else '')} ]; }}; then {force_new_cluster}; "
        "fi"
    )
    return "Swarm ready"


def _listen_addr(advertise: str) -> str:
    host = advertise.rsplit(":", 1)[0] if ":" in advertise and advertise.count(":") == 1 else advertise
    return f"{host}:2377"


def ensure_networks(remote: Executor, config: LumaConfig, *, include_egress: bool = True) -> str:
    networks = [config.public_network]
    if include_egress:
        networks.append(config.egress_network)
    for network in networks:
        _docker(
            remote,
            f"docker network inspect {shlex.quote(network)} >/dev/null 2>&1 || "
            f"docker network create --driver=overlay --attachable {shlex.quote(network)} >/dev/null"
        )
    return "Overlay networks ready"


def apply_labels(remote: Executor, profile: Profile, node: NodeConfig) -> str:
    label_args = []
    for key, value in profile.labels.items():
        label_args.extend(["--label-add", f"{key}={value}"])
    for role in profile.roles:
        label_args.extend(["--label-add", f"role.{role}=true"])
    label_text = " ".join(shlex.quote(arg) for arg in label_args)
    _docker(
        remote,
        'node="$(docker node ls --format \'{{.Hostname}}\' | head -1)"; '
        f"docker node update {label_text} \"$node\" >/dev/null"
    )
    return f"Labels applied: {profile.name}"


def prepare_paths(remote: Executor) -> str:
    remote.sudo(
        f"set -euo pipefail; "
        f"install -d -m 755 {ROOT}/stacks; "
        f"install -d -m 755 {ROOT}/routes; "
        f"install -d -m 700 {ROOT}/control; "
        f"install -d -m 700 {ROOT}/egress-gateway"
    )
    return "Runtime paths ready"


def bootstrap_node(
    config: LumaConfig,
    node: NodeConfig,
    profile: Profile,
    *,
    run_egress: bool = True,
    reset_portainer_state: bool = False,
    emit: Progress | None = None,
    executor: Executor | None = None,
) -> list[str]:
    remote = executor or RemoteExecutor(node)
    local_mode = isinstance(remote, LocalExecutor)
    tailscale_fix = "Run: luma tailscale connect" if local_mode else f"Run: luma tailscale connect {node.name}"
    portainer_fix = "Run: luma portainer setup" if local_mode else f"Run: luma portainer setup {node.name}"
    egress_fix = "luma egress setup" if local_mode else f"luma egress setup {node.name}"
    bootstrap_fix = "Re-run luma bootstrap manager after fixing the error" if local_mode else f"Run: luma node bootstrap {node.name} --profile {profile.name}"
    results: list[str] = []
    _step(results, emit, "Configure system DNS", lambda: configure_dns(remote))
    _step(results, emit, "Install Docker", lambda: install_docker(remote))
    _step(results, emit, "Install and connect Tailscale", lambda: setup_tailscale(node, executor=remote), fix=tailscale_fix)
    _step(results, emit, "Initialize Docker Swarm", lambda: ensure_swarm(remote, node))
    _step(results, emit, "Create overlay networks", lambda: ensure_networks(remote, config, include_egress=("egress" in profile.roles or "edge" in profile.roles)))
    _step(results, emit, "Apply node labels", lambda: apply_labels(remote, profile, node))
    _step(results, emit, "Create runtime paths", lambda: prepare_paths(remote))
    http_port, https_port = _traefik_ports(config)
    _step(results, emit, "Configure firewall", lambda: configure_firewall(remote, http_port=http_port, https_port=https_port, allow_portainer=True))
    if "edge" in profile.roles or profile.name == "single-node":
        _step(results, emit, "Deploy Traefik", lambda: _deploy_traefik(remote, config), fix=bootstrap_fix)
    if "swarm-manager" in profile.roles or profile.name == "single-node":
        if reset_portainer_state:
            _step(results, emit, "Reset Portainer state", lambda: _reset_portainer_state(remote), fix=portainer_fix)
        _step(results, emit, "Deploy Portainer", lambda: _deploy_portainer(remote, config), fix=portainer_fix)
    if run_egress and "egress" in profile.roles:
        subscription_url = os.environ.get("EGRESS_SUBSCRIPTION_URL")
        if not subscription_url:
            _emit(emit, "[fail] Set up egress gateway")
            _emit(emit, f"  Fix: set EGRESS_SUBSCRIPTION_URL in .env, or rerun with --skip-egress and later run: {egress_fix}")
            raise LumaError(
                "missing EGRESS_SUBSCRIPTION_URL for egress bootstrap. "
                f"Set it in .env, or rerun with --skip-egress and later run: {egress_fix}"
            )
        _emit(emit, "[start] Set up egress gateway")
        try:
            results.extend(setup_egress(config, node, subscription_url, emit=emit, executor=remote))
        except Exception:
            _emit(emit, "[fail] Set up egress gateway")
            _emit(emit, f"  Fix: {egress_fix}")
            raise
        _emit(emit, "[ok] Egress setup complete")
    return results


def bootstrap_manager_local(config: LumaConfig, node: NodeConfig, profile: Profile, domain: str, state: dict[str, object], *, run_egress: bool = True, emit: Progress | None = None) -> list[str]:
    remote = LocalExecutor()
    results = bootstrap_node(
        config,
        node,
        profile,
        run_egress=run_egress,
        reset_portainer_state=False,
        emit=emit,
        executor=remote,
    )
    state["portainerApiUrl"] = _portainer_api_url_for_node(node)
    _step(
        results,
        emit,
        "Bind Portainer credentials",
        lambda: bind_portainer_credentials(remote, config, state),
        fix=(
            "Check Portainer service logs and rerun bootstrap manager"
        ),
    )
    _step(results, emit, "Save Portainer credentials", lambda: install_control_state(remote, state))
    manager_info = local_swarm_join_info(node)
    state.update(manager_info)
    state["portainerApiUrl"] = _portainer_api_url_for_control(node, manager_info)
    _step(results, emit, "Sync control DNS", lambda: sync_control_dns(config, domain))
    _step(results, emit, "Install control config", lambda: install_control_config(remote, config, node))
    _step(results, emit, "Install control state", lambda: install_control_state(remote, state))
    _step(results, emit, "Deploy Luma control API", lambda: deploy_control_stack(remote, config, domain), fix="Build and publish the Luma control image, then rerun bootstrap manager")
    return results


def join_local_node(node: NodeConfig, profile: Profile, manager_addr: str, swarm_token: str, *, emit: Progress | None = None) -> list[str]:
    remote = LocalExecutor()
    results: list[str] = []
    _step(results, emit, "Install Docker", lambda: install_docker(remote))
    _step(results, emit, "Install and connect Tailscale", lambda: setup_tailscale(node, executor=remote), fix="Run: luma tailscale connect")
    _step(results, emit, "Join Docker Swarm", lambda: _join_swarm(remote, manager_addr, swarm_token))
    return results


def local_swarm_join_info(node: NodeConfig) -> dict[str, str]:
    remote = LocalExecutor()
    token = _last_command_value(_docker(remote, "docker swarm join-token -q worker"))
    swarm_id = _last_command_value(_docker(remote, "docker info --format '{{.Swarm.Cluster.ID}}'"))
    manager = str(node.raw.get("swarmJoinAddr") or _tailscale_ip(remote) or node.raw.get("advertiseAddr") or node.public_ip or os.uname().nodename)
    if ":" not in manager:
        manager = f"{manager}:2377"
    return {"managerAddr": manager, "swarmJoinToken": token, "swarmId": swarm_id}


def _last_command_value(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    if line.startswith("[sudo]") and ":" in line:
        return line.rsplit(":", 1)[-1].strip()
    return line


def _tailscale_ip(remote: Executor) -> str | None:
    result = remote.run_result("command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1")
    if result.code != 0:
        return None
    value = _last_command_value(result.output)
    return value or None


def _portainer_api_url_for_node(node: NodeConfig) -> str:
    host = str(node.raw.get("portainerHost") or node.public_ip or "127.0.0.1")
    port = int(node.raw.get("portainerPort") or 9443)
    return f"https://{host}:{port}/api"


def _portainer_api_url_for_control(node: NodeConfig, manager_info: dict[str, str]) -> str:
    explicit_host = node.raw.get("portainerHost")
    if explicit_host:
        host = str(explicit_host)
    elif node.public_ip and not _is_loopback_host(node.public_ip):
        host = node.public_ip
    else:
        manager_addr = str(manager_info.get("managerAddr") or "")
        host = manager_addr.rsplit(":", 1)[0] if manager_addr else ""
        if not host or _is_loopback_host(host):
            host = str(node.public_ip or "127.0.0.1")
    port = int(node.raw.get("portainerPort") or 9443)
    return f"https://{host}:{port}/api"


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _join_swarm(remote: Executor, manager_addr: str, swarm_token: str) -> str:
    if not manager_addr or not swarm_token:
        raise LumaError("control plane did not return managerAddr and swarmJoinToken")
    advertise = _tailscale_ip(remote)
    if not advertise and _is_tailscale_manager_addr(manager_addr):
        raise LumaError(
            "managerAddr is a Tailscale address but this node is not connected to Tailscale. "
            "Set TAILSCALE_AUTHKEY and rerun luma node join, run luma tailscale connect first, "
            "or configure swarmJoinAddr on the manager to use a reachable public/private address."
        )
    advertise_arg = f" --advertise-addr {shlex.quote(advertise)}" if advertise else ""
    _docker(
        remote,
        "set -euo pipefail; "
        'state="$(docker info --format \'{{.Swarm.LocalNodeState}}\')"; '
        'node_addr="$(docker info --format \'{{.Swarm.NodeAddr}}\' 2>/dev/null || true)"; '
        'managers="$(docker info --format \'{{range .Swarm.RemoteManagers}}{{.Addr}} {{end}}\' 2>/dev/null || true)"; '
        f"if [ \"$state\" = \"active\" ] && [ -n {shlex.quote(advertise or '')} ] && [ \"$node_addr\" != {shlex.quote(advertise or '')} ]; then "
        "docker swarm leave; state=inactive; "
        "fi; "
        f"if [ \"$state\" = \"active\" ] && ! printf '%s\\n' \"$managers\" | grep -F {shlex.quote(manager_addr)} >/dev/null 2>&1; then "
        "docker swarm leave; state=inactive; "
        "fi; "
        'if [ "$state" = "inactive" ]; then '
        f"docker swarm join --token {shlex.quote(swarm_token)}{advertise_arg} {shlex.quote(manager_addr)}; "
        "fi"
    )
    return "Swarm joined"


def _is_tailscale_manager_addr(manager_addr: str) -> bool:
    host = manager_addr.rsplit(":", 1)[0] if manager_addr.count(":") == 1 else manager_addr
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in ipaddress.ip_network("100.64.0.0/10")


def local_docker_node_name() -> str:
    remote = LocalExecutor()
    output = _last_command_value(_docker(remote, "docker info --format '{{.Name}}'"))
    return output or os.uname().nodename


def local_docker_node_id() -> str:
    remote = LocalExecutor()
    output = _last_command_value(_docker(remote, "docker info --format '{{.Swarm.NodeID}}'"))
    return output


def _docker(remote: Executor, command: str) -> str:
    if _is_darwin(remote):
        return remote.run(command)
    return remote.sudo(command)


def _is_darwin(remote: Executor) -> bool:
    return _uname(remote) == "Darwin"


def _uname(remote: Executor) -> str:
    result = remote.run_result("uname -s")
    if result.code != 0:
        return ""
    return result.output.strip().splitlines()[-1]


def setup_egress(config: LumaConfig, node: NodeConfig, subscription_url: str, *, emit: Progress | None = None, executor: Executor | None = None) -> list[str]:
    remote = executor or RemoteExecutor(node)
    results: list[str] = []
    config_text = minimal_mihomo_config_from_url(subscription_url)
    _step(results, emit, "Configure system DNS", lambda: configure_dns(remote))
    _step(results, emit, "Create egress runtime paths", lambda: prepare_paths(remote))
    _step(results, emit, "Write egress config secret", lambda: remote.write_secret(config_text, f"{ROOT}/egress-gateway/config.yaml"))
    _step(results, emit, "Ensure egress network", lambda: ensure_networks(remote, config, include_egress=True))
    _step(
        results,
        emit,
        "Disable Docker daemon proxy for egress bootstrap",
        lambda: _disable_docker_proxy(remote),
    )
    _step(
        results,
        emit,
        "Pull egress image without Docker daemon proxy",
        lambda: _pull_image(remote, _egress_image(config)),
        fix="Check defaults.images.egressGateway points to a domestic mirror image",
    )
    egress_profile = PROFILES["egress-gateway"]
    _step(results, emit, "Apply egress gateway label", lambda: apply_labels(remote, egress_profile, node))
    _step(
        results,
        emit,
        "Deploy egress gateway",
        lambda: [_deploy_egress_stack(remote, config), _wait_service_ready(remote, "egress_mihomo")],
        fix=f"Run: luma egress setup {node.name}",
    )
    _step(
        results,
        emit,
        "Configure Docker daemon proxy",
        lambda: _configure_docker_proxy(remote),
    )
    _step(
        results,
        emit,
        "Refresh core services",
        lambda: _refresh_core_services(remote),
    )
    return results


def _disable_docker_proxy(remote: Executor) -> str:
    remote.sudo(
        "set -euo pipefail; "
        "rm -f /etc/systemd/system/docker.service.d/http-proxy.conf; "
        "systemctl daemon-reload; "
        "systemctl restart docker"
    )
    return "Docker daemon proxy disabled for egress bootstrap"


def _configure_docker_proxy(remote: Executor) -> str:
    remote.sudo(
        "set -euo pipefail; "
        "mkdir -p /etc/systemd/system/docker.service.d; "
        "cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'\n"
        "[Service]\n"
        "Environment=\"HTTP_PROXY=http://127.0.0.1:7890\"\n"
        "Environment=\"HTTPS_PROXY=http://127.0.0.1:7890\"\n"
        "Environment=\"NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run\"\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl restart docker"
    )
    return "Docker daemon proxy configured"


def _refresh_core_services(remote: Executor) -> str:
    remote.sudo(
        "set -euo pipefail; "
        "for service in traefik_traefik portainer_portainer portainer_agent; do "
        "docker service inspect \"$service\" >/dev/null 2>&1 && docker service update --force \"$service\" >/dev/null || true; "
        "done"
    )
    return "Core services refresh requested"


def setup_portainer(node: NodeConfig, *, emit: Progress | None = None, executor: Executor | None = None) -> list[str]:
    remote = executor or RemoteExecutor(node)
    results: list[str] = []
    config = LumaConfig({}, None)
    _step(results, emit, "Create runtime paths", lambda: prepare_paths(remote))
    _step(results, emit, "Deploy Portainer", lambda: _deploy_portainer(remote, config), fix=f"Run: luma portainer setup {node.name}")
    return results
