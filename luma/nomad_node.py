from __future__ import annotations

"""Generate Nomad agent config and detect node facts for `luma node join`.

This module keeps node join as close to zero-config as possible: operators run
`luma node join`, type their password once, and Luma auto-detects the rest.

Every non-obvious choice here is tied to a real migration failure; the comments
explain WHY, not just WHAT.
"""

import os
import re
import json
import subprocess
from typing import Dict, List, Optional

from .errors import LumaError
from .nomad_security import NOMAD_VERSION, NOMAD_SHA256, mesh_ip, rpc_host
from .nomad_firewall import GUARD

CNI_PLUGINS_VERSION = "1.6.2"

# Heuristic clock for Apple Silicon. Nomad's CPU fingerprint reports a garbage
# total (~28 MHz) on M-series because it misreads the perf/efficiency core
# frequencies, so every job "exhausts CPU". We override cpu_total_compute with
# cores * this. Tuned on M4 (10 cores -> 30000).
APPLE_MHZ_PER_CORE = 3000


def detect_os(uname_s: Optional[str] = None) -> str:
    name = (uname_s or os.uname().sysname).lower()
    if "darwin" in name:
        return "darwin"
    if "linux" in name:
        return "linux"
    return name


def detect_tailscale_ip(run=None) -> Optional[str]:
    """Return the node's Tailscale IPv4, or None. Used for advertise addresses."""
    runner = run or _run
    for cmd in (
        ["tailscale", "ip", "-4"],
        ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"],
    ):
        out = runner(cmd)
        if out:
            ip = out.strip().splitlines()[0].strip()
            if _looks_like_ipv4(ip):
                return ip
    return None


def detect_cpu_total_compute(os_name: str, run=None) -> Optional[int]:
    """Apple-Silicon-only: compute an explicit cpu_total_compute override.

    Returns None on Linux (Nomad fingerprints CPU correctly there).
    """
    if os_name != "darwin":
        return None
    runner = run or _run
    out = runner(["sysctl", "-n", "hw.ncpu"])
    try:
        cores = int((out or "").strip())
    except (TypeError, ValueError):
        cores = 0
    if cores < 1:
        cores = 4  # conservative fallback
    return cores * APPLE_MHZ_PER_CORE


