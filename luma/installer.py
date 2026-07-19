from __future__ import annotations

import os
import shlex
import urllib.parse
from collections.abc import Mapping


DEFAULT_LUMA_INSTALL_REF = "main"
LUMA_INSTALLER_RAW_BASE = "https://raw.githubusercontent.com/LiuTianjie/luma"


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
    exact_ref = str(install_ref or source_env.get("LUMA_INSTALL_REF") or DEFAULT_LUMA_INSTALL_REF).strip()
    if not exact_ref:
        exact_ref = DEFAULT_LUMA_INSTALL_REF
    # Keep slash separators because Git refs commonly contain them. Encode all
    # other path-sensitive bytes, then shell-quote the complete URL.
    encoded_ref = urllib.parse.quote(exact_ref, safe="/-._~")
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
