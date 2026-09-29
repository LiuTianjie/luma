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
