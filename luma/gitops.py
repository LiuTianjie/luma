from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import LumaError


def commit(paths: list[Path], message: str) -> str:
    try:
        subprocess.run(["git", "add", *[str(path) for path in paths]], check=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    if result.returncode != 0:
        output = result.stdout.strip()
        if "nothing to commit" in output:
            return "Git commit skipped: nothing to commit"
        raise LumaError(f"git commit failed:\n{output}")
    return result.stdout.strip().splitlines()[-1]


def push() -> str:
    try:
        result = subprocess.run(
            ["git", "push"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise LumaError("git is not installed") from exc
    if result.returncode != 0:
        raise LumaError(f"git push failed:\n{result.stdout.strip()}")
    return "Git push complete"
