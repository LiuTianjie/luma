"""Compatibility shim delegating to the canonical ``luma.runner_http`` module."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from luma.runner_http import interruptible_urlopen
except ImportError:
    _repo_root = Path(__file__).resolve().parents[2]
    if (_repo_root / "luma" / "runner_http.py").is_file() and str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
        from luma.runner_http import interruptible_urlopen
    else:
        raise ImportError(
            "luma_build.runner_http requires the 'luma-infra' package (install with: pip install luma-infra)"
        ) from None

__all__ = ["interruptible_urlopen"]
