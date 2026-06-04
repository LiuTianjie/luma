from __future__ import annotations

import json
import os
import platform
import re
import shutil
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict

from .errors import LumaError
from .local import LocalExecutor
from .service import slugify

DEFAULT_AGENT_CONFIG = Path("/opt/luma/node-agent/agent.json")
DEFAULT_AGENT_SERVICE = "luma-node-agent"


def node_agent_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "linux":
        return "linux"
    return system or "unknown"


def node_agent_capabilities(os_name: str | None = None) -> list[str]:
    os_value = os_name or node_agent_os()
    if os_value == "linux":
        return ["nfs-host", "nfs-client", "managed-volume-path", "docker-volume", "docker-egress-proxy"]
    if os_value == "darwin":
        return ["nfs-host", "managed-volume-path", "docker-volume"]
    return []


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


def _agent_install_command(config_path: Path) -> str:
    os_value = node_agent_os()
    if os_value == "darwin":
        label = "io.luma.node-agent"
        plist = "/Library/LaunchDaemons/io.luma.node-agent.plist"
        plist_body = _launchd_plist(config_path)
        return (
            f"printf '%s' {shlex.quote(plist_body)} > {shlex.quote(plist)}; "
            f"chmod 644 {shlex.quote(plist)}; "
            f"launchctl bootout system/{label} >/dev/null 2>&1 || true; "
            f"launchctl bootstrap system {shlex.quote(plist)}; "
            f"launchctl kickstart -k system/{label}"
        )
    return (
        f"printf '%s' {shlex.quote(_systemd_unit(config_path))} > /etc/systemd/system/{DEFAULT_AGENT_SERVICE}.service; "
        "systemctl daemon-reload; "
        f"systemctl enable --now {DEFAULT_AGENT_SERVICE}.service; "
        f"systemctl restart {DEFAULT_AGENT_SERVICE}.service"
    )


def _agent_executable_args(config_path: Path) -> list[str]:
    executable = os.environ.get("LUMA_AGENT_EXECUTABLE") or shutil.which("luma") or _current_executable() or sys.executable
    if Path(executable).name.startswith("python"):
        return [executable, "-m", "luma.cli", "node-agent", "run", "--config", str(config_path)]
    return [executable, "node-agent", "run", "--config", str(config_path)]


def _current_executable() -> str:
    candidate = str(sys.argv[0] or "").strip()
    if not candidate or candidate == "-":
        return ""
    if "/" in candidate or Path(candidate).is_absolute():
        return candidate
    return shutil.which(candidate) or ""


def _systemd_unit(config_path: Path) -> str:
    args = " ".join(shlex.quote(part) for part in _agent_executable_args(config_path))
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


def _launchd_plist(config_path: Path) -> str:
    args = _agent_executable_args(config_path)
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
    while True:
        task = client.lease_agent_task(
            node_name=node_name,
            node_id=node_id,
            os_name=node_agent_os(),
            capabilities=node_agent_capabilities(),
            timeout=max(interval + 5, 15),
        ).get("task")
        if isinstance(task, dict) and task.get("id"):
            _complete_agent_task(client, node_name=node_name, node_id=node_id, task=task)
        if once:
            return 0
        time.sleep(max(interval, 1))


def _complete_agent_task(client: Any, *, node_name: str, node_id: str, task: Dict[str, Any]) -> None:
    task_id = str(task.get("id") or "")
    try:
        result = execute_agent_task(task)
        client.complete_agent_task(
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status="succeeded",
            message=str(result.get("message") or "ok"),
            result=result,
        )
    except Exception as exc:
        client.complete_agent_task(
            task_id=task_id,
            node_name=node_name,
            node_id=node_id,
            status="failed",
            message=str(exc),
            result={},
        )


def execute_agent_task(task: Dict[str, Any]) -> Dict[str, Any]:
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
    raise LumaError(f"unsupported node agent task action: {action}")


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
