"""Transport setup for Luma-owned registries before a worker joins Nomad."""
from __future__ import annotations

import ipaddress
import inspect
import json
import re
import shlex
import urllib.parse
from typing import Any

from .errors import LumaError
from .registry import normalize_registry_host
from .registry_proxy import proxy_bypasses_registry_hosts


def join_insecure_registries(state: dict[str, Any]) -> list[str]:
    """Explicit managed HTTP endpoints, plus the legacy Tailscale builder:5000.

    Never infer insecure transport for public registries or arbitrary private TLS
    endpoints. pushHost can be builder-local and must not be sent to workers.
    """
    transports = state.get("managedRegistryTransports") or {}
    hosts = {normalize_registry_host(h) for h, t in transports.items() if t == "http"}
    build = state.get("build") or {}
    raw = str(build.get("registryHost") or "").strip()
    if raw:
        host = normalize_registry_host(raw)
        address, _, port = host.rpartition(":")
        nodes = state.get("nodes") or {}
        known = {str(n.get("tailscaleIP") or "") for n in nodes.values() if isinstance(n, dict)}
        try:
            legacy = ipaddress.ip_address(address) in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            legacy = False
        if host not in transports and legacy and address in known and port == "5000":
            if host not in (state.get("registries") or {}):
                hosts.add(host)
    return sorted(hosts)


def registry_host_allows_http(state: dict[str, Any], host: str) -> bool:
    """Whether image copies to this managed registry may fall back to HTTP.

    An explicitly recorded managed transport wins. Otherwise only registries on
    loopback, private or Tailscale addresses (builder-local and mesh registries)
    may use HTTP; public hostnames always require TLS.
    """
    normalized = normalize_registry_host(host)
    transport = (state.get("managedRegistryTransports") or {}).get(normalized)
    if transport in {"http", "https"}:
        return transport == "http"
    if normalized in join_insecure_registries(state):
        return True
    address = normalized.rpartition(":")[0] or normalized
    if address == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def configure_join_registries(registries: list[str], *, executor: Any, os_name: str) -> str:
    """Verify the actual Docker runtime before a node joins Nomad."""
    try:
        result = ensure_managed_registry_access(registries, executor=executor, os_name=os_name)
    except LumaError as exc:
        raise LumaError(f"Managed registry setup failed; node must not join: {exc}") from exc
    return str(result["message"])


def normalize_managed_registry_hosts(registries: list[str]) -> list[str]:
    """Managed transport is an exact host allowlist, never a URL or CIDR."""
    if not isinstance(registries, list):
        raise LumaError("insecureRegistries must be a list")
    if len(registries) > 16:
        raise LumaError("managed registry access supports at most 16 hosts per request")
    hosts: set[str] = set()
    for value in registries:
        if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
            raise LumaError("managed registry host must be a literal host with an optional port")
        if not re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", value):
            raise LumaError("managed registry host must be an exact hostname or bracketed IP with an optional numeric port")
        try:
            parsed = urllib.parse.urlsplit("http://" + value)
            hostname = parsed.hostname
            port = parsed.port
            if not hostname or parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
                raise ValueError("invalid host")
            if "?" in value or "#" in value or "\\" in value or value.endswith(":"):
                raise ValueError("invalid host")
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                if len(hostname) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.split(".")):
                    raise ValueError("invalid host")
                if re.fullmatch(r"[0-9.]+", hostname):
                    raise ValueError("invalid IP address")
                rendered = hostname.lower()
            else:
                rendered = f"[{address.compressed}]" if address.version == 6 else address.compressed
            if port is not None:
                if not 1 <= port <= 65535:
                    raise ValueError("invalid port")
                rendered += f":{port}"
            hosts.add(rendered)
        except ValueError as exc:
            raise LumaError("managed registry host must be a valid hostname or IP with an optional port; schemes, paths and CIDRs are not supported") from exc
    return sorted(hosts)


