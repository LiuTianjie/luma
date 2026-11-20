#!/usr/bin/env python3
"""Package built Luma Dashboard static assets into luma-dashboard.tar.gz + manifest.json."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luma import __version__  # noqa: E402
from luma.control.dashboard_bundle import (  # noqa: E402
    DEFAULT_DASHBOARD_BUNDLE_URL,
    package_dashboard_directory,
)


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], cwd=str(ROOT), text=True)
        return out.strip()
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "luma" / "assets" / "dashboard",
        help="Directory containing built dashboard assets (default: luma/assets/dashboard)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for luma-dashboard.tar.gz and manifest.json",
    )
    parser.add_argument("--version", default="", help="Release version label (defaults to <luma-version>+<sha>)")
    parser.add_argument("--commit", default="", help="Git commit SHA (defaults to HEAD)")
    parser.add_argument("--bundle-url", default=DEFAULT_DASHBOARD_BUNDLE_URL, help="Public download URL recorded in manifest.json")
    args = parser.parse_args()

    commit = str(args.commit or _git_short_sha()).strip()
    version = str(args.version or "").strip()
    if not version:
        version = f"{__version__}+{commit[:8]}" if commit else __version__

    archive_bytes, manifest = package_dashboard_directory(
        args.source,
        version=version,
        commit=commit,
        bundle_url=str(args.bundle_url or "").strip(),
    )
    out_dir = args.output.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / "luma-dashboard.tar.gz"
    manifest_path = out_dir / "manifest.json"
    archive_path.write_bytes(archive_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "manifest": str(manifest_path),
                **manifest,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
