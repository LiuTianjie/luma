#!/usr/bin/env python3
"""Verify distributed CLI commands and release-paired skills outside the checkout.

Run after `python -m build`: python scripts/check-cli-package.py dist
Only the package under test and disposable assistant directories are written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]

SMOKE = r'''
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

target, workspace = map(Path, sys.argv[1:])
sys.path.insert(0, str(target))
import luma
from luma import skills
from luma.cli import build_parser
from luma.cli.main import main

assert Path(luma.__file__).is_relative_to(target), luma.__file__
assert skills.bundle_root().is_relative_to(target), skills.bundle_root()
for args in (
    ['observe', 'status', '--format', 'json'],
    ['observe', 'app', 'example', '--since', '1h', '--service', 'worker'],
    ['observe', 'alerts', '--app', 'example'],
    ['observe', 'tasks', 'example', '--view', 'trace', '--trace-id', 'a' * 32],
):
    assert build_parser().parse_args(args).command == 'observe'

# Redirect only installation destinations and local config loading. CLI dispatch,
# packaged bundle lookup, copying, registration and automatic sync remain real.
roots = {name: workspace / name / 'skills' for name in ('codex', 'claude')}
custom = workspace / 'other-assistant' / 'skills'
with patch.object(skills, 'agent_roots', return_value=roots), \
     patch('luma.cli.skills.agent_roots', return_value=roots), \
     patch.object(skills, '_config_root', return_value=workspace / 'config'), \
     patch('luma.cli.main._load_environment'), \
     contextlib.redirect_stdout(io.StringIO()), \
     contextlib.redirect_stderr(io.StringIO()):
    assert main(['version', '--local']) == 0
    assert not any(root.exists() for root in roots.values())
    assert main(['skills', 'install', '--agent', 'codex']) == 0
    assert main(['skills', 'install', '--agent', 'claude']) == 0
    assert main(['skills', 'install', '--path', str(custom)]) == 0
    for root in [*roots.values(), custom]:
        # Simulate an older installed release with a local customization.
        folder = root / 'luma-observe'
        marker = json.loads((folder / skills.MARKER).read_text())
        marker['version'] = '0.0.0'
        (folder / skills.MARKER).write_text(json.dumps(marker))
        (folder / 'SKILL.md').write_text('operator customization')
    assert main(['version', '--local']) == 0
    for root in [*roots.values(), custom]:
        for source in skills.bundle_root().iterdir():
            if (source / 'SKILL.md').is_file():
                assert skills._hashes(root / source.name) == skills._hashes(source)
        backups = list((root.parent / 'luma-skill-backups').glob('luma-observe-*'))
        assert any((p / 'SKILL.md').read_text() == 'operator customization' for p in backups)
    assert all(item['paired'] and not item['modified'] for item in skills.skill_status()['items'])
print('Installed wheel: observe commands, Codex/Claude/custom installs, upgrade sync and backups passed')
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    wheels = list(args.dist.glob("luma_infra-*.whl"))
    sources = list(args.dist.glob("luma_infra-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("expected exactly one luma-infra wheel and source distribution")
    canonical = {str(p.relative_to(ROOT)): p.read_bytes() for p in (ROOT / "skills").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sources[0]) as source:
        prefixes = {name.split("/")[0] for name in source.getnames()}
        assert len(prefixes) == 1, "source distribution has multiple roots"
        prefix = prefixes.pop()
        for name, content in canonical.items():
            assert wheel.read("luma/assets/" + name) == content, f"wheel skill mismatch: {name}"
            member = source.extractfile(prefix + "/" + name)
            assert member is not None and member.read() == content, f"source skill mismatch: {name}"
        assert wheel.read("luma/cli/observe.py") == (ROOT / "luma/cli/observe.py").read_bytes()
        print(f"Wheel and source distribution contain all {len(canonical)} canonical skill files")
    with tempfile.TemporaryDirectory(prefix="luma-package-check-") as temporary:
        workspace = Path(temporary)
        target = workspace / "installed"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
                        "--target", str(target), str(wheels[0].resolve())],
                       cwd=workspace, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "-I", "-B", "-c", SMOKE, str(target), str(workspace)],
                       cwd=workspace, check=True)
    print(json.dumps({"packageCheck": "passed", "wheel": wheels[0].name, "sdist": sources[0].name}))


if __name__ == "__main__":
    main()
