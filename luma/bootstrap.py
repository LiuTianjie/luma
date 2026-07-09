from __future__ import annotations

import json
import ipaddress
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Union

from .config import LumaConfig, NodeConfig
from .cloudflare import sync_control_dns
from .egress import minimal_mihomo_config_from_url
from .errors import LumaError
from .io import dump_yaml
from .local import LocalExecutor
from .profiles import PROFILES, Profile
from .registry import image_uses_mutable_latest_tag, registry_host_from_image
from .remote import RemoteExecutor


ROOT = "/opt/luma"
DEFAULT_TRAEFIK_IMAGE = "docker.1panel.live/library/traefik:v3.6"
DEFAULT_EGRESS_IMAGE = "docker.1panel.live/metacubex/mihomo:latest"
DEFAULT_CONTROL_IMAGE = "ghcr.io/liutianjie/luma-control:latest"
EGRESS_PROXY_URL = "http://127.0.0.1:7890"
EGRESS_NO_PROXY = "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run"
DEFAULT_EGRESS_PULL_REGISTRIES = {
    "docker.io",
    "registry-1.docker.io",
    "index.docker.io",
    "ghcr.io",
    "quay.io",
    "gcr.io",
    "k8s.gcr.io",
    "registry.k8s.io",
    "mcr.microsoft.com",
    "public.ecr.aws",
    "nvcr.io",
}
Progress = Callable[[str], None]


def _emit(emit: Progress | None, message: str) -> None:
    if emit:
        emit(message)


def _step(results: list[str], emit: Progress | None, title: str, action: Callable[[], str | list[str]], *, fix: str | None = None) -> None:
    _emit(emit, f"[start] {title}")
    try:
        output = action()
    except Exception as exc:
        # Emit the cause, not just the step title: otherwise the deploy event
        # stream records that a step failed but gives no way to tell why.
        detail = str(exc).strip()
        _emit(emit, f"[fail] {title}" + (f": {detail}" if detail else ""))
        if fix:
            _emit(emit, f"  Fix: {fix}")
        raise
    lines = output if isinstance(output, list) else [output]
    for line in lines:
        results.append(line)
        _emit(emit, f"[ok] {line}")


Executor = Union[RemoteExecutor, LocalExecutor]


def _core_image(config: LumaConfig, key: str, env_name: str, default: str) -> str:
    images = config.defaults.get("images") or {}
    if isinstance(images, dict) and images.get(key):
        return str(images[key])
    return os.environ.get(env_name, default)


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


def deploy_control_stack(
    remote: Executor,
    config: LumaConfig,
    domain: str,
    *,
    emit: Progress | None = None,
    require_pull_egress: bool = True,
    node_name: str | None = None,
) -> list[str]:
    results: list[str] = []
    engine = str(config.defaults.get("engine") or "nomad")
    image = _control_image(config)
    if require_pull_egress and _control_image_pull_requires_egress(image):
        _step(results, emit, "Ensure control image pull egress", lambda: _ensure_control_image_pull_egress(remote, image))
    _step(results, emit, "Pull Luma control image", lambda: _ensure_control_image(remote, image))
    deploy_image = image
    if image_uses_mutable_latest_tag(image):
        resolved: dict[str, str] = {}

        def resolve_digest() -> str:
            digest = _control_image_repo_digest(remote, image)
            resolved["image"] = digest
            return f"Control image digest resolved: {digest}"

        _step(results, emit, "Resolve Luma control image digest", resolve_digest)
        deploy_image = resolved["image"]

    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")

    from .nomad_render import render_control_job

    node = node_name or local_host_name()
    job_json = render_control_job(image=deploy_image, node_name=node)
    _step(results, emit, "Check Nomad tmpfs compatibility", lambda: _nomad_tmpfs_compat_status(remote))
    _step(results, emit, "Deploy Luma control job", lambda: _deploy_nomad_job(remote, job_json, "luma-control"))
    _step(results, emit, "Wait Luma control job", lambda: _wait_nomad_job(remote, "luma-control"))
    return results


def _deploy_nomad_job(remote: Executor, job_json: str, job_id: str) -> str:
    """Submit a rendered Nomad job (JSON) via the local Nomad agent.

    Uses base64 to avoid any shell-quoting hazard with the JSON payload.
    """
    import base64

    b64 = base64.b64encode(job_json.encode("utf-8")).decode("ascii")
    try:
        remote.run(
            "set -e; "
            "tmp=$(mktemp /tmp/luma-nomad-job.XXXXXX.json); "
            "trap 'rm -f \"$tmp\"' EXIT; "
            f"printf %s {shlex.quote(b64)} | base64 -d > \"$tmp\"; "
            "nomad job run -json \"$tmp\""
        )
    except LumaError as exc:
        _raise_nomad_tmpfs_error(exc)
    return f"Nomad job deployed: {job_id}"


def _raise_nomad_tmpfs_error(error: LumaError) -> None:
    text = str(error)
    lower = text.lower()
    if "noswap" in lower or ("tmpfs" in lower and "task_dir" in lower):
        raise LumaError(
            "Nomad failed while preparing the task secrets tmpfs. "
            "This is usually the Linux tmpfs `noswap` compatibility path: "
            "Nomad should fall back on kernels that do not support `noswap`, "
            "but this manager's Nomad/kernel combination did not complete the allocation. "
            "Check `nomad version`, `uname -r`, and `journalctl -u nomad -n 120 --no-pager`; "
            "then upgrade/reinstall Nomad or upgrade the manager kernel and rerun `luma update manager`.\n\n"
            f"Original error:\n{text.strip()}"
        ) from error
    raise error


