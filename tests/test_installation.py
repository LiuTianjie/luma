"""Runtime identity and process-local packaging policy: never use the host pip policy."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from luma import installation as ins
from luma.errors import LumaError


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / '.local/share/luma'
        self.runtime = self.root / 'venv'
        self.runtime.mkdir(parents=True)
        (self.runtime / 'pyvenv.cfg').write_text('home = /usr/bin\n')

    def record(self):
        return ins.write_record(self.runtime, root=self.root, bindir=self.home / '.local/bin',
                                home=self.home, source=self.runtime.parent / 'src', version='1.2.3',
                                policy=ins.dependency_policy({}), previous_runtime='/opt/old-runtime')

    def test_legacy_local_uses_runtime_not_root_home_or_path(self):
        with patch.dict(os.environ, {'HOME': '/root', 'PATH': '/wrong/bin'}):
            record = ins.runtime_record(self.runtime)
        self.assertEqual(record['userHome'], str(self.home))
        self.assertEqual(record['installHome'], str(self.root))
        self.assertEqual(record['binDir'], str(self.home / '.local/bin'))
        self.assertEqual(record['mode'], 'legacy-venv')

    def test_arbitrary_legacy_venv_keeps_own_root_and_bin(self):
        runtime = self.home / 'opt/luma-cli'
        runtime.mkdir(parents=True)
        (runtime / 'pyvenv.cfg').touch()
        record = ins.runtime_record(runtime)
        self.assertEqual(record['installHome'], str(runtime))
        self.assertEqual(record['binDir'], str(runtime / 'bin'))

    def test_non_local_share_is_not_mistaken_for_user_install(self):
        runtime = self.home / 'other/share/luma/venv'
        runtime.mkdir(parents=True)
        (runtime / 'pyvenv.cfg').touch()
        self.assertEqual(ins.runtime_record(runtime)['installHome'], str(runtime))

    def test_registered_candidate_retains_stable_root(self):
        self.runtime = self.root / 'releases/candidate.test/venv'
        self.runtime.mkdir(parents=True)
        record = self.record()
        self.assertEqual(ins.runtime_record(self.runtime), record)
        self.assertEqual(record['previousRuntime'], '/opt/old-runtime')
        self.assertEqual(list(self.runtime.glob('.installation-*')), [])
        self.assertEqual((self.runtime / ins.RECORD).stat().st_mode & 0o777, 0o600)

    def test_module_invocation_uses_sys_prefix(self):
        self.record()
        with patch.object(sys, 'prefix', str(self.runtime)), patch.object(sys, 'executable', '/usr/bin/python3'):
            self.assertEqual(ins.runtime_record()['runtime'], str(self.runtime))

    def test_development_and_system_interpreters_are_not_adopted(self):
        dev = self.home / 'repo/.venv'
        dev.mkdir(parents=True)
        (dev / 'pyvenv.cfg').touch()
        (dev.parent / 'pyproject.toml').touch()
        self.assertIsNone(ins.runtime_record(dev))
        self.assertIsNone(ins.runtime_record(self.home))

    def test_conflicting_explicit_layout_fails_closed(self):
        record = self.record()
        for key in ('LUMA_USER_HOME', 'LUMA_INSTALL_HOME', 'LUMA_BIN_DIR'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'conflicts'):
                ins.installer_environment({key: '/wrong'}, record)
        env = ins.installer_environment({'HOME': '/root'}, record)
        self.assertEqual(env['LUMA_INSTALL_HOME'], str(self.root))
        self.assertEqual(env['LUMA_PREVIOUS_RUNTIME'], str(self.runtime))

    def test_invalid_or_mutable_record_is_rejected(self):
        for field, value in [('schemaVersion', 'future'), ('runtime', '/wrong'), ('source', '../wrong'), ('binDir', None)]:
            record = self.record()
            record[field] = value
            (self.runtime / ins.RECORD).write_text(json.dumps(record))
            with self.subTest(field=field), self.assertRaises(ValueError):
                ins.read_record(self.runtime)
        self.record()
        path = self.runtime / ins.RECORD
        path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'writable'):
            ins.read_record(self.runtime)
        path.unlink()
        target = self.home / 'record'
        target.write_text('{}')
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular'):
            ins.read_record(self.runtime)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'regular'):
            ins.read_record(self.runtime)

    def test_policy_precedence_and_no_inherited_pip_options(self):
        policy = ins.dependency_policy({'LUMA_PIP_INDEX_URL': 'https://packages.example.com/simple'},
                                       {'indexUrl': 'https://previous.example.com/simple'})
        env = ins.pip_environment({'PIP_INDEX_URL': 'http://bad', 'PIP_NO_INDEX': '1',
            'PIP_EXTRA_INDEX_URL': 'http://leak', 'PIP_TRUSTED_HOST': 'bad', 'PIP_TARGET': '/bad',
            'PYTHONPATH': '/other-runtime', 'PYTHONHOME': '/bad', 'HTTPS_PROXY': 'http://proxy'}, policy)
        self.assertEqual(env['PIP_INDEX_URL'], policy['indexUrl'])
        for key in ('PIP_NO_INDEX', 'PIP_EXTRA_INDEX_URL', 'PIP_TRUSTED_HOST', 'PIP_TARGET', 'PYTHONPATH', 'PYTHONHOME'):
            self.assertNotIn(key, env)
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy')
        self.assertEqual(env['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(ins.dependency_policy({}, policy), policy)

    def test_offline_policy_never_adds_a_public_index(self):
        env = ins.pip_environment({}, ins.dependency_policy({'LUMA_PIP_WHEELHOUSE': str(self.home)}))
        self.assertEqual(env['PIP_NO_INDEX'], '1')
        self.assertEqual(env['PIP_FIND_LINKS'], str(self.home))
        self.assertNotIn('PIP_INDEX_URL', env)

    def test_invalid_tls_or_credentials_never_silently_fall_back(self):
        for index in ('http://mirror/simple', 'https://user:password@mirror/simple', 'https://mirror?secret=x', 'https://mirror/#x', 'https://bad host/simple'):
            with self.subTest(index=index), self.assertRaises(ValueError):
                ins.dependency_policy({'LUMA_PIP_INDEX_URL': index})
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            ins.dependency_policy({'LUMA_PIP_CA_BUNDLE': str(self.home / 'missing')})
        with self.assertRaises(ValueError):
            ins.dependency_policy({'LUMA_PIP_WHEELHOUSE': 'relative'})

    def test_real_pip_ignores_bad_host_configuration(self):
        config = self.home / '.config/pip'
        config.mkdir(parents=True)
        (config / 'pip.conf').write_text('[global]\nindex-url = http://unreachable.invalid/simple\nextra-index-url = http://leak.invalid\ntrusted-host = unreachable.invalid\nno-index = true\n')
        env = ins.pip_environment({**os.environ, 'HOME': str(self.home),
                                   'XDG_CONFIG_HOME': str(self.home / '.config'),
                                   'PIP_INDEX_URL': 'http://bad.invalid'}, ins.dependency_policy({}))
        result = subprocess.run([sys.executable, '-I', '-m', 'pip', 'config', 'list'], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://pypi.org/simple', result.stdout)
        for forbidden in ('unreachable.invalid', 'leak.invalid', 'bad.invalid', 'trusted-host', 'no-index'):
            self.assertNotIn(forbidden, result.stdout)

    def test_cli_update_and_agent_update_share_identity_policy(self):
        from luma.cli import _run_luma_installer
        record = self.record()
        record['policy']['indexUrl'] = 'https://enterprise.example.com/simple'
        with patch('luma.cli.runtime_record', return_value=record), patch('luma.cli._current_install_layout', return_value=None), patch('luma.cli.subprocess.run') as run:
            _run_luma_installer(install_ref='v1.2.3')
        self.assertEqual(run.call_args.kwargs['env']['LUMA_PIP_INDEX_URL'], record['policy']['indexUrl'])
        self.assertEqual(run.call_args.kwargs['env']['LUMA_INSTALL_HOME'], str(self.root))

    def test_local_doctor_reports_policy_without_control_or_state_writes(self):
        from luma.cli import main
        record = self.record()
        original = (self.runtime / ins.RECORD).read_bytes()
        with patch.object(ins, 'runtime_record', return_value=record), patch('luma.cli.ControlClient') as client, patch.object(ins.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--no-env', 'doctor', '--local', '--format', 'json']), 0)
        payload = json.loads(out.getvalue())
        # Standard CLI success envelope.
        result = payload.get('result', payload)
        self.assertEqual(result['scope'], 'local-installation')
        self.assertFalse(result['serviceHealthVerified'])
        self.assertEqual(result['installation']['installHome'], str(self.root))
        client.assert_not_called()
        self.assertEqual((self.runtime / ins.RECORD).read_bytes(), original)

    def test_diagnostics_do_not_print_rejected_index_secrets(self):
        result = ins.installation_diagnostics({'LUMA_PIP_INDEX_URL': 'https://user:TOP-SECRET@mirror/simple'})
        self.assertFalse(result['healthy'])
        self.assertNotIn('TOP-SECRET', json.dumps(result))

    def test_record_uid_mismatch_fails_closed(self):
        record = self.record()
        record['ownerUid'] = os.getuid() + 1
        (self.runtime / ins.RECORD).write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'owner identity'):
            ins.read_record(self.runtime)

    def test_explicit_override_can_recover_a_removed_saved_wheelhouse(self):
        wheelhouse = self.home / 'wheelhouse'
        wheelhouse.mkdir()
        record = self.record()
        record['policy']['wheelhouse'] = str(wheelhouse)
        (self.runtime / ins.RECORD).write_text(json.dumps(record))
        wheelhouse.rmdir()
        saved = ins.read_record(self.runtime)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            ins.installer_environment({}, saved)
        env = ins.installer_environment({'LUMA_PIP_WHEELHOUSE': ''}, saved)
        self.assertEqual(env['LUMA_PIP_WHEELHOUSE'], '')
        self.assertEqual(env['LUMA_PIP_INDEX_URL'], 'https://pypi.org/simple')

    def test_managed_update_reexec_uses_stable_shim_not_old_python(self):
        from luma.cli import _reexec_after_luma_update
        record = self.record()
        bindir = Path(record['binDir'])
        bindir.mkdir(parents=True)
        shim = bindir / 'luma'
        shim.write_text('#!/bin/sh\nexit 0\n')
        shim.chmod(0o755)
        with patch('luma.cli.runtime_record', return_value=record), patch.object(sys, 'argv', ['/old/src/luma/cli.py', 'update', 'manager']), patch('luma.cli.os.execvpe') as execute:
            _reexec_after_luma_update()
        self.assertEqual(execute.call_args.args[0], str(shim))
        self.assertEqual(execute.call_args.args[1], [str(shim), 'update', 'manager'])
        self.assertEqual(execute.call_args.args[2]['LUMA_UPDATE_REEXECED'], '1')
        shim.unlink()
        with patch('luma.cli.runtime_record', return_value=record), self.assertRaisesRegex(LumaError, 'old runtime'):
            _reexec_after_luma_update()

    def test_explicit_ca_cannot_be_overridden_by_host_requests_environment(self):
        cert = self.home / 'ca.pem'
        cert.touch()
        env = ins.pip_environment({'REQUESTS_CA_BUNDLE': '/bad-ca', 'CURL_CA_BUNDLE': '/other-bad-ca'},
                                 ins.dependency_policy({'LUMA_PIP_CA_BUNDLE': str(cert)}))
        self.assertEqual(env['PIP_CERT'], str(cert))
        self.assertNotIn('REQUESTS_CA_BUNDLE', env)
        self.assertNotIn('CURL_CA_BUNDLE', env)
        with self.assertRaises(ValueError):
            ins.dependency_policy({'LUMA_PIP_INDEX_URL': 'https://@mirror/simple'})

    def test_directory_alias_and_canonical_python_prefix_share_identity(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        record = ins.write_record(alias / 'venv', root=alias, bindir=self.home / '.local/bin',
                                  home=self.home, source=alias / 'src', version='1.2.3', policy=ins.dependency_policy({}))
        self.assertEqual(record['installHome'], str(self.root))
        self.assertEqual(ins.runtime_record(alias / 'venv'), ins.runtime_record(self.runtime))
        env = ins.installer_environment({'LUMA_INSTALL_HOME': str(alias)}, record)
        self.assertEqual(env['LUMA_INSTALL_HOME'], str(self.root))
