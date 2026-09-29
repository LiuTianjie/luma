from __future__ import annotations

import importlib.resources
from pathlib import Path


def asset_path(relative_path: str) -> Path:
    """Return the path of a file packaged under luma/assets."""
    return Path(str(importlib.resources.files("luma") / "assets" / relative_path))
