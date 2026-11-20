from __future__ import annotations

import json
import os
import re
import shlex
import urllib.parse
import urllib.request
from collections.abc import Mapping

from .errors import LumaError

LUMA_INSTALLER_RAW_BASE = "https://raw.githubusercontent.com/LiuTianjie/luma"
LUMA_TAGS_URL = "https://api.github.com/repos/LiuTianjie/luma/tags?per_page=100"
LUMA_PYPI_URL = "https://pypi.org/pypi/luma-infra/json"
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_INSTALL_REF_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_install_ref(value: str) -> str:
    """Validate an installRef so it cannot traverse out of the trusted repository."""
    raw = str(value or "")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise LumaError("installRef must not contain control characters")
    ref = raw.strip()
    if not ref or len(ref) > 128:
        raise LumaError("installRef must be between 1 and 128 characters")
    if ".." in ref or "%" in ref or "\\" in ref or any(ch.isspace() for ch in ref):
        raise LumaError(f"invalid installRef: {ref!r}")
    segments = ref.split("/")
    if len(segments) > 4 or any(not _INSTALL_REF_SEGMENT.fullmatch(segment) for segment in segments):
        raise LumaError(f"invalid installRef: {ref!r}")
    return ref


def latest_release_ref(*, timeout: float = 15) -> str:
    """Return the newest vX.Y.Z tag.

    Tags are the release channel: each one publishes the PyPI package and the
    Control image. GitHub Releases are created only occasionally.
    """
    try:
        with urllib.request.urlopen(LUMA_TAGS_URL, timeout=timeout) as response:
            names = [str(item.get("name") or "") for item in json.load(response)]
        versions = [tuple(int(part) for part in match.groups()) for match in map(RELEASE_TAG.fullmatch, names) if match]
        if versions:
            return "v{}.{}.{}".format(*max(versions))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with urllib.request.urlopen(LUMA_PYPI_URL, timeout=timeout) as response:
            version = str(json.load(response)["info"]["version"])
        if RELEASE_TAG.fullmatch(f"v{version}"):
            return f"v{version}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise LumaError("could not determine the latest Luma release; pass --install-ref <tag> (or main)")


def luma_installer_command(
    install_ref: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return an installer command and the exact source ref it will install.

    The bootstrap script and the source archive must come from the same ref.
    Fetching the bootstrap script from ``main`` while asking that script to
    install a tag/commit can silently mix two releases' installer semantics.
    """

    source_env = os.environ if environ is None else environ
    raw_ref = str(install_ref if install_ref is not None and str(install_ref).strip() else source_env.get("LUMA_INSTALL_REF") or "")
    if raw_ref.strip():
        exact_ref = validate_install_ref(raw_ref)
    else:
        exact_ref = validate_install_ref(latest_release_ref())
    encoded_ref = urllib.parse.quote(exact_ref, safe="/-._")
    installer_url = f"{LUMA_INSTALLER_RAW_BASE}/{encoded_ref}/scripts/install-luma.sh"
    # Do not use ``curl | sh`` here. POSIX shells report the pipeline's final
    # command status, so a failed curl followed by an empty, successful ``sh``
    # was previously reported as a completed update. Download first and only
    # execute after curl has returned zero.
    script = (
        "installer=$(mktemp); "
        "trap 'rm -f \"$installer\"' EXIT HUP INT TERM; "
        f"curl -fsSL {shlex.quote(installer_url)} -o \"$installer\" && "
        "sh \"$installer\""
    )
    return f"sh -c {shlex.quote(script)}", exact_ref
