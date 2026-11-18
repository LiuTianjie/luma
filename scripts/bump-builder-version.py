#!/usr/bin/env python3
"""Version the independent builder without changing Luma/Control."""
from pathlib import Path
import argparse
import re

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "builder/pyproject.toml", ROOT / "builder/luma_build/__init__.py"]
REFERENCES = [ROOT / "builder/README.md", *sorted((ROOT / "docs").rglob("*.md"))]
PATTERN = re.compile(r'(builder-v|luma-build-(?:server|runner):v)(\d+\.\d+\.\d+)')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="version", help="Set an x.y.z version instead of bumping patch")
    parser.add_argument("--check", action="store_true", help="Verify package, runtime and documentation versions")
    args = parser.parse_args()
    versions = [re.search(r'^(?:version|__version__) = "([^"]+)"', p.read_text(), re.M).group(1) for p in FILES]
    if len(set(versions)) != 1:
        raise SystemExit("Builder package and runtime versions disagree")
    current = versions[0]
    if args.check:
        for path in REFERENCES:
            for match in PATTERN.finditer(path.read_text()):
                if match[2] != current:
                    raise SystemExit(f"Stale builder version in {path.relative_to(ROOT)}: {match[0]}")
        print(current)
        return 0
    version = args.version or '.'.join([*current.split('.')[:2], str(int(current.split('.')[2]) + 1)])
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise SystemExit("Version must be x.y.z")
    for path in FILES:
        path.write_text(re.sub(r'^((?:version|__version__) = ")[^"]+(".*)$', rf'\g<1>{version}\2', path.read_text(), flags=re.M))
    for path in REFERENCES:
        path.write_text(PATTERN.sub(lambda m: m[1] + version, path.read_text()))
    print(f"{current} -> {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
