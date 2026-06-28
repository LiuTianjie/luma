from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import platform
import pty
import re
import select
import shutil
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict

from .errors import LumaError
from .local import LocalExecutor
from .service import slugify

DEFAULT_AGENT_CONFIG = Path("/opt/luma/node-agent/agent.json")
DEFAULT_AGENT_SERVICE = "luma-node-agent"
DEFAULT_CONTAINER_STATS_INTERVAL_SECONDS = 30


def node_agent_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "linux":
        return "linux"
    return system or "unknown"


def node_agent_arch() -> str:
    # Reported to Control so it can resolve a node's full os/arch platform.
    # The Mac/OrbStack deploy guards need os; docker image pulls need os/arch.
    return platform.machine().strip().lower()


def _docker_binary() -> str | None:
    docker = shutil.which("docker")
    if docker:
        return docker
    for candidate in (
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


_BUILDX_AVAILABLE: bool | None = None


def _docker_buildx_available() -> bool:
    global _BUILDX_AVAILABLE
    if _BUILDX_AVAILABLE is not None:
        return _BUILDX_AVAILABLE
    # Detect the buildx CLI plugin by filesystem presence rather than shelling
    # out, so capability advertisement stays cheap on every agent poll.
    found = bool(shutil.which("docker-buildx"))
    if not found:
        plugin_dirs = [
            os.path.expanduser("~/.docker/cli-plugins"),
            "/usr/local/lib/docker/cli-plugins",
            "/usr/lib/docker/cli-plugins",
            "/usr/libexec/docker/cli-plugins",
            "/opt/homebrew/lib/docker/cli-plugins",
            "/Applications/Docker.app/Contents/Resources/cli-plugins",
        ]
        found = any(os.path.exists(os.path.join(d, "docker-buildx")) for d in plugin_dirs)
    _BUILDX_AVAILABLE = found
    return _BUILDX_AVAILABLE


def node_agent_capabilities(os_name: str | None = None) -> list[str]:
    os_value = os_name or node_agent_os()
    if os_value == "linux":
        capabilities = [
            "nfs-host",
            "nfs-client",
            "managed-volume-path",
            "docker-volume",
            "docker-image",
            "docker-egress-proxy",
            "luma-update",
            "nomad-join",
            "terminal",
        ]
    elif os_value == "darwin":
        capabilities = ["nfs-host", "managed-volume-path", "docker-volume", "docker-image", "luma-update", "nomad-join", "terminal"]
    else:
        return []
    if _docker_buildx_available():
        capabilities.append("docker-build")
    return capabilities


def node_agent_metrics() -> Dict[str, Any]:
    cpu_count = os.cpu_count() or 1
    metrics: Dict[str, Any] = {"cpuCount": cpu_count}
    try:
        load1, _load5, _load15 = os.getloadavg()
        metrics["load1"] = round(float(load1), 2)
        metrics["loadPercent"] = round(min(max(load1 / cpu_count, 0), 1) * 100, 1)
    except (AttributeError, OSError):
        pass
    os_value = node_agent_os()
    if os_value == "linux":
        metrics.update(_linux_host_metrics())
    elif os_value == "darwin":
        metrics.update(_darwin_host_metrics(metrics.get("loadPercent")))
    return {key: value for key, value in metrics.items() if value not in ("", None)}


def node_agent_container_stats() -> list[Dict[str, Any]]:
    # Use _docker_binary() (not a bare shutil.which) so this works on macOS,
    # where the node agent runs under launchd's minimal PATH
    # (/usr/bin:/bin:/usr/sbin:/sbin) and the OrbStack/Docker Desktop/Homebrew
    # docker CLI is not on PATH. A bare which() returns None there and the
    # dashboard's per-container stats silently stay empty on every Mac node.
    docker = _docker_binary()
    if not docker:
        return []
    try:
        ps = subprocess.run(
            [
                docker,
                "ps",
                "--format",
                "\t".join(
                    [
                        "{{.ID}}",
                        "{{.Names}}",
                        "{{.Label \"com.hashicorp.nomad.alloc_id\"}}",
                        "{{.Label \"com.hashicorp.nomad.job_name\"}}",
                        "{{.Label \"com.hashicorp.nomad.task_name\"}}",
                        "{{.Label \"com.hashicorp.nomad.task_group_name\"}}",
                    ]
                ),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if ps.returncode != 0:
        return []
    containers: dict[str, Dict[str, Any]] = {}
    for line in ps.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        padded = [part.strip() for part in parts] + [""] * 6
        container_id, name, nomad_alloc_id, nomad_job, nomad_task, nomad_group = padded[:6]
        service = ""
        if nomad_job and nomad_job != "<no value>":
            service = nomad_job
        if not service and nomad_alloc_id and nomad_alloc_id != "<no value>":
            service = f"nomad:{nomad_alloc_id}"
        if not container_id or not service:
            continue
        item: Dict[str, Any] = {
            "containerId": container_id,
            "name": name,
            "service": service,
            "taskId": "" if nomad_task == "<no value>" else nomad_task,
        }
        if nomad_alloc_id and nomad_alloc_id != "<no value>":
            item["nomadAllocId"] = nomad_alloc_id
        if nomad_task and nomad_task != "<no value>":
            item["nomadTask"] = nomad_task
        if nomad_group and nomad_group != "<no value>":
            item["nomadGroup"] = nomad_group
        containers[container_id] = item
    if not containers:
        return []
    ids = list(containers)[:200]
    try:
        stats = subprocess.run(
            [docker, "stats", "--no-stream", "--format", "{{json .}}", *ids],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(containers.values())
    if stats.returncode != 0:
        return list(containers.values())
    for line in stats.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        container_id = str(raw.get("ID") or raw.get("Container") or "").strip()
        matched_id = _match_container_id(container_id, containers)
        if not matched_id:
            name = str(raw.get("Name") or "").strip()
            matched_id = next((key for key, value in containers.items() if value.get("name") == name), "")
        if not matched_id:
            continue
        item = containers[matched_id]
        item.update(_parse_docker_stats(raw))
    return list(containers.values())


class _ContainerStatsSampler:
    def __init__(
        self,
        interval_seconds: int,
        stats_func: Callable[[], list[Dict[str, Any]]] = node_agent_container_stats,
    ):
        self._interval = max(int(interval_seconds or DEFAULT_CONTAINER_STATS_INTERVAL_SECONDS), 1)
        self._stats_func = stats_func
        self._items: list[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="luma-node-agent-container-stats",
            daemon=True,
        )
        self._thread.start()

    def snapshot(self) -> list[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items]

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self._interval)

    def _sample_once(self) -> None:
        try:
            items = self._stats_func()
        except Exception:
            return
        with self._lock:
            self._items = [dict(item) for item in items if isinstance(item, dict)]


def _match_container_id(container_id: str, containers: dict[str, Dict[str, Any]]) -> str:
    if not container_id:
        return ""
    for candidate in containers:
        if candidate.startswith(container_id) or container_id.startswith(candidate):
            return candidate
    return ""


def _parse_docker_stats(raw: Dict[str, Any]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    cpu_percent = _parse_percent(str(raw.get("CPUPerc") or ""))
    memory_percent = _parse_percent(str(raw.get("MemPerc") or ""))
    if cpu_percent is not None:
        parsed["cpuPercent"] = cpu_percent
    if memory_percent is not None:
        parsed["memoryPercent"] = memory_percent
    memory_usage = str(raw.get("MemUsage") or "")
    used, _, limit = memory_usage.partition("/")
    used_bytes = _parse_size_bytes(used.strip())
    limit_bytes = _parse_size_bytes(limit.strip())
    if used_bytes:
        parsed["memoryUsageBytes"] = used_bytes
    if limit_bytes:
        parsed["memoryLimitBytes"] = limit_bytes
    return parsed


def _parse_percent(value: str) -> float | None:
    text = value.strip().rstrip("%")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _parse_size_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", value.strip())
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).lower()
    scale = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000 ** 2,
        "mib": 1024 ** 2,
        "gb": 1000 ** 3,
        "gib": 1024 ** 3,
        "tb": 1000 ** 4,
        "tib": 1024 ** 4,
    }.get(unit)
    if not scale:
        return 0
    return int(amount * scale)


def _linux_host_metrics() -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    first_cpu = _read_linux_cpu_times()
    if first_cpu:
        time.sleep(0.05)
        second_cpu = _read_linux_cpu_times()
        if second_cpu:
            total_delta = second_cpu[0] - first_cpu[0]
            idle_delta = second_cpu[1] - first_cpu[1]
            if total_delta > 0:
                metrics["cpuPercent"] = round(max(0, total_delta - idle_delta) / total_delta * 100, 1)
    try:
        values: Dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total > 0:
            metrics["memoryTotalBytes"] = total
            metrics["memoryAvailableBytes"] = available
            metrics["memoryUsedPercent"] = round(max(0, total - available) / total * 100, 1)
    except (OSError, ValueError):
        pass
    return metrics


def _read_linux_cpu_times() -> tuple[int, int] | None:
    try:
        first_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        values = [int(value) for value in first_line.split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _darwin_host_metrics(load_percent: object = None) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    if isinstance(load_percent, (int, float)):
        metrics["cpuPercent"] = round(float(load_percent), 1)
    try:
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        vm_stat = subprocess.check_output(["vm_stat"], text=True)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return metrics
    page_size = 4096
    match = re.search(r"page size of (\d+) bytes", vm_stat)
    if match:
        page_size = int(match.group(1))
    pages: Dict[str, int] = {}
    for line in vm_stat.splitlines():
        key, _, raw_value = line.partition(":")
        if not raw_value:
            continue
        try:
            pages[key.strip()] = int(raw_value.strip().rstrip("."))
        except ValueError:
            continue
    available_pages = pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)
    available = available_pages * page_size
    if total > 0:
        metrics["memoryTotalBytes"] = total
        metrics["memoryAvailableBytes"] = available
        metrics["memoryUsedPercent"] = round(max(0, total - available) / total * 100, 1)
    return metrics


def install_node_agent(
    *,
    endpoint: str,
    token: str,
    node_name: str,
    node_id: str = "",
    insecure: bool = False,
    resolve_ip: str | None = None,
    config_path: Path = DEFAULT_AGENT_CONFIG,
) -> None:
    config = {
        "endpoint": endpoint,
        "token": token,
        "nodeName": node_name,
        "nodeId": node_id,
        "insecure": insecure,
        "resolveIp": resolve_ip or "",
        "pollIntervalSeconds": 5,
        "statsIntervalSeconds": DEFAULT_CONTAINER_STATS_INTERVAL_SECONDS,
    }
    executor = LocalExecutor()
    escaped_config = shlex.quote(json.dumps(config, separators=(",", ":")))
    escaped_config_path = shlex.quote(str(config_path))
    command = (
        "set -euo pipefail; "
        f"install -d -m 700 {shlex.quote(str(config_path.parent))}; "
        f"printf '%s' {escaped_config} > {escaped_config_path}; "
        f"chmod 600 {escaped_config_path}; "
        f"{_agent_install_command(config_path)}"
    )
    executor.sudo(command)


def _agent_install_command(config_path: Path, *, executable: str | None = None) -> str:
    return _agent_service_command(config_path, executable=executable, restart=True)


def _agent_service_command(config_path: Path, *, executable: str | None = None, restart: bool) -> str:
    os_value = node_agent_os()
    if os_value == "darwin":
        label = "io.luma.node-agent"
        plist = "/Library/LaunchDaemons/io.luma.node-agent.plist"
        plist_body = _launchd_plist(config_path, executable=executable)
        command = (
            f"printf '%s' {shlex.quote(plist_body)} > {shlex.quote(plist)}; "
            f"chmod 644 {shlex.quote(plist)}"
        )
        if not restart:
            return command
        return (
            f"{command}; "
            f"launchctl bootout system/{label} >/dev/null 2>&1 || true; "
            f"launchctl bootstrap system {shlex.quote(plist)}; "
            f"launchctl kickstart -k system/{label}; "
            f"{_node_tailscale_watchdog_install_command(os_value)}"
        )
    unit = f"/etc/systemd/system/{DEFAULT_AGENT_SERVICE}.service"
    command = (
        f"printf '%s' {shlex.quote(_systemd_unit(config_path, executable=executable))} > {unit}; "
        "systemctl daemon-reload; "
        f"systemctl enable {DEFAULT_AGENT_SERVICE}.service >/dev/null"
    )
    if not restart:
        return command
    return (
        f"{command}; "
        f"systemctl start {DEFAULT_AGENT_SERVICE}.service; "
        f"systemctl restart {DEFAULT_AGENT_SERVICE}.service; "
        f"{_node_tailscale_watchdog_install_command(os_value)}"
    )


def _node_tailscale_watchdog_install_command(os_value: str | None = None) -> str:
    os_name = os_value or node_agent_os()
    script_path = "/opt/luma/node-agent/tailscale-watchdog.sh"
    script = _node_tailscale_watchdog_script(os_name)
    if os_name == "darwin":
        plist = "/Library/LaunchDaemons/io.luma.tailscale-watchdog.plist"
        plist_body = _node_tailscale_watchdog_launchd_plist(script_path)
        return (
            "if command -v tailscale >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then "
            f"printf '%s' {shlex.quote(script)} > {shlex.quote(script_path)}; "
            f"chmod 755 {shlex.quote(script_path)}; "
            f"printf '%s' {shlex.quote(plist_body)} > {shlex.quote(plist)}; "
            f"chmod 644 {shlex.quote(plist)}; "
            "launchctl bootout system/io.luma.tailscale-watchdog >/dev/null 2>&1 || true; "
            f"launchctl bootstrap system {shlex.quote(plist)}; "
            "launchctl kickstart -k system/io.luma.tailscale-watchdog; "
            "fi"
        )
    return (
        "if command -v systemctl >/dev/null 2>&1 && command -v tailscale >/dev/null 2>&1; then "
        f"printf '%s' {shlex.quote(script)} > {shlex.quote(script_path)}; "
        f"chmod 755 {shlex.quote(script_path)}; "
        "cat > /etc/systemd/system/luma-node-tailscale-watchdog.service <<'EOF'\n"
        "[Unit]\n"
        "Description=Luma node Tailscale watchdog\n"
        "After=network-online.target tailscaled.service nomad.service\n"
        "Wants=network-online.target tailscaled.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "EnvironmentFile=-/etc/default/luma-node-tailscale-watchdog\n"
        f"ExecStart={script_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        "EOF\n"
        "cat > /etc/systemd/system/luma-node-tailscale-watchdog.timer <<'EOF'\n"
        "[Unit]\n"
        "Description=Run Luma node Tailscale watchdog\n"
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
        "systemctl enable --now luma-node-tailscale-watchdog.timer >/dev/null; "
        "systemctl reset-failed luma-node-tailscale-watchdog.service >/dev/null 2>&1 || true; "
        "systemctl start luma-node-tailscale-watchdog.service >/dev/null || true; "
        "fi"
    )


def _node_tailscale_watchdog_script(os_name: str) -> str:
    if os_name == "darwin":
        restart = (
            "launchctl kickstart -k system/W5364U7YZB.io.tailscale.ipn.macsys.network-extension >/dev/null 2>&1 || "
            "launchctl kickstart -k system/io.tailscale.ipn.macsys.network-extension >/dev/null 2>&1 || "
            "killall Tailscale >/dev/null 2>&1 || true"
        )
    else:
        restart = "systemctl restart tailscaled"
    return f"""#!/bin/sh
set -eu
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PATH
threshold=${{LUMA_NODE_TAILSCALE_WATCHDOG_THRESHOLD:-3}}
ports="${{LUMA_NODE_TAILSCALE_WATCHDOG_PORTS:-4647}}"
configured_managers="${{LUMA_NODE_TAILSCALE_WATCHDOG_MANAGERS:-}}"
state_dir=/var/run/luma
state_file=$state_dir/node-tailscale-watchdog.failures
mkdir -p "$state_dir"
log() {{ printf '%s %s\\n' "$(date -Is)" "$*"; }}
tcp_probe() {{
  host="$1"
  port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 3 "$host" "$port" >/dev/null 2>&1
  elif command -v timeout >/dev/null 2>&1 && command -v bash >/dev/null 2>&1; then
    timeout 3 bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1
  else
    log 'skip: no TCP probe command available'
    return 0
  fi
}}
is_tailnet_addr() {{
  case "$1" in
    100.*) return 0 ;;
    fd7a:115c:a1e0:*) return 0 ;;
    *) return 1 ;;
  esac
}}
manager_hosts() {{
  if [ -n "$configured_managers" ]; then
    printf '%s\\n' "$configured_managers" | tr ',' '\\n'
  else
    for cfg in /etc/nomad.d/nomad.hcl /opt/nomad.d/nomad.hcl /usr/local/etc/nomad.d/nomad.hcl /opt/homebrew/etc/nomad.d/nomad.hcl; do
      [ -f "$cfg" ] || continue
      sed -n '/retry_join/s/.*=//p' "$cfg" | tr '[]",' '    ' | tr ' ' '\\n'
    done
  fi |
  while IFS= read -r addr; do
    [ -n "$addr" ] || continue
    host=${{addr#*://}}
    host=${{host%%/*}}
    host=${{host%:*}}
    is_tailnet_addr "$host" && printf '%s\\n' "$host"
  done |
  sort -u
}}
managers=$(manager_hosts)
[ -n "$managers" ] || {{ log 'skip: no Tailscale Nomad servers'; exit 0; }}
checked=0
bad=0
for host in $managers; do
  if ! tailscale ping --timeout=3s --c 2 "$host" >/dev/null 2>&1; then
    checked=$((checked + 1))
    bad=$((bad + 1))
    log "manager Tailscale ping failed: $host"
    continue
  fi
  for port in $ports; do
    checked=$((checked + 1))
    if ! tcp_probe "$host" "$port"; then
      bad=$((bad + 1))
      log "manager TCP probe failed: $host:$port"
    fi
  done
done
if [ "$bad" -eq 0 ]; then
  echo 0 > "$state_file"
  exit 0
fi
count=0
[ -f "$state_file" ] && count=$(cat "$state_file" 2>/dev/null || echo 0)
case "$count" in ''|*[!0-9]*) count=0 ;; esac
count=$((count + 1))
echo "$count" > "$state_file"
log "manager tailnet unhealthy: $bad/$checked checks failed, consecutive=$count/$threshold"
if [ "$count" -ge "$threshold" ]; then
  log 'restarting local Tailscale after consecutive manager connectivity failures'
  {restart}
  echo 0 > "$state_file"
fi
"""


def _node_tailscale_watchdog_launchd_plist(script_path: str) -> str:
    escaped_script_path = _xml_escape(script_path)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.luma.tailscale-watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escaped_script_path}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>/var/log/luma-tailscale-watchdog.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/luma-tailscale-watchdog.err</string>
</dict>
</plist>
"""


def _agent_executable_args(config_path: Path, *, executable: str | None = None) -> list[str]:
    executable = executable or os.environ.get("LUMA_AGENT_EXECUTABLE") or shutil.which("luma") or _current_executable() or sys.executable
    if Path(executable).name.startswith("python"):
        return [executable, "-m", "luma.cli", "node-agent", "run", "--config", str(config_path)]
    return [executable, "node-agent", "run", "--config", str(config_path)]


def _terminal_supervisor_args(config_path: Path, *, executable: str | None = None) -> list[str]:
    executable = executable or os.environ.get("LUMA_AGENT_EXECUTABLE") or shutil.which("luma") or _current_executable() or sys.executable
    if Path(executable).name.startswith("python"):
        return [executable, "-m", "luma.cli", "node-agent", "terminal-supervisor", "--config", str(config_path)]
    return [executable, "node-agent", "terminal-supervisor", "--config", str(config_path)]


class _TerminalSupervisorProcess:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.process: subprocess.Popen[Any] | None = None
        self.last_start = 0.0

    def ensure_running(self) -> None:
        if self.process and self.process.poll() is None:
            return
        now = time.time()
        if now - self.last_start < 5:
            return
        self.last_start = now
        try:
            self.process = subprocess.Popen(
                _terminal_supervisor_args(self.config_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            self.process = None

    def stop(self) -> None:
        process = self.process
        if not process or process.poll() is not None:
            return
        terminated_group = False
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                terminated_group = True
            else:
                process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                if terminated_group and hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass


def _current_executable() -> str:
    candidate = str(sys.argv[0] or "").strip()
    if not candidate or candidate == "-":
        return ""
    if "/" in candidate or Path(candidate).is_absolute():
        return candidate
    return shutil.which(candidate) or ""


def _install_layout_from_executable(executable: str) -> tuple[Path, Path, Path] | None:
    value = str(executable or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.name != "luma":
        return None
    parents = path.parents
    if (
        len(parents) >= 6
        and parents[0].name == "bin"
        and parents[1].name == "venv"
        and parents[2].name == "luma"
        and parents[3].name == "share"
        and parents[4].name == ".local"
    ):
        user_home = parents[5]
        return user_home, parents[2], user_home / ".local" / "bin"
    if len(parents) >= 3 and parents[0].name == "bin" and parents[1].name == ".local":
        user_home = parents[2]
        return user_home, user_home / ".local" / "share" / "luma", parents[0]
    return None


def _current_install_layout() -> tuple[Path, Path, Path] | None:
    for executable in (_current_executable(), shutil.which("luma") or ""):
        layout = _install_layout_from_executable(executable)
        if layout:
            return layout
    return None


def _installed_luma_executable() -> str:
    explicit = str(os.environ.get("LUMA_AGENT_EXECUTABLE") or "").strip()
    if explicit:
        return explicit
    layout = _current_install_layout()
    if layout:
        _user_home, _install_home, bin_dir = layout
        return str(bin_dir / "luma")
    candidates: list[Path] = []
    try:
        candidates.append(Path.home() / ".local" / "bin" / "luma")
    except Exception:
        pass
    home = str(os.environ.get("HOME") or "").strip()
    if home:
        candidates.append(Path(home) / ".local" / "bin" / "luma")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("luma") or _current_executable() or sys.executable


def _systemd_unit(config_path: Path, *, executable: str | None = None) -> str:
    args = " ".join(shlex.quote(part) for part in _agent_executable_args(config_path, executable=executable))
    return "\n".join(
        [
            "[Unit]",
            "Description=Luma node agent",
            "After=network-online.target docker.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={args}",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _launchd_plist(config_path: Path, *, executable: str | None = None) -> str:
    args = _agent_executable_args(config_path, executable=executable)
    program_arguments = "\n".join(f"    <string>{_xml_escape(arg)}</string>" for arg in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.luma.node-agent</string>
  <key>ProgramArguments</key>
  <array>
{program_arguments}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/luma-node-agent.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/luma-node-agent.err</string>
</dict>
</plist>
"""


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_node_agent(config_path: Path = DEFAULT_AGENT_CONFIG, *, once: bool = False, poll_interval: int | None = None) -> int:
    from .control.client import ControlClient

    config = json.loads(config_path.read_text(encoding="utf-8"))
    endpoint = str(config.get("endpoint") or "")
    token = str(config.get("token") or "")
    node_name = str(config.get("nodeName") or "")
    node_id = str(config.get("nodeId") or "")
    if not endpoint or not token or not node_name:
        raise LumaError(f"invalid node agent config: {config_path}")
    client = ControlClient(
        endpoint,
        token,
        insecure=bool(config.get("insecure")),
        resolve_ip=str(config.get("resolveIp") or "") or None,
    )
    interval = int(poll_interval or config.get("pollIntervalSeconds") or 5)
    stats_interval = int(
        os.environ.get("LUMA_NODE_AGENT_STATS_INTERVAL_SECONDS")
        or config.get("statsIntervalSeconds")
        or DEFAULT_CONTAINER_STATS_INTERVAL_SECONDS
    )
    stats_sampler = None if once else _ContainerStatsSampler(stats_interval)
    terminal_supervisor = None if once else _TerminalSupervisorProcess(config_path)
    if stats_sampler:
        stats_sampler.start()
    if terminal_supervisor:
        terminal_supervisor.ensure_running()
    try:
        while True:
            if terminal_supervisor:
                terminal_supervisor.ensure_running()
            if once:
                container_stats = node_agent_container_stats()
            else:
                container_stats = stats_sampler.snapshot() if stats_sampler else []
            try:
                task = client.lease_agent_task(
                    node_name=node_name,
                    node_id=node_id,
                    os_name=node_agent_os(),
                    arch=node_agent_arch(),
                    capabilities=node_agent_capabilities(),
                    metrics=node_agent_metrics(),
                    container_stats=container_stats,
                    timeout=max(interval + 5, 15),
                ).get("task")
            except Exception as exc:
                if once:
                    raise
                print(f"luma: node agent lease failed: {exc}", file=sys.stderr, flush=True)
                time.sleep(max(interval, 1))
                continue
            if isinstance(task, dict) and task.get("id"):
                if _complete_agent_task(client, node_name=node_name, node_id=node_id, task=task, config_path=config_path):
                    return 0
            if once:
                return 0
            time.sleep(max(interval, 1))
    finally:
        if terminal_supervisor:
            terminal_supervisor.stop()
        if stats_sampler:
            stats_sampler.stop()


def _complete_agent_task(client: Any, *, node_name: str, node_id: str, task: Dict[str, Any], config_path: Path = DEFAULT_AGENT_CONFIG) -> bool:
    task_id = str(task.get("id") or "")
    try:
        result = execute_agent_task(task, config_path=config_path)
        client.complete_agent_task(
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status="succeeded",
            message=str(result.get("message") or "ok"),
            result=result,
        )
        return bool(result.get("restartAgent"))
    except Exception as exc:
        client.complete_agent_task(
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status="failed",
            message=str(exc),
            result={},
        )
        return False


def run_terminal_supervisor(config_path: Path = DEFAULT_AGENT_CONFIG) -> int:
    previous_handlers: dict[int, Any] = {}
    lock_file = None
    if threading.current_thread() is threading.main_thread():
        for item in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if item is None:
                continue
            previous_handlers[int(item)] = signal.getsignal(item)
            signal.signal(item, _terminal_supervisor_shutdown_signal)
    try:
        lock_file = _acquire_terminal_supervisor_lock(config_path)
        if lock_file is None:
            return 0
        asyncio.run(_run_terminal_supervisor(config_path))
    except KeyboardInterrupt:
        return 0
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except Exception:
                pass
    return 0


def _terminal_supervisor_lock_path(config_path: Path) -> Path:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    node_name = slugify(str(config.get("nodeName") or config_path))
    return Path(tempfile.gettempdir()) / f"luma-terminal-supervisor-{node_name}.lock"


def _acquire_terminal_supervisor_lock(config_path: Path) -> Any | None:
    lock_path = _terminal_supervisor_lock_path(config_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _terminal_supervisor_shutdown_signal(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


async def _run_terminal_supervisor(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    endpoint = str(config.get("endpoint") or "")
    token = str(config.get("token") or "")
    node_name = str(config.get("nodeName") or "")
    node_id = str(config.get("nodeId") or "")
    if not endpoint or not token or not node_name:
        raise LumaError(f"invalid node agent config: {config_path}")
    sessions: dict[str, _PtySession] = {}
    outbound: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    try:
        while True:
            try:
                async with await _terminal_ws_connect(
                    endpoint,
                    token=token,
                    node_name=node_name,
                    node_id=node_id,
                    insecure=bool(config.get("insecure")),
                    resolve_ip=str(config.get("resolveIp") or "") or None,
                ) as websocket:
                    await websocket.send(json.dumps({"type": "auth", "token": token}, separators=(",", ":")))
                    send_task = asyncio.create_task(_terminal_sender(websocket, outbound))
                    try:
                        async for raw in websocket:
                            try:
                                message = json.loads(raw)
                            except (TypeError, json.JSONDecodeError):
                                continue
                            if isinstance(message, dict):
                                _handle_terminal_control_message(message, sessions=sessions, outbound=outbound, loop=loop)
                    finally:
                        send_task.cancel()
                        for session in list(sessions.values()):
                            session.close()
                        sessions.clear()
            except Exception:
                for session in list(sessions.values()):
                    session.close()
                sessions.clear()
                await asyncio.sleep(3)
    finally:
        for session in list(sessions.values()):
            session.close()
        sessions.clear()


async def _terminal_ws_connect(
    endpoint: str,
    *,
    token: str,
    node_name: str,
    node_id: str,
    insecure: bool,
    resolve_ip: str | None,
) -> Any:
    import websockets

    parsed = urllib.parse.urlparse(endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urllib.parse.urlencode({"node": node_name, "nodeId": node_id})
    netloc = parsed.netloc
    headers: Dict[str, str] = {}
    if resolve_ip:
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{resolve_ip}{port}"
        headers["Host"] = parsed.netloc
    uri = urllib.parse.urlunparse((scheme, netloc, "/v1/terminal/agent", "", query, ""))
    ssl_context: ssl.SSLContext | bool | None = None
    if scheme == "wss":
        ssl_context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    try:
        return websockets.connect(uri, ssl=ssl_context, additional_headers=headers or None, ping_interval=20, ping_timeout=20, max_size=None)
    except TypeError:
        return websockets.connect(uri, ssl=ssl_context, extra_headers=headers or None, ping_interval=20, ping_timeout=20, max_size=None)


async def _terminal_sender(websocket: Any, outbound: "asyncio.Queue[Dict[str, Any]]") -> None:
    while True:
        message = await outbound.get()
        await websocket.send(json.dumps(message, separators=(",", ":")))


def _handle_terminal_control_message(
    message: Dict[str, Any],
    *,
    sessions: dict[str, "_PtySession"],
    outbound: "asyncio.Queue[Dict[str, Any]]",
    loop: asyncio.AbstractEventLoop,
) -> None:
    kind = str(message.get("type") or "")
    session_id = str(message.get("sessionId") or "")
    if not session_id:
        return
    if kind == "open":
        if session_id in sessions:
            return
        try:
            rows = int(message.get("rows") or 32)
            cols = int(message.get("cols") or 120)
            sessions[session_id] = _PtySession(session_id, outbound=outbound, loop=loop, rows=rows, cols=cols)
        except Exception as exc:
            outbound.put_nowait({"type": "error", "sessionId": session_id, "message": str(exc)})
        return
    session = sessions.get(session_id)
    if not session:
        return
    if kind == "input":
        session.write(str(message.get("data") or ""))
    elif kind == "resize":
        session.resize(rows=int(message.get("rows") or 32), cols=int(message.get("cols") or 120))
    elif kind == "close":
        session.close()
        sessions.pop(session_id, None)


class _PtySession:
    def __init__(self, session_id: str, *, outbound: "asyncio.Queue[Dict[str, Any]]", loop: asyncio.AbstractEventLoop, rows: int, cols: int):
        self.session_id = session_id
        self.outbound = outbound
        self.loop = loop
        self.closed = threading.Event()
        self.master_fd, slave_fd = pty.openpty()
        _set_pty_size(self.master_fd, rows=rows, cols=cols)
        shell = _terminal_shell()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        cwd = str(Path.home()) if Path.home().exists() else "/"
        kwargs: Dict[str, Any] = {
            "stdin": slave_fd,
            "stdout": slave_fd,
            "stderr": slave_fd,
            "env": env,
            "cwd": cwd,
            "close_fds": True,
        }
        if hasattr(os, "setsid"):
            kwargs["preexec_fn"] = os.setsid
        self.process = subprocess.Popen([shell], **kwargs)
        os.close(slave_fd)
        self.reader = threading.Thread(target=self._read_loop, name=f"luma-terminal-{session_id}", daemon=True)
        self.reader.start()

    def write(self, data: str) -> None:
        if self.closed.is_set():
            return
        try:
            os.write(self.master_fd, data.encode("utf-8", errors="replace"))
        except OSError:
            self.close()

    def resize(self, *, rows: int, cols: int) -> None:
        if self.closed.is_set():
            return
        try:
            _set_pty_size(self.master_fd, rows=rows, cols=cols)
        except OSError:
            pass

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            if self.process.poll() is None:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    else:
                        self.process.kill()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    def _read_loop(self) -> None:
        try:
            while not self.closed.is_set():
                ready, _, _ = select.select([self.master_fd], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(self.master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    self._emit({"type": "output", "sessionId": self.session_id, "data": data.decode("utf-8", errors="replace")})
                if self.process.poll() is not None:
                    break
            exit_code = self.process.poll()
            if exit_code is None:
                try:
                    exit_code = self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    exit_code = None
            self._emit({"type": "exit", "sessionId": self.session_id, "exitCode": exit_code if exit_code is not None else -1})
        except Exception as exc:
            self._emit({"type": "error", "sessionId": self.session_id, "message": str(exc)})
        finally:
            self.closed.set()
            try:
                os.close(self.master_fd)
            except OSError:
                pass

    def _emit(self, event: Dict[str, Any]) -> None:
        try:
            self.loop.call_soon_threadsafe(self.outbound.put_nowait, event)
        except RuntimeError:
            pass


def _terminal_shell() -> str:
    if node_agent_os() == "darwin":
        candidates = [os.environ.get("SHELL") or "", "/bin/zsh", "/bin/bash", "/bin/sh"]
    else:
        candidates = [os.environ.get("SHELL") or "", "/bin/bash", "/bin/zsh", "/bin/sh"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return "/bin/sh"


def _set_pty_size(fd: int, *, rows: int, cols: int) -> None:
    safe_rows = max(min(int(rows or 32), 200), 8)
    safe_cols = max(min(int(cols or 120), 400), 20)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", safe_rows, safe_cols, 0, 0))


def execute_agent_task(task: Dict[str, Any], *, config_path: Path = DEFAULT_AGENT_CONFIG) -> Dict[str, Any]:
    action = str(task.get("action") or "")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if action == "prepare-managed-nfs-host":
        name = _required(payload, "name")
        path = _safe_absolute_path(_required(payload, "path"))
        return prepare_managed_nfs_host(name=name, path=path)
    if action == "prepare-managed-volume-path":
        root = _safe_absolute_path(_required(payload, "root"))
        relative = _safe_relative_path(_required(payload, "relative"))
        full_path = str(Path(root) / relative)
        _run_fixed_host_task(_volume_path_command(full_path))
        return {"path": full_path, "message": "volume path ready"}
    if action == "remove-managed-volume-path":
        root = _safe_absolute_path(_required(payload, "root"))
        relative = _safe_relative_path(_required(payload, "relative"))
        full_path = str(Path(root) / relative)
        _run_fixed_host_task(_remove_volume_path_command(root, relative))
        return {"path": full_path, "message": "volume path removed"}
    if action == "remove-docker-volume":
        name = _safe_docker_volume_name(_required(payload, "name"))
        _run_fixed_host_task(_docker_volume_remove_command(name), prefer_container=False)
        return {"name": name, "message": "Docker volume removed"}
    if action == "remove-managed-nfs-export":
        name = _required(payload, "name")
        return remove_managed_nfs_export(name=name)
    if action == "configure-docker-egress-proxy":
        proxy = str(payload.get("proxy") or "http://127.0.0.1:7890")
        no_proxy = str(
            payload.get("noProxy")
            or "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run"
        )
        return configure_docker_egress_proxy(proxy=proxy, no_proxy=no_proxy)
    if action == "resolve-docker-image":
        image = _safe_docker_image_ref(_required(payload, "image"))
        registry_auth = payload.get("registryAuth") if isinstance(payload.get("registryAuth"), dict) else None
        return resolve_docker_image(
            image=image,
            registry_auth=registry_auth,
            force_pull=bool(payload.get("forcePull")),
            platform=str(payload.get("platform") or ""),
        )
    if action == "update-luma":
        return update_luma_install(install_ref=str(payload.get("installRef") or ""), config_path=config_path)
    if action == "build-image":
        return build_image(payload)
    if action == "configure-insecure-registry":
        return configure_insecure_registry(registry=_required(payload, "registry"))
    if action == "join-nomad":
        return join_nomad_node(
            node_name=_required(payload, "nodeName"),
            region=_required(payload, "region"),
            server_addr=_required(payload, "serverAddr"),
            tailscale_authkey=str(payload.get("tailscaleAuthKey") or ""),
            egress_proxy=str(payload.get("egressProxy") or ""),
        )
    raise LumaError(f"unsupported node agent task action: {action}")


def join_nomad_node(
    *,
    node_name: str,
    region: str,
    server_addr: str,
    tailscale_authkey: str = "",
    egress_proxy: str = "",
) -> Dict[str, Any]:
    from .bootstrap import _tailscale_ip, install_nomad_node, local_nomad_node_info
    from .config import NodeConfig

    safe_node_name = str(node_name or "").strip()
    safe_region = str(region or "").strip()
    safe_server_addr = str(server_addr or "").strip()
    if not safe_node_name:
        raise LumaError("nodeName is required")
    if safe_region not in {"cn", "global", "home"}:
        raise LumaError("region must be one of cn, global, home")
    if not safe_server_addr:
        raise LumaError("serverAddr is required")
    node = NodeConfig(
        name=safe_node_name,
        host=safe_node_name,
        region=safe_region,
        roles=[safe_region],
        raw={"tailscaleHostname": f"luma-{safe_node_name}"},
    )
    install_messages = install_nomad_node(
        node,
        role="client",
        region=safe_region,
        node_name=safe_node_name,
        server_addrs=[safe_server_addr],
        install_docker_first=True,
        egress_proxy=egress_proxy or None,
        tailscale_authkey=tailscale_authkey or None,
    )
    actual_node_name, nomad_node_id = local_nomad_node_info()
    tailscale_ip = _tailscale_ip(LocalExecutor()) or ""
    return {
        "message": f"Nomad node joined: {safe_node_name}",
        "registeredName": safe_node_name,
        "nodeName": actual_node_name or safe_node_name,
        "nodeId": nomad_node_id,
        "nomadNodeId": nomad_node_id,
        "region": safe_region,
        "tailscaleIP": tailscale_ip,
        "install": install_messages,
    }


def prepare_managed_nfs_host(*, name: str, path: str) -> Dict[str, Any]:
    os_value = node_agent_os()
    safe_path = _safe_absolute_path(path)
    if os_value == "darwin":
        _run_fixed_host_task(_macos_prepare_nfs_command(name, safe_path), prefer_container=False)
    elif os_value == "linux":
        _run_fixed_host_task(_linux_prepare_nfs_command(name, safe_path))
    else:
        raise LumaError(f"managed NFS storage is not supported on {os_value}")
    return {"name": name, "path": safe_path, "message": "host NFS export ready"}


def remove_managed_nfs_export(*, name: str) -> Dict[str, Any]:
    os_value = node_agent_os()
    if os_value == "darwin":
        _run_fixed_host_task(_macos_remove_nfs_command(name), prefer_container=False)
    elif os_value == "linux":
        _run_fixed_host_task(_linux_remove_nfs_command(name))
    else:
        raise LumaError(f"managed NFS storage is not supported on {os_value}")
    return {"name": name, "message": "managed NFS export removed"}


def configure_docker_egress_proxy(*, proxy: str, no_proxy: str) -> Dict[str, Any]:
    os_value = node_agent_os()
    if os_value != "linux":
        raise LumaError(f"Docker daemon egress proxy setup is not supported on {os_value}")
    if not proxy.startswith(("http://", "https://")):
        raise LumaError("Docker daemon proxy must start with http:// or https://")
    command = (
        "set -euo pipefail; "
        "mkdir -p /etc/systemd/system/docker.service.d; "
        "cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'\n"
        "[Service]\n"
        f"Environment=\"HTTP_PROXY={proxy}\"\n"
        f"Environment=\"HTTPS_PROXY={proxy}\"\n"
        f"Environment=\"NO_PROXY={no_proxy}\"\n"
        "EOF\n"
        "systemctl daemon-reload; "
        "systemctl restart docker"
    )
    LocalExecutor().sudo(command)
    return {"proxy": proxy, "noProxy": no_proxy, "message": "Docker daemon egress proxy configured"}


def resolve_docker_image(
    *,
    image: str,
    registry_auth: Dict[str, Any] | None = None,
    force_pull: bool = False,
    platform: str = "",
) -> Dict[str, Any]:
    image = _safe_docker_image_ref(image)
    platform = str(platform or "").strip()
    executor = LocalExecutor()
    with tempfile.TemporaryDirectory(prefix="luma-docker-config-") as docker_config:
        _write_docker_auth_config(Path(docker_config), registry_auth)
        if not force_pull and not platform:
            inspect = executor.run_result(_docker_image_inspect_command(image, docker_config=docker_config), timeout=60)
            if inspect.code == 0:
                digest = _first_repo_digest(image, inspect.output)
                return {
                    "image": image,
                    "deployed": digest or image,
                    "digest": digest,
                    "pulled": False,
                    "platform": platform,
                    "message": "Target node image already present",
                }
        env_prefix = f"DOCKER_CONFIG={shlex.quote(docker_config)}"
        pull_command = f"set -euo pipefail; {_docker_cli_prelude()}; {env_prefix} \"$docker_cli\" pull"
        if platform:
            pull_command += f" --platform {shlex.quote(platform)}"
        pull_command += f" {shlex.quote(image)}"
        pull = executor.run_result(pull_command, timeout=600)
        if pull.code != 0:
            raise LumaError(_docker_image_pull_error(image=image, output=pull.output, platform=platform))
        inspect = executor.run_result(_docker_image_inspect_command(image, docker_config=docker_config), timeout=60)
        digest = _first_repo_digest(image, inspect.output) if inspect.code == 0 else ""
        return {
            "image": image,
            "deployed": digest or image,
            "digest": digest,
            "pulled": True,
            "platform": platform,
            "message": "Target node image pull ready",
        }


def _docker_image_inspect_command(image: str, *, docker_config: str) -> str:
    return (
        f"set -euo pipefail; {_docker_cli_prelude()}; "
        f"DOCKER_CONFIG={shlex.quote(docker_config)} \"$docker_cli\" image inspect "
        f"{shlex.quote(image)} --format '{{{{json .RepoDigests}}}}'"
    )


def _write_docker_auth_config(config_dir: Path, registry_auth: Dict[str, Any] | None) -> None:
    if not registry_auth:
        return
    username = str(registry_auth.get("username") or "")
    password = str(registry_auth.get("password") or "")
    server = str(registry_auth.get("serveraddress") or registry_auth.get("serverAddress") or "")
    if not username or not password or not server:
        return
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"auths": {server: {"auth": auth}}}), encoding="utf-8")
    try:
        os.chmod(config_file, 0o600)
    except OSError:
        pass


def _first_repo_digest(image: str, inspect_output: str) -> str:
    try:
        digests = json.loads((inspect_output or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return ""
    if not isinstance(digests, list):
        return ""
    repository = _docker_image_repository(image)
    for value in digests:
        digest = str(value or "")
        if repository and digest.startswith(f"{repository}@"):
            return digest
    for value in digests:
        digest = str(value or "")
        if "@sha256:" in digest:
            return digest
    return ""


def _safe_image_repo(value: str) -> str:
    repo = str(value or "").strip().lower().strip("/")
    if not repo or not re.fullmatch(r"[a-z0-9]([a-z0-9._/-]*[a-z0-9])?", repo):
        raise LumaError(f"invalid image repository: {value}")
    return repo


def _safe_registry_host(value: str) -> str:
    host = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+(:\d+)?", host):
        raise LumaError(f"invalid registry host: {value}")
    return host


def _safe_repo_subpath(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise LumaError(f"path escapes repository: {rel}")
    return candidate


def _auth_for_host(registry_auth: Dict[str, Any] | None, host: str) -> Dict[str, Any] | None:
    if not registry_auth:
        return None
    return {
        "username": registry_auth.get("username"),
        "password": registry_auth.get("password"),
        "serveraddress": host,
    }


def _ensure_buildx_builder(docker: str, *, proxy: str = "", no_proxy: str = "") -> str:
    # docker-container driver runs BuildKit in its own container/daemon. It does
    # NOT inherit the host dockerd proxy nor the CLI env, so FROM base-image pulls
    # need the proxy baked into the BuildKit container env at creation. Use a
    # distinct builder name per proxy state so a build never reuses a builder that
    # lacks the proxy it needs.
    name = "luma-builder-egress" if proxy else "luma-builder"
    inspect = subprocess.run(
        [docker, "buildx", "inspect", name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if inspect.returncode != 0:
        create_cmd = [docker, "buildx", "create", "--name", name, "--driver", "docker-container", "--driver-opt", "network=host"]
        if proxy:
            create_cmd += [
                "--driver-opt", f"env.HTTP_PROXY={proxy}",
                "--driver-opt", f"env.HTTPS_PROXY={proxy}",
                "--driver-opt", f"env.NO_PROXY={no_proxy}",
            ]
        create_cmd.append("--bootstrap")
        create = subprocess.run(
            create_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        if create.returncode != 0:
            raise LumaError(f"failed to create buildx builder:\n{create.stdout.strip()}")
    return name


def build_image(payload: Dict[str, Any]) -> Dict[str, Any]:
    from . import gitops

    repo_url = _required(payload, "repoUrl")
    ref = str(payload.get("ref") or "").strip() or None
    proxy = str(payload.get("proxy") or "").strip() or None
    git_token = str(payload.get("gitToken") or "").strip() or None
    registry_host = _safe_registry_host(_required(payload, "registryHost"))
    push_host = _safe_registry_host(str(payload.get("pushHost") or "localhost:5000"))
    repo = _safe_image_repo(_required(payload, "repo"))
    registry_auth = payload.get("registryAuth") if isinstance(payload.get("registryAuth"), dict) else None
    build_timeout = int(payload.get("buildTimeout") or 1800)

    docker = _docker_binary()
    if not docker:
        raise LumaError("docker command not found on build node")
    if not _docker_buildx_available():
        raise LumaError("docker buildx is not available on build node")

    with tempfile.TemporaryDirectory(prefix="luma-build-") as workdir:
        src = Path(workdir) / "src"
        gitops.clone(repo_url, src, ref=ref, proxy=proxy, token=git_token)
        sha = gitops.head_commit(src)

        manifest_text = ""
        for candidate in (".luma.yml", ".luma.yaml"):
            manifest_path = src / candidate
            if manifest_path.is_file():
                manifest_text = manifest_path.read_text(encoding="utf-8")
                break

        # Build params: repo's .luma.yml build block is the declarative source of
        # truth; payload values (from CLI flags) act as overrides when provided.
        build_block: Dict[str, Any] = {}
        if manifest_text.strip():
            import yaml

            try:
                parsed = yaml.safe_load(manifest_text) or {}
            except yaml.YAMLError as exc:
                raise LumaError(f"invalid .luma.yml in repository: {exc}") from exc
            if isinstance(parsed, dict) and isinstance(parsed.get("build"), dict):
                build_block = parsed["build"]
        context_rel = str(payload.get("context") or build_block.get("context") or ".").strip() or "."
        dockerfile_rel = str(payload.get("dockerfile") or build_block.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
        platform = str(payload.get("platform") or build_block.get("platform") or "linux/amd64").strip() or "linux/amd64"

        context_dir = _safe_repo_subpath(src, context_rel)
        dockerfile_path = _safe_repo_subpath(src, dockerfile_rel)
        if not dockerfile_path.is_file():
            raise LumaError(f"Dockerfile not found in repository: {dockerfile_rel}")

        docker_config = Path(workdir) / "docker-config"
        _write_docker_auth_config(docker_config, _auth_for_host(registry_auth, push_host))

        tag_sha = f"{push_host}/{repo}:{sha}"
        tag_latest = f"{push_host}/{repo}:latest"
        env = dict(os.environ)
        env["DOCKER_CONFIG"] = str(docker_config)
        # cn/home build nodes reach the internet through the egress proxy. The
        # proxy must reach three places: the buildx CLI + FROM pulls (env), the
        # RUN steps inside the build (--build-arg; BuildKit does not inherit the
        # CLI/daemon proxy), and the BuildKit container itself (set at builder
        # creation in _ensure_buildx_builder). Keep the in-cluster registry and
        # localhost out of the proxy so --push stays on the internal network.
        no_proxy = f"localhost,127.0.0.1,::1,{push_host},{registry_host}"
        proxy_build_args: list[str] = []
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["NO_PROXY"] = no_proxy
            proxy_build_args = [
                "--build-arg", f"HTTP_PROXY={proxy}",
                "--build-arg", f"HTTPS_PROXY={proxy}",
                "--build-arg", f"NO_PROXY={no_proxy}",
            ]
        builder = _ensure_buildx_builder(docker, proxy=proxy or "", no_proxy=no_proxy)
        command = [
            docker, "buildx", "build",
            "--builder", builder,
            "--platform", platform,
            *proxy_build_args,
            "--push",
            "-t", tag_sha,
            "-t", tag_latest,
            "-f", str(dockerfile_path),
            str(context_dir),
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
                timeout=build_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LumaError(f"docker buildx build timed out after {build_timeout}s") from exc
        if result.returncode != 0:
            raise LumaError(f"docker buildx build failed:\n{(result.stdout or '').strip()}")

    image = f"{registry_host}/{repo}:{sha}"
    return {
        "image": image,
        "sha": sha,
        "manifest": manifest_text,
        "message": f"Built and pushed {image}",
    }


def configure_insecure_registry(*, registry: str) -> Dict[str, Any]:
    os_value = node_agent_os()
    if os_value != "linux":
        raise LumaError(f"insecure-registries configuration is not supported on {os_value}")
    host = _safe_registry_host(registry)
    script = (
        "set -euo pipefail; "
        "mkdir -p /etc/docker; "
        'f=/etc/docker/daemon.json; '
        '[ -f "$f" ] || echo "{}" > "$f"; '
        f"python3 - \"$f\" {shlex.quote(host)} <<'PY'\n"
        "import json, sys\n"
        "path, host = sys.argv[1], sys.argv[2]\n"
        "try:\n"
        "    data = json.load(open(path))\n"
        "except Exception:\n"
        "    data = {}\n"
        "if not isinstance(data, dict):\n"
        "    data = {}\n"
        "regs = data.get('insecure-registries')\n"
        "if not isinstance(regs, list):\n"
        "    regs = []\n"
        "if host not in regs:\n"
        "    regs.append(host)\n"
        "data['insecure-registries'] = regs\n"
        "open(path, 'w').write(json.dumps(data, indent=2))\n"
        "PY\n"
        "systemctl restart docker"
    )
    LocalExecutor().sudo(script)
    return {"registry": host, "message": f"insecure-registry configured: {host}"}


def _docker_image_repository(image: str) -> str:
    image = image.split("@", 1)[0]
    if ":" in image.rsplit("/", 1)[-1]:
        return image.rsplit(":", 1)[0]
    return image


def _safe_docker_image_ref(value: str) -> str:
    image = str(value or "").strip()
    if not image:
        raise LumaError("docker image is required")
    if any(ch.isspace() for ch in image) or any(ch in image for ch in ("'", '"', "`", "$", ";", "|", "&", "<", ">")):
        raise LumaError(f"invalid docker image reference: {value}")
    return image


def _docker_image_pull_error(*, image: str, output: str, platform: str = "") -> str:
    detail = (output or "").strip()
    message = f"target node Docker pull failed for {image}: {detail}"
    lowered = detail.lower()
    if platform and any(marker in lowered for marker in ("no matching manifest", "no match for platform", "not found")):
        message += f"; image does not provide a manifest for target platform {platform}"
    if any(marker in lowered for marker in ("failed to do request", "eof", "timeout", "connection reset")):
        message += "; target node cannot reach the registry, configure registry egress/proxy for that node or use a reachable mirror"
    return message


def update_luma_install(*, install_ref: str = "", config_path: Path = DEFAULT_AGENT_CONFIG) -> Dict[str, Any]:
    env = os.environ.copy()
    if install_ref:
        env["LUMA_INSTALL_REF"] = install_ref
    layout = _current_install_layout()
    if layout:
        user_home, install_home, bin_dir = layout
        env.setdefault("LUMA_USER_HOME", str(user_home))
        env.setdefault("LUMA_INSTALL_HOME", str(install_home))
        env.setdefault("LUMA_BIN_DIR", str(bin_dir))
    command = "curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh"
    try:
        completed = subprocess.run(
            command,
            shell=True,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "")
        raise LumaError("Luma installer timed out" + (f": {_tail_text(output)}" if output else "")) from exc
    output = completed.stdout or ""
    if completed.returncode != 0:
        raise LumaError(f"Luma installer failed with exit code {completed.returncode}: {_tail_text(output)}")
    os_value = node_agent_os()
    executable = _installed_luma_executable()
    restart_agent = True
    service_message = "node agent service refreshed"
    try:
        executor = LocalExecutor()
        if os_value == "darwin":
            refresh_command = _agent_install_command(config_path, executable=executable)
            delayed_script = f"( sleep ${{LUMA_AGENT_RELOAD_DELAY_SECONDS:-20}}; {refresh_command} ) >/tmp/luma-node-agent-reload.log 2>&1 &"
            delayed_command = f"sh -c {shlex.quote(delayed_script)}"
            executor.sudo(delayed_command, timeout=10)
            restart_agent = False
            service_message = "node agent launchd reload scheduled"
        else:
            executor.sudo(_agent_service_command(config_path, executable=executable, restart=False), timeout=60)
    except Exception as exc:
        raise LumaError(f"Luma installer finished but node agent service refresh failed: {exc}") from exc
    watchdog_message = "Tailscale watchdog skipped"
    try:
        LocalExecutor().sudo(_node_tailscale_watchdog_install_command(node_agent_os()), timeout=60)
        watchdog_message = "Tailscale watchdog installed"
    except Exception as exc:
        watchdog_message = f"Tailscale watchdog skipped: {exc}"
    return {
        "installRef": install_ref,
        "message": f"Luma installer finished; {service_message}; {watchdog_message}",
        "output": _tail_text(output),
        "restartAgent": restart_agent,
    }


def _tail_text(text: str, *, limit: int = 1200) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _run_fixed_host_task(
    command: str,
    *,
    prefer_container: bool = True,
    timeout_seconds: int | None = None,
    cleanup_command: str | None = None,
) -> str:
    if prefer_container:
        try:
            from .control.server import _run_host_prep_container

            return _run_host_prep_container(command)
        except LumaError:
            pass
    executor = LocalExecutor()
    try:
        return executor.sudo(command, timeout=timeout_seconds)
    except LumaError:
        if cleanup_command:
            executor.sudo(cleanup_command, check=False)
        raise


def _linux_prepare_nfs_command(name: str, path: str) -> str:
    safe_path = _safe_absolute_path(path).rstrip("/") or "/"
    export_file = f"/etc/exports.d/luma-{slugify(name)}.exports"
    export_line = f"{safe_path} *(rw,async,no_subtree_check,no_auth_nlm,insecure,no_root_squash)"
    return (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "command -v apt-get >/dev/null 2>&1 || { "
        "echo 'automatic managed NFS preparation currently supports apt-based Linux only' >&2; "
        "exit 1; "
        "}; "
        "if ! command -v exportfs >/dev/null 2>&1 || ! command -v mount.nfs >/dev/null 2>&1; then "
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
        "apt-get install -y nfs-kernel-server nfs-common; "
        "fi; "
        f"install -d -m 755 {shlex.quote(safe_path)}; "
        "install -d -m 755 /etc/exports.d; "
        f"printf '%s\\n' {shlex.quote(export_line)} > {shlex.quote(export_file)}; "
        "mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd; "
        "systemctl enable --now nfs-server >/dev/null 2>&1 "
        "|| systemctl enable --now nfs-kernel-server >/dev/null 2>&1 "
        "|| service nfs-kernel-server restart; "
        "exportfs -ra; "
        f"exportfs -v | grep -F {shlex.quote(safe_path)} >/dev/null"
    )


def _linux_remove_nfs_command(name: str) -> str:
    export_file = f"/etc/exports.d/luma-{slugify(name)}.exports"
    return (
        "set -euo pipefail; "
        f"rm -f {shlex.quote(export_file)}; "
        "if command -v exportfs >/dev/null 2>&1; then exportfs -ra; fi"
    )


def _macos_prepare_nfs_command(name: str, path: str) -> str:
    safe_path = _safe_absolute_path(path).rstrip("/") or "/"
    begin = f"# BEGIN LUMA {slugify(name)}"
    end = f"# END LUMA {slugify(name)}"
    export_line = f'"{safe_path}" -alldirs -maproot=root'
    return (
        "set -euo pipefail; "
        f"install -d -m 755 {shlex.quote(safe_path)}; "
        "touch /etc/exports; "
        f"awk -v b={shlex.quote(begin)} -v e={shlex.quote(end)} "
        "'$0==b{skip=1; next} $0==e{skip=0; next} !skip{print}' /etc/exports > /tmp/luma-exports.$$; "
        f"printf '%s\\n%s\\n%s\\n' {shlex.quote(begin)} {shlex.quote(export_line)} {shlex.quote(end)} >> /tmp/luma-exports.$$; "
        "cat /tmp/luma-exports.$$ > /etc/exports; "
        "rm -f /tmp/luma-exports.$$; "
        "/sbin/nfsd checkexports; "
        "/sbin/nfsd enable >/dev/null 2>&1 || true; "
        "/sbin/nfsd update || /sbin/nfsd restart"
    )


def _macos_remove_nfs_command(name: str) -> str:
    begin = f"# BEGIN LUMA {slugify(name)}"
    end = f"# END LUMA {slugify(name)}"
    return (
        "set -euo pipefail; "
        "[ -f /etc/exports ] || exit 0; "
        f"awk -v b={shlex.quote(begin)} -v e={shlex.quote(end)} "
        "'$0==b{skip=1; next} $0==e{skip=0; next} !skip{print}' /etc/exports > /tmp/luma-exports.$$; "
        "cat /tmp/luma-exports.$$ > /etc/exports; "
        "rm -f /tmp/luma-exports.$$; "
        "/sbin/nfsd checkexports; "
        "/sbin/nfsd update || true"
    )


def _volume_path_command(path: str) -> str:
    safe_path = _safe_absolute_path(path)
    return f"set -euo pipefail; install -d -m 755 {shlex.quote(safe_path)}"


def _remove_volume_path_command(root: str, relative: str) -> str:
    safe_root = Path(_safe_absolute_path(root))
    safe_relative = Path(_safe_relative_path(relative))
    full_path = safe_root / safe_relative
    if full_path == safe_root:
        raise LumaError("refusing to remove storage root")
    return (
        "set -euo pipefail; "
        f"root={shlex.quote(str(safe_root))}; "
        f"target={shlex.quote(str(full_path))}; "
        'case "$target" in "$root"/*) ;; *) echo "target outside storage root" >&2; exit 1;; esac; '
        'if [ -L "$target" ]; then echo "refusing to remove symlink" >&2; exit 1; fi; '
        'rm -rf -- "$target"'
    )


def _docker_volume_remove_command(name: str) -> str:
    safe_name = _safe_docker_volume_name(name)
    quoted = shlex.quote(safe_name)
    return (
        "set -euo pipefail; "
        f"{_docker_cli_prelude()}; "
        "for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do "
        f"\"$docker_cli\" volume inspect {quoted} >/dev/null 2>&1 || exit 0; "
        f"\"$docker_cli\" volume rm -f {quoted} && exit 0; "
        "sleep 2; "
        "done; "
        f"\"$docker_cli\" volume inspect {quoted} >/dev/null 2>&1 || exit 0; "
        f"\"$docker_cli\" volume rm -f {quoted}"
    )


def _docker_cli_prelude() -> str:
    candidates = [
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ]
    candidate_checks = " ".join(shlex.quote(candidate) for candidate in candidates)
    return (
        'docker_cli="${DOCKER:-}"; '
        'if [ -z "$docker_cli" ]; then docker_cli="$(command -v docker 2>/dev/null || true)"; fi; '
        'if [ -z "$docker_cli" ]; then '
        f"for candidate in {candidate_checks}; do "
        '[ -x "$candidate" ] || continue; docker_cli="$candidate"; break; '
        "done; "
        "fi; "
        '[ -n "$docker_cli" ] || { echo "docker command not found" >&2; exit 1; }'
    )


def _required(payload: Dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise LumaError(f"node agent task missing required field: {key}")
    return value


def _safe_absolute_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise LumaError(f"path must be absolute: {value}")
    if ".." in path.parts:
        raise LumaError(f"path must not contain ..: {value}")
    try:
        if path.exists() and path.is_symlink():
            raise LumaError(f"path must not be a symlink: {value}")
    except OSError as exc:
        raise LumaError(f"failed to inspect path {value}: {exc}") from exc
    return str(path)


def _safe_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not str(path):
        raise LumaError(f"volume path must be relative without ..: {value}")
    return str(path)


def _safe_docker_volume_name(value: str) -> str:
    name = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise LumaError(f"invalid Docker volume name: {value}")
    return name
