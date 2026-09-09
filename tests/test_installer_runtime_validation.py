"""Execute the installer's publication gate without touching host services."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallerRuntimeValidationTests(unittest.TestCase):
    def test_runtime_gate_precedes_shim_and_service_publication(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/install-luma.sh').read_text()
        gate = script.index('if ! validate_luma_runtime; then')
        self.assertLess(gate, script.index('mv -f "$shim_tmp" "$BIN_DIR/luma"'))
        self.assertLess(gate, script.rindex('    refresh_node_agent_service'))

    def test_runtime_failure_never_reaches_publication(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/install-luma.sh').read_text()
        start = script.index('validate_luma_runtime() {')
        end = script.index('if [ "$LOCAL_CHECKOUT" -eq 0 ]; then', start)
        gate = script[start:end]
        for failure in ('import', 'cli', 'check', 'none'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'bin').mkdir()
                python = root / 'bin/python'
                python.write_text('''#!/bin/sh
case "$*" in
  *"-c"*) [ "$FAILURE" != import ] ;;
  *"-m luma.cli"*) [ "$FAILURE" != cli ] ;;
  *"-m pip check"*) [ "$FAILURE" != check ] ;;
  *) exit 99 ;;
esac
''')
                python.chmod(0o755)
                result = subprocess.run(['sh', '-c', 'set -eu\n' + gate + '\necho PUBLISHED'],
                    env={**os.environ, 'VENV_DIR': tmp, 'SOURCE_DIR': tmp, 'FAILURE': failure},
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if failure == 'none' else 1)
                self.assertEqual('PUBLISHED' in result.stdout, failure == 'none')

    def test_real_empty_venv_preserves_existing_entry_and_service(self):
        """Reproduce PPT: new empty runtime beside a working old installation."""
        import venv

        script = (Path(__file__).resolve().parents[1] / 'scripts/install-luma.sh').read_text()
        tail = script[script.index('validate_luma_runtime() {'):]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / 'empty-venv'
            venv.EnvBuilder(with_pip=False).create(runtime)
            bindir = root / 'bin'
            bindir.mkdir()
            shim = bindir / 'luma'
            original = '#!/bin/sh\nexec /opt/luma-cli/bin/python -m luma.cli "$@"\n'
            shim.write_text(original)
            service_marker = root / 'service-refreshed'
            source = Path(__file__).resolve().parents[1]
            env = {**os.environ, 'SOURCE_DIR': str(source), 'VENV_DIR': str(runtime),
                   'BIN_DIR': str(bindir), 'LOCAL_CHECKOUT': '0',
                   'SERVICE_MARKER': str(service_marker)}
            env.pop('PYTHONPATH', None)
            env.pop('PYTHONHOME', None)
            result = subprocess.run(
                ['sh', '-c', 'set -eu\nrefresh_node_agent_service() { touch "$SERVICE_MARKER"; }\n'
                 'ensure_path() { :; }\nchown_install_paths() { :; }\n' + tail],
                env=env, text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No module named 'yaml'", result.stderr)
            self.assertIn('Luma runtime validation failed', result.stderr)
            self.assertEqual(shim.read_text(), original)
            self.assertFalse(service_marker.exists())
            self.assertNotIn('Luma installed in', result.stdout)
            self.assertNotIn('Luma version:', result.stdout)

    def test_shim_uses_the_validated_python_not_stale_console_entry(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/install-luma.sh').read_text()
        import sys
        from unittest.mock import patch
        from luma import installation
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / 'shim'
            bindir.mkdir()
            runtime = root / 'venv with spaces'
            (runtime / 'bin').mkdir(parents=True)
            python = runtime / 'bin/python'
            python.write_text('#!/bin/sh\nprintf "validated-python:%s\\n" "$*"\n')
            python.chmod(0o755)
            stale = runtime / 'bin/luma'
            stale.write_text('#!/bin/sh\necho WRONG-ENVIRONMENT\nexit 99\n')
            stale.chmod(0o755)
            out = io.StringIO()
            with patch.object(sys, 'prefix', str(runtime)), patch.object(sys, 'argv', ['installation.py', 'shim']), patch.dict(os.environ, {'SOURCE_DIR': tmp}), contextlib.redirect_stdout(out):
                self.assertEqual(installation.main(), 0)
            (bindir / 'luma').write_text(out.getvalue())
            result = subprocess.run(['sh', str(bindir / 'luma'), 'node-agent', 'run', '--help'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('validated-python:-m luma.cli node-agent run --help', result.stdout)
            self.assertNotIn('WRONG-ENVIRONMENT', result.stdout)
