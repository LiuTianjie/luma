"""Apply managed HTTP registry access to the actual OrbStack socket owner.

The file may also be executed with isolated Python under that owner's UID.
Keep this module stdlib-only: privileged Luma agents must not write through
user-controlled home directories, and the child must load this exact code.
"""
from __future__ import annotations

import json
import contextlib
import fcntl
import inspect
import ipaddress
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__:
    from .registry_proxy import docker_proxy_settings, no_proxy_bypasses_registry_hosts


DOCKER_SOCKET = Path("/var/run/docker.sock")
ORBCTL = Path("/Applications/OrbStack.app/Contents/MacOS/bin/orbctl")
RESULT_PREFIX = "LUMA_MANAGED_REGISTRY_RESULT="


def _environment() -> dict[str, str]:
    # Pin every Docker operation to Nomad's socket, independent of login-shell
    # contexts. Isolated Python also ignores user PYTHONPATH and user site dirs.
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith("DOCKER_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }


def _run(command: list[str], *, operation: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                timeout=timeout, env=_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{operation} did not complete within its time limit") from exc
    if result.returncode != 0:
        # Runtime/config output can contain proxy credentials. Do not echo it.
        raise RuntimeError(f"{operation} failed (exit {result.returncode})")
    return result.stdout


def _socket_owner() -> tuple[object, Path]:
    try:
        socket = DOCKER_SOCKET.resolve(strict=True)
        info = socket.stat()
        owner = pwd.getpwuid(info.st_uid)
        home = Path(owner.pw_dir).resolve(strict=True)
    except (OSError, RuntimeError, KeyError) as exc:
        raise RuntimeError("Cannot identify the owner of Nomad's Docker socket") from exc
    if not stat.S_ISSOCK(info.st_mode) or owner.pw_uid == 0:
        raise RuntimeError("Managed macOS registry access requires a user-owned OrbStack socket")
    if socket != home / ".orbstack/run/docker.sock":
        raise RuntimeError("Unsupported macOS Docker runtime: Nomad's socket is not an OrbStack socket")
    return owner, socket


def ensure_orbstack_registries(hosts: list[str], *, docker: str, check_only: bool = False) -> dict[str, object]:
    owner, socket = _socket_owner()
    if os.geteuid() not in {0, owner.pw_uid}:
        raise RuntimeError("Run registry setup as the OrbStack socket owner or the Luma node agent")
    # All home-directory access and OrbStack IPC happen with the same UID as
    # the socket, even when the caller is a system LaunchDaemon running as root.
    # Root installations can live below /var/root. Passing this stdlib-only
    # code avoids giving the owner access to the privileged installation.
    source = Path(__file__).read_text(encoding="utf-8").replace("from __future__ import annotations\n", "", 1)
    helpers = "\n".join(inspect.getsource(helper) for helper in (docker_proxy_settings, no_proxy_bypasses_registry_hosts))
    source = "from __future__ import annotations\n" + helpers + "\n" + source
    command = ["/usr/bin/python3", "-I", "-c", source]
    if os.geteuid() != owner.pw_uid:
        command = ["/usr/bin/sudo", "-n", "-H", "-u", owner.pw_name, "--", *command]
    request = {"registries": hosts, "docker": docker, "socket": str(socket), "checkOnly": check_only}
    try:
        environment = {**_environment(), "HOME": owner.pw_dir, "USER": owner.pw_name, "LOGNAME": owner.pw_name}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
        try:
            stdout, _stderr = process.communicate(json.dumps(request), timeout=300)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("OrbStack registry setup did not complete; verify the Docker engine before retrying") from exc
    lines = stdout.splitlines()
    response = next((line[len(RESULT_PREFIX):] for line in reversed(lines) if line.startswith(RESULT_PREFIX)), "")
    try:
        value = json.loads(response)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("OrbStack registry setup returned no verified result") from exc
    if process.returncode != 0 or not isinstance(value, dict) or value.get("verified") is not True:
        message = value.get("error") if isinstance(value, dict) else ""
        raise RuntimeError(str(message or "OrbStack registry setup failed verification"))
    return value


def _docker_info(docker: str, socket: Path) -> dict[str, object]:
    output = _run([docker, "--host", f"unix://{socket}", "info", "--format", "{{json .}}"],
                  operation="OrbStack Docker inspection", timeout=5)
    try:
        info = json.loads(output)
    except ValueError as exc:
        raise RuntimeError("Docker returned invalid engine information") from exc
    if not isinstance(info, dict) or info.get("OperatingSystem") != "OrbStack":
        raise RuntimeError("Unsupported macOS Docker runtime: the selected engine is not OrbStack")
    docker_proxy_settings(info)
    return info


def _transport_ready(info: dict[str, object], hosts: list[str]) -> bool:
    config = info.get("RegistryConfig")
    indexes = config.get("IndexConfigs") if isinstance(config, dict) else None
    return isinstance(indexes, dict) and all(
        isinstance(indexes.get(host), dict) and indexes[host].get("Secure") is False
        for host in hosts
    )


@contextlib.contextmanager
def _settings_lock(directory: Path, owner_uid: int):
    descriptor = os.open(directory / ".luma-registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid:
            raise RuntimeError("OrbStack registry setup lock has an unexpected owner or file type")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another OrbStack registry setup is in progress; retry after it finishes") from exc
        yield
    finally:
        os.close(descriptor)


def _reject_json_constant(value: str):
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


def _assert_idle(docker: str, socket: Path) -> None:
    if _run([docker, "--host", f"unix://{socket}", "ps", "-q"], operation="OrbStack running-container check").strip():
        raise RuntimeError("OrbStack Docker has running containers; drain this node in a maintenance window before changing registry transport")


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".luma-registry-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(hosts: list[str]) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for host in hosts:
        try:
            with opener.open("http://" + host + "/v2/", timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Managed registry is not reachable over HTTP: {host}") from exc
        if status not in {200, 401}:
            raise RuntimeError(f"Managed registry /v2/ probe failed: {host} HTTP {status}")


def _parse_system_proxy(output: str) -> dict[str, object]:
    """Parse scutil's typed tree, retaining scope boundaries and duplicate checks."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    position = 0

    def container(kind: str):
        nonlocal position
        result = {}
        while position < len(lines):
            line = lines[position]
            position += 1
            if line == "}":
                if kind == "array":
                    if list(result) != [str(index) for index in range(len(result))]:
                        raise ValueError("invalid array")
                    return list(result.values())
                return result
            key, separator, value = line.partition(" : ")
            if not separator or not key or key in result:
                raise ValueError("invalid field")
            if value in {"<dictionary> {", "<array> {"}:
                value = container("array" if value == "<array> {" else "dictionary")
            elif value.startswith("<") or value.endswith("{"):
                raise ValueError("unsupported value")
            result[key] = value
        raise ValueError("unclosed container")

    try:
        if not lines or lines[0] != "<dictionary> {":
            raise ValueError("missing dictionary")
        position = 1
        result = container("dictionary")
        if position != len(lines):
            raise ValueError("trailing output")
    except ValueError as exc:
        raise RuntimeError("Cannot interpret macOS system proxy settings; maintenance is required") from exc
    return result


def _proxy_host_valid(host: object) -> bool:
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return "%" not in host
    except ValueError:
        if re.fullmatch(r"[0-9.]+", host):
            return False
        return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in host.rstrip(".").split("."))


def _system_proxy_state(settings: dict[str, object]) -> tuple[bool, list[str]]:
    # Scope- or destination-dependent decisions cannot be inferred from a
    # global flag, and PAC/WPAD evaluation is deliberately outside this owner.
    for key in ("__SCOPED__", "__SUPPLEMENTAL__"):
        if key in settings and settings[key] not in ({}, []):
            raise RuntimeError("Scoped macOS proxy settings require manual maintenance")
    supported_flags = {"HTTPEnable", "HTTPSEnable", "SOCKSEnable"}
    for key, value in settings.items():
        if key.endswith("Enable"):
            if value not in ("0", "1"):
                raise RuntimeError("macOS proxy enable flags are invalid; maintenance is required")
            if value == "1" and key not in supported_flags:
                raise RuntimeError("PAC, WPAD, or unsupported macOS proxy settings require manual maintenance")
    if settings.get("ProxyAutoDiscoveryEnable", "0") != "0" or settings.get("ProxyAutoConfigEnable", "0") != "0":
        raise RuntimeError("PAC or WPAD macOS proxy settings require manual maintenance")
    allowed = {"ExceptionsList", "ExcludeSimpleHostnames", "__SCOPED__", "__SUPPLEMENTAL__",
               "HTTPEnable", "HTTPProxy", "HTTPPort", "HTTPSEnable", "HTTPSProxy", "HTTPSPort",
               "SOCKSEnable", "SOCKSProxy", "SOCKSPort", "ProxyAutoConfigEnable", "ProxyAutoConfigURLString",
               "ProxyAutoDiscoveryEnable", "FTPEnable", "FTPProxy", "FTPPort", "FTPPassive",
               "GopherEnable", "GopherProxy", "GopherPort", "RTSPEnable", "RTSPProxy", "RTSPPort"}
    if set(settings) - allowed:
        raise RuntimeError("Unrecognized macOS proxy settings require manual maintenance")
    if settings.get("ExcludeSimpleHostnames", "0") not in ("0", "1"):
        raise RuntimeError("macOS simple hostname proxy setting is invalid; maintenance is required")
    active = False
    for protocol in ("HTTP", "HTTPS", "SOCKS"):
        if settings.get(protocol + "Enable", "0") == "1":
            host, port = settings.get(protocol + "Proxy"), settings.get(protocol + "Port")
            if (not _proxy_host_valid(host)
                    or not isinstance(port, str) or not port.isdigit() or not 1 <= int(port) <= 65535):
                raise RuntimeError("macOS manual proxy settings are incomplete; maintenance is required")
            active = True
    exclusions = settings.get("ExceptionsList", [])
    if not isinstance(exclusions, list) or any(not isinstance(item, str) or "," in item for item in exclusions):
        raise RuntimeError("macOS proxy exceptions are invalid; maintenance is required")
    return active, exclusions


def _native_proxy_value(key: str) -> str:
    output = _run([str(ORBCTL), "config", "get", key], operation="OrbStack proxy configuration inspection")
    # Empty exclude is valid; empty mode is not. Neither command output nor
    # custom proxy URLs may be included in errors or public task receipts.
    value = output.strip()
    if "\n" in value or "\r" in value or "\x00" in value:
        raise RuntimeError("OrbStack proxy configuration has an unsupported shape; maintenance is required")
    return value


def _proxy_state(config: dict[str, object]) -> dict[str, object]:
    engine = config.get("proxies", {})
    if not isinstance(engine, dict) or engine:
        raise RuntimeError("Custom Docker engine proxy settings require manual maintenance with their configuration owner")
    mode = _native_proxy_value("network_proxy")
    exclusions = _native_proxy_value("network.proxy.exclude")
    system = None
    system_exclusions = []
    if mode == "none":
        active = False
    elif mode == "auto":
        system = _parse_system_proxy(_run(["/usr/sbin/scutil", "--proxy"], operation="macOS system proxy inspection"))
        active, system_exclusions = _system_proxy_state(system)
    else:
        try:
            parsed = urllib.parse.urlsplit(mode)
            valid = (parsed.scheme in {"http", "https", "socks5", "socks5h"} and _proxy_host_valid(parsed.hostname)
                     and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                     and not re.search(r"\s", mode)
                     and re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", parsed.netloc.rsplit("@", 1)[-1])
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("Unknown OrbStack proxy mode; maintenance is required")
        active = True
    return {"mode": mode, "exclusions": exclusions, "system": system,
            "active": active, "systemExclusions": system_exclusions}


def _proxy_ready(state: dict[str, object], hosts: list[str]) -> bool:
    if not state["active"]:
        return True
    for host in hosts:
        if no_proxy_bypasses_registry_hosts(state["exclusions"], [host]):
            continue
        hostname = urllib.parse.urlsplit("http://" + host).hostname.lower()
        # macOS exception hostnames have different semantics from NO_PROXY:
        # a bare name matches only itself; a *. prefix covers subdomains.
        # Unrecognized patterns are retained, but never used as bypass proof.
        if not any(exception.lower() == hostname or
                   (exception.startswith("*.") and _proxy_host_valid(exception[2:]) and
                    hostname.endswith(exception[1:].lower()))
                   for exception in state["systemExclusions"]):
            return False
    return True


def _settings_content(path: Path, owner_uid: int) -> tuple[str | None, int]:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != owner_uid or path.stat().st_size > 1024 * 1024)):
        raise RuntimeError("OrbStack Docker settings must be a regular file owned by its socket owner")
    return (path.read_text(encoding="utf-8") if path.exists() else None,
            stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)


def _read_settings(path: Path, owner_uid: int) -> tuple[str | None, int, dict[str, object]]:
    original, mode = _settings_content(path, owner_uid)
    try:
        config = json.loads(original, parse_constant=_reject_json_constant, object_pairs_hook=_unique_object) if original is not None else {}
    except ValueError as exc:
        raise RuntimeError("OrbStack Docker settings contain invalid JSON; existing settings were preserved") from exc
    if not isinstance(config, dict):
        raise RuntimeError("OrbStack Docker settings must contain a JSON object")
    return original, mode, config


def _assert_owner(socket: Path, owner_uid: int) -> None:
    owner, current = _socket_owner()
    if current != socket or owner.pw_uid != owner_uid or os.geteuid() != owner_uid:
        raise RuntimeError("OrbStack socket ownership changed during registry setup")


def _apply(hosts: list[str], *, docker: str, expected_socket: Path, check_only: bool = False) -> dict[str, object]:
    owner, socket = _socket_owner()
    if socket != expected_socket or os.geteuid() != owner.pw_uid:
        raise RuntimeError("OrbStack socket ownership changed before registry setup")
    home = Path(owner.pw_dir).resolve(strict=True)
    directory = home / ".orbstack/config"
    path = directory / "docker.json"
    if directory.is_symlink() or (directory.exists() and directory.resolve(strict=True) != directory) or path.is_symlink():
        raise RuntimeError("OrbStack Docker settings must not use symbolic links")
    if directory.exists() and directory.stat().st_uid != owner.pw_uid:
        raise RuntimeError("OrbStack Docker settings directory has a different owner")
    if check_only:
        # A read-only manager never creates even a lock/config file.
        return _apply_settings(hosts, docker=docker, socket=socket, path=path, owner_uid=owner.pw_uid, check_only=True)
    with _settings_lock(directory, owner.pw_uid):
        return _apply_settings(hosts, docker=docker, socket=socket, path=path, owner_uid=owner.pw_uid)


def _apply_settings(hosts: list[str], *, docker: str, socket: Path, path: Path, owner_uid: int,
                    check_only: bool = False) -> dict[str, object]:
    info = _docker_info(docker, socket)
    original, mode, config = _read_settings(path, owner_uid)
    declared = config.get("insecure-registries", [])
    if not isinstance(declared, list) or any(not isinstance(host, str) for host in declared):
        raise RuntimeError("OrbStack insecure-registries must be an array of strings")
    state = _proxy_state(config)
    proxy_ready = _proxy_ready(state, hosts)
    if check_only and (not _transport_ready(info, hosts) or not proxy_ready):
        raise RuntimeError("Managed HTTP registry transport or proxy bypass is not configured; maintenance is required because this node is read-only for Docker configuration")
    config_changed = not check_only and any(host not in declared for host in hosts)
    needs_restart = not check_only and (config_changed or not _transport_ready(info, hosts))
    proxy_changed = not proxy_ready
    candidate = original
    expected_state = state
    if needs_restart or proxy_changed:
        _assert_owner(socket, owner_uid)
        _assert_idle(docker, socket)
        if _read_settings(path, owner_uid)[0] != original or _proxy_state(config) != state:
            raise RuntimeError("OrbStack settings changed during setup; retry after the edit completes")
        new_exclusions = state["exclusions"]
        if proxy_changed:
            missing = [host for host in hosts if not _proxy_ready(state, [host])]
            new_exclusions = ",".join([item for item in [state["exclusions"], *missing] if item])
            expected_state = {**state, "exclusions": new_exclusions}
        restart_started = False
        config_written = False
        try:
            if proxy_changed:
                _run([str(ORBCTL), "config", "set", "network.proxy.exclude", new_exclusions], operation="OrbStack managed registry proxy exclusion update")
                if _proxy_state(config) != expected_state:
                    raise RuntimeError("OrbStack proxy exclusions did not read back as requested; readiness is unverified")
            if config_changed:
                if proxy_changed:
                    _assert_idle(docker, socket)
                if _read_settings(path, owner_uid)[0] != original or _proxy_state(config) != expected_state:
                    raise RuntimeError("OrbStack settings changed during setup; retry after the edit completes")
                backup = path.with_name(path.name + ".luma-registry.bak")
                if backup.is_symlink():
                    raise RuntimeError("OrbStack registry backup must not be a symbolic link")
                if original is not None:
                    _atomic_write(backup, original, mode=mode)
                config["insecure-registries"] = list(dict.fromkeys([*declared, *hosts]))
                candidate = json.dumps(config, indent=2) + "\n"
                _atomic_write(path, candidate, mode=mode)
                config_written = True
            # Native proxy preferences can apply immediately. Require an idle
            # owner for those writes too, and recheck before a Docker restart.
            _assert_owner(socket, owner_uid)
            _assert_idle(docker, socket)
            if _read_settings(path, owner_uid)[0] != candidate or _proxy_state(config) != expected_state:
                raise RuntimeError("OrbStack settings changed during setup; retry after the edit completes")
            if needs_restart:
                restart_started = True
                _run([str(ORBCTL), "restart", "docker"], operation="OrbStack Docker restart", timeout=120)
        except (RuntimeError, OSError):
            if not restart_started:
                # Restore only our own candidates. Concurrent operator edits
                # have priority and are never replaced during failure cleanup.
                if config_written and _settings_content(path, owner_uid)[0] == candidate:
                    if original is None:
                        path.unlink()
                    else:
                        _atomic_write(path, original, mode=mode)
                if proxy_changed and _native_proxy_value("network.proxy.exclude") == new_exclusions:
                    _run([str(ORBCTL), "config", "set", "network.proxy.exclude", state["exclusions"]], operation="OrbStack proxy exclusion restoration")
                    if _native_proxy_value("network.proxy.exclude") != state["exclusions"]:
                        raise RuntimeError("OrbStack proxy exclusion restoration could not be verified; maintenance is required") from None
            raise
        if needs_restart:
            deadline = time.monotonic() + 45
            while True:
                try:
                    info = _docker_info(docker, socket)
                    if _transport_ready(info, hosts):
                        break
                except RuntimeError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("OrbStack Docker did not activate managed HTTP registry transport after restart; settings were saved but readiness is unverified")
                time.sleep(0.5)
    _probe(hosts)
    _assert_owner(socket, owner_uid)
    if _read_settings(path, owner_uid)[0] != candidate or _proxy_state(config) != expected_state:
        raise RuntimeError("OrbStack settings changed during verification; readiness is unverified")
    return _result(hosts, changed=needs_restart or proxy_changed, docker_restarted=needs_restart)


def _result(hosts: list[str], *, changed: bool, docker_restarted: bool) -> dict[str, object]:
    return {"registries": hosts, "runtime": "orbstack", "verified": True,
            "changed": changed, "dockerRestarted": docker_restarted,
            "affectedAllocationIds": [], "message": "Managed HTTP registry transport verified: " + ", ".join(hosts)}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        result = _apply(request["registries"], docker=request["docker"], expected_socket=Path(request["socket"]),
                        check_only=request.get("checkOnly", False))
    except Exception as exc:
        print(RESULT_PREFIX + json.dumps({"verified": False, "error": str(exc)}))
        raise SystemExit(1)
    print(RESULT_PREFIX + json.dumps(result))