def _nomad_tmpfs_compat_status(remote: Executor) -> str:
    """Describe whether Nomad's secrets tmpfs noswap fallback should be available.

    Nomad 1.9.5+ tries `noswap` for the per-task secrets tmpfs and falls back
    when the kernel rejects it. We avoid doing our own probe mount here because
    that would print the same kernel warning operators are trying to understand.
    """
    os_name = remote.run_result("uname -s 2>/dev/null || true").output.strip().lower()
    if "linux" not in os_name:
        return "Nomad tmpfs compatibility check skipped: non-Linux manager"

    kernel = remote.run_result("uname -r 2>/dev/null || true").output.strip()
    nomad_output = remote.run_result("nomad version 2>/dev/null | head -1 || true").output.strip()
    nomad_version = _parse_nomad_version(nomad_output)
    kernel_version = _parse_kernel_version(kernel)

    if kernel_version and kernel_version >= (6, 4):
        return f"Nomad secrets tmpfs noswap supported by Linux kernel {kernel}"

    if nomad_version and nomad_version >= (1, 9, 5):
        return (
            "Nomad secrets tmpfs noswap fallback available"
            f" (Nomad {'.'.join(str(p) for p in nomad_version)}, Linux {kernel or 'unknown'}); "
            "older kernels may still print a harmless `tmpfs: Unknown parameter 'noswap'` warning"
        )

    if nomad_version:
        return (
            "Nomad secrets tmpfs noswap not expected: "
            f"Nomad {'.'.join(str(p) for p in nomad_version)} predates the known noswap secrets tmpfs change"
        )

    return "Nomad secrets tmpfs compatibility unknown: could not read Nomad version"


def _parse_nomad_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"Nomad\s+v?(\d+)\.(\d+)\.(\d+)", output or "", re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _parse_kernel_version(output: str) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _wait_nomad_job(remote: Executor, job_id: str, *, timeout: int = 120) -> str:
    """Poll until the Nomad job reports a running allocation."""
    deadline = time.monotonic() + timeout
    while True:
        result = remote.run_result(
            f"nomad job status -short {shlex.quote(job_id)} 2>/dev/null | grep -iE 'Status' | head -1"
        )
        if "running" in result.output.lower():
            return f"Nomad job running: {job_id}"
        if time.monotonic() >= deadline:
            break
        time.sleep(4)
    raise LumaError(
        f"Nomad job {job_id} did not reach running within {timeout}s. "
        f"Check `nomad job status {job_id}` and alloc logs."
    )

def _ensure_control_image(remote: Executor, image: str) -> str:
    image_arg = shlex.quote(image)
    try:
        _docker(
            remote,
            f"docker pull {image_arg}",
        )
    except Exception as exc:
        detail = str(exc)
        suffix = f" Docker error: {detail}" if detail else ""
        raise LumaError(
            f"failed to pull Luma Control image: {image}. "
            "Publish the image or set LUMA_CONTROL_IMAGE/defaults.images.lumaControl to a pullable image tag. "
            "If this manager cannot reach the registry directly, configure EGRESS_SUBSCRIPTION_URL "
            "and rerun without --skip-egress."
            f"{suffix}"
        ) from exc
    else:
        return f"Control image pulled: {image}"


def _resolve_control_image(remote: Executor, image: str) -> tuple[str, str]:
    egress_result = _ensure_control_image_pull_egress(remote, image)
    pull_result = _ensure_control_image(remote, image)
    result = "; ".join(part for part in [egress_result, pull_result] if part)
    if not image_uses_mutable_latest_tag(image):
        return image, result
    digest_image = _control_image_repo_digest(remote, image)
    return digest_image, f"{result}; resolved digest: {digest_image}"


def _ensure_control_image_pull_egress(remote: Executor, image: str) -> str:
    registry = registry_host_from_image(image)
    if not _control_image_pull_requires_egress(image):
        return ""
    _require_egress_gateway_running(remote)
    if _docker_daemon_uses_egress_proxy(remote):
        return f"Control image pull egress ready for {registry}: Docker daemon proxy {EGRESS_PROXY_URL}"
    _configure_docker_proxy(remote)
    for _attempt in range(30):
        try:
            if _docker_daemon_uses_egress_proxy(remote):
                return f"Control image pull egress configured for {registry}: Docker daemon proxy {EGRESS_PROXY_URL}"
        except Exception:
            pass
        time.sleep(1)
    raise LumaError(f"control image pull egress proxy was configured, but Docker daemon did not report {EGRESS_PROXY_URL}")


def _control_image_pull_requires_egress(image: str) -> bool:
    return registry_host_from_image(image) in _egress_pull_registries()


def _egress_pull_registries() -> set[str]:
    raw = os.environ.get("LUMA_EGRESS_PULL_REGISTRIES")
    if raw is None:
        return set(DEFAULT_EGRESS_PULL_REGISTRIES)
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if values == {"none"}:
        return set()
    return values


def _require_egress_gateway_running(remote: Executor) -> None:
    result = remote.run_result(
        "command -v nomad >/dev/null 2>&1 && "
        "nomad job status -short egress 2>/dev/null | grep -qiE 'Status[[:space:]]*=[[:space:]]*running|Status[[:space:]]+running'"
    )
    if result.code != 0:
        raise LumaError("control image pull egress requires a running Nomad egress job; run `luma egress setup` on the manager")


def _docker_daemon_uses_egress_proxy(remote: Executor) -> bool:
    output = _docker(
        remote,
        "docker info --format 'HTTPProxy={{.HTTPProxy}} HTTPSProxy={{.HTTPSProxy}}'",
    )
    values = _parse_key_values(output)
    expected = EGRESS_PROXY_URL.rstrip("/")
    return any(str(values.get(key) or "").rstrip("/") == expected for key in ("HTTPProxy", "HTTPSProxy"))


def _control_image_repo_digest(remote: Executor, image: str) -> str:
    inspect_output = _last_command_value(
        _docker(remote, f"docker image inspect --format '{{{{json .RepoDigests}}}}' {shlex.quote(image)}")
    )
    try:
        repo_digests = json.loads(inspect_output)
    except json.JSONDecodeError as exc:
        raise LumaError(f"failed to read repo digest for Luma Control image: {image}") from exc
    if not isinstance(repo_digests, list):
        raise LumaError(f"failed to read repo digest for Luma Control image: {image}")

    repository = _image_repository(image)
    digests = [str(item) for item in repo_digests if isinstance(item, str) and "@sha256:" in item]
    for digest in digests:
        if digest.split("@", 1)[0] == repository:
            return digest
    if digests:
        return digests[0]
    raise LumaError(
        f"Docker pulled Luma Control image {image}, but no immutable repo digest was available. "
        "Set LUMA_CONTROL_IMAGE/defaults.images.lumaControl to a digest-pinned image."
    )


