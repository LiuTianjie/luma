"""Installation identity and dependency policy, usable before Luma dependencies exist.

This module deliberately uses only the standard library. The shell bootstrap and
an already installed CLI/agent share it; neither needs to infer root's PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

SCHEMA = "luma.installation/v1"
RECORD = "luma-installation.json"
LIFECYCLE = "lifecycle"
TARGET = "target.json"
PREVIOUS_SHIM = "previous-luma"
PREVIOUS_META = "previous.json"


def _absolute(value: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("installation path must be a string")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(c in value for c in "\r\n\0"):
        raise ValueError("installation paths must be absolute and normalized")
    return path


def dependency_policy(env: dict[str, str], previous: dict | None = None, *, validate_paths: bool = True) -> dict:
    previous = previous or {}
    index = env.get("LUMA_PIP_INDEX_URL", previous.get("indexUrl", "https://pypi.org/simple"))
    wheelhouse = env.get("LUMA_PIP_WHEELHOUSE", previous.get("wheelhouse", ""))
    ca = env.get("LUMA_PIP_CA_BUNDLE", previous.get("caBundle", ""))
    parts = urlsplit(index)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment or any(c.isspace() for c in index):
        raise ValueError("LUMA_PIP_INDEX_URL must be HTTPS without credentials, query or fragment")
    for name, value in (("wheelhouse", wheelhouse), ("caBundle", ca)):
        if value:
            _absolute(value)
            if validate_paths and not (Path(value).is_dir() if name == "wheelhouse" else Path(value).is_file()):
                raise ValueError(f"installation {name} does not exist")
    return {"indexUrl": index, "wheelhouse": wheelhouse, "caBundle": ca}


def pip_environment(env: dict[str, str], policy: dict) -> dict[str, str]:
    # Ignore both environment and global/user/site pip configuration. A dead
    # cloud-image pip.conf must not determine the managed runtime's dependencies.
    result = {k: v for k, v in env.items() if not k.startswith(("PIP_", "PYTHON"))}
    result.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1",
                  PIP_TIMEOUT="25", PIP_RETRIES="2")
    if policy.get("wheelhouse"):
        result.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=policy["wheelhouse"])
    else:
        result["PIP_INDEX_URL"] = policy["indexUrl"]
    if policy.get("caBundle"):
        # Requests' environment override otherwise takes precedence over the
        # explicit pip/session certificate setting.
        result.pop("REQUESTS_CA_BUNDLE", None)
        result.pop("CURL_CA_BUNDLE", None)
        result["PIP_CERT"] = policy["caBundle"]
    return result


def read_record(prefix: Path) -> dict | None:
    prefix = prefix.resolve()
    path = prefix / RECORD
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("installation record must be a regular, non-group/world-writable file")
    if prefix.stat().st_mode & 0o022:
        raise ValueError("installation runtime must not be group/world-writable")
    if info.st_uid != prefix.stat().st_uid:
        raise ValueError("installation record owner differs from its runtime")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or record.get("schemaVersion") != SCHEMA:
        raise ValueError("unsupported installation record schema")
    for key in ("installHome", "binDir", "userHome", "runtime", "source"):
        record[key] = str(_absolute(record.get(key)).resolve())
    if Path(record["runtime"]) != prefix:
        raise ValueError("installation record does not describe the running Python environment")
    root = Path(record["installHome"])
    if root not in prefix.parents and prefix != root:
        raise ValueError("installation runtime is outside its recorded root")
    if Path(record["source"]) != prefix.parent / "src":
        raise ValueError("installation source is not the runtime's paired source directory")
    if not isinstance(record.get("policy"), dict):
        raise ValueError("installation dependency policy must be an object")
    record["policy"] = dependency_policy({}, record["policy"], validate_paths=False)
    if record.get("ownerUid") != prefix.stat().st_uid:
        raise ValueError("installation owner identity differs from its runtime")
    return record


def runtime_record(prefix: Path | None = None) -> dict | None:
    """Inspect the actual venv first, never resolve its Python symlink to /usr/bin."""
    prefix = (prefix or Path(sys.prefix)).resolve()
    registered = read_record(prefix)
    if registered:
        return registered
    if not (prefix / "pyvenv.cfg").is_file():
        return None
    # Editable project environments remain development installations; don't
    # silently take them over via a node operation.
    if prefix.name == ".venv" and (prefix.parent / "pyproject.toml").exists():
        return None
    owner = pwd.getpwuid(prefix.stat().st_uid)
    if prefix.name == "venv" and prefix.parent.name == "luma" and prefix.parent.parent.name == "share" and prefix.parent.parent.parent.name == ".local":
        root = prefix.parent
        home = root.parent.parent.parent
        bindir = home / ".local/bin"
    else:
        # A legacy pip/venv install such as /opt/luma-cli remains its own root.
        root, home, bindir = prefix, Path(owner.pw_dir), prefix / "bin"
    return {
        "schemaVersion": SCHEMA, "installHome": str(root), "binDir": str(bindir),
        "userHome": str(home), "runtime": str(prefix), "source": str(root),
        "ownerUid": owner.pw_uid, "mode": "legacy-venv", "policy": {},
    }


def installer_environment(env: dict[str, str], record: dict | None) -> dict[str, str]:
    result = dict(env)
    if record:
        # A registered/running identity wins over ambient root HOME or stale
        # install overrides. Explicit relocation is not an ordinary update.
        for key, field in (("LUMA_USER_HOME", "userHome"), ("LUMA_INSTALL_HOME", "installHome"), ("LUMA_BIN_DIR", "binDir")):
            if result.get(key) and Path(result[key]).resolve() != Path(record[field]).resolve():
                raise ValueError(f"{key} conflicts with the running installation; relocation requires explicit adoption")
            result[key] = record[field]
        result["LUMA_PREVIOUS_RUNTIME"] = record["runtime"]
    policy = dependency_policy(result, (record or {}).get("policy"))
    result.update(LUMA_PIP_INDEX_URL=policy["indexUrl"], LUMA_PIP_WHEELHOUSE=policy["wheelhouse"], LUMA_PIP_CA_BUNDLE=policy["caBundle"])
    return result


def lifecycle_dir(install_home: Path) -> Path:
    path = _absolute(str(install_home)) / LIFECYCLE
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o077)
    except OSError:
        pass
    return path


def write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup_shim(bin_dir: Path, install_home: Path, *, previous_runtime: str = "") -> dict | None:
    """Copy the live command shim aside. Missing shim is a first install, not an error."""
    shim = _absolute(str(bin_dir)) / "luma"
    home = lifecycle_dir(install_home)
    if not shim.is_file():
        return None
    destination = home / PREVIOUS_SHIM
    shutil.copy2(shim, destination)
    os.chmod(destination, 0o755)
    meta = {"shim": str(shim), "previousRuntime": previous_runtime, "backedUpAt": int(time.time())}
    write_json(home / PREVIOUS_META, meta)
    return meta


def restore_shim(install_home: Path, bin_dir: Path) -> None:
    home = lifecycle_dir(install_home)
    previous = home / PREVIOUS_SHIM
    if not previous.is_file():
        raise ValueError("no previous Luma command shim is available to restore")
    shim = _absolute(str(bin_dir)) / "luma"
    shim.parent.mkdir(parents=True, exist_ok=True)
    temporary = shim.with_name(f".luma.rollback.{os.getpid()}")
    shutil.copy2(previous, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, shim)


def write_target(install_home: Path, *, runtime: Path, version: str, source: Path,
                 bin_dir: Path, previous_runtime: str = "") -> dict:
    record = {
        "runtime": str(_absolute(str(runtime)).resolve()),
        "version": version,
        "source": str(_absolute(str(source)).resolve()),
        "binDir": str(_absolute(str(bin_dir)).resolve()),
        "installHome": str(_absolute(str(install_home)).resolve()),
        "previousRuntime": previous_runtime,
    }
    write_json(lifecycle_dir(install_home) / TARGET, record)
    return record


def read_target(install_home: Path) -> dict | None:
    path = lifecycle_dir(install_home) / TARGET
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"lifecycle target record is unreadable: {exc}") from exc
    if not isinstance(data, dict) or not data.get("runtime"):
        raise ValueError("lifecycle target record is invalid")
    return data


def write_operation(install_home: Path, record: dict) -> dict:
    if not isinstance(record, dict) or not str(record.get("id") or "").strip():
        raise ValueError("lifecycle operation id is required")
    record = dict(record)
    home = lifecycle_dir(install_home)
    write_json(home / "current.json", {"id": record["id"]})
    write_json(home / "operations" / f"{record['id']}.json", record)
    return record


def read_operation(install_home: Path, operation_id: str) -> dict | None:
    path = lifecycle_dir(install_home) / "operations" / f"{operation_id}.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"lifecycle operation is unreadable: {exc}") from exc
    return data if isinstance(data, dict) else None


RELEASES = "releases"
# A managed candidate holds exactly these; anything else (an operator's config,
# notes, logs) means the directory is not ours to delete.
RELEASE_PAYLOAD = frozenset({"src", "venv"})
PRUNE_MIN_AGE_SECONDS = 600


def _release_names(text: str, roots: tuple[Path, ...]) -> set[str]:
    """Names of release directories referenced by absolute paths in ``text``."""
    found = set()
    for word in text.replace("=", " ").replace("'", " ").replace('"', " ").replace(":", " ").split():
        if not word.startswith("/"):
            continue
        path = Path(word)
        for candidate in (path, *path.parents):
            if candidate.parent in roots:
                found.add(candidate.name)
                break
    return found


def _running_release_names(roots: tuple[Path, ...]) -> set[str] | None:
    """Releases used by any live process, or None when processes can't be listed."""
    try:
        result = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return _release_names(result.stdout, roots)


