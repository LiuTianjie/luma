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
import secrets
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
from typing import Any, Callable, Dict, Mapping

from . import __version__
from .builder_build_executor import build_plan, builder_build_available
from .builder_executor import (
    BuilderCleanupFailed,
    BuilderTaskCanceled,
    analyze_source,
    builder_analyze_available,
    open_builder_analysis_artifact,
)
from .errors import LumaError
from .installer import luma_installer_command
from .local import LocalExecutor, LocalResult
from .service import slugify

DEFAULT_AGENT_CONFIG = Path("/opt/luma/node-agent/agent.json")
DEFAULT_AGENT_SERVICE = "luma-node-agent"
DEFAULT_CONTAINER_STATS_INTERVAL_SECONDS = 30
DEFAULT_BUSY_HEARTBEAT_INTERVAL_SECONDS = 30.0


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


def _crane_binary() -> str | None:
    crane = shutil.which("crane")
    if crane:
        return crane
    for candidate in ("/usr/local/bin/crane", "/opt/homebrew/bin/crane"):
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
            "manager-update-v1",
            "nomad-join",
            "nomad-cni-repair",
            "terminal",
        ]
    elif os_value == "darwin":
        capabilities = ["nfs-host", "managed-volume-path", "docker-volume", "docker-image", "luma-update", "nomad-join", "terminal"]
    else:
        return []
    if _docker_buildx_available():
        capabilities.append("docker-build")
    if builder_analyze_available(os_value):
        # Do not advertise the aggregate builder-task-v1 capability: this node
        # implements analyze-source only.  build-plan gets its own executor and
        # capability when that code genuinely exists.
        capabilities.append("builder-analyze-v1")
        capabilities.append("builder-artifact-export-v1")
    if builder_build_available(os_value):
        # build-plan has a separate, stricter rootless BuildKit + supply-chain
        # gate.  Never advertise the aggregate builder-task-v1 capability.
        capabilities.append("builder-build-v1")
    if os_value == "linux" and _crane_binary():
        # The hardened LAE Builder setup installs crane. Control uses this
        # narrow capability to cache its own release image in the internal
        # registry before a manager rollout, avoiding a dockerd -> GHCR
        # dependency during the short control-plane replacement window.
        capabilities.append("control-image-mirror-v1")
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


def _agent_node_diagnostics(*, executor: LocalExecutor | None = None) -> Dict[str, Any]:
    executor = executor or LocalExecutor()
    docker_mirrors = _diagnostic_docker_mirrors(executor)
    docker_proxy = _diagnostic_docker_proxy(executor)
    pull_activity_timeout = _diagnostic_nomad_pull_activity_timeout(executor)
    recent_image_pull_errors = _diagnostic_recent_image_pull_errors(executor)
    cni_hostports = _diagnostic_nomad_cni_hostports(executor)
    return {
        "docker": {
            "mirrors": docker_mirrors,
            "proxy": docker_proxy,
        },
        "nomad": {
            "dockerDriver": {
                "pullActivityTimeout": pull_activity_timeout,
            },
            "cniHostPorts": cni_hostports,
        },
        "recentImagePullErrors": recent_image_pull_errors,
    }


def _diagnostic_docker_mirrors(executor: LocalExecutor) -> list[Dict[str, Any]]:
    script = (
        "python3 - <<'PY'\n"
        "import json\n"
        "from pathlib import Path\n"
        "path = Path('/etc/docker/daemon.json')\n"
        "try:\n"
        "    data = json.loads(path.read_text()) if path.exists() else {}\n"
        "except Exception:\n"
        "    data = {}\n"
        "mirrors = data.get('registry-mirrors') or []\n"
        "print(json.dumps(mirrors if isinstance(mirrors, list) else []))\n"
        "PY"
    )
    result = executor.run_result(script, timeout=10)
    if result.code != 0:
        return []
    try:
        raw = json.loads(result.output.strip() or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    mirrors = [str(item).strip() for item in raw if str(item or "").strip()]
    return [_diagnostic_docker_mirror_health(executor, mirror) for mirror in mirrors]


def _diagnostic_docker_mirror_health(executor: LocalExecutor, mirror: str) -> Dict[str, Any]:
    script = (
        "python3 - "
        f"{shlex.quote(mirror)}"
        " <<'PY'\n"
        "import json, socket, sys, urllib.error, urllib.parse, urllib.request\n"
        "url = sys.argv[1].rstrip('/')\n"
        "parsed = urllib.parse.urlparse(url)\n"
        "host = parsed.hostname or ''\n"
        "result = {'url': url, 'ok': False, 'message': 'unreachable'}\n"
        "try:\n"
        "    socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80))\n"
        "except Exception as exc:\n"
        "    result['message'] = 'DNS lookup failed: ' + str(exc)\n"
        "    print(json.dumps(result))\n"
        "    raise SystemExit(0)\n"
        "try:\n"
        "    req = urllib.request.Request(url + '/v2/', method='GET')\n"
        "    with urllib.request.urlopen(req, timeout=5) as resp:\n"
        "        code = getattr(resp, 'status', 200)\n"
        "    result['ok'] = code < 400\n"
        "    result['message'] = 'reachable' if result['ok'] else 'HTTP ' + str(code)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    result['ok'] = exc.code in (200, 401)\n"
        "    result['message'] = 'reachable' if result['ok'] else 'HTTP ' + str(exc.code)\n"
        "except Exception as exc:\n"
        "    result['message'] = str(exc)\n"
        "print(json.dumps(result))\n"
        "PY"
    )
    result = executor.run_result(script, timeout=8)
    if result.code != 0:
        return {"url": mirror, "ok": False, "message": (result.output or "health check failed").strip()}
    try:
        parsed = json.loads(result.output.strip() or "{}")
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "url": str(parsed.get("url") or mirror),
        "ok": bool(parsed.get("ok")),
        "message": str(parsed.get("message") or ""),
    }


def _diagnostic_docker_proxy(executor: LocalExecutor) -> Dict[str, str]:
    result = executor.run_result(
        "set -euo pipefail; "
        f"{_docker_cli_prelude()}; "
        "\"$docker_cli\" info --format 'HTTPProxy={{.HTTPProxy}} HTTPSProxy={{.HTTPSProxy}} NoProxy={{.NoProxy}}'",
        timeout=10,
    )
    if result.code != 0:
        return {}
    values: Dict[str, str] = {}
    key_map = {"HTTPProxy": "http", "HTTPSProxy": "https", "NoProxy": "noProxy"}
    for token in (result.output or "").strip().split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        mapped = key_map.get(key)
        if mapped:
            values[mapped] = value
    return values