def _image_repository(image: str) -> str:
    reference = str(image or "").strip().split("@", 1)[0]
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    if colon > slash:
        return reference[:colon]
    return reference


def install_control_config(remote: Executor, config: LumaConfig, node: NodeConfig | None = None) -> str:
    content = Path(config.path).read_text(encoding="utf-8") if config.path else dump_yaml(_bootstrap_config(config, node))
    return remote.write_secret(content, f"{ROOT}/luma.yaml", mode="644")


def _bootstrap_config(config: LumaConfig, node: NodeConfig | None = None) -> dict[str, object]:
    raw = dict(config.raw)
    raw.setdefault("project", "luma")
    defaults = raw.setdefault("defaults", {})
    if isinstance(defaults, dict):
        # Fresh installs are Nomad-native by default — this is the product's
        # target engine. Existing clusters keep their luma.yaml as-is (read
        # directly, not regenerated), so this only affects new bootstraps.
        defaults.setdefault("engine", "nomad")
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
        result = remote.run_result("command -v docker >/dev/null 2>&1")
        if result.code != 0:
            raise LumaError(
                "Docker is required before this macOS node can run Nomad workloads. "
                "Install Docker Desktop, start it, then rerun luma node join."
            )
        result = remote.run_result("docker info >/dev/null 2>&1")
        if result.code != 0:
            raise LumaError(
                "Docker is installed but the Docker daemon is not reachable. "
                "Start Docker Desktop and wait until `docker info` succeeds, then rerun luma node join."
            )
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
        "-e 's#http://mirrors.cloud.aliyuncs.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' "
        "-e 's#https://mirrors.cloud.aliyuncs.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' "
        "\"$file\"; "
        "done; "
        "apt-get update; "
        "apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml nfs-common; "
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
    output = remote.sudo(
        "set -euo pipefail; "
        "if ! command -v tailscale >/dev/null 2>&1; then "
        "curl -fsSL https://tailscale.com/install.sh | sh; "
        "echo luma_tailscale_installed; "
        "else "
        "echo luma_tailscale_present; "
        "fi; "
        "systemctl enable --now tailscaled"
    )
    if "luma_tailscale_present" in output:
        results.append("Tailscale already installed")
    else:
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


def configure_firewall(remote: Executor, *, http_port: int = 80, https_port: int = 443, tcp_ports: list[int] | None = None, engine: str = "nomad") -> str:
    if _is_darwin(remote):
        return "Firewall configuration skipped on macOS"
    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    restrict_nomad_public = _tailscale_ip(remote) is not None
    commands = [
        "ufw --force enable",
        "ufw allow OpenSSH",
        f"ufw allow {int(http_port)}/tcp",
        f"ufw allow {int(https_port)}/tcp",
        "ufw allow 4646/tcp",
        "ufw allow 4647/tcp",
        "ufw allow 4648/tcp",
        "ufw allow 4648/udp",
    ]
    commands += [
        "ufw deny 7890/tcp",
        "ufw deny 7890/udp",
    ]
    for port in sorted({int(port) for port in (tcp_ports or [])}):
        commands.append(f"ufw allow {port}/tcp")
    remote.sudo("set -euo pipefail; " + "; ".join(commands))
    configure_public_port_guards(remote, restrict_nomad_public=restrict_nomad_public, engine=engine)
    return "Firewall configured"


def configure_public_port_guards(remote: Executor, *, restrict_nomad_public: bool = False, engine: str = "nomad") -> str:
    if _is_darwin(remote):
        return "Public port guards skipped on macOS"
    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    nomad_guard = "yes" if restrict_nomad_public else "no"
    remote.sudo(
        "set -euo pipefail; "
        f"install -d -m 755 {ROOT}/firewall; "
        f"cat > {ROOT}/firewall/public-port-guards.sh <<'EOF'\n"
        "#!/bin/sh\n"
        "set -eu\n"
        f"restrict_nomad_public={nomad_guard}\n"
        "iface=$(ip route show default 2>/dev/null | awk '{print $5; exit}')\n"
        "[ -n \"${iface:-}\" ] || exit 0\n"
        "add_rule() {\n"
        "  table_cmd=\"$1\"\n"
        "  chain=\"$2\"\n"
        "  shift 2\n"
        "  if command -v \"$table_cmd\" >/dev/null 2>&1; then\n"
        "    \"$table_cmd\" -C \"$chain\" \"$@\" 2>/dev/null || \"$table_cmd\" -I \"$chain\" 1 \"$@\"\n"
        "  fi\n"
        "}\n"
        "add_input_drop() {\n"
        "  proto=\"$1\"\n"
        "  port=\"$2\"\n"
        "  add_rule iptables INPUT -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "  add_rule ip6tables INPUT -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "}\n"
        "add_prerouting_drop() {\n"
        "  proto=\"$1\"\n"
        "  port=\"$2\"\n"
        "  if command -v iptables >/dev/null 2>&1; then\n"
        "    iptables -t raw -C PREROUTING -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP 2>/dev/null || "
        "iptables -t raw -I PREROUTING 1 -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "  fi\n"
        "  if command -v ip6tables >/dev/null 2>&1; then\n"
        "    ip6tables -t raw -C PREROUTING -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP 2>/dev/null || "
        "ip6tables -t raw -I PREROUTING 1 -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "  fi\n"
        "}\n"
        "add_docker_drop() {\n"
        "  proto=\"$1\"\n"
        "  port=\"$2\"\n"
        "  if command -v iptables >/dev/null 2>&1; then\n"
        "    iptables -N DOCKER-USER 2>/dev/null || true\n"
        "  fi\n"
        "  if command -v ip6tables >/dev/null 2>&1; then\n"
        "    ip6tables -N DOCKER-USER 2>/dev/null || true\n"
        "  fi\n"
        "  add_rule iptables DOCKER-USER -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "  add_rule ip6tables DOCKER-USER -i \"$iface\" -p \"$proto\" --dport \"$port\" -j DROP\n"
        "}\n"
        "add_input_drop tcp 7890\n"
        "add_input_drop udp 7890\n"
        "add_prerouting_drop tcp 7890\n"
        "add_prerouting_drop udp 7890\n"
        "add_docker_drop tcp 7890\n"
        "add_docker_drop udp 7890\n"
        "if [ \"$restrict_nomad_public\" = \"yes\" ]; then\n"
        "  add_input_drop tcp 4646\n"
        "  add_input_drop tcp 4647\n"
        "  add_input_drop tcp 4648\n"
        "  add_input_drop udp 4648\n"
        "fi\n"
        "EOF\n"
        f"chmod 755 {ROOT}/firewall/public-port-guards.sh; "
        "cat > /etc/systemd/system/luma-public-port-guards.service <<'EOF'\n"
        "[Unit]\n"
        "Description=Luma public port guards\n"
        "After=network-online.target docker.service ufw.service\n"
        "Wants=network-online.target docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={ROOT}/firewall/public-port-guards.sh\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl enable luma-public-port-guards.service >/dev/null; "
        "systemctl restart luma-public-port-guards.service"
    )
    return "Public port guards installed"


