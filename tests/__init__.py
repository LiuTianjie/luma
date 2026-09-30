"""Test package marker for stable cross-module test imports.

Tests must never read or modify the developer's real Luma settings or login
contexts, so both point at an empty per-run directory unless a test overrides
them explicitly.
"""
import atexit
import os
import shutil
import tempfile

_ISOLATED_HOME = tempfile.mkdtemp(prefix="luma-tests-")
atexit.register(shutil.rmtree, _ISOLATED_HOME, ignore_errors=True)
os.environ["LUMA_USER_CONFIG"] = os.path.join(_ISOLATED_HOME, "user-config.json")
os.environ["LUMA_CONFIG_HOME"] = os.path.join(_ISOLATED_HOME, "config")
# Control reads its cluster config from LUMA_CONTROL_CONFIG; give tests a fixed one.
os.environ.setdefault("LUMA_CONTROL_CONFIG", os.path.join(os.path.dirname(__file__), "fixtures", "cluster.yaml"))

# The CLI reads Luma settings from ./.env by design, and tests run from the
# repository root, where a developer's real .env may hold Cloudflare or Tailscale
# credentials. Tests that exercise implicit .env loading chdir to a temporary
# directory first, so only the checkout's own .env is ignored here.
import importlib as _importlib
from pathlib import Path as _Path

# luma.cli exports a main() function that shadows the submodule attribute.
_cli_main = _importlib.import_module("luma.cli.main")

_CHECKOUT = _Path(__file__).resolve().parents[1]
_load_luma_settings = _cli_main.load_luma_settings


def _load_luma_settings_outside_checkout(path):
    if _Path(path).resolve().parent == _CHECKOUT:
        return []
    return _load_luma_settings(path)


_cli_main.load_luma_settings = _load_luma_settings_outside_checkout