def _diagnostic_nomad_pull_activity_timeout(executor: LocalExecutor) -> str:
    result = executor.run_result('grep -R "pull_activity_timeout" /etc/nomad.d 2>/dev/null || true', timeout=10)
    if result.code != 0:
        return ""
    for line in (result.output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        match = re.search(r"pull_activity_timeout\s*=\s*\"?([^\"\n#]+)\"?", stripped)
        if match:
            return match.group(1).strip()
    return ""


def _diagnostic_recent_image_pull_errors(executor: LocalExecutor) -> list[str]:
    result = executor.run_result(
        "journalctl -u nomad --since '30 minutes ago' --no-pager 2>/dev/null "
        "| grep -Ei 'image pull|pull.*image|inactivity|context canceled|tls|eof|proxyconnect|registry' "
        "| tail -n 20 || true",
        timeout=10,
    )
    if result.code != 0:
        return []
    return [line.strip() for line in (result.output or "").splitlines() if line.strip()]


def _diagnostic_nomad_cni_hostports(executor: LocalExecutor) -> Dict[str, Any]:
    if node_agent_os() != "linux":
        return {"conflicts": [], "missingNetworks": []}
    result = executor.run_result("iptables -t nat -S CNI-HOSTPORT-DNAT 2>/dev/null || true", timeout=10)
    missing_networks = _diagnostic_nomad_cni_missing_networks(executor)
    if result.code != 0:
        return {
            "conflicts": [],
            "missingNetworks": missing_networks,
            "message": (result.output or "iptables unavailable").strip(),
        }
    return {
        "conflicts": _parse_cni_hostport_conflicts(result.output or ""),
        "missingNetworks": missing_networks,
    }


def _diagnostic_nomad_cni_missing_networks(executor: LocalExecutor) -> list[Dict[str, Any]]:
    # Nomad's bridge network is owned by its running nomad_init_* container. A
    # Docker daemon restart can restore that container with network=none while
    # Nomad still considers the allocation healthy. Inspect /proc instead of
    # entering the namespace: this keeps the check read-only and avoids making
    # nsenter/ip availability a prerequisite.
    command = (
        "set -euo pipefail; "
        f"{_docker_cli_prelude()}; "
        'command -v awk >/dev/null 2>&1 || exit 0; '
        '"$docker_cli" ps --filter name=nomad_init_ --format \'{{.ID}}\' 2>/dev/null '
        "| while IFS= read -r container; do "
        '[ -n "$container" ] || continue; '
        'record=$("$docker_cli" inspect --format '
        "'{{.Id}}|{{.Name}}|{{.State.Pid}}|{{index .Config.Labels \"com.hashicorp.nomad.alloc_id\"}}' "
        '"$container" 2>/dev/null || true); '
        '[ -n "$record" ] || continue; '
        "IFS='|' read -r container_id container_name pid alloc_id <<< \"$record\"; "
        "case \"$pid\" in ''|*[!0-9]*) continue ;; esac; "
        '[ "$pid" -gt 0 ] 2>/dev/null || continue; '
        '[ -r "/proc/$pid/net/dev" ] || continue; '
        "interfaces=$(awk -F: 'NR > 2 { name=$1; gsub(/^[[:space:]]+|[[:space:]]+$/, \"\", name); "
        "if (name != \"\") { if (seen) printf \",\"; printf \"%s\", name; seen=1 } } "
        "END { if (seen) printf \"\\n\" }' \"/proc/$pid/net/dev\" 2>/dev/null || true); "
        '[ -n "$interfaces" ] || continue; '
        "printf '%s\\t%s\\t%s\\t%s\\n' \"$container_id\" \"$container_name\" \"$alloc_id\" \"$interfaces\"; "
        "done"
    )
    result = executor.run_result(command, timeout=10)
    if result.code != 0:
        return []
    return _parse_nomad_cni_missing_networks(result.output or "")


def _parse_nomad_cni_missing_networks(output: str) -> list[Dict[str, Any]]:
    missing: list[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for line in str(output or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        raw_container, raw_name, raw_alloc_id, raw_interfaces = (part.strip() for part in parts)
        name = raw_name.removeprefix("/")
        if not re.fullmatch(r"[0-9a-fA-F]{12,64}", raw_container):
            continue
        if not re.fullmatch(r"nomad_init_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", raw_alloc_id):
            continue
        interfaces = [item.strip() for item in raw_interfaces.split(",") if item.strip()]
        # Only a successfully inspected namespace containing exactly loopback
        # is evidence of this failure. Empty, malformed, or additional
        # interfaces are intentionally treated as non-actionable.
        if interfaces != ["lo"]:
            continue
        container = raw_container[:12].lower()
        key = (raw_alloc_id, container, name)
        if key in seen:
            continue
        seen.add(key)
        missing.append(
            {
                "allocId": raw_alloc_id,
                "container": container,
                "name": name,
                "interfaces": ["lo"],
            }
        )
    return sorted(missing, key=lambda item: (item["allocId"], item["name"], item["container"]))


def _parse_cni_hostport_conflicts(output: str) -> list[Dict[str, Any]]:
    rules_by_port: dict[tuple[str, str], list[Dict[str, str]]] = {}
    for line in str(output or "").splitlines():
        rule = _parse_cni_hostport_rule(line)
        if not rule:
            continue
        for port in rule["ports"].split(","):
            normalized_port = port.strip()
            if not normalized_port:
                continue
            rules_by_port.setdefault((rule["protocol"], normalized_port), []).append(rule)
    conflicts: list[Dict[str, Any]] = []
    for (protocol, port), rules in sorted(rules_by_port.items(), key=lambda item: (item[0][0], int(item[0][1]) if item[0][1].isdigit() else item[0][1])):
        if len(rules) < 2:
            continue
        alloc_ids = [rule["allocId"] for rule in rules]
        conflicts.append(
            {
                "protocol": protocol,
                "port": port,
                "allocIds": alloc_ids,
                "shadowedAllocIds": alloc_ids[1:],
                "ruleCount": len(rules),
            }
        )
    return conflicts


def _parse_cni_hostport_rule(line: str) -> Dict[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("-A CNI-HOSTPORT-DNAT "):
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return None
    protocol = _token_after(parts, "-p")
    ports = _token_after(parts, "--dports")
    if not protocol or not ports:
        return None
    comment = _token_after(parts, "--comment")
    alloc_match = re.search(r'id:\s*"([^"]+)"', comment or "")
    jump = _token_after(parts, "-j")
    return {
        "protocol": protocol,
        "ports": ports,
        "allocId": alloc_match.group(1) if alloc_match else "",
        "jump": jump or "",
    }


def repair_nomad_cni_hostports(*, executor: LocalExecutor | None = None, ports: Any = None) -> Dict[str, Any]:
    if node_agent_os() != "linux":
        return {"deleted": 0, "staleAllocIds": [], "message": "Nomad CNI hostport repair is only supported on Linux"}
    executor = executor or LocalExecutor()
    allowed_ports = _normalize_hostport_filter(ports)
    if not allowed_ports:
        return {"deleted": 0, "staleAllocIds": [], "activeAllocIds": [], "hostPorts": [], "message": "No Nomad CNI host ports requested"}
    active_alloc_ids = _active_nomad_docker_alloc_ids(executor)
    if not active_alloc_ids:
        return {"deleted": 0, "staleAllocIds": [], "activeAllocIds": [], "hostPorts": sorted(allowed_ports), "message": "No active Nomad Docker allocations found"}
    result = executor.run_result("iptables -t nat -S CNI-HOSTPORT-DNAT 2>/dev/null || true", timeout=10)
    if result.code != 0:
        raise LumaError((result.output or "failed to inspect Nomad CNI hostports").strip())
    rules_by_port: dict[tuple[str, str], list[Dict[str, str]]] = {}
    for line in str(result.output or "").splitlines():
        rule = _parse_cni_hostport_rule(line)
        if not rule:
            continue
        rule["line"] = line.strip()
        for port in rule["ports"].split(","):
            normalized_port = port.strip()
            if allowed_ports and normalized_port not in allowed_ports:
                continue
            if normalized_port:
                rules_by_port.setdefault((rule["protocol"], normalized_port), []).append(rule)
    stale_rules_by_line: dict[str, Dict[str, str]] = {}
    for rules in rules_by_port.values():
        if len(rules) <= 1:
            continue
        active_indexes = [index for index, rule in enumerate(rules) if rule.get("allocId") in active_alloc_ids]
        if not active_indexes:
            continue
        keep_index = max(active_indexes)
        for rule in rules[:keep_index]:
            alloc_id = rule.get("allocId") or ""
            if alloc_id and alloc_id not in active_alloc_ids:
                stale_rules_by_line.setdefault(rule["line"], rule)
    stale_rules = list(stale_rules_by_line.values())
    if not stale_rules:
        return {
            "deleted": 0,
            "staleAllocIds": [],
            "activeAllocIds": sorted(active_alloc_ids),
            "hostPorts": sorted(allowed_ports),
            "message": "No stale duplicate Nomad CNI hostport rules detected",
        }
    commands = []
    stale_alloc_ids = []
    for rule in stale_rules:
        stale_alloc_ids.append(rule.get("allocId") or "")
        commands.append(_iptables_delete_command_from_save_rule(rule["line"]))
    _run_fixed_host_task("set -euo pipefail\n" + "\n".join(commands))
    return {
        "deleted": len(stale_rules),
        "staleAllocIds": sorted({alloc_id for alloc_id in stale_alloc_ids if alloc_id}),
        "activeAllocIds": sorted(active_alloc_ids),
        "hostPorts": sorted(allowed_ports),
        "message": f"Deleted {len(stale_rules)} stale Nomad CNI hostport rule(s)",
    }


def _normalize_hostport_filter(raw_ports: Any) -> set[str]:
    if raw_ports is None:
        return set()
    values = raw_ports if isinstance(raw_ports, list) else [raw_ports]
    ports: set[str] = set()
    for value in values:
        try:
            port = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if port > 0:
            ports.add(str(port))
    return ports


def _iptables_delete_command_from_save_rule(line: str) -> str:
    parts = shlex.split(line)
    if len(parts) < 2 or parts[0] != "-A" or parts[1] != "CNI-HOSTPORT-DNAT":
        raise LumaError(f"unsupported iptables rule: {line}")
    return shlex.join(["iptables", "-w", "5", "-t", "nat", "-D", parts[1], *parts[2:]])


def _active_nomad_docker_alloc_ids(executor: LocalExecutor) -> set[str]:
    result = executor.run_result(
        "set -euo pipefail; "
        f"{_docker_cli_prelude()}; "
        '"$docker_cli" ps --format \'{{.Label "com.hashicorp.nomad.alloc_id"}}\' 2>/dev/null || true',
        timeout=10,
    )
    if result.code != 0:
        return set()
    return {
        line.strip()
        for line in str(result.output or "").splitlines()
        if line.strip() and line.strip() not in {"<no value>", "-"}
    }


def _token_after(parts: list[str], token: str) -> str:
    try:
        index = parts.index(token)
    except ValueError:
        return ""
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


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
        self._consecutive_failures = 0

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
        except Exception as exc:
            # Surface persistent sampling failures instead of swallowing them
            # silently — log the first failure and periodically thereafter so a
            # broken docker/stats path is diagnosable without flooding stderr.
            self._consecutive_failures += 1
            if self._consecutive_failures == 1 or self._consecutive_failures % 20 == 0:
                print(
                    f"luma: node agent container stats sampling failed "
                    f"({self._consecutive_failures}x): {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return
        if self._consecutive_failures:
            print(
                "luma: node agent container stats sampling recovered",
                file=sys.stderr,
                flush=True,
            )
            self._consecutive_failures = 0
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
    backup = (
        f"if [ -f {unit} ]; then "
        f"cp -a {unit} {unit}.luma-backup-$(date +%Y%m%d%H%M%S); "
        "fi"
    )
    command = (
        f"{backup}; "
        f"printf '%s' {shlex.quote(_systemd_unit(config_path, executable=executable))} > {unit}; "
        "systemctl daemon-reload; "
        f"systemctl enable {DEFAULT_AGENT_SERVICE}.service >/dev/null; "
        f"systemctl reset-failed {DEFAULT_AGENT_SERVICE}.service >/dev/null 2>&1 || true"
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
    # Preserve the install layout of the running Luma command. In particular,
    # a manager refresh may run with root privileges while the supported CLI
    # lives in the manager operator's home. Falling back to root's PATH here
    # rewrites a previously healthy service to /root/.local/bin/luma.
    executable = executable or _installed_luma_executable()
    if Path(executable).name.startswith("python"):
        return [executable, "-m", "luma.cli", "node-agent", "run", "--config", str(config_path)]
    return [executable, "node-agent", "run", "--config", str(config_path)]


def _terminal_supervisor_args(config_path: Path, *, executable: str | None = None) -> list[str]:
    executable = executable or _installed_luma_executable()
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
            "After=network-online.target docker.service nomad.service",
            "Wants=network-online.target docker.service nomad.service",
            "StartLimitIntervalSec=0",
            "",
            "[Service]",
            "Type=simple",
            "EnvironmentFile=-/etc/default/luma-node-agent",
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
    diagnostics_interval = int(os.environ.get("LUMA_NODE_AGENT_DIAGNOSTICS_INTERVAL_SECONDS") or config.get("diagnosticsIntervalSeconds") or 60)
    diagnostics: Dict[str, Any] = {}
    diagnostics_at = 0.0
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
            now = time.time()
            if once or now - diagnostics_at >= max(diagnostics_interval, 1):
                try:
                    diagnostics = _agent_node_diagnostics()
                except Exception as exc:
                    # Diagnostics are best-effort telemetry; a failure here must
                    # never crash the poll loop and take the node agent offline.
                    if once:
                        raise
                    print(f"luma: node agent diagnostics failed: {exc}", file=sys.stderr, flush=True)
                diagnostics_at = now
            try:
                task = client.lease_agent_task(
                    node_name=node_name,
                    node_id=node_id,
                    os_name=node_agent_os(),
                    arch=node_agent_arch(),
                    version=__version__,
                    capabilities=node_agent_capabilities(),
                    metrics=node_agent_metrics(),
                    container_stats=container_stats,
                    diagnostics=diagnostics,
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
    cancel_event = threading.Event()

    def progress(event: Dict[str, Any]) -> None:
        try:
            client.progress_agent_task(task_id=task_id, node_name=node_name, node_id=node_id, events=[event])
        except Exception as exc:
            print(f"luma: node agent task progress failed: {exc}", file=sys.stderr, flush=True)

    heartbeat_stop, heartbeat_thread = _start_agent_task_heartbeat(
        client,
        node_name=node_name,
        node_id=node_id,
        active_task_id=task_id,
        cancel_event=cancel_event,
        config_path=config_path,
    )
    try:
        # Keep task execution and result reporting in separate try scopes: a
        # network error while reporting SUCCESS must not be caught and inverted
        # into a "failed" report after the host mutation already happened.
        try:
            if str(task.get("action") or "") == "export-builder-artifact":
                if cancel_event.is_set():
                    raise BuilderTaskCanceled("builder artifact export canceled")
                export = open_builder_analysis_artifact(
                    task.get("payload") if isinstance(task.get("payload"), dict) else {},
                    cancel_event=cancel_event,
                )
                try:
                    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
                    response = client.upload_builder_artifact(
                        lease_id=str(payload.get("leaseId") or ""),
                        node_name=node_name,
                        node_id=node_id,
                        stream=export.stream,
                        media_type=export.media_type,
                        digest=export.digest,
                        size_bytes=export.size_bytes,
                        timeout=60,
                    )
                    if response != {
                        "leaseId": str(payload.get("leaseId") or ""),
                        "accepted": True,
                    }:
                        raise LumaError("builder artifact upload was not accepted")
                    result = {
                        "leaseId": str(payload.get("leaseId") or ""),
                        "digest": export.digest,
                        "sizeBytes": export.size_bytes,
                        "message": "builder artifact exported",
                    }
                finally:
                    export.close()
            else:
                result = execute_agent_task(task, config_path=config_path, progress=progress, cancel_event=cancel_event)
        except BuilderCleanupFailed:
            _report_agent_task_result(
                client,
                task_id=task_id,
                node_name=node_name,
                node_id=node_id,
                status="failed",
                message="builder sandbox cleanup failed",
                result={},
            )
            return False
        except BuilderTaskCanceled as exc:
            _report_agent_task_result(
                client,
                task_id=task_id,
                node_name=node_name,
                node_id=node_id,
                status="canceled",
                message=str(exc),
                result={},
            )
            return False
        except Exception as exc:
            status = "canceled" if cancel_event.is_set() else "failed"
            action = str(task.get("action") or "")
            if action == "analyze-source":
                failure_message = "builder analyze-source failed"
            elif action == "build-plan":
                failure_message = "builder build-plan failed"
            elif action == "export-builder-artifact":
                failure_message = "builder artifact export failed"
            else:
                failure_message = str(exc)
            _report_agent_task_result(
                client,
                task_id=task_id,
                node_name=node_name,
                node_id=node_id,
                status=status,
                message="builder task canceled" if status == "canceled" else failure_message,
                result={},
            )
            return False
        if cancel_event.is_set():
            _report_agent_task_result(
                client,
                task_id=task_id,
                node_name=node_name,
                node_id=node_id,
                status="canceled",
                message="builder task canceled",
                result={},
            )
            return False
        restart = bool(result.get("restartAgent"))
        _report_agent_task_result(
            client,
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status="succeeded",
            message=str(result.get("message") or "ok"),
            result=result,
        )
        return restart
    finally:
        _stop_agent_task_heartbeat(heartbeat_stop, heartbeat_thread)


def _report_agent_task_result(
    client: Any,
    *,
    task_id: str,
    node_name: str,
    node_id: str,
    status: str,
    message: str,
    result: Dict[str, Any],
) -> None:
    """Report a task's terminal result to Control, guarding the call so a
    reporting failure (network drop, Control restart) is logged rather than
    escaping and crashing the node agent's poll loop."""
    try:
        client.complete_agent_task(
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status=status,
            message=message,
            result=result,
        )
    except Exception as exc:
        print(
            f"luma: node agent failed to report task {task_id} result ({status}): {exc}",
            file=sys.stderr,
            flush=True,
        )


def _start_agent_task_heartbeat(
    client: Any,
    *,
    node_name: str,
    node_id: str,
    active_task_id: str,
    cancel_event: threading.Event,
    config_path: Path,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    interval = min(_agent_task_heartbeat_interval(config_path), 2.0)

    def loop() -> None:
        while not stop.is_set():
            try:
                response = client.heartbeat_agent(
                    node_name=node_name,
                    node_id=node_id,
                    active_task_id=active_task_id,
                    os_name=node_agent_os(),
                    arch=node_agent_arch(),
                    version=__version__,
                    capabilities=node_agent_capabilities(),
                    metrics=node_agent_metrics(),
                    timeout=_agent_task_heartbeat_timeout(interval),
                )
                if isinstance(response, dict) and bool(response.get("cancelRequested")):
                    cancel_event.set()
            except Exception as exc:
                print(f"luma: node agent busy heartbeat failed: {exc}", file=sys.stderr, flush=True)
            if stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name=f"luma-agent-task-heartbeat-{node_name}", daemon=True)
    thread.start()
    return stop, thread


def _stop_agent_task_heartbeat(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=1)


def _agent_task_heartbeat_interval(config_path: Path) -> float:
    raw: object = os.environ.get("LUMA_NODE_AGENT_BUSY_HEARTBEAT_SECONDS") or ""
    if not raw:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            raw = config.get("busyHeartbeatIntervalSeconds") if isinstance(config, dict) else ""
        except (OSError, json.JSONDecodeError):
            raw = ""
    try:
        interval = float(raw or DEFAULT_BUSY_HEARTBEAT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        interval = DEFAULT_BUSY_HEARTBEAT_INTERVAL_SECONDS
    return min(max(interval, 0.01), 300.0)


def _agent_task_heartbeat_timeout(interval: float) -> int:
    return max(2, min(15, int(max(interval, 1))))


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
    backoff = 1.0
    max_backoff = 60.0

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
                    backoff = 1.0  # connection established; reset reconnect backoff
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
            except Exception as exc:
                for session in list(sessions.values()):
                    session.close()
                sessions.clear()
                # Log the cause instead of silently swallowing it, and back off
                # exponentially so a persistently-down Control isn't hammered.
                print(
                    f"luma: terminal supervisor reconnecting after error ({exc}); retry in {backoff:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
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


def execute_agent_task(
    task: Dict[str, Any],
    *,
    config_path: Path = DEFAULT_AGENT_CONFIG,
    progress: Callable[[Dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
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
    if action == "diagnose-docker-pull":
        image = _safe_docker_image_ref(_required(payload, "image"))
        registry_auth = payload.get("registryAuth") if isinstance(payload.get("registryAuth"), dict) else None
        return diagnose_docker_pull(
            image=image,
            registry_auth=registry_auth,
            platform=str(payload.get("platform") or ""),
            timeout=int(payload.get("timeout") or 600),
            progress=progress,
        )
    if action == "update-luma":
        return update_luma_install(
            install_ref=str(payload.get("installRef") or ""),
            config_path=config_path,
            progress=progress,
        )
    if action == "start-manager-update":
        return start_manager_control_update(
            install_ref=str(payload.get("installRef") or ""),
            control_image=str(payload.get("controlImage") or ""),
            domain=str(payload.get("domain") or ""),
        )
    if action == "manager-update-status":
        return manager_control_update_status(update_id=str(payload.get("updateId") or ""))
    if action == "mirror-control-image":
        return mirror_control_image(
            source_image=_required(payload, "sourceImage"),
            push_image=_required(payload, "pushImage"),
            destination_image=_required(payload, "destinationImage"),
            proxy=str(payload.get("proxy") or ""),
            insecure=bool(payload.get("insecure")),
            timeout=int(payload.get("timeout") or 900),
            progress=progress,
        )
    if action == "build-image":
        return build_image(payload, progress=progress)
    if action == "analyze-source":
        return analyze_source(payload, progress=progress, cancel_event=cancel_event)
    if action == "build-plan":
        return build_plan(payload, progress=progress, cancel_event=cancel_event)
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
    if action == "repair-nomad-cni-hostports":
        return repair_nomad_cni_hostports(ports=payload.get("ports"))
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


_DOCKER_CONFIG_CHANGED_MARKER = "LUMA_DOCKER_CONFIG_CHANGED="


def _docker_config_changed(output: str) -> bool:
    matches = re.findall(
        rf"(?m)^{re.escape(_DOCKER_CONFIG_CHANGED_MARKER)}([01])\s*$",
        str(output or ""),
    )
    if not matches:
        raise LumaError("Docker daemon configuration completed without reporting whether it changed")
    return matches[-1] == "1"


def configure_docker_egress_proxy(*, proxy: str, no_proxy: str) -> Dict[str, Any]:
    os_value = node_agent_os()
    if os_value != "linux":
        raise LumaError(f"Docker daemon egress proxy setup is not supported on {os_value}")
    if not proxy.startswith(("http://", "https://")):
        raise LumaError("Docker daemon proxy must start with http:// or https://")
    desired = (
        "[Service]\n"
        f'Environment="HTTP_PROXY={proxy}"\n'
        f'Environment="HTTPS_PROXY={proxy}"\n'
        f'Environment="NO_PROXY={no_proxy}"\n'
    )
    command = (
        "set -euo pipefail; "
        "mkdir -p /etc/systemd/system/docker.service.d; "
        "f=/etc/systemd/system/docker.service.d/http-proxy.conf; "
        'tmp=$(mktemp "${f}.luma.XXXXXX"); '
        'trap \'rm -f "$tmp"\' EXIT; '
        f"printf '%s' {shlex.quote(desired)} > \"$tmp\"; "
        'if [ -f "$f" ] && cmp -s "$tmp" "$f"; then '
        "changed=0; "
        "else "
        'install -m 0644 "$tmp" "$f"; '
        "systemctl daemon-reload; "
        "systemctl restart docker; "
        "changed=1; "
        "fi; "
        f"printf '{_DOCKER_CONFIG_CHANGED_MARKER}%s\\n' \"$changed\""
    )
    executor = LocalExecutor()
    active_alloc_ids = sorted(_active_nomad_docker_alloc_ids(executor))
    changed = _docker_config_changed(executor.sudo(command))
    return {
        "proxy": proxy,
        "noProxy": no_proxy,
        "changed": changed,
        "dockerRestarted": changed,
        "affectedAllocationIds": active_alloc_ids if changed else [],
        "message": "Docker daemon egress proxy configured" if changed else "Docker daemon egress proxy already configured",
    }


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


def diagnose_docker_pull(
    *,
    image: str,
    registry_auth: Dict[str, Any] | None = None,
    platform: str = "",
    timeout: int = 600,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    image = _safe_docker_image_ref(image)
    platform = str(platform or "").strip()
    timeout = min(max(int(timeout or 600), 30), 1800)
    with tempfile.TemporaryDirectory(prefix="luma-docker-config-") as docker_config:
        _write_docker_auth_config(Path(docker_config), registry_auth)
        command = f"set -euo pipefail; {_docker_cli_prelude()}; DOCKER_CONFIG={shlex.quote(docker_config)} \"$docker_cli\" pull"
        if platform:
            command += f" --platform {shlex.quote(platform)}"
        command += f" {shlex.quote(image)}"
        def on_line(line: str) -> None:
            if progress:
                progress({"type": "output", "line": line})

        result = _run_command_streaming(command, timeout=timeout, on_line=on_line)
    output = str(result.output or "")
    lines = _diagnostic_output_lines(output)
    return {
        "image": image,
        "platform": platform,
        "ok": result.code == 0,
        "exitCode": result.code,
        "output": output,
        "lines": lines,
        "message": "Docker pull diagnostic finished" if result.code == 0 else "Docker pull diagnostic failed",
    }


def _diagnostic_output_lines(output: str, *, limit: int = 200) -> list[str]:
    normalized = str(output or "").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.splitlines() if line.strip()]
    return lines[-limit:]


def _run_command_streaming(
    command: str,
    *,
    timeout: int | None = None,
    on_line: Callable[[str], None] | None = None,
) -> LocalResult:
    process = subprocess.Popen(
        ["bash", "-lc", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        bufsize=1,
    )
    output_parts: list[str] = []
    line_buffer: list[str] = []
    deadline = time.monotonic() + timeout if timeout else None

    def flush_line() -> None:
        text = "".join(line_buffer).strip()
        line_buffer.clear()
        if text and on_line:
            on_line(text)

    try:
        while True:
            if deadline and time.monotonic() > deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                flush_line()
                message = f"command timed out after {timeout}s"
                output_parts.append("\n" + message)
                return LocalResult(code=124, output="".join(output_parts).strip())
            stream = process.stdout
            if stream is None:
                break
            ready, _, _ = select.select([stream], [], [], 0.2)
            if ready:
                chunk = stream.read(1)
                if not chunk:
                    if process.poll() is not None:
                        break
                    continue
                output_parts.append(chunk)
                if chunk in {"\n", "\r"}:
                    flush_line()
                else:
                    line_buffer.append(chunk)
                continue
            if process.poll() is not None:
                rest = stream.read() or ""
                output_parts.append(rest)
                for char in rest:
                    if char in {"\n", "\r"}:
                        flush_line()
                    else:
                        line_buffer.append(char)
                break
        flush_line()
        return LocalResult(code=process.wait(), output="".join(output_parts))
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_process_streaming(
    command: list[str],
    *,
    env: Dict[str, str] | None = None,
    timeout: int | None = None,
    on_line: Callable[[str], None] | None = None,
) -> LocalResult:
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        bufsize=1,
    )
    output_parts: list[str] = []
    line_buffer: list[str] = []
    deadline = time.monotonic() + timeout if timeout else None

    def flush_line() -> None:
        text = "".join(line_buffer).strip()
        line_buffer.clear()
        if text and on_line:
            on_line(text)

    try:
        while True:
            if deadline and time.monotonic() > deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                flush_line()
                message = f"process timed out after {timeout}s"
                output_parts.append("\n" + message)
                return LocalResult(code=124, output="".join(output_parts).strip())
            stream = process.stdout
            if stream is None:
                break
            ready, _, _ = select.select([stream], [], [], 0.2)
            if ready:
                chunk = stream.read(1)
                if not chunk:
                    if process.poll() is not None:
                        break
                    continue
                output_parts.append(chunk)
                if chunk in {"\n", "\r"}:
                    flush_line()
                else:
                    line_buffer.append(chunk)
                continue
            if process.poll() is not None:
                rest = stream.read() or ""
                output_parts.append(rest)
                for char in rest:
                    if char in {"\n", "\r"}:
                        flush_line()
                    else:
                        line_buffer.append(char)
                break
        flush_line()
        return LocalResult(code=process.wait(), output="".join(output_parts))
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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


def _safe_repo_path_from(base: Path, root: Path, rel: str) -> Path:
    value = str(rel or "").strip()
    if not value:
        raise LumaError("repository path is required")
    path = Path(value)
    if path.is_absolute():
        raise LumaError(f"path escapes repository: {rel}")
    candidate = (base / path).resolve()
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


def _ensure_buildx_builder(
    docker: str,
    *,
    proxy: str = "",
    no_proxy: str = "",
    recreate: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    # docker-container driver runs BuildKit in its own container/daemon. It does
    # NOT inherit the host dockerd proxy nor the CLI env, so FROM base-image pulls
    # need the proxy baked into the BuildKit container env at creation. Use a
    # distinct builder name per proxy state so a build never reuses a builder that
    # lacks the proxy it needs.
    name = "luma-builder-egress" if proxy else "luma-builder"
    if recreate:
        subprocess.run(
            [docker, "buildx", "rm", "-f", name],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=env,
            timeout=60,
        )
    inspect = subprocess.run(
        [docker, "buildx", "inspect", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
        timeout=30,
    )
    needs_create = inspect.returncode != 0
    if not needs_create and not _buildx_builder_proxy_matches(
        docker,
        name,
        proxy=proxy,
        no_proxy=no_proxy,
        env=env,
    ):
        # Builder names distinguish proxy/no-proxy in the common case, but the
        # egress proxy URL and NO_PROXY list can change while the named builder
        # survives. Reusing it would keep BuildKit on the stale network path.
        # Remove the incompatible builder before creating its replacement; if
        # removal fails, stop rather than accidentally using the stale builder.
        remove = subprocess.run(
            [docker, "buildx", "rm", "-f", name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=env,
            timeout=60,
        )
        if remove.returncode != 0:
            details = "\n".join(part for part in (inspect.stdout.strip(), remove.stdout.strip()) if part)
            raise LumaError(f"failed to reconfigure buildx builder {name}:\n{details}")
        needs_create = True
    if needs_create:
        create_cmd = [docker, "buildx", "create", "--name", name, "--driver", "docker-container", "--driver-opt", "network=host"]
        if proxy:
            create_cmd += [
                "--driver-opt", f"env.HTTP_PROXY={proxy}",
                "--driver-opt", f"env.HTTPS_PROXY={proxy}",
                "--driver-opt", _buildx_driver_opt(f"env.NO_PROXY={no_proxy}"),
            ]
        create_cmd.append("--bootstrap")
        create = subprocess.run(
            create_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=env,
            timeout=180,
        )
        if create.returncode != 0:
            raise LumaError(f"failed to create buildx builder:\n{create.stdout.strip()}")
        verify = subprocess.run(
            [docker, "buildx", "inspect", name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=env,
            timeout=60,
        )
        if verify.returncode != 0:
            details = "\n".join(part for part in (create.stdout.strip(), verify.stdout.strip()) if part)
            raise LumaError(f"failed to create buildx builder:\n{details}")
        if not _buildx_builder_proxy_matches(
            docker,
            name,
            proxy=proxy,
            no_proxy=no_proxy,
            env=env,
        ):
            raise LumaError(f"failed to create buildx builder {name} with the requested proxy settings")
    return name


def _buildx_builder_proxy_matches(
    docker: str,
    name: str,
    *,
    proxy: str,
    no_proxy: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether BuildKit's persisted container env matches this build."""
    # Current buildx versions do not expose create-time driver opts through
    # `buildx inspect`. The docker-container driver persists env.* driver opts
    # as Config.Env on its deterministic BuildKit container, which gives us the
    # actual settings that base-image pulls will use.
    inspect = subprocess.run(
        [
            docker,
            "inspect",
            "--format",
            "{{json .Config.Env}}",
            f"buildx_buildkit_{name}0",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
        timeout=30,
    )
    if inspect.returncode != 0:
        return False
    try:
        container_env = json.loads(inspect.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(container_env, list):
        return False
    options: dict[str, str] = {}
    for item in container_env:
        if not isinstance(item, str):
            return False
        key, separator, value = item.partition("=")
        if separator and key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}:
            options[key.upper()] = value

    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
    if not proxy:
        return all(key not in options for key in proxy_keys)
    return (
        options.get("HTTP_PROXY") == proxy
        and options.get("HTTPS_PROXY") == proxy
        and options.get("NO_PROXY") == no_proxy
    )


def _buildx_driver_opt(value: str) -> str:
    # buildx parses --driver-opt as CSV. For comma-containing env values, pass
    # literal CSV quotes through to buildx; backslash-comma is not enough and
    # gets parsed as separate invalid k=v fragments on some buildx versions.
    if "," not in value and '"' not in value:
        return value
    return '"' + value.replace('"', '""') + '"'


def _find_luma_deployment_manifest(repo: Path) -> tuple[str, Path] | None:
    repo_root = repo.resolve()
    service_patterns = (".luma.yml", ".luma.yaml", "luma.yml", "luma.yaml", "*.luma.yml", "*.luma.yaml")
    compose_patterns = (
        "luma.compose.yml",
        "luma.compose.yaml",
        ".luma.compose.yml",
        ".luma.compose.yaml",
        "*.luma.compose.yml",
        "*.luma.compose.yaml",
        "*.compose.luma.yml",
        "*.compose.luma.yaml",
        "docker-compose.luma.yml",
        "docker-compose.luma.yaml",
    )
    matches: dict[Path, str] = {}
    for kind, patterns in (("service", service_patterns), ("compose", compose_patterns)):
        for pattern in patterns:
            for path in repo_root.rglob(pattern):
                if not path.is_file() or ".git" in path.parts:
                    continue
                if kind == "service" and _looks_like_compose_luma_manifest(path):
                    continue
                matches[path.resolve()] = kind
    if not matches:
        return None

    def rel(path: Path) -> Path:
        return path.relative_to(repo_root)

    ranked = sorted(
        ((kind, path) for path, kind in matches.items()),
        key=lambda item: (len(rel(item[1]).parts), str(rel(item[1]))),
    )
    best_depth = len(rel(ranked[0][1]).parts)
    best = [item for item in ranked if len(rel(item[1]).parts) == best_depth]
    if len(best) > 1:
        names = ", ".join(str(rel(path)) for _, path in best)
        raise LumaError(f"multiple Luma deployment manifests found at the same priority: {names}")
    return ranked[0]


def _select_luma_compose_manifest(repo: Path, value: str) -> Path:
    """Resolve and structurally validate an explicitly selected Compose sidecar."""

    import yaml

    from .compose import load_compose_deployment
    from .repo_paths import normalize_repo_relative_path
    from .service import VALID_REGIONS

    selected = normalize_repo_relative_path(value, label="composeSidecar")
    repo_root = repo.resolve()
    unresolved = repo_root.joinpath(*selected.split("/"))
    try:
        sidecar_path = unresolved.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LumaError(f"selected composeSidecar does not exist: {selected}") from exc
    if sidecar_path != repo_root and repo_root not in sidecar_path.parents:
        raise LumaError("selected composeSidecar escapes the repository")
    if not sidecar_path.is_file() or unresolved.suffix not in {".yml", ".yaml"}:
        raise LumaError("selected composeSidecar must be a YAML file")

    try:
        raw = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LumaError(f"selected composeSidecar is not valid YAML: {selected}") from exc
    if not isinstance(raw, dict):
        raise LumaError("selected composeSidecar must contain a YAML mapping")

    compose_value = raw.get("compose", "docker-compose.yml")
    if not isinstance(compose_value, str) or not compose_value.strip():
        raise LumaError("selected composeSidecar requires string field: compose")
    compose_path = _safe_repo_path_from(
        sidecar_path.parent, repo_root, compose_value
    )
    if not compose_path.is_file():
        raise LumaError(
            f"selected composeSidecar references a missing Compose file: {compose_value}"
        )

    volumes = raw.get("volumes")
    storage_names = (
        {
            str(spec.get("storageClass"))
            for spec in volumes.values()
            if isinstance(spec, dict) and spec.get("storageClass")
        }
        if isinstance(volumes, dict)
        else set()
    )
    validation_storage = {
        name: {
            "provider": "nfs",
            "mode": "external",
            "endpoint": "nfs.invalid:/luma-sidecar-validation",
            "regions": sorted(VALID_REGIONS),
        }
        for name in storage_names
    }
    try:
        load_compose_deployment(
            sidecar_path,
            storage_classes=validation_storage,
            allow_sidecar_storage_classes=False,
            allow_build_services=True,
        )
    except LumaError as exc:
        raise LumaError(f"selected composeSidecar is invalid: {exc}") from exc
    return sidecar_path


def _looks_like_compose_luma_manifest(path: Path) -> bool:
    name = path.name.lower()
    return bool(
        re.fullmatch(r".+\.compose\.luma\.ya?ml", name)
        or re.fullmatch(r"docker-compose\.luma\.ya?ml", name)
        or re.fullmatch(r".+\.luma\.compose\.ya?ml", name)
    )


def _docker_buildx_build(
    *,
    docker: str,
    builder: str,
    docker_config: Path,
    push_host: str,
    registry_host: str,
    repo: str,
    sha: str,
    context_dir: Path,
    dockerfile_path: Path,
    platform: str,
    proxy: str,
    build_timeout: int,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> str:
    tag_sha = f"{push_host}/{repo}:{sha}"
    tag_latest = f"{push_host}/{repo}:latest"
    no_proxy = f"localhost,127.0.0.1,::1,{push_host},{registry_host}"
    env = dict(os.environ)
    env["DOCKER_CONFIG"] = str(docker_config)
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
    command = [
        docker, "buildx", "build",
        "--builder", builder,
        "--platform", platform,
        *proxy_build_args,
        # Luma's managed build registry is intentionally served over the
        # tailnet as plain HTTP. Host dockerd already knows it as an insecure
        # registry, but the docker-container BuildKit daemon is independent and
        # does not inherit daemon.json. Tell the image exporter explicitly so
        # pushes do not get upgraded to HTTPS inside BuildKit.
        "--output", "type=image,push=true,registry.insecure=true",
        "-t", tag_sha,
        "-t", tag_latest,
        "-f", str(dockerfile_path),
        str(context_dir),
    ]
    def on_build_line(line: str) -> None:
        if progress:
            progress({"type": "output", "line": line})

    def run_build() -> LocalResult:
        return _run_process_streaming(
            command,
            env=env,
            timeout=build_timeout,
            on_line=on_build_line,
        )

    result = run_build()
    if result.code == 124:
        raise LumaError(f"docker buildx build timed out after {build_timeout}s")
    if result.code != 0 and _buildx_missing_builder_error(result.output or "", builder):
        if progress:
            progress({"type": "output", "line": "Buildx builder is missing on the build node; recreating it and retrying once."})
        _ensure_buildx_builder(docker, proxy=proxy or "", no_proxy=no_proxy, recreate=True, env=env)
        result = run_build()
        if result.code == 124:
            raise LumaError(f"docker buildx build timed out after {build_timeout}s")
    if result.code != 0:
        output = (result.output or "").strip()
        if _buildx_missing_builder_error(output, builder):
            raise LumaError(
                "docker buildx builder could not be initialized on the build node. "
                "Luma tried to recreate it automatically, but Docker still reports it missing. "
                f"Original output:\n{output}"
            )
        raise LumaError(f"docker buildx build failed:\n{output}")
    return f"{registry_host}/{repo}:{sha}"


def _buildx_missing_builder_error(output: str, builder: str) -> bool:
    lowered = output.lower()
    if "no builder" not in lowered:
        return False
    return not builder or builder.lower() in lowered


def _compose_build_spec(body: Dict[str, Any]) -> Dict[str, Any] | None:
    raw = body.get("build")
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"context": raw}
    if not isinstance(raw, dict):
        raise LumaError("compose service build must be a string or mapping")
    return dict(raw)


def _build_compose_images(
    *,
    src: Path,
    sidecar_path: Path,
    docker: str,
    docker_config: Path,
    registry_host: str,
    push_host: str,
    repo: str,
    sha: str,
    proxy: str,
    build_timeout: int,
    payload: Dict[str, Any],
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    import yaml

    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    sidecar = yaml.safe_load(sidecar_text) or {}
    if not isinstance(sidecar, dict):
        raise LumaError("luma.compose.yml must contain a YAML mapping")
    compose_value = str(sidecar.get("compose") or "docker-compose.yml").strip() or "docker-compose.yml"
    compose_path = _safe_repo_path_from(sidecar_path.parent, src, compose_value)
    if not compose_path.is_file():
        raise LumaError(f"Compose file not found in repository: {compose_value}")
    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    if not isinstance(compose_data, dict):
        raise LumaError("docker-compose.yml must contain a YAML mapping")
    services = compose_data.get("services")
    if not isinstance(services, dict) or not services:
        raise LumaError("docker-compose.yml requires a non-empty services mapping")

    build_services: list[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    for service_name, service_body in services.items():
        if not isinstance(service_body, dict):
            raise LumaError(f"compose service {service_name} must be a mapping")
        spec = _compose_build_spec(service_body)
        if spec is not None:
            build_services.append((str(service_name), service_body, spec))
        elif not isinstance(service_body.get("image"), str) or not service_body.get("image"):
            raise LumaError(f"compose service {service_name} requires image or build")

    no_proxy = f"localhost,127.0.0.1,::1,{push_host},{registry_host}"
    buildx_env = dict(os.environ)
    buildx_env["DOCKER_CONFIG"] = str(docker_config)
    builder = _ensure_buildx_builder(docker, proxy=proxy or "", no_proxy=no_proxy, env=buildx_env)
    images: Dict[str, str] = {}
    single_build = len(build_services) == 1
    for service_name, service_body, spec in build_services:
        context_rel = str(payload.get("context") or spec.get("context") or ".").strip() or "."
        context_dir = _safe_repo_path_from(compose_path.parent, src, context_rel)
        dockerfile_rel = str(payload.get("dockerfile") or spec.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
        dockerfile_path = _safe_repo_path_from(context_dir, src, dockerfile_rel)
        if not dockerfile_path.is_file():
            raise LumaError(f"Dockerfile not found in repository: {dockerfile_rel}")
        platform = str(payload.get("platform") or spec.get("platform") or service_body.get("platform") or "linux/amd64").strip() or "linux/amd64"
        repo_override = str(spec.get("repo") or spec.get("x-luma-repo") or "").strip()
        image_repo = _safe_image_repo(repo_override or (repo if single_build else f"{repo}/{slugify(service_name)}"))
        image = _docker_buildx_build(
            docker=docker,
            builder=builder,
            docker_config=docker_config,
            push_host=push_host,
            registry_host=registry_host,
            repo=image_repo,
            sha=sha,
            context_dir=context_dir,
            dockerfile_path=dockerfile_path,
            platform=platform,
            proxy=proxy,
            build_timeout=build_timeout,
            progress=progress,
        )
        service_body["image"] = image
        service_body.pop("build", None)
        images[service_name] = image

    result = {
        "kind": "compose",
        "manifest": sidecar_text,
        "composeContent": yaml.safe_dump(compose_data, sort_keys=False, allow_unicode=False),
        "images": images,
        "image": next(iter(images.values()), ""),
        "sha": sha,
        "message": f"Built and pushed {len(images)} compose image(s)" if images else "Compose images already declared",
    }
    selected_sidecar = str(payload.get("composeSidecar") or "")
    if selected_sidecar:
        result["composeSidecar"] = selected_sidecar
    return result


def build_image(payload: Dict[str, Any], *, progress: Callable[[Dict[str, Any]], None] | None = None) -> Dict[str, Any]:
    from . import gitops

    repo_url = _required(payload, "repoUrl")
    ref = str(payload.get("ref") or "").strip() or None
    proxy = str(payload.get("proxy") or "").strip() or None
    git_token = str(payload.get("gitToken") or "").strip() or None
    git_username = str(payload.get("gitUsername") or "").strip() or None
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
        gitops.clone(repo_url, src, ref=ref, proxy=proxy, token=git_token, username=git_username)
        sha = gitops.head_commit(src)

        docker_config = Path(workdir) / "docker-config"
        _write_docker_auth_config(docker_config, _auth_for_host(registry_auth, push_host))

        selected_sidecar = str(payload.get("composeSidecar") or "")
        deployment_manifest = (
            ("compose", _select_luma_compose_manifest(src, selected_sidecar))
            if selected_sidecar
            else _find_luma_deployment_manifest(src)
        )
        if deployment_manifest and deployment_manifest[0] == "compose":
            return _build_compose_images(
                src=src,
                sidecar_path=deployment_manifest[1],
                docker=docker,
                docker_config=docker_config,
                registry_host=registry_host,
                push_host=push_host,
                repo=repo,
                sha=sha,
                proxy=proxy or "",
                build_timeout=build_timeout,
                payload=payload,
                progress=progress,
            )

        manifest_path = deployment_manifest[1] if deployment_manifest else None
        manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path else ""

        # For single-service imports, the repo manifest's build block is the
        # declarative source of truth; payload values act as overrides.
        build_block: Dict[str, Any] = {}
        if manifest_text.strip():
            import yaml

            try:
                parsed = yaml.safe_load(manifest_text) or {}
            except yaml.YAMLError as exc:
                raise LumaError(f"invalid Luma deployment manifest in repository: {exc}") from exc
            if isinstance(parsed, dict) and isinstance(parsed.get("build"), dict):
                build_block = parsed["build"]
        repo = _safe_image_repo(str(build_block.get("repo") or repo))
        context_rel = str(payload.get("context") or build_block.get("context") or ".").strip() or "."
        dockerfile_rel = str(payload.get("dockerfile") or build_block.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
        platform = str(payload.get("platform") or build_block.get("platform") or "linux/amd64").strip() or "linux/amd64"

        context_dir = _safe_repo_subpath(src, context_rel)
        dockerfile_path = _safe_repo_subpath(src, dockerfile_rel)
        if not dockerfile_path.is_file():
            raise LumaError(f"Dockerfile not found in repository: {dockerfile_rel}")

        # cn/home build nodes reach the internet through the egress proxy. The
        # proxy must reach three places: the buildx CLI + FROM pulls (env), the
        # RUN steps inside the build (--build-arg; BuildKit does not inherit the
        # CLI/daemon proxy), and the BuildKit container itself (set at builder
        # creation in _ensure_buildx_builder). Keep the in-cluster registry and
        # localhost out of the proxy so --push stays on the internal network.
        no_proxy = f"localhost,127.0.0.1,::1,{push_host},{registry_host}"
        buildx_env = dict(os.environ)
        buildx_env["DOCKER_CONFIG"] = str(docker_config)
        builder = _ensure_buildx_builder(docker, proxy=proxy or "", no_proxy=no_proxy, env=buildx_env)
        image = _docker_buildx_build(
            docker=docker,
            builder=builder,
            docker_config=docker_config,
            push_host=push_host,
            registry_host=registry_host,
            repo=repo,
            sha=sha,
            context_dir=context_dir,
            dockerfile_path=dockerfile_path,
            platform=platform,
            proxy=proxy or "",
            build_timeout=build_timeout,
            progress=progress,
        )

    return {
        "kind": "service",
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
        f"changed=$(python3 - \"$f\" {shlex.quote(host)} <<'PY'\n"
        "import os, json, sys\n"
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
        "changed = host not in regs\n"
        "if host not in regs:\n"
        "    regs.append(host)\n"
        "if changed:\n"
        "    data['insecure-registries'] = regs\n"
        "    tmp = path + '.luma.tmp'\n"
        "    open(tmp, 'w').write(json.dumps(data, indent=2) + '\\n')\n"
        "    os.replace(tmp, path)\n"
        "print('1' if changed else '0')\n"
        "PY\n"
        "); "
        'if [ "$changed" = "1" ]; then systemctl restart docker; fi; '
        f"printf '{_DOCKER_CONFIG_CHANGED_MARKER}%s\\n' \"$changed\""
    )
    executor = LocalExecutor()
    active_alloc_ids = sorted(_active_nomad_docker_alloc_ids(executor))
    changed = _docker_config_changed(executor.sudo(script))
    return {
        "registry": host,
        "changed": changed,
        "dockerRestarted": changed,
        "affectedAllocationIds": active_alloc_ids if changed else [],
        "message": f"insecure-registry configured: {host}" if changed else f"insecure-registry already configured: {host}",
    }


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


def _image_registry_host(image: str) -> str:
    first = image.split("/", 1)[0]
    if first == "localhost" or "." in first or ":" in first:
        return first
    return "registry-1.docker.io"


def mirror_control_image(
    *,
    source_image: str,
    push_image: str,
    destination_image: str,
    proxy: str = "",
    insecure: bool = False,
    timeout: int = 900,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Copy a Control release through the Builder into Luma's pull registry.

    The action receives only validated argv values and never invokes a shell.
    It is intentionally narrower than a general registry-copy primitive: the
    destination repository must be ``luma-control`` and the public pull ref is
    returned separately from the builder-local push ref.
    """

    crane = _crane_binary()
    if not crane:
        raise LumaError("control image mirror requires crane on the builder node")
    source = _safe_docker_image_ref(source_image)
    push = _safe_docker_image_ref(push_image)
    destination = _safe_docker_image_ref(destination_image)
    if not _docker_image_repository(push).endswith("/luma-control"):
        raise LumaError("control image mirror destination repository must be luma-control")
    if not _docker_image_repository(destination).endswith("/luma-control"):
        raise LumaError("control image public repository must be luma-control")
    if push.rsplit(":", 1)[-1] != destination.rsplit(":", 1)[-1]:
        raise LumaError("control image push and pull tags must match")
    timeout = min(max(int(timeout or 900), 60), 1800)
    env = dict(os.environ)
    proxy_value = str(proxy or "").strip()
    if proxy_value:
        parsed_proxy = urllib.parse.urlparse(proxy_value)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
            raise LumaError("control image mirror proxy must be an HTTP(S) URL")
        env["HTTP_PROXY"] = proxy_value
        env["HTTPS_PROXY"] = proxy_value
    no_proxy = ["localhost", "127.0.0.1", "::1", _image_registry_host(push), _image_registry_host(destination)]
    env["NO_PROXY"] = ",".join(dict.fromkeys(value for value in no_proxy if value))

    def emit(line: str) -> None:
        value = str(line or "").strip()
        if value and progress:
            progress({"type": "status", "line": value, "ts": int(time.time())})

    command = [crane, "copy", source, push]
    if insecure:
        command.append("--insecure")
    emit(f"Caching {source} on the internal Builder registry.")
    result: LocalResult | None = None
    transient_markers = ("eof", "timeout", "connection reset", "temporary", "tls handshake", "unexpected status code 5")
    for attempt in range(1, 4):
        result = _run_process_streaming(command, env=env, timeout=timeout, on_line=emit)
        if result.code == 0:
            break
        output = str(result.output or "").strip()
        if attempt >= 3 or not any(marker in output.lower() for marker in transient_markers):
            raise LumaError(f"control image mirror failed: {_tail_text(output)}")
        emit(f"Registry transfer was interrupted; retrying ({attempt + 1}/3).")
        time.sleep(2**attempt)
    if result is None or result.code != 0:
        raise LumaError("control image mirror failed")

    digest_command = [crane, "digest", push]
    if insecure:
        digest_command.append("--insecure")
    digest_result = _run_process_streaming(digest_command, env=env, timeout=120, on_line=None)
    digest = str(digest_result.output or "").strip().splitlines()[-1] if digest_result.output else ""
    if digest_result.code != 0 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise LumaError(f"control image mirror verification failed: {_tail_text(digest_result.output or '')}")
    emit(f"Internal Control image verified at {digest}.")
    return {
        "sourceImage": source,
        "pushImage": push,
        "destinationImage": destination,
        "digest": digest,
        "message": "Control image cached and verified in the internal registry.",
    }


def _docker_image_pull_error(*, image: str, output: str, platform: str = "") -> str:
    detail = (output or "").strip()
    message = f"target node Docker pull failed for {image}: {detail}"
    lowered = detail.lower()
    if platform and any(marker in lowered for marker in ("no matching manifest", "no match for platform", "not found")):
        message += f"; image does not provide a manifest for target platform {platform}"
    if any(marker in lowered for marker in ("failed to do request", "eof", "timeout", "connection reset")):
        message += "; target node cannot reach the registry, configure registry egress/proxy for that node or use a reachable mirror"
    return message


def update_luma_install(
    *,
    install_ref: str = "",
    config_path: Path = DEFAULT_AGENT_CONFIG,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    def emit(message: str) -> None:
        if progress:
            progress({"type": "status", "line": message, "ts": int(time.time())})

    env = os.environ.copy()
    command, exact_ref = luma_installer_command(install_ref, environ=env)
    env["LUMA_INSTALL_REF"] = exact_ref
    layout = _current_install_layout()
    if layout:
        user_home, install_home, bin_dir = layout
        env.setdefault("LUMA_USER_HOME", str(user_home))
        env.setdefault("LUMA_INSTALL_HOME", str(install_home))
        env.setdefault("LUMA_BIN_DIR", str(bin_dir))
    emit(f"Downloading and installing Luma {exact_ref}.")
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
    emit("Package installed; refreshing the node agent service definition.")
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
    emit(f"{service_message.capitalize()}; refreshing the node watchdog.")
    watchdog_message = "Tailscale watchdog skipped"
    try:
        LocalExecutor().sudo(_node_tailscale_watchdog_install_command(node_agent_os()), timeout=60)
        watchdog_message = "Tailscale watchdog installed"
    except Exception as exc:
        watchdog_message = f"Tailscale watchdog skipped: {exc}"
    emit(f"Update prepared successfully; {watchdog_message}.")
    return {
        "installRef": exact_ref,
        "message": f"Luma installer finished; {service_message}; {watchdog_message}",
        "output": _tail_text(output),
        "restartAgent": restart_agent,
    }


def _manager_update_root() -> Path:
    return Path(os.environ.get("LUMA_MANAGER_UPDATE_ROOT") or "/opt/luma/manager-updates")


def _manager_update_id(value: str) -> str:
    update_id = str(value or "").strip()
    if not re.fullmatch(r"manager-[0-9]{10,}-[a-f0-9]{8}", update_id):
        raise LumaError("invalid manager update id")
    return update_id


def _manager_update_ref(value: str) -> str:
    install_ref = str(value or "").strip()
    if not install_ref or len(install_ref) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", install_ref):
        raise LumaError("installRef must be a Git tag, branch, or commit")
    return install_ref


def _manager_update_domain(value: str) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
        raise LumaError("invalid control domain")
    return domain


def _write_manager_update_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start_manager_control_update(*, install_ref: str, control_image: str, domain: str) -> Dict[str, Any]:
    if node_agent_os() != "linux" or not shutil.which("systemd-run"):
        raise LumaError("managed control-plane update requires Linux systemd")
    safe_ref = _manager_update_ref(install_ref)
    safe_image = _safe_docker_image_ref(control_image)
    safe_domain = _manager_update_domain(domain)
    executable = _installed_luma_executable()
    if not executable or not Path(executable).exists():
        raise LumaError("installed Luma executable is unavailable")
    root = _manager_update_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    # Fail closed while another manager rollout has no terminal status. The
    # dashboard can reconnect to that operation instead of launching a race.
    for meta_path in sorted(root.glob("manager-*.json"), reverse=True)[:20]:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        current_id = str(meta.get("id") or "")
        if current_id and not (root / f"{current_id}.status").exists():
            unit = str(meta.get("unit") or "")
            active = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            if active:
                raise LumaError(f"manager update already running: {current_id}")
    update_id = f"manager-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    unit = f"luma-{update_id}"
    log_path = root / f"{update_id}.log"
    status_path = root / f"{update_id}.status"
    meta_path = root / f"{update_id}.json"
    layout = _current_install_layout()
    environment = {
        "LUMA_CONTROL_IMAGE": safe_image,
        "LUMA_MANAGER_UPDATE_LOG_PATH": str(log_path),
        "LUMA_MANAGER_UPDATE_STATUS_PATH": str(status_path),
    }
    if layout:
        user_home, install_home, bin_dir = layout
        environment.update(
            {
                "LUMA_USER_HOME": str(user_home),
                "LUMA_INSTALL_HOME": str(install_home),
                "LUMA_BIN_DIR": str(bin_dir),
            }
        )
    _write_manager_update_json(
        meta_path,
        {
            "schemaVersion": "luma.manager-update/v1",
            "id": update_id,
            "unit": unit,
            "installRef": safe_ref,
            "controlImage": safe_image,
            "domain": safe_domain,
            "createdAt": int(time.time()),
        },
    )
    wrapper = (
        'umask 077; exec >> "$LUMA_MANAGER_UPDATE_LOG_PATH" 2>&1; '
        '"$@"; code=$?; printf "%s\\n" "$code" > "$LUMA_MANAGER_UPDATE_STATUS_PATH"; exit "$code"'
    )
    invocation = [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--no-block",
        "--property=Type=exec",
    ]
    invocation.extend(f"--setenv={key}={value}" for key, value in environment.items())
    invocation.extend(
        [
            "sh",
            "-c",
            wrapper,
            "luma-manager-update",
            executable,
            "update",
            "manager",
            "--install-ref",
            safe_ref,
            "--domain",
            safe_domain,
        ]
    )
    completed = subprocess.run(invocation, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        status_path.write_text(f"{completed.returncode}\n", encoding="utf-8")
        status_path.chmod(0o600)
        raise LumaError(f"unable to start manager update: {_tail_text(completed.stdout or '')}")
    return {
        "updateId": update_id,
        "status": "running",
        "installRef": safe_ref,
        "controlImage": safe_image,
        "createdAt": int(time.time()),
        "message": "Control-plane update started; the dashboard will reconnect automatically during the rollout.",
    }


def manager_control_update_status(*, update_id: str = "") -> Dict[str, Any]:
    root = _manager_update_root()
    if update_id:
        safe_id = _manager_update_id(update_id)
        meta_path = root / f"{safe_id}.json"
    else:
        candidates = sorted(root.glob("manager-*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if root.exists() else []
        if not candidates:
            return {"status": "none", "message": "No manager update has been recorded."}
        meta_path = candidates[0]
        safe_id = _manager_update_id(meta_path.stem)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LumaError("manager update not found") from exc
    except (OSError, ValueError) as exc:
        raise LumaError("manager update state is unreadable") from exc
    status_path = root / f"{safe_id}.status"
    log_path = root / f"{safe_id}.log"
    status = "running"
    exit_code: int | None = None
    if status_path.exists():
        try:
            exit_code = int(status_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            exit_code = -1
        status = "succeeded" if exit_code == 0 else "failed"
    else:
        unit = str(meta.get("unit") or "")
        active = bool(unit) and subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not active:
            status = "interrupted"
    lines: list[str] = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
    except OSError:
        pass
    return {
        "updateId": safe_id,
        "status": status,
        "exitCode": exit_code,
        "installRef": str(meta.get("installRef") or ""),
        "controlImage": str(meta.get("controlImage") or ""),
        "createdAt": int(meta.get("createdAt") or 0),
        "log": lines,
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
        f"python3 - {shlex.quote(export_file)} {shlex.quote(export_line)} {shlex.quote(safe_path)} <<'PY'\n"
        "import glob, os, pathlib, sys\n"
        "target = pathlib.Path(sys.argv[1])\n"
        "export_line = sys.argv[2]\n"
        "export_path = sys.argv[3]\n"
        "reused = False\n"
        "for raw in glob.glob('/etc/exports.d/luma-*.exports'):\n"
        "    candidate = pathlib.Path(raw)\n"
        "    if candidate == target or not candidate.is_file():\n"
        "        continue\n"
        "    lines = [line.strip() for line in candidate.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]\n"
        "    if export_line in lines:\n"
        "        target.unlink(missing_ok=True)\n"
        "        reused = True\n"
        "        break\n"
        "    if any(line.split(None, 1)[0] == export_path for line in lines):\n"
        "        raise SystemExit('managed NFS export path already exists with different options: ' + str(candidate))\n"
        "if not reused:\n"
        "    tmp = target.with_name(target.name + '.luma.tmp')\n"
        "    tmp.write_text(export_line + '\\n')\n"
        "    os.replace(tmp, target)\n"
        "PY\n"
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