def configure_tailscale_watchdog(remote: Executor) -> str:
    if _is_darwin(remote):
        return "Tailscale watchdog skipped on macOS"
    remote.sudo(
        "set -euo pipefail; "
        "if ! command -v systemctl >/dev/null 2>&1 || ! command -v tailscale >/dev/null 2>&1; then "
        "echo skipped; exit 0; "
        "fi; "
        "systemctl list-unit-files tailscaled.service >/dev/null 2>&1 || { echo skipped; exit 0; }; "
        f"install -d -m 755 {ROOT}/watchdog; "
        f"cat > {ROOT}/watchdog/tailscale-watchdog.sh <<'EOF'\n"
        "#!/bin/sh\n"
        "set -eu\n"
        "threshold=${LUMA_TAILSCALE_WATCHDOG_THRESHOLD:-3}\n"
        "port=${LUMA_TAILSCALE_WATCHDOG_PORT:-4647}\n"
        "peers=${LUMA_TAILSCALE_WATCHDOG_PEERS:-}\n"
        "state_dir=/run/luma\n"
        "state_file=$state_dir/tailscale-watchdog.failures\n"
        "mkdir -p \"$state_dir\"\n"
        "log() { printf '%s %s\\n' \"$(date -Is)\" \"$*\"; }\n"
        "tcp_probe() {\n"
        "  host=\"$1\"\n"
        "  if command -v nc >/dev/null 2>&1; then\n"
        "    nc -z -w 3 \"$host\" \"$port\" >/dev/null 2>&1\n"
        "  elif command -v timeout >/dev/null 2>&1 && command -v bash >/dev/null 2>&1; then\n"
        "    timeout 3 bash -c \"</dev/tcp/$host/$port\" >/dev/null 2>&1\n"
        "  else\n"
        "    log 'skip: no TCP probe command available'\n"
        "    return 0\n"
        "  fi\n"
        "}\n"
        "is_tailnet_addr() {\n"
        "  case \"$1\" in\n"
        "    100.*) return 0 ;;\n"
        "    fd7a:115c:a1e0:*) return 0 ;;\n"
        "    *) return 1 ;;\n"
        "  esac\n"
        "}\n"
        "if ! systemctl is-active --quiet tailscaled; then\n"
        "  log 'tailscaled inactive; restarting'\n"
        "  systemctl restart tailscaled\n"
        "  echo 0 > \"$state_file\"\n"
        "  exit 0\n"
        "fi\n"
        "peers=$(printf '%s' \"$peers\" | tr ',' ' ')\n"
        "checked=0\n"
        "bad=0\n"
        "for addr in $peers; do\n"
        "  [ -n \"$addr\" ] || continue\n"
        "  addr=${addr%%:*}\n"
        "  is_tailnet_addr \"$addr\" || continue\n"
        "  if tailscale ping --timeout=3s --c 2 \"$addr\" >/dev/null 2>&1; then\n"
        "    checked=$((checked + 1))\n"
        "    if ! tcp_probe \"$addr\"; then\n"
        "      bad=$((bad + 1))\n"
        "      log \"tailnet TCP probe failed: $addr:$port\"\n"
        "    fi\n"
        "  fi\n"
        "done\n"
        "if [ \"$checked\" -eq 0 ]; then\n"
        "  log 'skip: no configured reachable Tailscale peers to validate'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$bad\" -eq 0 ]; then\n"
        "  echo 0 > \"$state_file\"\n"
        "  exit 0\n"
        "fi\n"
        "count=0\n"
        "[ -f \"$state_file\" ] && count=$(cat \"$state_file\" 2>/dev/null || echo 0)\n"
        "case \"$count\" in ''|*[!0-9]*) count=0 ;; esac\n"
        "count=$((count + 1))\n"
        "echo \"$count\" > \"$state_file\"\n"
        "log \"tailnet TCP unhealthy: $bad/$checked peers failed, consecutive=$count/$threshold\"\n"
        "if [ \"$count\" -ge \"$threshold\" ]; then\n"
        "  log 'restarting tailscaled after consecutive tailnet TCP failures'\n"
        "  systemctl restart tailscaled\n"
        "  echo 0 > \"$state_file\"\n"
        "fi\n"
        "EOF\n"
        f"chmod 755 {ROOT}/watchdog/tailscale-watchdog.sh; "
        "cat > /etc/systemd/system/luma-tailscale-watchdog.service <<'EOF'\n"
        "[Unit]\n"
        "Description=Luma Tailscale TCP watchdog\n"
        "After=network-online.target tailscaled.service nomad.service\n"
        "Wants=network-online.target tailscaled.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "EnvironmentFile=-/etc/default/luma-tailscale-watchdog\n"
        f"ExecStart={ROOT}/watchdog/tailscale-watchdog.sh\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        "EOF\n"
        "cat > /etc/systemd/system/luma-tailscale-watchdog.timer <<'EOF'\n"
        "[Unit]\n"
        "Description=Run Luma Tailscale TCP watchdog\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=1min\n"
        "AccuracySec=15s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl enable --now luma-tailscale-watchdog.timer >/dev/null; "
        "systemctl reset-failed luma-tailscale-watchdog.service >/dev/null 2>&1 || true; "
        "systemctl start luma-tailscale-watchdog.service >/dev/null || true"
    )
    return "Tailscale watchdog installed"