def ensure_managed_registry_access(
    registries: list[str], *, executor: Any, os_name: str, docker: str = "", check_only: bool = False
) -> dict[str, Any]:
    hosts = normalize_managed_registry_hosts(registries)
    if not isinstance(check_only, bool):
        raise LumaError("checkOnly must be a boolean")
    if not hosts:
        return {"registries": [], "verified": True, "changed": False, "dockerRestarted": False,
                "affectedAllocationIds": [], "message": "No managed HTTP registries configured"}
    if os_name == "darwin":
        from .orbstack_registry import ensure_orbstack_registries

        if not docker:
            # The supported App bundle provides the CLI even under launchd's
            # minimal PATH. It is only executed by the socket owner's child.
            docker = "/Applications/OrbStack.app/Contents/MacOS/xbin/docker"
        try:
            result = ensure_orbstack_registries(hosts, docker=docker, check_only=check_only)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LumaError(f"Managed OrbStack registry setup failed: {exc}") from exc
        if result.get("verified") is not True or result.get("registries") != hosts:
            raise LumaError("OrbStack registry setup returned an invalid verification result")
        return result
    if os_name != "linux":
        raise LumaError(f"Managed registry setup is not supported on {os_name}")
    # Setup is deliberately before Nomad starts. Refuse disruptive reconfiguration
    # of an already busy node; operators must use the maintenance/recovery path.
    payload = shlex.quote(json.dumps(hosts)) + " " + ("true" if check_only else "false")
    script = "set -euo pipefail\npython3 - " + payload + " <<'LUMA_REGISTRY_PY'\n" + REGISTRY_SETUP_SCRIPT + "\nLUMA_REGISTRY_PY"
    try:
        output = executor.sudo(script)
    except LumaError as exc:
        raise LumaError(f"Managed registry setup failed for {', '.join(hosts)}: {exc}") from exc
    prefix = "LUMA_MANAGED_REGISTRY_RESULT="
    try:
        line = next(line[len(prefix):] for line in str(output).splitlines() if line.startswith(prefix))
        result = json.loads(line)
    except (StopIteration, ValueError) as exc:
        raise LumaError("Docker registry setup returned no verified result") from exc
    if not isinstance(result, dict) or result.get("verified") is not True or result.get("registries") != hosts:
        raise LumaError("Docker registry setup returned an invalid verification result")
    return result


