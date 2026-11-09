"""Persistent, fail-closed Nomad network boundary (Linux nftables / macOS PF).

Rules run before Docker/CNI accept and NAT rules, and do not depend on a public
interface name or the default route. Only exact enrolled mesh peers may reach
RPC. HTTP is limited to loopback and explicitly listed control-plane peers.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import fcntl
import re
import shlex
import time
from pathlib import Path
from typing import Any, Mapping

from .errors import LumaError
from .launchd import restart_service
from .nomad_security import SECURITY_ROOT, mesh_ip, private_write, read_private_json, security_root

GUARD = "/opt/luma/security/bin/nomad-guard"
RULES = str(SECURITY_ROOT / "firewall.rules")
APPLIED = str(SECURITY_ROOT / "firewall.applied")


def network_policy(*, os_name: str, address: str, rpc_peers: list[str], http_peers: list[str], serf_peers: list[str] | None = None, interface: str = "") -> dict[str, Any]:
    if os_name not in {"linux", "darwin"}:
        raise LumaError("Nomad network guard supports Linux and macOS only")
    if os_name == "darwin" and not re.fullmatch(r"(?:utun[0-9]+|tailscale[0-9]+)", interface):
        raise LumaError("cannot identify the macOS Tailscale interface; refusing an unprotected Nomad start")
    return {
        "schema": 1, "os": os_name, "address": mesh_ip(address),
        "rpcPeers": sorted({mesh_ip(p) for p in rpc_peers}),
        "httpPeers": sorted({mesh_ip(p) for p in http_peers}),
        "serfPeers": sorted({mesh_ip(p) for p in (serf_peers or [])}),
        "interface": interface if os_name == "darwin" else "tailscale0",
    }


def _validated(policy: Mapping[str, Any]) -> dict[str, Any]:
    if policy.get("schema") != 1:
        raise LumaError("invalid Nomad network policy")
    return network_policy(
        os_name=str(policy.get("os") or ""), address=str(policy.get("address") or ""),
        rpc_peers=list(policy.get("rpcPeers") or []), http_peers=list(policy.get("httpPeers") or []),
        serf_peers=list(policy.get("serfPeers") or []), interface=str(policy.get("interface") or ""),
    )


def render_nftables(policy: Mapping[str, Any]) -> str:
    p = _validated(policy)
    if p["os"] != "linux":
        raise LumaError("nftables requires a Linux policy")
    lines = [
        "# One nft transaction: never flush the host ruleset.",
        "add table inet luma_nomad",
        "flush table inet luma_nomad",
    ]
    for chain, hook in (("ingress", "prerouting"), ("local", "input")):
        lines.append(f"add chain inet luma_nomad {chain} {{ type filter hook {hook} priority -310; policy accept; }}")
        lines.append(f'add rule inet luma_nomad {chain} iifname "lo" return')
        # Local workloads intentionally use the egress proxy. These are the
        # bridge interfaces created by Docker and Luma's Nomad CNI, never a
        # blanket exception for the control-plane ports.
        for interface in ("docker0", "br-*", "nomad"):
            lines.append(f'add rule inet luma_nomad {chain} iifname "{interface}" tcp dport 7890 return')
            lines.append(f'add rule inet luma_nomad {chain} iifname "{interface}" udp dport 7890 return')
        for key, proto, port in (("httpPeers", "tcp", 4646), ("rpcPeers", "tcp", 4647), ("serfPeers", "tcp", 4648), ("serfPeers", "udp", 4648), ("rpcPeers", "tcp", 7890), ("rpcPeers", "udp", 7890)):
            for peer in p[key]:
                family = "ip" if ipaddress.ip_address(peer).version == 4 else "ip6"
                # Do not allow routed/SNATed traffic simply because its source
                # resembles a mesh address. Both interface and source must match.
                lines.append(f'add rule inet luma_nomad {chain} iifname "tailscale0" {family} saddr {peer} {proto} dport {port} return')
        lines.append(f"add rule inet luma_nomad {chain} tcp dport {{ 4646, 4647, 4648, 7890 }} counter drop")
        lines.append(f"add rule inet luma_nomad {chain} udp dport {{ 4648, 7890 }} counter drop")
    lines += [
        "add chain inet luma_nomad transit { type filter hook forward priority -310; policy accept; }",
        "# Includes Docker bridges, Nomad CNI, IPv6 and traffic to OTHER nodes.",
        "add rule inet luma_nomad transit tcp dport { 4646, 4647, 4648 } counter drop",
        "add rule inet luma_nomad transit udp dport 4648 counter drop",
    ]
    return "\n".join(lines) + "\n"


def render_pf(policy: Mapping[str, Any]) -> str:
    p = _validated(policy)
    if p["os"] != "darwin":
        raise LumaError("PF requires a macOS policy")
    lines = ['# Loaded before other filter anchors; affects inet and inet6.', 'pass in quick on lo0 proto tcp to any port { 4646, 4647, 4648, 7890 }', 'pass in quick on lo0 proto udp to any port { 4648, 7890 }']
    for key, proto, port in (("httpPeers", "tcp", 4646), ("rpcPeers", "tcp", 4647), ("serfPeers", "tcp", 4648), ("serfPeers", "udp", 4648), ("rpcPeers", "tcp", 7890), ("rpcPeers", "udp", 7890)):
        for peer in p[key]:
            lines.append(f'pass in quick on {p["interface"]} proto {proto} from {peer} to any port {port} no state')
    lines += ["block drop in quick proto tcp to any port { 4646, 4647, 4648, 7890 }", "block drop in quick proto udp to any port { 4648, 7890 }"]
    return "\n".join(lines) + "\n"


def render_guard_script(os_name: str, policy: Mapping[str, Any] | None = None) -> str:
    if os_name == "linux":
        apply = 'command -v nft >/dev/null\nnft --check --file "$work/rules"\nnft --file "$work/rules"\nsha256sum "$work/rules" | cut -d \' \' -f 1'
    elif os_name == "darwin":
        p = _validated(policy or {})
        peer = (p["rpcPeers"] or [p["address"]])[0]
        apply = (
            # macOS assigns a new utun number after some Tailscale restarts.
            # Rediscover only the interface carrying our exact mesh address or
            # route to an enrolled server; never permit every utun interface.
            f"iface=$(/sbin/ifconfig | /usr/bin/awk -v ip={shlex.quote(p['address'])} '/^[a-zA-Z0-9]+:/ {{ iface=$1; sub(/:$/,\"\",iface) }} $1==\"inet\" && $2==ip {{ print iface; exit }}')\n"
            f"if [ -z \"$iface\" ]; then iface=$(/sbin/route -n get {shlex.quote(peer)} | /usr/bin/awk '/interface:/ {{print $2; exit}}'); fi\n"
            "printf '%s' \"$iface\" | /usr/bin/grep -Eq '^(utun|tailscale)[0-9]+$'\n"
            'cp "$work/rules" "$work/source"\n'
            '/usr/bin/sed -E "s/on (utun[0-9]+|tailscale[0-9]+)/on $iface/g" "$work/source" > "$work/rules"\n'
            '/sbin/pfctl -nf "$work/rules"\n'
            "# Fail if a later PF reload dropped the anchor from the root ruleset.\n"
            "/sbin/pfctl -sr | /usr/bin/grep -q 'anchor \"luma-nomad\"'\n"
            '/sbin/pfctl -a luma-nomad -f "$work/rules"\n'
            "/sbin/pfctl -s info 2>/dev/null | /usr/bin/grep -q 'Status: Enabled' || /sbin/pfctl -E >/dev/null\n"
            # The acknowledgment identifies the enrolled peer policy. Only its
            # OS-derived interface name is substituted, and is recorded too.
            f'printf "%s\\n" "$iface" > {SECURITY_ROOT}/firewall.interface\n'
            '/usr/bin/shasum -a 256 "$work/source" | cut -d \' \' -f 1'
        )
    else:
        raise LumaError("unsupported firewall platform")
    return (
        "#!/bin/sh\nset -eu\numask 077\n"
        f"test -f {RULES} && test ! -L {RULES}\n"
        f"work=$(mktemp -d {SECURITY_ROOT}/.guard.XXXXXX)\n"
        "trap 'rm -rf \"$work\"' EXIT HUP INT TERM\n"
        f'cp {RULES} "$work/rules"\n'
        "apply_rules() {\n" + apply + "\n}\n"
        'apply_rules > "$work/applied"\n'
        f'mv "$work/applied" {APPLIED}\n'
    )


def guard_service() -> str:
    return (
        "[Unit]\nDescription=Luma Nomad network boundary\n"
        "After=network-online.target\nBefore=nomad.service\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\n" + f"ExecStart={GUARD}\n" +
        "[Install]\nWantedBy=multi-user.target\n"
    )


def install_guard(remote: Any, policy: Mapping[str, Any]) -> str:
    """Only called by explicit bootstrap/join/security apply, never by import."""
    p = _validated(policy)
    rules = render_nftables(p) if p["os"] == "linux" else render_pf(p)
    remote.sudo(f"set -eu; install -d -m 700 {SECURITY_ROOT}; install -d -m 755 /opt/luma/security/bin")
    remote.write_secret(json.dumps(p, indent=2) + "\n", str(SECURITY_ROOT / "network.json"))
    remote.write_secret(rules, RULES)
    remote.write_secret(render_guard_script(p["os"], p), GUARD, mode="700")
    if p["os"] == "linux":
        remote.sudo(
            "set -eu; if ! command -v nft >/dev/null; then "
            "if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq nftables; "
            "elif command -v dnf >/dev/null; then dnf install -y nftables; "
            "else echo 'Install nftables before starting Nomad' >&2; exit 1; fi; fi"
        )
        remote.write_secret(guard_service(), "/etc/systemd/system/luma-nomad-guard.service", mode="644")
        remote.write_secret(
            "/opt/luma/control/security-audit.jsonl {\n  daily\n  rotate 30\n  compress\n  delaycompress\n  missingok\n  notifempty\n  create 0600 root root\n}\n",
            "/etc/logrotate.d/luma-security-audit", mode="644",
        )
        remote.write_secret(
            "[Unit]\nDescription=Reconcile enrolled Nomad peers\n[Timer]\nOnBootSec=5s\nOnUnitActiveSec=5s\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n",
            "/etc/systemd/system/luma-nomad-guard.timer", mode="644",
        )
        remote.sudo("set -eu; systemctl daemon-reload; systemctl enable luma-nomad-guard.service luma-nomad-guard.timer; systemctl restart luma-nomad-guard.service; systemctl start luma-nomad-guard.timer")
        # nft return/accept does not override a later UFW DROP. Permit the mesh
        # interface at that second layer; the earlier nft hook remains the
        # exact-source authority and is required before every Nomad start.
        allowances = [("tailscale0", port, protocol) for port, protocol in ((4646, "tcp"), (4647, "tcp"), (4648, "tcp"), (4648, "udp"), (7890, "tcp"), (7890, "udp"))]
        allowances += [(interface, 7890, protocol) for interface in ("docker0", "br+", "nomad") for protocol in ("tcp", "udp")]
        ufw = []
        for interface, port, protocol in allowances:
            rule = f"allow in on {interface} to any port {port} proto {protocol}"
            # Move our interface exceptions before any existing catch-all deny
            # (older releases already installed a deny for the proxy port).
            # Unlike insert 1, prepend also accepts a fresh, empty ruleset.
            ufw += [f"ufw --force delete {rule} >/dev/null 2>&1 || true", f"ufw prepend {rule}"]
        remote.sudo("set -eu; if command -v ufw >/dev/null; then " + "; ".join(ufw) + "; fi")
    else:
        # Preserve unrelated PF configuration; load only our anchor thereafter.
        # The root anchor must be before all other filter rules, after PF's
        # options/normalization/NAT sections. Inserting before the first filter
        # anchor is deterministic for Apple's stock and ordinary custom files.
        import base64
        installer = '''from pathlib import Path
import re, shutil
p=Path('/etc/pf.conf')
s=p.read_text()
if 'anchor "luma-nomad"' not in s:
    lines=s.splitlines(keepends=True)
    i=next((i for i,line in enumerate(lines) if re.match(r'\\s*(?:anchor |pass |block )', line)),len(lines))
    lines[i:i]=['anchor "luma-nomad" quick\\n', 'load anchor "luma-nomad" from "/opt/luma/security/nomad/firewall.rules"\\n']
    backup=p.with_name('pf.conf.luma-nomad-before')
    if not backup.exists(): shutil.copy2(p,backup)
    p.write_text(''.join(lines))
'''
        encoded = base64.b64encode(installer.encode()).decode()
        remote.sudo(f"set -eu; printf %s {shlex.quote(encoded)} | base64 -d | python3; /sbin/pfctl -nf /etc/pf.conf; /sbin/pfctl -f /etc/pf.conf; {GUARD}")
        plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>io.luma.nomad-guard</string>
<key>ProgramArguments</key><array><string>{GUARD}</string></array>
<key>RunAtLoad</key><true/><key>StartInterval</key><integer>5</integer>
<key>StandardErrorPath</key><string>/var/log/luma-nomad-guard.log</string>
</dict></plist>
'''
        remote.write_secret(plist, "/Library/LaunchDaemons/io.luma.nomad-guard.plist", mode="644")
        restart_service(remote, label="io.luma.nomad-guard", plist="/Library/LaunchDaemons/io.luma.nomad-guard.plist")
    return "Nomad network boundary installed and applied"


def stage_peer(address: str, *, timeout: float = 20) -> None:
    """Enroll one authenticated node before issuing its introduction token.

    Control only writes its own manager's rule file. A host service applies it
    and acknowledges the exact content; API success cannot race ahead of rules.
    """
    root = security_root()
    fd = os.open(root / "peer-enrollment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _stage_peer_locked(address, timeout=timeout)
    finally:
        os.close(fd)


def _stage_peer_locked(address: str, *, timeout: float) -> None:
    root = security_root()
    policy = read_private_json(root / "network.json")
    p = _validated(policy)
    address = mesh_ip(address)
    p["rpcPeers"] = sorted(set(p["rpcPeers"]) | {address})
    rules = render_nftables(p) if p["os"] == "linux" else render_pf(p)
    digest = hashlib.sha256(rules.encode()).hexdigest()
    private_write(root / "network.json", json.dumps(p, indent=2) + "\n")
    private_write(root / "firewall.rules", rules)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if private_read_ack(root / "firewall.applied") == digest:
            return
        time.sleep(0.25)
    raise LumaError("Nomad peer enrollment is pending: host firewall did not acknowledge the new policy; no introduction token was issued")


def private_read_ack(path: Path) -> str:
    from .nomad_security import private_read
    return private_read(path, optional=True).strip()