def prepare_paths(remote: Executor) -> str:
    remote.sudo(
        f"set -euo pipefail; "
        f"install -d -m 755 {ROOT}/stacks; "
        f"install -d -m 755 {ROOT}/routes; "
        f"install -d -m 700 {ROOT}/control; "
        f"install -d -m 700 {ROOT}/egress-gateway"
    )
    return "Runtime paths ready"


def _bootstrap_node_nomad(
    config: LumaConfig,
    node: NodeConfig,
    profile: Profile,
    *,
    run_egress: bool = True,
    tcp_ports: list[int] | None = None,
    emit: Progress | None = None,
    remote: Executor,
) -> list[str]:
    """Nomad-native manager/single-node bootstrap.

    Reuses building blocks already live-verified on the running cluster:
    install_nomad_node (agent install + config), the core-job renderers, and the
    engine-aware firewall.
    NOTE: the from-scratch orchestration ordering is constructed from those
    verified pieces but a clean-room manager bootstrap is the one path that
    cannot be re-tested without tearing down the live cluster; treat with care.
    """
    results: list[str] = []
    roles = profile.roles if profile else node.roles
    is_server = "nomad-manager" in roles or profile.name == "single-node"
    region = node.region or "cn"
    extra_meta: dict[str, str] = {}
    if "edge" in roles or profile.name == "single-node":
        extra_meta["ingress"] = "true"
    if "egress" in roles:
        extra_meta["egress"] = "true"

    _step(results, emit, "Configure system DNS", lambda: configure_dns(remote))

    # install_nomad_node handles Docker + Tailscale + Nomad agent install/config.
    install_results = install_nomad_node(
        node,
        role="server" if is_server else "client",
        region=region,
        node_name=node.name,
        server_addrs=None if is_server else _nomad_server_addrs(config, node),
        extra_meta=extra_meta,
        emit=emit,
        install_docker_first=True,
    )
    results.extend(install_results)

    _step(results, emit, "Create runtime paths", lambda: prepare_paths(remote))
    http_port, https_port = _traefik_ports(config)
    _step(
        results, emit, "Configure firewall",
        lambda: configure_firewall(remote, http_port=http_port, https_port=https_port,
                                   tcp_ports=tcp_ports, engine="nomad"),
    )
    if is_server:
        _step(results, emit, "Install Tailscale watchdog", lambda: configure_tailscale_watchdog(remote))

    if "edge" in roles or profile.name == "single-node":
        def _deploy_traefik_nomad() -> str:
            from .nomad_render import render_traefik_job
            job = render_traefik_job(
                image=_traefik_image(config),
                nomad_addr="http://127.0.0.1:4646",
                acme_email=_acme_email(config),
                cert_resolver=config.cert_resolver,
                tcp_entrypoints=sorted({int(p) for p in (tcp_ports or [])}),
            )
            _deploy_nomad_job(remote, job, "traefik")
            return _wait_nomad_job(remote, "traefik")
        _step(results, emit, "Deploy Traefik (Nomad)", _deploy_traefik_nomad)

    if run_egress and "egress" in roles:
        def _deploy_egress_nomad() -> str:
            from .nomad_render import render_egress_job
            job = render_egress_job(image=_egress_image(config))
            _deploy_nomad_job(remote, job, "egress")
            return _wait_nomad_job(remote, "egress")
        _step(results, emit, "Deploy egress (Nomad)", _deploy_egress_nomad)

    return results


def _nomad_server_addrs(config: LumaConfig, node: NodeConfig) -> list[str]:
    """Resolve the Nomad server RPC address(es) a client should join.

    Prefer the manager's Tailscale IP: Nomad advertises over Tailscale and the
    host firewall keeps 4647 off the public interface, so a public IP would not
    be reachable. Fall back to advertise/public/config only if no Tailscale IP
    is recorded.
    """
    manager = config.default_manager()
    host = ""
    if manager is not None:
        host = str(
            manager.raw.get("tailscaleIP")
            or manager.raw.get("advertiseAddr")
            or manager.public_ip
            or ""
        )
    if not host:
        host = str(config.defaults.get("nomadServer") or "")
    if not host:
        raise LumaError("cannot resolve Nomad server address; set defaults.nomadServer or a manager node")
    if ":" not in host:
        host = f"{host}:4647"
    return [host]


def bootstrap_node(
    config: LumaConfig,
    node: NodeConfig,
    profile: Profile,
    *,
    run_egress: bool = True,
    tcp_ports: list[int] | None = None,
    emit: Progress | None = None,
    executor: Executor | None = None,
) -> list[str]:
    remote = executor or RemoteExecutor(node)
    engine = str(config.defaults.get("engine") or "nomad")
    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    return _bootstrap_node_nomad(
        config, node, profile, run_egress=run_egress, tcp_ports=tcp_ports,
        emit=emit, remote=remote,
    )


def bootstrap_manager_local(config: LumaConfig, node: NodeConfig, profile: Profile, domain: str, state: dict[str, object], *, run_egress: bool = True, emit: Progress | None = None) -> list[str]:
    remote = LocalExecutor()
    tcp_ports = _state_tcp_relay_ports(state)
    results = bootstrap_node(
        config,
        node,
        profile,
        run_egress=run_egress,
        tcp_ports=tcp_ports,
        emit=emit,
        executor=remote,
    )
    engine = str(config.defaults.get("engine") or "nomad")
    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    manager_node_name = _remember_local_manager_node(state, node, profile, remote)
    state["nomadAddr"] = str(state.get("nomadAddr") or "http://127.0.0.1:4646")
    _step(results, emit, "Sync control DNS", lambda: sync_control_dns(config, domain))
    _step(results, emit, "Install control config", lambda: install_control_config(remote, config, node))
    _step(results, emit, "Install control state", lambda: install_control_state(remote, state))
    _step(results, emit, "Write control route", lambda: _write_control_route(remote, config, domain, node))
    _step(
        results,
        emit,
        "Deploy Luma control API",
        lambda: deploy_control_stack(remote, config, domain, emit=emit, require_pull_egress=run_egress, node_name=manager_node_name),
        fix=(
            "Build and publish the Luma control image, then rerun bootstrap manager. "
            "For mainland managers using the default GHCR image, configure EGRESS_SUBSCRIPTION_URL "
            "and do not use --skip-egress."
        ),
    )
    return results


