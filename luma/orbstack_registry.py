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
import os
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__:
    from .registry_proxy import proxy_bypasses_registry_hosts


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
    source = "from __future__ import annotations\n" + inspect.getsource(proxy_bypasses_registry_hosts) + "\n" + source
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


def _apply(hosts: list[str], *, docker: str, expected_socket: Path, check_only: bool = False) -> dict[str, object]:
    owner, socket = _socket_owner()
    if socket != expected_socket or os.geteuid() != owner.pw_uid:
        raise RuntimeError("OrbStack socket ownership changed before registry setup")
    info = _docker_info(docker, socket)
    if not proxy_bypasses_registry_hosts(info, hosts):
        raise RuntimeError("OrbStack Docker proxy does not bypass the managed registry; configure the runtime's proxy exclusions in a maintenance window")
    if check_only:
        if not _transport_ready(info, hosts):
            raise RuntimeError("Managed HTTP registry transport is not configured; maintenance is required because this node is read-only for Docker configuration")
        _probe(hosts)
        return _result(hosts, changed=False)
    home = Path(owner.pw_dir).resolve(strict=True)
    directory = home / ".orbstack/config"
    path = directory / "docker.json"
    # Reject redirected settings. The child UID is also the security boundary
    # if the user changes a directory after this check.
    if directory.resolve(strict=True) != directory or path.is_symlink():
        raise RuntimeError("OrbStack Docker settings must not use symbolic links")
    if directory.stat().st_uid != owner.pw_uid:
        raise RuntimeError("OrbStack Docker settings directory has a different owner")
    with _settings_lock(directory, owner.pw_uid):
        return _apply_settings(hosts, docker=docker, socket=socket, path=path, owner_uid=owner.pw_uid)


def _apply_settings(hosts: list[str], *, docker: str, socket: Path, path: Path, owner_uid: int) -> dict[str, object]:
    info = _docker_info(docker, socket)
    if not proxy_bypasses_registry_hosts(info, hosts):
        raise RuntimeError("OrbStack Docker proxy does not bypass the managed registry; configure the runtime's proxy exclusions in a maintenance window")
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != owner_uid or path.stat().st_size > 1024 * 1024)):
        raise RuntimeError("OrbStack Docker settings must be a regular file owned by its socket owner")
    original = path.read_text(encoding="utf-8") if path.exists() else None
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    try:
        config = json.loads(original, parse_constant=_reject_json_constant, object_pairs_hook=_unique_object) if original is not None else {}
    except ValueError as exc:
        raise RuntimeError("OrbStack Docker settings contain invalid JSON; existing settings were preserved") from exc
    if not isinstance(config, dict):
        raise RuntimeError("OrbStack Docker settings must contain a JSON object")
    declared = config.get("insecure-registries", [])
    if not isinstance(declared, list) or any(not isinstance(host, str) for host in declared):
        raise RuntimeError("OrbStack insecure-registries must be an array of strings")
    changed = any(host not in declared for host in hosts)
    needs_restart = changed or not _transport_ready(info, hosts)
    if needs_restart:
        if not ORBCTL.is_file():
            raise RuntimeError("OrbStack orbctl was not found; cannot apply Docker registry settings")
        _assert_idle(docker, socket)
        # Do not overwrite concurrent operator edits while collecting facts.
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != original:
            raise RuntimeError("OrbStack Docker settings changed during setup; retry after the edit completes")
        if changed:
            backup = path.with_name(path.name + ".luma-registry.bak")
            if backup.is_symlink():
                raise RuntimeError("OrbStack registry backup must not be a symbolic link")
            if original is not None:
                _atomic_write(backup, original, mode=mode)
            config["insecure-registries"] = list(dict.fromkeys([*declared, *hosts]))
            candidate = json.dumps(config, indent=2) + "\n"
            _atomic_write(path, candidate, mode=mode)
        # A second check narrows the scheduling race. It is not a promise of
        # zero downtime: OrbStack exposes restart, not a supported reload API.
        try:
            _assert_idle(docker, socket)
        except RuntimeError:
            if changed and path.read_text(encoding="utf-8") == candidate:
                if original is None:
                    path.unlink()
                else:
                    _atomic_write(path, original, mode=mode)
            raise
        _run([str(ORBCTL), "restart", "docker"], operation="OrbStack Docker restart", timeout=120)
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
    if not proxy_bypasses_registry_hosts(info, hosts):
        raise RuntimeError("OrbStack Docker proxy does not bypass the managed registry after restart")
    _probe(hosts)
    return _result(hosts, changed=needs_restart)


def _result(hosts: list[str], *, changed: bool) -> dict[str, object]:
    return {"registries": hosts, "runtime": "orbstack", "verified": True,
            "changed": changed, "dockerRestarted": changed,
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
