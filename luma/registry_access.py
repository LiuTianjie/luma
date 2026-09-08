"""Transport setup for Luma-owned registries before a worker joins Nomad."""
from __future__ import annotations

import ipaddress
import json
import shlex
from typing import Any

from .errors import LumaError
from .registry import normalize_registry_host


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


def configure_join_registries(registries: list[str], *, executor: Any, os_name: str) -> str:
    """Configure only Linux's Docker driver; macOS Nomad nodes use exec."""
    if not isinstance(registries, list):
        raise LumaError("insecureRegistries must be a list")
    hosts = sorted({normalize_registry_host(h) for h in registries})
    if not hosts:
        return "No managed HTTP registries configured"
    if os_name == "darwin":
        return "Registry Docker transport not applicable to macOS exec nodes"
    if os_name != "linux":
        raise LumaError(f"Managed registry setup is not supported on {os_name}")
    # Setup is deliberately before Nomad starts. Refuse disruptive reconfiguration
    # of an already busy node; operators must use the maintenance/recovery path.
    payload = shlex.quote(json.dumps(hosts))
    script = "set -euo pipefail\npython3 - " + payload + " <<'LUMA_REGISTRY_PY'\n" + REGISTRY_SETUP_SCRIPT + "\nLUMA_REGISTRY_PY"
    try:
        executor.sudo(script)
    except LumaError as exc:
        raise LumaError(f"Managed registry setup failed for {', '.join(hosts)}; node must not join: {exc}") from exc
    return "Managed HTTP registry transport verified: " + ", ".join(hosts)


REGISTRY_SETUP_SCRIPT = r'''
import json, os, pathlib, subprocess, sys, tempfile, urllib.request, urllib.error
hosts = json.loads(sys.argv[1])
path = pathlib.Path('/etc/docker/daemon.json')
dropin = pathlib.Path('/etc/systemd/system/docker.service.d/zz-luma-registry-no-proxy.conf')

def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()

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

# Malformed configuration is an error, never permission to overwrite it.
data = json.loads(path.read_text()) if path.exists() else {}
if not isinstance(data, dict): raise RuntimeError('Docker daemon config must be an object')
regs = data.get('insecure-registries', [])
if not isinstance(regs, list): raise RuntimeError('insecure-registries must be an array')
info = json.loads(run('docker', 'info', '--format', '{{json .}}'))
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
needs_restart = (config_changed or dropin_changed
                 or any(indexes.get(h, {}).get('Secure', True) for h in hosts)
                 or any(h not in str(info.get('NoProxy') or '').split(',') for h in hosts))
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
info = json.loads(run('docker', 'info', '--format', '{{json .}}'))
for host in hosts:
    if info.get('RegistryConfig', {}).get('IndexConfigs', {}).get(host, {}).get('Secure', True):
        raise RuntimeError('Docker did not activate HTTP registry transport: ' + host)
    if host not in str(info.get('NoProxy') or '').split(','):
        raise RuntimeError('Docker did not activate registry proxy bypass: ' + host)
    # No external proxy or TLS fallback: this must be the intended HTTP registry.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://' + host + '/v2/', timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status not in (200, 401):
        raise RuntimeError('Registry /v2/ probe failed: ' + host + ' HTTP ' + str(status))
print('Managed registry transport and reachability verified')
'''