def _write_control_route(remote: Executor, config: LumaConfig, domain: str, node: NodeConfig) -> str:
    """Write the Traefik file-provider route for luma-control (Nomad engine).

    The control job runs bridge :8080 on the manager; Traefik reaches it over the
    manager's Tailscale IP. Without this route the control domain is unreachable
    (the job has no nomad-provider tags), so this closes the bootstrap gap.
    """
    import base64

    target = _tailscale_ip(remote) or node.public_ip or "127.0.0.1"
    entrypoint = config.entrypoint
    cert_resolver = config.cert_resolver
    route_yaml = (
        "http:\n"
        "  routers:\n"
        "    luma-control:\n"
        f"      rule: Host(`{domain}`)\n"
        "      entryPoints:\n"
        f"      - {entrypoint}\n"
        "      tls:\n"
        f"        certResolver: {cert_resolver}\n"
        "      service: luma-control\n"
        "  services:\n"
        "    luma-control:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f"        - url: http://{target}:8080\n"
    )
    b64 = base64.b64encode(route_yaml.encode("utf-8")).decode("ascii")
    remote.sudo(
        f"set -e; install -d -m 755 {ROOT}/routes; "
        f"echo {b64} | base64 -d > {ROOT}/routes/luma-control.yml"
    )
    return f"Control route written: {domain} -> {target}:8080"


def _state_tcp_relay_ports(state: dict[str, object]) -> list[int]:
    deployments = state.get("deployments") if isinstance(state.get("deployments"), dict) else {}
    ports: set[int] = set()
    for bucket_name in ("services", "compose"):
        bucket = deployments.get(bucket_name) if isinstance(deployments.get(bucket_name), dict) else {}
        for record in bucket.values():
            if not isinstance(record, dict):
                continue
            if str(record.get("status") or "") != "active":
                continue
            for port in record.get("tcpRelayPorts") or []:
                try:
                    parsed = int(port)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    ports.add(parsed)
    return sorted(ports)


def refresh_manager_control_local(config: LumaConfig, node: NodeConfig, domain: str, state: dict[str, object], *, emit: Progress | None = None) -> list[str]:
    remote = LocalExecutor()
    results: list[str] = []
    state["domain"] = domain
    engine = str(config.defaults.get("engine") or "nomad")
    if engine != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    tcp_ports = _state_tcp_relay_ports(state)
    manager_node_name = _remember_local_manager_node(state, node, None, remote)
    http_port, https_port = _traefik_ports(config)
    _step(
        results,
        emit,
        "Refresh firewall TCP relay ports",
        lambda: configure_firewall(remote, http_port=http_port, https_port=https_port, tcp_ports=tcp_ports, engine="nomad"),
    )
    if "edge" in node.roles:
        def _deploy_traefik_nomad() -> str:
            from .nomad_render import render_traefik_job
            job = render_traefik_job(
                image=_traefik_image(config),
                nomad_addr=str(state.get("nomadAddr") or config.defaults.get("nomadAddr") or "http://127.0.0.1:4646"),
                acme_email=_acme_email(config),
                cert_resolver=config.cert_resolver,
                tcp_entrypoints=tcp_ports,
            )
            _deploy_nomad_job(remote, job, "traefik")
            return _wait_nomad_job(remote, "traefik")

        _step(results, emit, "Refresh Traefik ingress", _deploy_traefik_nomad)
    _step(results, emit, "Install Tailscale watchdog", lambda: configure_tailscale_watchdog(remote))
    _step(results, emit, "Install control config", lambda: install_control_config(remote, config, node))
    _step(results, emit, "Install control state", lambda: install_control_state(remote, state))
    _step(
        results,
        emit,
        "Refresh Luma control API",
        lambda: deploy_control_stack(remote, config, domain, emit=emit, node_name=manager_node_name),
        fix="Check luma-control service logs and rerun luma update manager",
    )
    return results


def _manager_canonical_node_name(node: NodeConfig, *, hostname: str, nomad_name: str) -> str:
    configured = str(node.name or "").strip()
    node_host = str(node.host or "").strip()
    host_aliases = {hostname, node_host, "localhost", "127.0.0.1", "::1"}
    if configured and configured not in host_aliases:
        return configured
    if nomad_name and nomad_name != hostname:
        return nomad_name
    return configured or nomad_name or hostname


def _remember_local_manager_node(state: dict[str, object], node: NodeConfig, profile: Profile | None, remote: Executor) -> str:
    hostname = local_host_name(remote)
    nomad_name, node_id = local_nomad_node_info(remote)
    canonical_name = _manager_canonical_node_name(node, hostname=hostname, nomad_name=nomad_name)
    labels = {**(profile.labels if profile else {"region": node.region})}
    roles = profile.roles if profile else node.roles
    for role in roles:
        labels[f"role.{role}"] = "true"
    labels["luma.node.name"] = canonical_name
    if node_id:
        labels["luma.node.id"] = node_id
    region = str(labels.get("region") or node.region or "cn")
    aliases = {
        value
        for value in (hostname, nomad_name, node.host)
        if value and value != canonical_name
    }
    values: dict[str, object] = {
        "region": region,
        "status": "manager",
        "displayName": canonical_name,
        "hostname": hostname,
        "labels": labels,
        "nomadRole": "server",
        "nomadServer": True,
        "aliases": sorted(aliases),
    }
    if node_id:
        values["nodeId"] = node_id
        values["nomadNodeId"] = node_id
    tailscale_ip = _tailscale_ip(remote)
    if tailscale_ip:
        values["tailscaleIP"] = tailscale_ip
    nodes = state.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        state["nodes"] = nodes
    current = nodes.get(canonical_name) if isinstance(nodes.get(canonical_name), dict) else {}
    legacy = nodes.get(hostname) if hostname != canonical_name and isinstance(nodes.get(hostname), dict) else {}
    merged_aliases = set(values["aliases"])
    for source in (legacy, current):
        raw_aliases = source.get("aliases") if isinstance(source, dict) else []
        if isinstance(raw_aliases, list):
            merged_aliases.update(str(alias) for alias in raw_aliases if alias and str(alias) != canonical_name)
    values["aliases"] = sorted(merged_aliases)
    nodes[canonical_name] = {**legacy, **current, **values}
    if hostname != canonical_name:
        nodes.pop(hostname, None)
    if node.name and node.name != hostname:
        alias_values = {**values, "displayName": node.name}
        nodes[node.name] = {**(nodes.get(node.name) if isinstance(nodes.get(node.name), dict) else {}), **alias_values}
    return canonical_name


