from __future__ import annotations

"""Generate Nomad agent config and detect node facts for `luma node join`.

This module keeps node join as close to zero-config as possible: operators run
`luma node join`, type their password once, and Luma auto-detects the rest.

Every non-obvious choice here is tied to a real migration failure; the comments
explain WHY, not just WHAT.
"""

import os
import re
import subprocess
from typing import Dict, List, Optional

from .errors import LumaError

# Pinned, battle-tested release. The checkpoint "latest" endpoint served 2.0.x
# (days old, with a node-introduction-token enforcement that complicates join);
# a reliability migration must not run on a brand-new major.
NOMAD_VERSION = "1.9.7"
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

    lines: List[str] = []
    lines.append('data_dir = "/opt/nomad/data"')
    # bind 0.0.0.0 so the HTTP API is reachable on 127.0.0.1 (Luma Control's
    # default nomadAddr) AND on Tailscale. The host firewall (ufw, public-iface
    # DROP) keeps 4646/4647/4648 off the public interface, so 0.0.0.0 is safe.
    # Binding only the Tailscale IP broke Control's localhost client.
    lines.append('bind_addr = "0.0.0.0"')
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
        lines.append("}")
        lines.append("")

    lines.append("client {")
    lines.append("  enabled = true")
    if cpu_total_compute:
        lines.append(f"  cpu_total_compute = {int(cpu_total_compute)}")
    if role == "client":
        joins = ", ".join(f'"{a}"' for a in (server_addrs or []))
        lines.append("  server_join {")
        lines.append(f"    retry_join = [{joins}]")
        lines.append("  }")
    lines.append("  meta {")
    lines.append(f'    region         = "{region}"')
    lines.append(f'    luma_node_name = "{node_name}"')
    for k, v in (extra_meta or {}).items():
        lines.append(f'    {k} = "{v}"')
    lines.append("  }")
    lines.append("}")
    lines.append("")

    # Host bind mounts (egress config, Control's own /opt/luma mounts, Traefik
    # dynamic dir) require volumes enabled — Nomad's docker driver forbids host
    # paths by default. Named volumes work without this, but core components use
    # host paths, so always enable.
    lines.append('plugin "docker" {')
    lines.append("  config {")
    lines.append('    pull_activity_timeout = "30m"')
    lines.append("    volumes {")
    lines.append("      enabled = true")
    lines.append("    }")
    lines.append("  }")
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
    hand on aly (linux/systemd) and gaojiu (darwin/launchd) during migration.
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
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
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
        "  <array><string>/usr/local/bin/nomad</string><string>agent</string>"
        "<string>-config</string><string>/etc/nomad.d</string></array>\n"
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
