from __future__ import annotations

from pathlib import PurePosixPath

from .errors import LumaError


def normalize_repo_relative_path(value: str, *, label: str) -> str:
    """Validate a canonical POSIX path relative to a cloned repository."""

    candidate = str(value)
    if (
        not candidate
        or candidate != candidate.strip()
        or len(candidate) > 1024
        or "\\" in candidate
        or any(character in candidate for character in ("\0", "\n", "\r"))
    ):
        raise LumaError(f"{label} must be a normalized repository-relative path")
    raw_parts = candidate.split("/")
    path = PurePosixPath(candidate)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.as_posix() != candidate
    ):
        raise LumaError(f"{label} must be a normalized repository-relative path")
    return candidate