def install_nomad_node(
    node: NodeConfig,
    *,
    role: str,
    region: str,
    node_name: str,
    server_addrs: list[str] | None = None,
    extra_meta: dict[str, str] | None = None,
    emit: Progress | None = None,
    install_docker_first: bool = True,
    egress_proxy: str | None = None,
    tailscale_authkey: str | None = None,
) -> list[str]:
    """Install and start a Nomad agent on the local node.

    Auto-detects node facts (Tailscale IP, OS/arch, Apple-Silicon CPU) and
    generates the agent config via nomad_node, so the operator just runs the
    command and types their password once — zero hand-tuning. The shell steps
    mirror what was validated by hand on aly/lab/gaojiu during the migration.
    NOTE: constructed to match those hand-verified steps; pending a fresh-node
    live test before being wired as the default join path.
    """
    import base64

    from . import nomad_node

    remote = LocalExecutor()
    results: list[str] = []
    if install_docker_first:
        _step(results, emit, "Install Docker", lambda: install_docker(remote))
    _step(
        results,
        emit,
        "Install and connect Tailscale",
        lambda: setup_tailscale(node, authkey=tailscale_authkey, executor=remote),
        fix="Run: luma tailscale connect",
    )

    os_name = nomad_node.detect_os()
    arch = os.uname().machine
    tailscale_ip = _tailscale_ip(remote) or ""
    if not tailscale_ip:
        raise LumaError(
            "could not detect this node's Tailscale IPv4; run `luma tailscale connect` then rerun"
        )
    cpu_override = nomad_node.detect_cpu_total_compute(os_name, run=_subprocess_capture)
    config_hcl = nomad_node.render_agent_config(
        os_name=os_name,
        role=role,
        tailscale_ip=tailscale_ip,
        region=region,
        node_name=node_name,
        server_addrs=server_addrs,
        cpu_total_compute=cpu_override,
        extra_meta=extra_meta,
    )
    install = nomad_node.install_nomad_commands(os_name=os_name, arch=arch, config_hcl=config_hcl)

    # CN nodes cannot reach releases.hashicorp.com / GitHub directly; route the
    # binary + CNI downloads through the egress proxy when one is provided
    # (workers derive it from the manager's Tailscale IP).
    proxy = f"https_proxy={shlex.quote(egress_proxy)} http_proxy={shlex.quote(egress_proxy)} " if egress_proxy else ""

    def _install_binary() -> str:
        url = install["download_url"]
        version_regex = rf"^Nomad v?{re.escape(nomad_node.NOMAD_VERSION)}([^0-9.]|$)"
        output = remote.sudo(
            "set -e; "
            "if command -v nomad >/dev/null 2>&1 "
            f"&& nomad version 2>/dev/null | head -n 1 | grep -Eq {shlex.quote(version_regex)}; then "
            "echo luma_nomad_binary_present; "
            "else "
            f"cd /tmp; {proxy}curl -fsSL {shlex.quote(url)} -o nomad.zip; "
            "command -v unzip >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq unzip) || true; "
            "unzip -o nomad.zip -d /usr/local/bin/ >/dev/null; "
            "echo luma_nomad_binary_installed; "
            "fi"
        )
        if "luma_nomad_binary_present" in output:
            return "Nomad binary already installed"
        return "Nomad binary installed"

    _step(results, emit, "Install Nomad binary", _install_binary)

    def _write_config() -> str:
        cfg_b64 = base64.b64encode(install["config"].encode("utf-8")).decode("ascii")
        unit_b64 = base64.b64encode(install["service_unit"].encode("utf-8")).decode("ascii")
        cmds = "set -e; mkdir -p /etc/nomad.d /opt/nomad/data; "
        cmds += f"echo {cfg_b64} | base64 -d > /etc/nomad.d/nomad.hcl; "
        if install["service_kind"] == "systemd":
            cmds += (
                "if [ -f /etc/systemd/system/nomad.service ]; then "
                "cp -a /etc/systemd/system/nomad.service "
                "/etc/systemd/system/nomad.service.luma-backup-$(date +%Y%m%d%H%M%S); "
                "fi; "
            )
            cmds += f"echo {unit_b64} | base64 -d > /etc/systemd/system/nomad.service"
        else:
            cmds += f"echo {unit_b64} | base64 -d > /Library/LaunchDaemons/io.luma.nomad.plist"
        remote.sudo(cmds)
        return "Nomad config written"

    _step(results, emit, "Write Nomad config", _write_config)

    if os_name != "darwin":
        def _install_cni() -> str:
            # Bridge mode (Linux) requires CNI plugins + bridge netfilter.
            cni_url = nomad_node.cni_plugins_url(arch)
            remote.sudo(
                "set -e; if [ ! -f /opt/cni/bin/bridge ]; then mkdir -p /opt/cni/bin; cd /tmp; "
                f"{proxy}curl -fsSL {shlex.quote(cni_url)} -o cni.tgz; tar -C /opt/cni/bin -xzf cni.tgz; fi; "
                "modprobe bridge br_netfilter 2>/dev/null || true; "
                "sysctl -w net.bridge.bridge-nf-call-iptables=1 >/dev/null 2>&1 || true"
            )
            return "CNI plugins installed"

        _step(results, emit, "Install CNI plugins", _install_cni)

    def _start_service() -> str:
        if install["service_kind"] == "systemd":
            # Use `restart`, not `enable --now`. `--now` only does enable+start,
            # and `start` is a no-op on an already-running unit — so re-running
            # bootstrap/join after _write_config rewrote nomad.hcl (changed
            # region / ingress / egress meta, or a new HCL layout from a newer
            # CLI) would leave the live agent serving the OLD config while still
            # reporting success. `restart` re-reads the config on a running unit
            # and also starts a stopped one, matching the macOS unload+load path.
            # A Nomad client restart does not kill running allocations (docker
            # tasks survive and the agent re-attaches).
            remote.sudo(
                "systemctl daemon-reload && systemctl enable nomad && "
                "(systemctl reset-failed nomad || true) && systemctl restart nomad"
            )
        else:
            remote.sudo(
                "launchctl unload /Library/LaunchDaemons/io.luma.nomad.plist 2>/dev/null || true; "
                "launchctl load /Library/LaunchDaemons/io.luma.nomad.plist"
            )
        return "Nomad agent started"

    _step(results, emit, "Start Nomad agent", _start_service)
    _step(results, emit, "Verify Nomad node", lambda: verify_local_nomad_node(remote, http_addrs=[tailscale_ip]))
    return results


