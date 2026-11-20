"""Thin compatibility wrapper delegating to the canonical ``luma.builder_runner``."""
from __future__ import annotations

import contextlib
import subprocess  # noqa: F401 - exposed for unittest.mock.patch("luma_build.runner.subprocess.*")
import sys
import urllib.request  # noqa: F401 - exposed for unittest.mock.patch("luma_build.runner.urllib.*")
from pathlib import Path
from typing import Any

from .errors import BuildCanceled, BuildError

try:
    from luma import build_sources as _build_sources
    from luma import builder_runner as _canonical
    from luma.errors import LumaError
except ImportError:
    _repo_root = Path(__file__).resolve().parents[2]
    if (_repo_root / "luma" / "builder_runner.py").is_file() and str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
        from luma import build_sources as _build_sources
        from luma import builder_runner as _canonical
        from luma.errors import LumaError
    else:
        raise BuildError(
            "luma-build runner requires the 'luma-infra' package (install with: pip install luma-infra)"
        ) from None


class PlatformRequeueRequired(BuildError):
    """Raised when the cloned repository manifest targets a different native runner platform."""

    def __init__(self, platforms: list[str]) -> None:
        self.platforms = platforms
        super().__init__(f"manifest requires native {', '.join(platforms)} runner")


@contextlib.contextmanager
def _translate_errors():
    try:
        yield
    except _canonical.PlatformRequeueRequired as exc:
        raise PlatformRequeueRequired(exc.platforms) from exc
    except _canonical._RunnerCanceled as exc:
        raise BuildCanceled(str(exc)) from exc
    except LumaError as exc:
        raise BuildError(str(exc)) from exc


def _wrap(fn):
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with _forwarded_patches(), _translate_errors():
            return fn(*args, **kwargs)

    _wrapped.__name__ = fn.__name__
    _wrapped.__doc__ = fn.__doc__
    return _wrapped


_render_buildkitd_toml = _wrap(_canonical._render_buildkitd_toml)
_url_bypasses_proxy = _wrap(_canonical._url_bypasses_proxy)
_write_docker_config = _wrap(_canonical._write_docker_config)
_discover_manifest = _wrap(_canonical._discover_manifest)
_resolve_post_clone_platforms = _wrap(_canonical._resolve_post_clone_platforms)
_check_runner_platform_compatibility = _wrap(_canonical._check_runner_platform_compatibility)
find_docker_binary = _wrap(_canonical.find_docker_binary)
capture_docker_runtime = _wrap(_canonical.capture_docker_runtime)
detect_native_engine_platform = _wrap(_canonical.detect_native_engine_platform)
_clone_repo = _wrap(_canonical._clone_repo)
_ensure_runner_buildx = _wrap(_canonical._ensure_runner_buildx)
_run_buildx_command = _wrap(_canonical._run_buildx_command)
execute_build_task = _wrap(_canonical.execute_build_task)

_PATCHABLE = {
    "find_docker_binary": find_docker_binary,
    "capture_docker_runtime": capture_docker_runtime,
    "detect_native_engine_platform": detect_native_engine_platform,
    "_clone_repo": _clone_repo,
    "_ensure_runner_buildx": _ensure_runner_buildx,
    "_run_buildx_command": _run_buildx_command,
    "execute_build_task": execute_build_task,
}


@contextlib.contextmanager
def _forwarded_patches():
    this = sys.modules[__name__]
    originals: dict[str, Any] = {}
    orig_gitops_clone = None
    for name, default_fn in _PATCHABLE.items():
        current = getattr(this, name)
        if current is not default_fn:
            originals[name] = getattr(_canonical, name)
            setattr(_canonical, name, current)
            if name == "_clone_repo":
                orig_gitops_clone = _build_sources.gitops.clone

                def _bridge_clone(
                    url,
                    dest,
                    *,
                    ref=None,
                    proxy=None,
                    token=None,
                    username=None,
                    strict_https_origin=False,
                    _override=current,
                ):
                    auth = {"username": username or "x-access-token", "token": token} if token else None
                    _override(repo_url=url, ref=ref or "main", dest=dest, git_auth=auth, proxy=proxy or "")
                    return f"Cloned {url}"

                _build_sources.gitops.clone = _bridge_clone
    try:
        yield
    finally:
        if orig_gitops_clone is not None:
            _build_sources.gitops.clone = orig_gitops_clone
        for name, orig in originals.items():
            setattr(_canonical, name, orig)


def _prepare_git_contexts(src_dir: Path, build_spec: Any, *, primary_repo_url: str, **kwargs: Any) -> dict[str, Path]:
    with _forwarded_patches(), _translate_errors():
        return _canonical._prepare_runner_git_contexts(
            src_dir, build_spec, repo_url=primary_repo_url, **kwargs
        )


class RunnerDaemon(_canonical.RunnerDaemon):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        with _forwarded_patches(), _translate_errors():
            super().__init__(*args, **kwargs)

    def run_once_or_forever(self, *args: Any, **kwargs: Any) -> int:
        with _forwarded_patches(), _translate_errors():
            return super().run_once_or_forever(*args, **kwargs)

    def _run_leased_task(self, task: dict[str, Any]) -> None:
        with _forwarded_patches():
            super()._run_leased_task(task)
