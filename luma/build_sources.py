"""Pinned, provider-scoped Git inputs prepared outside the Docker build context.

The caller owns ``workspace`` and its successful prepared directories. Failed
preparations always remove their own output; checkout and credential helpers are
never part of the exported source. Cancellation is checked between bounded Git
operations (``gitops.clone`` does not interrupt an in-flight network command).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from . import gitops
from .errors import LumaError


EXTENSION = "x-luma-git-contexts"
MAX_CONTEXTS = 16
MAX_LOCK_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * MAX_ARCHIVE_BYTES
MAX_ARCHIVE_MEMBERS = 100_000
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESERVED_ALIASES = {"context", "dockerfile", "scratch"}


class GitContextCanceled(LumaError):
    """The caller canceled before an additional source could enter BuildKit."""


@dataclass(frozen=True)
class GitContextLock:
    repository: str
    commit: str
    archive_sha256: str


@dataclass(frozen=True)
class PreparedGitContext:
    path: Path
    repository: str
    commit: str
    archive_sha256: str


def _check_canceled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise GitContextCanceled("additional Git source preparation canceled")


def _relative_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise LumaError(f"{label} must be a relative POSIX path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LumaError(f"{label} contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise LumaError(f"{label} must stay inside its source root")
    if any(part.casefold() == ".git" for part in path.parts):
        raise LumaError(f"{label} must not reference Git metadata")
    return path


def validate_git_contexts_spec(build_spec: Mapping[str, object]) -> dict[str, str]:
    """Validate the extension shape without reading the source filesystem."""
    if not isinstance(build_spec, Mapping):
        raise LumaError("build must be a mapping")
    if EXTENSION not in build_spec:
        return {}
    contexts = build_spec[EXTENSION]
    if not isinstance(contexts, Mapping) or not contexts or len(contexts) > MAX_CONTEXTS:
        raise LumaError(f"build.{EXTENSION} must contain 1 to {MAX_CONTEXTS} named sources")
    result: dict[str, str] = {}
    for alias, spec in contexts.items():
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise LumaError("Git context alias must be a lowercase name of at most 63 characters")
        if alias in result or alias in _RESERVED_ALIASES:
            raise LumaError(f"Git context alias is duplicate or reserved: {alias}")
        if not isinstance(spec, Mapping) or set(spec) != {"lock"}:
            raise LumaError(f"Git context {alias} must declare only a lock path")
        result[alias] = str(_relative_path(spec["lock"], label=f"Git context {alias} lock"))
    return result


def _https_origin(repository: object) -> tuple[str, int]:
    if not isinstance(repository, str) or not repository:
        raise LumaError("Git context repository must be an HTTPS URL")
    if any(ord(character) <= 32 or ord(character) == 127 for character in repository):
        raise LumaError("Git context repository URL contains whitespace or a control character")
    try:
        parsed = urllib.parse.urlsplit(repository)
        port = parsed.port if parsed.port is not None else 443
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in repository
            or "#" in repository
            or "\\" in repository
            or not parsed.path.strip("/")
            or any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/"))
            or not 0 < port <= 65535
        ):
            raise ValueError("invalid repository URL")
    except ValueError as exc:
        raise LumaError("Git source must use an HTTPS repository URL without credentials, query or fragment") from exc
    return parsed.hostname.lower(), port


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def parse_git_contexts(
    repository_root: Path,
    build_spec: Mapping[str, object],
    *,
    repository_url: str,
) -> dict[str, GitContextLock]:
    """Read reviewed locks, permitting only the primary provider's HTTPS origin."""
    specs = validate_git_contexts_spec(build_spec)
    if not specs:
        return {}
    primary_origin = _https_origin(repository_url)
    root = Path(repository_root).resolve(strict=True)
    result: dict[str, GitContextLock] = {}
    for alias, relative in specs.items():
        try:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise LumaError(f"Git context {alias} lock must be a file inside the primary repository")
            with path.open("rb") as stream:
                content = stream.read(MAX_LOCK_BYTES + 1)
            if len(content) > MAX_LOCK_BYTES:
                raise LumaError(f"Git context {alias} lock exceeds the size limit")
            value = json.loads(content, object_pairs_hook=_unique_json_object)
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            raise LumaError(f"Git context {alias} lock is missing or invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"repository", "commit", "archive_sha256"}:
            raise LumaError(f"Git context {alias} lock must contain exactly repository, commit and archive_sha256")
        repository, commit, archive_sha256 = value["repository"], value["commit"], value["archive_sha256"]
        if _https_origin(repository) != primary_origin:
            raise LumaError(f"Git context {alias} must use the primary repository's HTTPS origin")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise LumaError(f"Git context {alias} requires a full lowercase Git commit")
        if not isinstance(archive_sha256, str) or not _SHA256.fullmatch(archive_sha256):
            raise LumaError(f"Git context {alias} requires a lowercase archive SHA256")
        result[alias] = GitContextLock(repository, commit, archive_sha256)
    return result


def _archive_source(repo: Path, archive: Path, commit: str) -> None:
    try:
        with archive.open("xb") as stream:
            result = subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar.gz", commit],
                stdout=stream,
                stderr=subprocess.PIPE,
                check=False,
                timeout=gitops.GIT_LOCAL_TIMEOUT,
            )
        if result.returncode != 0:
            # Do not echo Git output: this boundary never exposes transport or
            # provider details to the Docker build log.
            raise LumaError("unable to archive the pinned Git source")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LumaError("unable to archive the pinned Git source within the time limit") from exc