REGISTRY_SETUP_SCRIPT = inspect.getsource(proxy_bypasses_registry_hosts) + r'''
import json, os, pathlib, re, shlex, subprocess, sys, tempfile, time, urllib.request, urllib.error
hosts = json.loads(sys.argv[1])
check_only = len(sys.argv) > 2 and sys.argv[2] == 'true'
path = pathlib.Path('/etc/docker/daemon.json')
dropin = pathlib.Path('/etc/systemd/system/docker.service.d/zz-luma-registry-no-proxy.conf')

def run(*args):
    if args[0] == 'docker':
        args = (args[0], '--host', 'unix:///var/run/docker.sock', *args[1:])
    env = {key: value for key, value in os.environ.items() if not key.startswith('DOCKER_')}
    return subprocess.check_output(args, text=True, timeout=120, env=env).strip()

def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.luma-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

info = json.loads(run('docker', 'info', '--format', '{{json .}}'))
if str(info.get('OperatingSystem', '')).lower() in {'orbstack', 'docker desktop'}:
    raise RuntimeError('Linux Docker configuration does not own the selected Docker runtime')
indexes = info.get('RegistryConfig', {}).get('IndexConfigs', {})
runtime_ready = all(indexes.get(h, {}).get('Secure') is False for h in hosts)
runtime_ready &= proxy_bypasses_registry_hosts(info, hosts)
if check_only and not runtime_ready:
    raise RuntimeError('Managed HTTP registry transport is not configured; maintenance is required because this node is read-only for Docker configuration')
# A daemon configured through CLI flags or another owner is already usable.
# Do not create a competing daemon.json entry or restart a busy, ready daemon.
check_only = check_only or runtime_ready
if not check_only:
    if run('systemctl', 'is-active', 'docker') != 'active':
        raise RuntimeError('Managed registry setup requires the local systemd Docker service; configure this runtime in a maintenance window')
    service = run('systemctl', 'show', 'docker', '--property=ExecStart', '--value')
    matched = re.search(r'argv\[\]=(.*?)\s+;', service)
    argv = shlex.split(matched.group(1)) if matched else []
    if not argv or pathlib.Path(argv[0]).name != 'dockerd':
        raise RuntimeError('Cannot verify the local Docker service configuration owner; maintenance is required')
    def options(name):
        result = []
        for i, arg in enumerate(argv):
            if arg == name:
                if i + 1 >= len(argv): raise RuntimeError('Docker service has an invalid option')
                result.append(argv[i + 1])
            elif arg.startswith(name + '='): result.append(arg.split('=', 1)[1])
            elif name == '-H' and arg.startswith('-H') and len(arg) > 2: result.append(arg[2:])
        return result
    configs = options('--config-file')
    if any(value != '/etc/docker/daemon.json' for value in configs):
        raise RuntimeError('Docker uses a custom configuration file; configure registry transport with that owner in a maintenance window')
    if options('--insecure-registry') or options('--no-proxy'):
        raise RuntimeError('Docker registry transport is configured by service flags; update those flags in a maintenance window')
    endpoints = options('-H') + options('--host')
    if endpoints and not any(value in {'fd://', 'unix:///var/run/docker.sock', 'unix:///run/docker.sock'} for value in endpoints):
        raise RuntimeError('The systemd Docker service does not own the default local socket; maintenance is required')
# Malformed configuration is an error, never permission to overwrite it.
def reject_constant(value): raise ValueError('Docker daemon config contains a non-finite JSON number')
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Docker daemon config contains a duplicate JSON field')
        result[key] = value
    return result
data = json.loads(path.read_text(), parse_constant=reject_constant, object_pairs_hook=unique_object) if path.exists() and not check_only else {}
if not isinstance(data, dict): raise RuntimeError('Docker daemon config must be an object')
regs = data.get('insecure-registries', [])
if not isinstance(regs, list): raise RuntimeError('insecure-registries must be an array')
no_proxy = [x.strip() for x in str(info.get('NoProxy') or '').split(',') if x.strip()]
for host in hosts:
    for entry in [host, host.rsplit(':', 1)[0] if ':' in host else host]:
        if entry not in no_proxy: no_proxy.append(entry)
no_proxy = ','.join(no_proxy)
config_changed = any(h not in regs for h in hosts)
data['insecure-registries'] = list(dict.fromkeys(regs + hosts))
# daemon.json proxy settings take precedence over systemd environment.
if 'proxies' in data:
    if not isinstance(data['proxies'], dict): raise RuntimeError('Docker proxies must be an object')
    config_changed |= data['proxies'].get('no-proxy') != no_proxy
    data['proxies']['no-proxy'] = no_proxy
# Escape systemd's environment quoting and percent specifiers.
escaped = no_proxy.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
text = '[Service]\nEnvironment="NO_PROXY=' + escaped + '"\nEnvironment="no_proxy=' + escaped + '"\n'
dropin_changed = not dropin.exists() or dropin.read_text() != text
indexes = info.get('RegistryConfig', {}).get('IndexConfigs', {})
needs_restart = not check_only and (config_changed or not runtime_ready)
if needs_restart:
    if run('docker', 'ps', '-q'):
        raise RuntimeError('Docker has running containers; drain this node before changing registry transport')
    if config_changed:
        # Validate a candidate before replacing the user's configuration.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, candidate = tempfile.mkstemp(prefix='.luma-check-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as f: json.dump(data, f)
            run('dockerd', '--validate', '--config-file=' + candidate)
            if path.exists(): atomic_write(path.with_name(path.name + '.luma-registry.bak'), path.read_text())
            atomic_write(path, json.dumps(data, indent=2) + '\n')
        finally:
            os.unlink(candidate)
    if dropin_changed: atomic_write(dropin, text)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', 'docker')
deadline = time.monotonic() + 45
while True:
    try:
        info = json.loads(run('docker', 'info', '--format', '{{json .}}'))
        break
    except Exception:
        if time.monotonic() >= deadline: raise RuntimeError('Docker did not reconnect after registry setup')
        time.sleep(0.5)
for host in hosts:
    if info.get('RegistryConfig', {}).get('IndexConfigs', {}).get(host, {}).get('Secure', True):
        raise RuntimeError('Docker did not activate HTTP registry transport: ' + host)
    if not proxy_bypasses_registry_hosts(info, [host]):
        raise RuntimeError('Docker did not activate registry proxy bypass: ' + host)
    # No external proxy or TLS fallback: this must be the intended HTTP registry.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open('http://' + host + '/v2/', timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status not in (200, 401):
        raise RuntimeError('Registry /v2/ probe failed: ' + host + ' HTTP ' + str(status))
print('LUMA_MANAGED_REGISTRY_RESULT=' + json.dumps({'registries': hosts, 'runtime': 'linux-docker',
    'verified': True, 'changed': bool(needs_restart), 'dockerRestarted': bool(needs_restart),
    'affectedAllocationIds': [], 'message': 'Managed HTTP registry transport verified: ' + ', '.join(hosts)}))
'''
