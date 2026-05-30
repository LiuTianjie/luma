#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = [
    ROOT / "pyproject.toml",
    ROOT / "luma" / "__init__.py",
    ROOT / "luma" / "assets" / "pyproject.toml",
]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump Luma version files.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set", dest="set_version", help="Set an explicit x.y.z version")
    group.add_argument("--minor", action="store_true", help="Bump the minor version")
    group.add_argument("--major", action="store_true", help="Bump the major version")
    parser.add_argument("--check", action="store_true", help="Only verify that tracked version files match")
    args = parser.parse_args()

    versions = read_versions()
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{path.relative_to(ROOT)}={version}" for path, version in versions.items())
        raise SystemExit(f"version files disagree: {details}")
    current = next(iter(unique))

    if args.check:
        print(current)
        return 0

    if args.set_version:
        next_version = args.set_version
        if not SEMVER_RE.fullmatch(next_version):
            raise SystemExit("--set must use x.y.z")
    else:
        next_version = bump(current, major=args.major, minor=args.minor)

    for path in VERSION_FILES:
        text = path.read_text(encoding="utf-8")
        path.write_text(replace_version(text, current, next_version, path), encoding="utf-8")
    print(f"{current} -> {next_version}")
    return 0


def read_versions() -> dict[Path, str]:
    versions: dict[Path, str] = {}
    for path in VERSION_FILES:
        text = path.read_text(encoding="utf-8")
        version = extract_version(text, path)
        versions[path] = version
    return versions


def extract_version(text: str, path: Path) -> str:
    if path.name == "pyproject.toml":
        match = re.search(r'^version = "([^"]+)"$', text, flags=re.MULTILINE)
    else:
        match = re.search(r'^__version__ = "([^"]+)"$', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit(f"could not find version in {path.relative_to(ROOT)}")
    version = match.group(1)
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"invalid version in {path.relative_to(ROOT)}: {version}")
    return version


def replace_version(text: str, current: str, next_version: str, path: Path) -> str:
    if path.name == "pyproject.toml":
        return re.sub(r'^version = "[^"]+"$', f'version = "{next_version}"', text, count=1, flags=re.MULTILINE)
    return re.sub(r'^__version__ = "[^"]+"$', f'__version__ = "{next_version}"', text, count=1, flags=re.MULTILINE)


def bump(version: str, *, major: bool, minor: bool) -> str:
    major_part, minor_part, patch_part = [int(part) for part in version.split(".")]
    if major:
        return f"{major_part + 1}.0.0"
    if minor:
        return f"{major_part}.{minor_part + 1}.0"
    return f"{major_part}.{minor_part}.{patch_part + 1}"


if __name__ == "__main__":
    raise SystemExit(main())
