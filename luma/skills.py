"""Install and refresh the skills paired with this CLI, preserving local edits in backups."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from . import __version__
from .errors import LumaError

MARKER = ".luma-managed.json"
PACKAGED = Path(__file__).parent / "assets" / "skills"


def bundle_root() -> Path:
    if PACKAGED.is_dir():
        return PACKAGED
    source = Path(__file__).resolve().parent.parent / "skills"
    if source.is_dir():
        return source
    raise LumaError("This Luma installation has no bundled skills; reinstall the matching release")


def _hashes(path: Path) -> dict[str, str]:
    result = {}
    for item in sorted(path.rglob("*")):
        if item.name == MARKER or "__pycache__" in item.parts:
            continue
        relative = str(item.relative_to(path))
        if item.is_symlink():
            result[relative] = "symlink:" + os.readlink(item)
        elif item.is_file():
            result[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        return {}
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (ValueError, OSError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".luma-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _config_root(user_home: Path | None = None) -> Path:
    if user_home is not None:
        return user_home / ".config" / "luma"
    return Path(os.environ.get("LUMA_CONFIG_HOME") or Path.home() / ".config" / "luma")


def agent_roots(user_home: Path | None = None) -> dict[str, Path]:
    home = user_home or Path.home()
    return {
        "codex": Path((os.environ.get("CODEX_HOME") if user_home is None else None) or home / ".codex") / "skills",
        "claude": Path((os.environ.get("CLAUDE_CONFIG_DIR") if user_home is None else None) or home / ".claude") / "skills",
    }


def _bundles() -> list[Path]:
    return sorted(path for path in bundle_root().iterdir()
                  if path.is_dir() and not path.is_symlink() and (path / "SKILL.md").is_file())


def installed_roots(user_home: Path | None = None) -> list[Path]:
    names = [path.name for path in _bundles()]
    roots = list(agent_roots(user_home).values())
    registered = _read_json(_config_root(user_home) / "skill-roots.json").get("roots", [])
    roots.extend(Path(value) for value in registered if isinstance(value, str) and Path(value).is_absolute())
    # Do not recreate a deleted suite, or add skills to an assistant that never used Luma.
    return sorted({root.absolute() for root in roots
                   if any((root / name).exists() or (root / name).is_symlink() for name in names)})


@contextmanager
def _root_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".luma-skills.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _sync_one(source: Path, root: Path, *, force: bool = False) -> dict:
    target = root / source.name
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise LumaError(f"Cannot update {target}: expected a regular skill directory; preserve or relocate this custom target")
    hashes = _hashes(source)
    marker = _read_json(target / MARKER)
    result = {"name": source.name, "path": str(target), "version": __version__, "status": "current"}
    if not force and marker.get("version") == __version__ and marker.get("files") == hashes:
        return result
    existing = _hashes(target) if target.exists() else {}
    # An unmanaged old installation is backed up unless it is already byte-identical.
    modified = target.exists() and existing != (marker.get("files") or hashes)
    with tempfile.TemporaryDirectory(prefix=".luma-skill-stage-", dir=root.parent) as temporary:
        stage = Path(temporary)
        candidate = stage / "candidate"
        shutil.copytree(source, candidate)
        _write_json(candidate / MARKER, {"schema": 1, "version": __version__, "files": hashes})
        had_previous = target.exists()
        previous = None
        if had_previous:
            backups = root.parent / "luma-skill-backups"
            if backups.is_symlink():
                raise LumaError(f"Skill backup directory must not be a symlink: {backups}")
            backups.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            previous = backups / f"{source.name}-{stamp}-{uuid.uuid4().hex[:8]}"
            target.rename(previous)
        try:
            candidate.rename(target)
        except BaseException:
            if previous is not None:
                try:
                    previous.rename(target)
                except OSError as exc:
                    # Never place the only old copy under TemporaryDirectory:
                    # even a failed rollback must leave it available for recovery.
                    raise LumaError(f"Skill replacement and rollback failed; previous files preserved at {previous}") from exc
            raise
        if previous is not None:
            if modified:
                result["backup"] = str(previous)
            else:
                try:
                    shutil.rmtree(previous)
                except OSError:
                    result["backup"] = str(previous)
    result["status"] = "updated" if had_previous else "installed"
    return result


def sync_skills(roots: list[Path] | None = None, *, user_home: Path | None = None,
                register: bool = False, force: bool = False) -> dict:
    roots = roots if roots is not None else installed_roots(user_home)
    items, warnings = [], []
    for root in roots:
        root = root.expanduser().absolute()
        try:
            with _root_lock(root):
                for source in _bundles():
                    try:
                        items.append(_sync_one(source, root, force=force))
                    except (OSError, LumaError) as exc:
                        warnings.append(str(exc))
        except (OSError, LumaError) as exc:
            warnings.append(str(exc))
    if register and roots:
        path = _config_root(user_home) / "skill-roots.json"
        old = _read_json(path).get("roots", [])
        _write_json(path, {"roots": sorted(set(old) | {str(p.expanduser().absolute()) for p in roots})})
    return {"version": __version__, "items": items, "warnings": warnings}


def skill_status() -> dict:
    items = []
    for root in installed_roots():
        for source in _bundles():
            target = root / source.name
            marker = _read_json(target / MARKER)
            hashes = _hashes(target) if target.is_dir() and not target.is_symlink() else {}
            expected = _hashes(source)
            items.append({
                "name": source.name, "path": str(target), "version": marker.get("version"),
                "paired": marker.get("version") == __version__ and marker.get("files") == expected,
                "modified": bool(marker) and hashes != marker.get("files"),
                "status": "missing" if not target.exists() else "symlink" if target.is_symlink() else "installed",
            })
    return {"version": __version__, "items": items}


def report_sync(result: dict) -> None:
    for item in result["items"]:
        if item["status"] != "current":
            print(f"luma: skill {item['status']}: {item['path']} (Luma {__version__})", file=sys.stderr)
        if item.get("backup"):
            print(f"luma: preserved previous skill files (including local changes): {item['backup']}", file=sys.stderr)
    for warning in result["warnings"]:
        print(f"luma: skill sync incomplete: {warning}; run 'luma skills sync' after resolving it", file=sys.stderr)


def auto_sync() -> None:
    # Source checkouts must not silently replace a user's release-paired skills.
    if not PACKAGED.is_dir():
        return
    try:
        report_sync(sync_skills())
    except (OSError, LumaError) as exc:
        print(f"luma: skill sync incomplete: {exc}", file=sys.stderr)


def main() -> int:
    """Installer hook: update existing suites as the installation owner, not root."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--user-home", type=Path, required=True)
    args = parser.parse_args()
    owner = os.environ.get("LUMA_INSTALL_OWNER", "")
    if os.geteuid() == 0:
        uid, gid = (map(int, owner.split(":")) if owner else
                    (args.user_home.stat().st_uid, args.user_home.stat().st_gid))
        if uid:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
    result = sync_skills(user_home=args.user_home)
    report_sync(result)
    return int(bool(result["warnings"]))


if __name__ == "__main__":
    raise SystemExit(main())
