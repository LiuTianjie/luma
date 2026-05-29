from __future__ import annotations

import os
import shlex
from pathlib import Path

from .config import LumaConfig, NodeConfig
from .egress import minimal_mihomo_config_from_url
from .errors import LumaError
from .profiles import PROFILES, Profile
from .remote import RemoteExecutor


ROOT = "/opt/luma"


def _deploy_stack(remote: RemoteExecutor, local_stack: Path, stack_name: str) -> str:
    if not local_stack.exists():
        raise LumaError(f"stack file not found: {local_stack}")
    remote_tmp = f"/tmp/luma-{stack_name}-stack.yml"
    remote.upload(local_stack, remote_tmp)
    remote.sudo(f"docker stack deploy -c {shlex.quote(remote_tmp)} {shlex.quote(stack_name)}")
    return f"Stack deployed: {stack_name}"


def install_docker(remote: RemoteExecutor) -> str:
    remote.sudo(
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml; "
        "systemctl enable --now docker"
    )
    return "Docker installed"


def setup_tailscale(node: NodeConfig, *, authkey: str | None = None) -> list[str]:
    remote = RemoteExecutor(node)
    authkey = authkey or os.environ.get("TAILSCALE_AUTHKEY")
    hostname = str(node.raw.get("tailscaleHostname") or f"luma-{node.name}")
    results = []
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
    remote.sudo(
        "set -euo pipefail; "
        "tailscale up "
        f"--authkey {shlex.quote(authkey)} "
        f"--hostname {shlex.quote(hostname)} "
        "--accept-dns=false"
    )
    results.append(f"Tailscale connected: {hostname}")
    return results


def configure_firewall(remote: RemoteExecutor, *, allow_portainer: bool = True) -> str:
    commands = [
        "ufw --force enable",
        "ufw allow OpenSSH",
        "ufw allow 80/tcp",
        "ufw allow 443/tcp",
        "ufw deny 7890/tcp",
        "ufw deny 7890/udp",
    ]
    if allow_portainer:
        commands.append("ufw allow 9443/tcp")
    remote.sudo("set -euo pipefail; " + "; ".join(commands))
    return "Firewall configured"


def ensure_swarm(remote: RemoteExecutor, node: NodeConfig) -> str:
    advertise = node.raw.get("advertiseAddr") or node.public_ip
    init = "docker swarm init"
    if advertise:
        init += f" --advertise-addr {shlex.quote(str(advertise))}"
    remote.sudo(
        "set -euo pipefail; "
        'state="$(docker info --format \'{{.Swarm.LocalNodeState}}\')"; '
        f'if [ "$state" = "inactive" ]; then {init}; fi'
    )
    return "Swarm ready"


def ensure_networks(remote: RemoteExecutor, config: LumaConfig, *, include_egress: bool = True) -> str:
    networks = [config.public_network]
    if include_egress:
        networks.append(config.egress_network)
    for network in networks:
        remote.sudo(
            f"docker network inspect {shlex.quote(network)} >/dev/null 2>&1 || "
            f"docker network create --driver=overlay --attachable {shlex.quote(network)} >/dev/null"
        )
    return "Overlay networks ready"


def apply_labels(remote: RemoteExecutor, profile: Profile, node: NodeConfig) -> str:
    label_args = []
    for key, value in profile.labels.items():
        label_args.extend(["--label-add", f"{key}={value}"])
    for role in profile.roles:
        label_args.extend(["--label-add", f"role.{role}=true"])
    label_text = " ".join(shlex.quote(arg) for arg in label_args)
    remote.sudo(
        'node="$(docker node ls --format \'{{.Hostname}}\' | head -1)"; '
        f"docker node update {label_text} \"$node\" >/dev/null"
    )
    return f"Labels applied: {profile.name}"


def prepare_paths(remote: RemoteExecutor) -> str:
    remote.sudo(
        f"set -euo pipefail; "
        f"install -d -m 755 {ROOT}/routes; "
        f"install -d -m 700 {ROOT}/egress-gateway"
    )
    return "Runtime paths ready"


def bootstrap_node(config: LumaConfig, node: NodeConfig, profile: Profile) -> list[str]:
    remote = RemoteExecutor(node)
    results = [
        install_docker(remote),
        *setup_tailscale(node),
        ensure_swarm(remote, node),
        ensure_networks(remote, config, include_egress=("egress" in profile.roles or "edge" in profile.roles)),
        apply_labels(remote, profile, node),
        prepare_paths(remote),
        configure_firewall(remote, allow_portainer=True),
    ]
    if "edge" in profile.roles or profile.name == "single-node":
        results.append(_deploy_stack(remote, Path("stacks/core/traefik/stack.yml"), "traefik"))
    if "swarm-manager" in profile.roles or profile.name == "single-node":
        results.append(_deploy_stack(remote, Path("stacks/core/portainer/stack.yml"), "portainer"))
    return results


def setup_egress(config: LumaConfig, node: NodeConfig, subscription_url: str) -> list[str]:
    remote = RemoteExecutor(node)
    config_text = minimal_mihomo_config_from_url(subscription_url)
    results = [
        prepare_paths(remote),
        remote.write_secret(config_text, f"{ROOT}/egress-gateway/config.yaml"),
        ensure_networks(remote, config, include_egress=True),
    ]
    egress_profile = PROFILES["egress-gateway"]
    results.append(apply_labels(remote, egress_profile, node))
    results.append(_deploy_stack(remote, Path("stacks/core/egress-gateway/stack.yml"), "egress"))
    remote.sudo(
        "set -euo pipefail; "
        "mkdir -p /etc/systemd/system/docker.service.d; "
        "cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'\n"
        "[Service]\n"
        "Environment=\"HTTP_PROXY=http://127.0.0.1:7890\"\n"
        "Environment=\"HTTPS_PROXY=http://127.0.0.1:7890\"\n"
        "Environment=\"NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16\"\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl restart docker"
    )
    results.append("Docker daemon proxy configured")
    return results


def setup_portainer(node: NodeConfig) -> list[str]:
    remote = RemoteExecutor(node)
    return [
        prepare_paths(remote),
        _deploy_stack(remote, Path("stacks/core/portainer/stack.yml"), "portainer"),
    ]


def deploy_direct(config: LumaConfig, node: NodeConfig, stack_file: Path, stack_name: str, route_file: Path | None = None) -> list[str]:
    remote = RemoteExecutor(node)
    results = []
    if route_file:
        remote.upload(route_file, f"{ROOT}/routes/{route_file.name}")
        results.append(f"Route uploaded: {route_file.name}")
    results.append(_deploy_stack(remote, stack_file, stack_name))
    return results