def verify_local_nomad_node(remote: Executor | None = None, *, http_addrs: list[str] | None = None) -> str:
    remote = remote or LocalExecutor()
    candidates = ["127.0.0.1"]
    for addr in http_addrs or []:
        value = str(addr or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    probes = " || ".join(
        f"curl -fsS --connect-timeout 4 http://{shlex.quote(addr)}:4646/v1/agent/self >/dev/null 2>&1"
        for addr in candidates
    )
    deadline = time.monotonic() + 30
    while True:
        result = remote.run_result(f"({probes}) && echo ready || echo waiting")
        if "ready" in result.output:
            return "Nomad agent ready"
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    raise LumaError(
        f"Nomad agent did not become ready within 30s on {', '.join(candidates)}. Check `journalctl -u nomad` "
        "(Linux) or /var/log/nomad.log (macOS)."
    )


def _subprocess_capture(cmd: list[str]) -> str | None:
    import subprocess as _sp

    try:
        out = _sp.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, _sp.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _last_command_value(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    if line.startswith("[sudo]") and ":" in line:
        return line.split(":", 1)[-1].strip()
    return line


def _tailscale_ip(remote: Executor) -> str | None:
    result = remote.run_result("command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1")
    if result.code != 0:
        return None
    value = _last_command_value(result.output)
    return value or None


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_tailscale_manager_addr(manager_addr: str) -> bool:
    host = manager_addr.rsplit(":", 1)[0] if manager_addr.count(":") == 1 else manager_addr
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in ipaddress.ip_network("100.64.0.0/10")


def local_host_name(remote: Executor | None = None) -> str:
    remote = remote or LocalExecutor()
    result = remote.run_result("hostname -s 2>/dev/null || hostname 2>/dev/null || true")
    return _last_command_value(result.output) or os.uname().nodename


def local_nomad_node_info(remote: Executor | None = None) -> tuple[str, str]:
    remote = remote or LocalExecutor()
    result = remote.run_result(
        "if command -v nomad >/dev/null 2>&1; then nomad_bin=$(command -v nomad); "
        "elif test -x /usr/local/bin/nomad; then nomad_bin=/usr/local/bin/nomad; "
        "elif test -x /opt/homebrew/bin/nomad; then nomad_bin=/opt/homebrew/bin/nomad; "
        "elif test -x /usr/bin/nomad; then nomad_bin=/usr/bin/nomad; "
        "else nomad_bin=; fi; "
        "test -n \"$nomad_bin\" && \"$nomad_bin\" node status -self -json 2>/dev/null"
    )
    if result.code != 0 or not result.output.strip():
        return local_host_name(remote), ""
    try:
        data = json.loads(result.output)
    except json.JSONDecodeError:
        return local_host_name(remote), ""
    node_id = str(data.get("ID") or "").strip()
    meta = data.get("Meta") if isinstance(data.get("Meta"), dict) else {}
    node_name = str(meta.get("luma_node_name") or data.get("Name") or "").strip()
    return node_name or local_host_name(remote), node_id


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    key = ""
    value_parts: list[str] = []
    for part in text.split():
        if "=" in part:
            if key:
                values[key] = " ".join(value_parts)
            key, value = part.split("=", 1)
            value_parts = [value]
        elif key:
            value_parts.append(part)
    if key:
        values[key] = " ".join(value_parts)
    return values


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
    _step(
        results,
        emit,
        "Deploy egress gateway",
        lambda: _deploy_egress_nomad(remote, config),
        fix=f"Run: luma egress setup {node.name}",
    )
    _step(
        results,
        emit,
        "Install public port guards",
        lambda: configure_public_port_guards(remote, restrict_nomad_public=(_tailscale_ip(remote) is not None), engine="nomad"),
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
        f"Environment=\"HTTP_PROXY={EGRESS_PROXY_URL}\"\n"
        f"Environment=\"HTTPS_PROXY={EGRESS_PROXY_URL}\"\n"
        f"Environment=\"NO_PROXY={EGRESS_NO_PROXY}\"\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl restart docker"
    )
    return "Docker daemon proxy configured"


def _refresh_core_services(remote: Executor) -> str:
    # Restart each running core job but don't abort the whole refresh if one
    # fails; instead print the failed job names so the caller can surface them
    # rather than silently reporting success on a failed restart.
    output = remote.run(
        "for job in traefik egress luma-control; do "
        "nomad job status \"$job\" >/dev/null 2>&1 || continue; "
        "nomad job restart -yes \"$job\" >/dev/null 2>&1 || echo \"$job\"; "
        "done"
    )
    failed = [line.strip() for line in output.splitlines() if line.strip()]
    if failed:
        return "Core services refresh failed for: " + ", ".join(failed)
    return "Core services refresh requested"


def _deploy_egress_nomad(remote: Executor, config: LumaConfig) -> list[str]:
    from .nomad_render import render_egress_job

    job = render_egress_job(image=_egress_image(config))
    return [
        _deploy_nomad_job(remote, job, "egress"),
        _wait_nomad_job(remote, "egress"),
    ]
