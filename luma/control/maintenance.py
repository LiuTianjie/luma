"""Offline-friendly Control backup, integrity verification and fresh-host restore.

This protects Control state, not Nomad Raft, application volumes, ACME or the
rest of /opt/luma. The SQLite backup API includes committed WAL transactions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from .. import __version__
from ..errors import LumaError
from . import database
from .state import state_dir

MANIFEST = "backup-manifest.json"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(destination: Path) -> dict:
    destination = Path(destination).absolute()
    if destination.exists():
        raise LumaError("backup destination already exists")
    root = state_dir().resolve()
    if root == destination.parent or root in destination.parents:
        raise LumaError("write the backup outside the Control state directory")
    database.ensure_initialized()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="luma-control-backup-") as temporary:
        stage = Path(temporary)
        database.backup_database(stage / database.DATABASE_FILE)
        files = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise LumaError("Control state contains a symlink; backup requires regular files")
            if path.is_dir():
                continue
            name = path.name
            if name in {MANIFEST, database.DATABASE_FILE, database.DATABASE_FILE + ".manifest.json"} or name.startswith(database.DATABASE_FILE + "-") or name.endswith((".lock", ".tmp")):
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise LumaError("Control state contains a non-regular file")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o600)
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                files[path.relative_to(stage).as_posix()] = {"bytes": path.stat().st_size, "sha256": _digest(path)}
        manifest = {"format": 1, "version": __version__, "createdAt": int(time.time()), "scope": "control-state", "files": files}
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                with tarfile.open(fileobj=output, mode="w:gz") as archive:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            archive.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    result = {"path": str(destination), "scope": "control-state", "files": len(files), "bytes": destination.stat().st_size, "sha256": _digest(destination)}
    with database.transaction() as conn:
        conn.execute("INSERT INTO database_meta(key,value) VALUES('last_backup_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(int(time.time())),))
    return result


def restore(archive_path: Path, destination: Path) -> dict:
    """Restore into a new directory only. Never replace a live database."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise LumaError("restore destination must not exist; stop Control and choose a fresh directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".luma-restore-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or MANIFEST not in names:
                raise LumaError("invalid backup manifest or duplicate entries")
            for member in members:
                relative = PurePosixPath(member.name)
                if not member.isfile() or relative.is_absolute() or ".." in relative.parts or "\\" in member.name:
                    raise LumaError("backup contains unsafe archive entries")
                target = stage.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise LumaError("backup file is missing")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o600)
        manifest = json.loads((stage / MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or manifest.get("scope") != "control-state" or not isinstance(manifest.get("files"), dict):
            raise LumaError("unsupported Control backup format")
        expected = manifest["files"]
        if set(names) != set(expected) | {MANIFEST} or database.DATABASE_FILE not in expected:
            raise LumaError("backup contents do not match manifest")
        for name, info in expected.items():
            path = stage / name
            if path.stat().st_size != info["bytes"] or _digest(path) != info["sha256"]:
                raise LumaError("backup integrity verification failed")
        check = database.check_database(stage / database.DATABASE_FILE)
        if not check["ok"] or check["schemaVersion"] > database.SCHEMA_VERSION:
            raise LumaError("restored database failed integrity or schema compatibility checks")
        # A completed restore is published only after all checks, on the same
        # filesystem. No existing destination can be silently overwritten.
        stage.chmod(0o700)
        if destination.exists():
            raise LumaError("restore destination appeared during verification")
        os.rename(stage, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"destination": str(destination), "scope": "control-state", "files": len(expected), "database": check}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup", help="Create a private Control-state archive including a consistent SQLite snapshot")
    save.add_argument("output", type=Path)
    load = commands.add_parser("restore", help="Verify and restore a Control-state archive into a NEW directory")
    load.add_argument("archive", type=Path)
    load.add_argument("--destination", required=True, type=Path)
    check = commands.add_parser("check", help="Read SQLite integrity and schema status")
    check.add_argument("--database", type=Path)
    commands.add_parser("status", help="Show current database integrity and entity counts")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup(args.output)
        elif args.command == "restore":
            result = restore(args.archive, args.destination)
        else:
            result = database.check_database(getattr(args, "database", None))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LumaError, OSError, ValueError, tarfile.TarError, KeyError, TypeError) as exc:
        parser.exit(1, f"Control maintenance failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