def _file_release_names(path: Path, roots: tuple[Path, ...]) -> set[str]:
    try:
        return _release_names(path.read_text(), roots)
    except (OSError, UnicodeDecodeError):
        return set()


def prune_releases(install_home: Path, *, bin_dir: Path | None = None, keep_runtimes: tuple[str, ...] = (),
                   now: float | None = None) -> list[str]:
    """Remove superseded managed candidates.

    Keeps the active and rollback runtimes, every release a running process
    uses, recent candidates, and any directory holding more than src/venv.
    Removes nothing when the active runtime or running processes are unknown.
    """
    home = _absolute(str(install_home))
    releases = home / RELEASES
    if not releases.is_dir() or releases.is_symlink():
        return []
    roots = tuple(dict.fromkeys((releases, releases.resolve())))
    references = list(keep_runtimes)
    lifecycle = lifecycle_dir(home)
    for name in (TARGET, PREVIOUS_META):
        try:
            data = json.loads((lifecycle / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            references.extend(str(data.get(key) or "") for key in ("runtime", "source", "previousRuntime"))
    keep = _release_names(" ".join(references + [str(Path(ref).resolve()) for ref in references if ref.startswith("/")]), roots)
    keep |= _file_release_names(lifecycle / PREVIOUS_SHIM, roots)
    if bin_dir is not None:
        keep |= _file_release_names(_absolute(str(bin_dir)) / "luma", roots)
    running = _running_release_names(roots)
    if not keep or running is None:
        return []
    keep |= running
    now = time.time() if now is None else now
    removed = []
    for entry in sorted(releases.iterdir()):
        if entry.name in keep or not entry.name.startswith("candidate.") or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            names = {child.name for child in entry.iterdir()}
            if not names <= RELEASE_PAYLOAD or now - entry.stat().st_mtime < PRUNE_MIN_AGE_SECONDS:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue
        removed.append(entry.name)
    return removed


def write_record(prefix: Path, *, root: Path, bindir: Path, home: Path, source: Path,
                 policy: dict, version: str, previous_runtime: str = "") -> dict:
    for path in (prefix, root, bindir, home, source):
        _absolute(str(path))
    # Resolve directories, never the venv's Python executable symlink. macOS
    # /tmp and /var aliases otherwise disagree with Python's canonical prefix.
    prefix, root, bindir, home, source = (p.resolve() for p in (prefix, root, bindir, home, source))
    # venv inherits the operator's umask (commonly 0002 on Linux).
    # Normalize only the candidate runtime; never relax the reader's checks
    # or recursively change an existing installation.
    prefix.chmod(stat.S_IMODE(prefix.stat().st_mode) & ~0o022)
    record = {
        "schemaVersion": SCHEMA, "installationId": hashlib.sha256(str(root).encode()).hexdigest()[:24],
        "installHome": str(root), "binDir": str(bindir), "userHome": str(home),
        "runtime": str(prefix), "source": str(source), "ownerUid": prefix.stat().st_uid,
        "version": version, "mode": "managed", "policy": policy,
        "previousRuntime": previous_runtime,
    }
    fd, temporary = tempfile.mkstemp(prefix=".installation-", dir=prefix)
    try:
        with os.fdopen(fd, "w") as file:
            if os.geteuid() == 0:
                owner = prefix.stat()
                os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
            json.dump(record, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, prefix / RECORD)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Publication must satisfy the same trust checks used by the running
    # agent, not merely successful imports/version output.
    if read_record(prefix) != record:
        raise ValueError("prepared installation identity failed read-back validation")
    return record


def installation_diagnostics(env: dict[str, str] | None = None) -> dict:
    """No network, no secrets and no state writes. Service health is out of scope."""
    env = dict(os.environ) if env is None else env
    checks = []
    record = None
    policy = None
    try:
        record = runtime_record()
        policy_env = installer_environment(env, record)
        policy = dependency_policy(policy_env)
        checks.append({"name": "installation identity and dependency policy", "ok": True})
    except (ValueError, OSError) as exc:
        checks.append({"name": "installation identity and dependency policy", "ok": False,
                       "detail": str(exc)})
    # -I prevents an ambient PYTHONPATH from making a broken venv look healthy.
    try:
        result = subprocess.run([sys.executable, "-I", "-c",
            "import importlib, json, sys\n"
            "missing=[]\n"
            "for name in ('yaml', 'starlette', 'uvicorn', 'websockets', 'python_socks'):\n"
            " try: importlib.import_module(name)\n"
            " except Exception: missing.append(name)\n"
            "print(json.dumps(missing))\n"
            "sys.exit(bool(missing))"],
            env=pip_environment(env, policy or dependency_policy({})),
            capture_output=True, text=True, timeout=15)
        missing = []
        if result.returncode:
            try:
                reported = json.loads(result.stdout)
                missing = [name for name in reported if name in {"yaml", "starlette", "uvicorn", "websockets", "python_socks"}]
            except (ValueError, TypeError):
                pass
        checks.append({"name": "runtime dependency imports", "ok": result.returncode == 0,
                       "detail": "" if result.returncode == 0 else f"Missing or unloadable runtime modules: {', '.join(missing) or 'unknown'}. Prepare a new candidate from an approved package source."})
        result = subprocess.run([sys.executable, "-I", "-m", "pip", "check"],
            env=pip_environment(env, policy or dependency_policy({})),
            capture_output=True, text=True, timeout=15)
        checks.append({"name": "runtime dependency consistency", "ok": result.returncode == 0,
                       "detail": "" if result.returncode == 0 else "pip check failed; prepare a new candidate rather than editing the active runtime."})
    except (OSError, subprocess.TimeoutExpired):
        checks.append({"name": "runtime dependencies", "ok": False, "detail": "Local interpreter check failed or timed out."})
    return {"scope": "local-installation", "healthy": all(c["ok"] for c in checks),
            "interpreter": sys.executable, "runtime": sys.prefix,
            "installation": record, "dependencyPolicy": policy,
            "ignoredHostPipSettings": sorted(k for k in env if k.startswith("PIP_")),
            "checks": checks, "serviceHealthVerified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pip", "record", "shim", "backup-shim", "target", "restore-shim", "prune"))
    args, rest = parser.parse_known_args()
    env = dict(os.environ)
    try:
        policy = dependency_policy(env)
        if args.action == "pip":
            return subprocess.call([sys.executable, "-I", "-m", "pip", *rest], env=pip_environment(env, policy))
        if args.action == "backup-shim":
            backup_shim(Path(env["BIN_DIR"]), Path(env["INSTALL_HOME"]),
                        previous_runtime=env.get("LUMA_PREVIOUS_RUNTIME", ""))
            return 0
        if args.action == "restore-shim":
            restore_shim(Path(env["INSTALL_HOME"]), Path(env["BIN_DIR"]))
            return 0
        if args.action == "prune":
            removed = prune_releases(
                Path(env["INSTALL_HOME"]), bin_dir=Path(env["BIN_DIR"]),
                keep_runtimes=(str(Path(sys.prefix)), env.get("LUMA_PREVIOUS_RUNTIME", "")),
            )
            if removed:
                print(f"Removed {len(removed)} superseded Luma release(s)")
            return 0
        source = _absolute(env["SOURCE_DIR"])
        if args.action == "shim":
            # Quote paths as shell data, not interpolated shell program text.
            print("#!/usr/bin/env sh\nunset PYTHONHOME")
            print("export PYTHONNOUSERSITE=1")
            print("export PYTHONPATH=" + shlex.quote(str(source)))
            print("exec " + shlex.quote(str(Path(sys.prefix) / "bin/python")) + ' -m luma.cli "$@"')
            return 0
        version = next(line.split('"')[1] for line in (source / "luma/__init__.py").read_text().splitlines() if line.startswith('__version__ = "'))
        if args.action == "target":
            write_target(Path(env["INSTALL_HOME"]), runtime=Path(sys.prefix), version=version,
                         source=source, bin_dir=Path(env["BIN_DIR"]),
                         previous_runtime=env.get("LUMA_PREVIOUS_RUNTIME", ""))
            return 0
        write_record(Path(sys.prefix), root=Path(env["INSTALL_HOME"]), bindir=Path(env["BIN_DIR"]),
                     home=Path(env["LUMA_USER_HOME"]), source=source, policy=policy,
                     version=version, previous_runtime=env.get("LUMA_PREVIOUS_RUNTIME", ""))
    except (ValueError, KeyError, OSError, StopIteration) as exc:
        print(f"Luma installation preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
