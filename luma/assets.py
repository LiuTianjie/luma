from __future__ import annotations

import importlib.resources
from pathlib import Path


def asset_path(relative_path: str) -> Path:
    """Return a package asset path, falling back to the checkout root during development."""
    checkout_path = Path(relative_path)
    if checkout_path.exists():
        return checkout_path
    return Path(str(importlib.resources.files("luma") / "assets" / relative_path))


def asset_text(relative_path: str) -> str:
    return asset_path(relative_path).read_text(encoding="utf-8")