def render_agent_config(
    *,
    os_name: str,
    role: str,
    tailscale_ip: str,
    region: str,
    node_name: str,
    server_addrs: Optional[List[str]] = None,
    cpu_total_compute: Optional[int] = None,
    extra_meta: Optional[Dict[str, str]] = None,
    acl_enabled: bool = True,
    trusted_host: bool = False,
    client_enabled: bool = True,
    version: str = NOMAD_VERSION,
    tls: Optional[Dict[str, str]] = None,
    tls_http: bool = True,
    rpc_upgrade_mode: bool = False,
    introduction_strict: bool = True,
) -> str:
    """Render a Nomad agent HCL config from detected node facts.

    role: "server" (manager; also runs as client) or "client" (worker).
    """
    if role not in {"server", "client"}:
        raise LumaError(f"role must be server or client, got {role!r}")
    if not tailscale_ip:
        raise LumaError("tailscale_ip is required (Nomad advertises over the Tailscale mesh)")
    if role == "client" and not server_addrs:
        raise LumaError("client role requires server_addrs to join")
    tailscale_ip = mesh_ip(tailscale_ip)
    for address in server_addrs or []:
        rpc_host(address)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", node_name):
        raise LumaError("invalid Nomad node name")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise LumaError("invalid Nomad version")

    lines: List[str] = []
    lines.append('data_dir = "/opt/nomad/data"')
    lines.append(f'name = {json.dumps(node_name)}')
    lines.append(f'bind_addr = "{tailscale_ip}"')
    # HTTP supports multiple literal addresses. Keep localhost for Control and
    # node diagnostics, without making every public/LAN NIC a listener.
    lines.extend(["addresses {", f'  http = "127.0.0.1 {tailscale_ip}"', f'  rpc = "{tailscale_ip}"', f'  serf = "{tailscale_ip}"', "}"])
    lines.extend(["acl {", f"  enabled = {str(acl_enabled).lower()}", "}"])
    lines.append("disable_update_check = true")
    lines.append("")
    lines.append("advertise {")
    lines.append(f'  http = "{tailscale_ip}"')
    lines.append(f'  rpc  = "{tailscale_ip}"')
    lines.append(f'  serf = "{tailscale_ip}"')
    lines.append("}")
    lines.append("")

    if role == "server":
        lines.append("server {")
        lines.append("  enabled          = true")
        lines.append("  bootstrap_expect = 1")
        # Tolerate slow/flaky home links (Tailscale DERP relay adds latency)
        # before declaring a client lost.
        lines.append('  heartbeat_grace   = "30s"')
        lines.append('  min_heartbeat_ttl = "15s"')
        if tuple(int(v) for v in version.split(".")) >= (2, 0, 0):
            lines.extend(["  client_introduction {", f'    enforcement = "{"strict" if introduction_strict else "warn"}"', '    default_identity_ttl = "10m"', '    max_identity_ttl = "30m"', "  }"])
        lines.append("}")
        lines.append("")

    lines.append("client {")
    lines.append(f"  enabled = {str(client_enabled).lower()}")
    lines.extend(['  options = {', '    "driver.allowlist" = "docker"', '  }'])
    lines.extend(["  template {", "    disable_file_sandbox = false", "  }"])
    if cpu_total_compute:
        lines.append(f"  cpu_total_compute = {int(cpu_total_compute)}")
    if role == "client":
        joins = ", ".join(json.dumps(a) for a in (server_addrs or []))
        lines.append("  server_join {")
        lines.append(f"    retry_join = [{joins}]")
        lines.append("  }")
    lines.append("  meta {")
    lines.append(f'    region         = {json.dumps(region)}')
    lines.append(f'    luma_node_name = "{node_name}"')
    # Edge ingress runs on the manager while tenant allocations may run on any
    # Tailscale-connected client. Nomad's default host service address can be a
    # provider-private LAN address, so publish the mesh address as schedulable
    # node metadata for runtime service interpolation.
    lines.append(f'    luma_tailscale_ip = "{tailscale_ip}"')
    lines.append(f'    luma_host_access = "{"trusted" if trusted_host else "isolated"}"')
    for k, v in (extra_meta or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", k) or k in {"region", "luma_node_name", "luma_tailscale_ip", "luma_host_access"}:
            raise LumaError("invalid or reserved Nomad node metadata key")
        lines.append(f'    {k} = {json.dumps(str(v))}')
    lines.append("  }")
    lines.append("}")
    lines.append("")

    # Ordinary workers cannot bind arbitrary host paths. Administrators may
    # explicitly designate a trusted system/storage node after reviewing its
    # running jobs; Control separately enforces grants for those jobs.
    lines.append('plugin "docker" {')
    lines.append("  config {")
    lines.append('    pull_activity_timeout = "30m"')
    lines.append('    extra_labels = ["job_id", "job_name", "task_group_name", "task_name"]')
    lines.append("    allow_privileged = false")
    lines.append('    allow_caps = ["chown", "dac_override", "fowner", "fsetid", "kill", "net_bind_service", "setgid", "setuid", "sys_chroot"]')
    if tuple(int(v) for v in version.split(".")) >= (2, 0, 0):
        lines.append('    allowed_modes = { pid = [""], ipc = ["", "private"], userns = [""], uts = [""] }')
    lines.append("    volumes {")
    lines.append(f"      enabled = {str(trusted_host).lower()}")
    lines.append("    }")
    lines.append("  }")
    lines.append("}")
    if tls:
        if set(tls) != {"ca_file", "cert_file", "key_file"} or not all(str(p).startswith("/") for p in tls.values()):
            raise LumaError("Nomad TLS requires absolute ca_file, cert_file and key_file paths")
        lines.extend(["tls {", f"  http = {str(tls_http).lower()}", "  rpc = true", "  verify_https_client = true", "  verify_server_hostname = true", f"  rpc_upgrade_mode = {str(rpc_upgrade_mode).lower()}"])
        lines.extend(f"  {key} = {json.dumps(str(value))}" for key, value in tls.items())
        lines.append("}")
    lines.append("")
    return "\n".join(lines)


def install_nomad_commands(
    *,
    os_name: str,
    arch: str,
    config_hcl: str,
) -> Dict[str, str]:
    """Return the shell artifacts to install a Nomad agent on this node.

    Pure/testable: returns {download_url, config, service_kind, service_unit}.
    The caller writes/executes them with sudo. Mirrors the steps validated by
    hand on Linux (systemd) and macOS (launchd) nodes.
    """
    if os_name == "darwin":
        plat = f"darwin_{_nomad_arch(arch)}"
        service_kind = "launchd"
        service_unit = _launchd_plist()
    else:
        plat = f"linux_{_nomad_arch(arch)}"
        service_kind = "systemd"
        service_unit = _systemd_unit()
    url = (
        f"https://releases.hashicorp.com/nomad/{NOMAD_VERSION}/"
        f"nomad_{NOMAD_VERSION}_{plat}.zip"
    )
    return {
        "download_url": url,
        "sha256": NOMAD_SHA256[plat],
        "config": config_hcl,
        "service_kind": service_kind,
        "service_unit": service_unit,
    }


def cni_plugins_url(arch: str) -> str:
    plat = f"linux-{_nomad_arch(arch)}"
    return (
        "https://github.com/containernetworking/plugins/releases/download/"
        f"v{CNI_PLUGINS_VERSION}/cni-plugins-{plat}-v{CNI_PLUGINS_VERSION}.tgz"
    )


def _nomad_arch(arch: str) -> str:
    a = (arch or "").lower()
    if a in {"x86_64", "amd64"}:
        return "amd64"
    if a in {"arm64", "aarch64"}:
        return "arm64"
    return a or "amd64"


def _systemd_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Nomad\n"
        "Wants=network-online.target docker.service tailscaled.service\n"
        "After=network-online.target docker.service tailscaled.service\n"
        "Requires=luma-nomad-guard.service\n"
        "After=luma-nomad-guard.service\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        f"ExecStartPre={GUARD}\n"
        "ExecStart=/usr/local/bin/nomad agent -config /etc/nomad.d\n"
        "ExecReload=/bin/kill -HUP $MAINPID\n"
        "KillMode=process\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "LimitNOFILE=65536\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _launchd_plist() -> str:
    # PATH includes /usr/local/bin so the docker driver finds the OrbStack/
    # Docker Desktop docker binary (only on the login-shell PATH otherwise).
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>io.luma.nomad</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array><string>/bin/sh</string><string>-c</string>"
        f"<string>{GUARD} &amp;&amp; exec /usr/local/bin/nomad agent -config /etc/nomad.d</string></array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "  <key>StandardErrorPath</key><string>/var/log/nomad.log</string>\n"
        "  <key>StandardOutPath</key><string>/var/log/nomad.log</string>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict><key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>\n"
        "</dict></plist>\n"
    )


def _run(cmd: List[str]) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _looks_like_ipv4(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value))
