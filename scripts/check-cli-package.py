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
import shutil
import sys
from unittest.mock import patch

target, workspace = map(Path, sys.argv[1:])
sys.path.insert(0, str(target))
import luma
from luma import skills
from luma.cli import build_parser
from luma.cli.main import main
from luma import command_session
from luma import nomad_upgrade, nomad_migration_node, nomad_pki, nomad_consumer

assert Path(luma.__file__).is_relative_to(target), luma.__file__
assert skills.bundle_root().is_relative_to(target), skills.bundle_root()
assert Path(command_session.__file__).is_relative_to(target)
for module in (nomad_upgrade, nomad_migration_node, nomad_pki, nomad_consumer):
    assert Path(module.__file__).is_relative_to(target), module.__file__
for args in (
    ['observe', 'status', '--format', 'json'],
    ['observe', 'app', 'example', '--since', '1h', '--service', 'worker'],
    ['observe', 'alerts', '--app', 'example'],
    ['observe', 'tasks', 'example', '--view', 'trace', '--trace-id', 'a' * 32],
):
    assert build_parser().parse_args(args).command == 'observe'
for command, name in [('node', 'worker'), ('app', 'shop_api')]:
    parsed = build_parser().parse_args([command, 'shell', name, '--transport', 'relay'])
    assert parsed.command == command and parsed.name == name and parsed.transport == 'relay'
parsed = build_parser().parse_args(['node', 'exec', '--stdin', '--timeout', '60', 'worker', '--', 'python3', '-'])
assert parsed.node_command == 'exec' and parsed.argv == ['python3', '-']
assert parsed.stdin and parsed.timeout == 60 and parsed.transport == 'relay'

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
        # The previous suite had no shell skill; an ordinary command must add it.
        shutil.rmtree(root / 'luma-shell')
    assert main(['version', '--local']) == 0
    for root in [*roots.values(), custom]:
        for source in skills.bundle_root().iterdir():
            if (source / 'SKILL.md').is_file():
                assert skills._hashes(root / source.name) == skills._hashes(source)
        backups = list((root.parent / 'luma-skill-backups').glob('luma-observe-*'))
        assert any((p / 'SKILL.md').read_text() == 'operator customization' for p in backups)
    assert all(item['paired'] and not item['modified'] for item in skills.skill_status()['items'])
# Exercise the shipped alert engine with an empty state directory and a local
# metrics endpoint. No source checkout, saved login, business rules or real chat.
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from luma.control import alerting, task_alerts
from luma.control.state import save_state
assert Path(task_alerts.__file__).is_relative_to(target)
metric_value = 10
class Metrics(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        from urllib.parse import parse_qs, urlsplit
        expr = parse_qs(urlsplit(self.path).query)['query'][0]
        assert 'luma_stack="fresh-workers"' in expr and 'service_name="worker"' in expr
        rows = [] if metric_value is None else [{'metric': {'luma_stack': 'fresh-workers'}, 'value': [1000, str(metric_value)]}]
        raw = json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': rows}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
server = ThreadingHTTPServer(('127.0.0.1', 0), Metrics)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': str(workspace / 'fresh-control'),
                                 'LUMA_OBSERVE_URL': 'http://127.0.0.1:' + str(server.server_port)}):
        # Use the same state writer as bootstrap, with no applications or old alert tables.
        save_state({'clusterId':'fixture-cluster', 'domain':'luma.example.test'})
        assert task_alerts.METRICS <= {x['metric'] for x in alerting.dispatch('GET', 'presets')['items']}
        assert alerting.dispatch('GET', 'rules')['items'] == []
        assert alerting.dispatch('GET', 'channels')['items'] == []
        channel = alerting.save_channel({'name':'Fixture only', 'appId':'cli_test1234', 'appSecret':'fixture-secret', 'chatId':'oc_group1234'})
        rule = alerting.save_rule({'name':'Worker heartbeat', 'metric':'app.task_heartbeat_age',
                                  'target':'fresh-workers', 'taskQuery':{'service':'worker'},
                                  'threshold':120, 'forSeconds':0, 'channelIds':[channel['id']]})
        alerting.tick(alerting.load_evaluation_state(now=1000), now=1000)
        assert alerting.dispatch('GET', 'incidents')['items'] == []
        metric_value = 180
        alerting.tick(alerting.load_evaluation_state(now=1015), now=1015)
        assert alerting.dispatch('GET', 'incidents')['items'][0]['status'] == 'firing'
        messages = []
        alerting.deliver_pending(now=1015, transport=lambda *args: messages.append(args))
        assert len(messages) == 1 and 'worker' in messages[0][3]
        metric_value = None
        alerting.tick(alerting.load_evaluation_state(now=1030), now=1030)
        incident = alerting.dispatch('GET', 'incidents')['items'][0]
        assert incident['status'] == 'firing' and incident['noData']
        metric_value = 10
        alerting.tick(alerting.load_evaluation_state(now=1045), now=1045)
        alerting.deliver_pending(now=1045, transport=lambda *args: messages.append(args))
        assert alerting.dispatch('GET', 'incidents')['items'][0]['status'] == 'resolved'
        assert len(messages) == 2
        assert alerting.dispatch('GET', 'rules')['items'][0]['taskQuery'] == {'service':'worker'}
        assert all(x['status'] == 'sent' for x in alerting.dispatch('GET', 'deliveries')['items'])
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
print('Installed wheel: fresh Control presets, rule persistence, metric query, firing, missing data and recovery passed')
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
        for name in ("luma/command_session.py", "luma/cli/terminal.py", "luma/terminal_direct.py"):
            assert wheel.read(name) == (ROOT / name).read_bytes(), f"wheel command module mismatch: {name}"
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
