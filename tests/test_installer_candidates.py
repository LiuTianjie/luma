"""Exercise the managed shell bootstrap with local archives and no network."""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-luma.sh'


class InstallerCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.install = self.home / 'managed'
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.script = self.home / 'install.sh'
        self.script.write_bytes(SCRIPT.read_bytes())
        self.old = {}
        for relative, content in [('src/luma.py', 'old-source'), ('venv/dependencies', 'old-dependencies'), ('bin/luma', 'old-entry')]:
            path = self.install / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            self.old[path] = content
        self.env = {**os.environ, 'HOME': str(self.home), 'LUMA_USER_HOME': str(self.home),
                    'LUMA_INSTALL_HOME': str(self.install), 'LUMA_BIN_DIR': str(self.install / 'bin'),
                    'LUMA_INSTALL_REF': 'v1.2.3', 'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        for key in ('LUMA_INSTALL_LOCK_HELD', 'LUMA_VENV_DIR', 'LUMA_ARCHIVE_URL', 'LUMA_INSTALL_OWNER'):
            self.env.pop(key, None)

    def assert_old_preserved(self):
        for path, content in self.old.items():
            self.assertEqual(path.read_text(), content)

    def test_source_extraction_uses_unique_final_candidate_without_touching_active(self):
        package = self.home / 'release'
        package.mkdir()
        (package / 'payload').write_text('new-source')
        archive = self.home / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(package, arcname='luma-release')
        curl = self.bin / 'curl'
        curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n if [ "$1" = -o ]; then shift; cp "$TEST_ARCHIVE" "$1"; exit; fi\n shift\ndone\nexit 90\n')
        curl.chmod(0o755)
        script = SCRIPT.read_text()
        start = script.index('download_source() {')
        end = script.index('\nif ! command -v python3', start)
        command = 'set -eu\nis_commit_ref() { return 1; }\nrepair_install_ownership() { :; }\n' + script[start:end] + '\ndownload_source\nprintf "%s\\n" "$SOURCE_DIR"\n'
        env = {**self.env, 'INSTALL_REF': 'v1.2.3', 'REPO_URL': 'https://example.invalid', 'INSTALL_HOME': str(self.install), 'TEST_ARCHIVE': str(archive)}
        paths = []
        for _ in range(2):
            result = subprocess.run(['sh', '-c', command], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            source = Path(result.stdout.strip().splitlines()[-1])
            self.assertEqual((source / 'payload').read_text(), 'new-source')
            self.assertTrue(source.is_relative_to(self.install / 'releases'))
            paths.append(source)
        self.assertNotEqual(paths[0], paths[1])
        self.assert_old_preserved()

    def test_second_installer_is_rejected_while_first_prepares(self):
        # A controlled failing download holds the actual installer lock; no venv
        # or system service is created and nothing accesses an external URL.
        marker = self.home / 'download-started'
        release = self.home / 'release-download'
        curl = self.bin / 'curl'
        curl.write_text('#!/bin/sh\ntouch "$TEST_MARKER"\ni=0\nwhile [ ! -e "$TEST_RELEASE" ] && [ "$i" -lt 100 ]; do sleep .1; i=$((i+1)); done\nexit 22\n')
        curl.chmod(0o755)
        env = {**self.env, 'TEST_MARKER': str(marker), 'TEST_RELEASE': str(release)}
        first = subprocess.Popen(['sh', str(self.script)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            second = subprocess.run(['sh', str(self.script)], env=env, text=True, capture_output=True, timeout=5)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('Another Luma installation is in progress', second.stderr)
            self.assert_old_preserved()
        finally:
            release.touch()
            first.communicate(timeout=15)
        self.assertNotEqual(first.returncode, 0)
        # Failed process releases the lock: next attempt reaches download, not a
        # permanently stale lockfile error.
        retry = subprocess.run(['sh', str(self.script)], env=env, text=True, capture_output=True, timeout=5)
        self.assertNotIn('Another Luma installation', retry.stderr)
        self.assertNotEqual(retry.returncode, 0)
        self.assert_old_preserved()

    def test_managed_install_refuses_custom_active_venv_before_download(self):
        result = subprocess.run(['sh', str(self.script)],
            env={**self.env, 'LUMA_VENV_DIR': str(self.install / 'venv')}, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('isolated candidates', result.stderr)
        self.assert_old_preserved()

    def test_piped_bootstrap_keeps_the_same_lock_without_reexecuting_a_file(self):
        result = subprocess.run(['sh'], input=SCRIPT.read_text(), cwd=self.home,
            env={**self.env, 'LUMA_VENV_DIR': str(self.install / 'venv')}, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('isolated candidates', result.stderr)
        self.assertNotIn('cannot open sh', result.stderr)
        self.assert_old_preserved()

    def test_symlink_lock_is_rejected_without_modifying_its_target(self):
        protected = self.home / 'protected'
        protected.write_text('do not truncate')
        (self.install / '.installer.lock').symlink_to(protected)
        result = subprocess.run(['sh', str(self.script)], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-symlink file', result.stderr)
        self.assertEqual(protected.read_text(), 'do not truncate')
        self.assert_old_preserved()