def _verify_archive(archive: Path, expected: str, cancel_event: threading.Event | None) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise LumaError("Git context archive exceeds the size limit")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        while True:
            _check_canceled(cancel_event)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise LumaError("Git context archive SHA256 does not match its reviewed lock")


def _extract_source(archive: Path, destination: Path, cancel_event: threading.Event | None) -> None:
    """Extract ordinary source files first, then confined relative symlinks.

    Never use extractall: both the write paths and the entire symlink graph are
    checked before returning any context to the caller.
    """
    try:
        with tarfile.open(archive, "r:gz") as source:
            members: dict[PurePosixPath, tarfile.TarInfo] = {}
            total_size = 0
            for member in source:
                _check_canceled(cancel_event)
                name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
                path = _relative_path(name, label="Git archive entry")
                if path in members:
                    raise LumaError("Git context archive contains a duplicate path")
                if not (member.isdir() or member.isreg() or member.issym()) or member.sparse:
                    raise LumaError("Git context archive contains an unsupported file type")
                if member.size < 0:
                    raise LumaError("Git context archive contains an invalid file size")
                total_size += member.size
                if len(members) >= MAX_ARCHIVE_MEMBERS or total_size > MAX_EXTRACTED_BYTES:
                    raise LumaError("Git context archive exceeds the extraction limit")
                if member.issym() and (
                    not member.linkname
                    or PurePosixPath(member.linkname).is_absolute()
                    or "\\" in member.linkname
                    or any(ord(character) < 32 for character in member.linkname)
                ):
                    raise LumaError("Git context archive contains an invalid symlink")
                members[path] = member
            for path in members:
                for parent in path.parents:
                    ancestor = members.get(parent)
                    if ancestor is not None and not ancestor.isdir():
                        raise LumaError("Git context archive writes through a non-directory entry")
            destination.mkdir(mode=0o755)
            for path, member in members.items():
                _check_canceled(cancel_event)
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                elif member.isreg():
                    with source.extractfile(member) as reader, target.open("xb") as writer:
                        while True:
                            _check_canceled(cancel_event)
                            chunk = reader.read(1024 * 1024)
                            if not chunk:
                                break
                            writer.write(chunk)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
            for path, member in members.items():
                _check_canceled(cancel_event)
                if member.issym():
                    destination.joinpath(*path.parts).symlink_to(member.linkname)
            root = destination.resolve(strict=True)
            for path, member in members.items():
                if member.issym():
                    _check_canceled(cancel_event)
                    target = destination.joinpath(*path.parts)
                    if not target.resolve(strict=False).is_relative_to(root):
                        raise LumaError("Git context symlink escapes its source root")
                    try:
                        # Python 3.13 resolve(strict=False) tolerates loops;
                        # stat still rejects ELOOP while allowing dangling
                        # internal links intended for generated source files.
                        target.stat()
                    except FileNotFoundError:
                        pass
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        raise LumaError("Git context archive cannot be safely extracted") from exc


def prepare_git_contexts(
    repository_root: Path,
    build_spec: Mapping[str, object],
    *,
    repository_url: str,
    workspace: Path,
    token: str | None = None,
    username: str | None = None,
    proxy: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, PreparedGitContext]:
    """Freeze validated additional sources in caller-owned sibling workspace.

    In-flight clone operations retain gitops' bounded network timeouts; a
    cancellation observed afterwards prevents archive/export and BuildKit use.
    """
    _check_canceled(cancel_event)
    locks = parse_git_contexts(repository_root, build_spec, repository_url=repository_url)
    if not locks:
        return {}
    root = Path(repository_root).resolve(strict=True)
    work = Path(workspace).resolve(strict=True)
    if not work.is_dir() or work.is_relative_to(root):
        raise LumaError("Git context workspace must be outside the primary repository")
    output = Path(tempfile.mkdtemp(prefix="luma-git-contexts-", dir=work))
    prepared: dict[str, PreparedGitContext] = {}
    try:
        for alias, lock in locks.items():
            _check_canceled(cancel_event)
            if progress:
                progress(f"Preparing pinned Git source {alias}")
            with tempfile.TemporaryDirectory(prefix="luma-git-fetch-", dir=work) as scratch:
                repo = Path(scratch) / "repository"
                archive = Path(scratch) / "source.tar.gz"
                gitops.clone(
                    lock.repository,
                    repo,
                    ref=lock.commit,
                    token=token,
                    username=username,
                    proxy=proxy,
                    strict_https_origin=True,
                )
                _check_canceled(cancel_event)
                if gitops.head_commit_full(repo) != lock.commit:
                    raise LumaError(f"Git context {alias} checkout does not match its pinned commit")
                _check_canceled(cancel_event)
                _archive_source(repo, archive, lock.commit)
                _check_canceled(cancel_event)
                _verify_archive(archive, lock.archive_sha256, cancel_event)
                path = output / alias
                _extract_source(archive, path, cancel_event)
            prepared[alias] = PreparedGitContext(path, lock.repository, lock.commit, lock.archive_sha256)
        _check_canceled(cancel_event)
        return prepared
    except BaseException as exc:
        shutil.rmtree(output)
        if isinstance(exc, LumaError):
            _check_canceled(cancel_event)
            if token:
                safe_message = str(exc).replace(token, "***")
                if safe_message != str(exc):
                    raise type(exc)(safe_message) from None
        raise
