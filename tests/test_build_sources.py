"""Pinned additional Git sources and the credential transport boundary."""
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from luma import build_sources, gitops
from luma.build_sources import (
    GitContextCanceled,
    parse_git_contexts,
    prepare_git_contexts,
    validate_git_contexts_spec,
)
from luma.errors import LumaError


class GitContextSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.primary = self.workspace / "primary"
        self.primary.mkdir()
        self.fixture = self.workspace / "fixture"
        self.fixture.mkdir()
        self.git("init", "--quiet")
        (self.fixture / "src").mkdir()
        (self.fixture / "src/main.py").write_text("print('pinned')\n")
        executable = self.fixture / "run.sh"
        executable.write_text("#!/bin/sh\necho pinned\n")
        executable.chmod(0o755)
        (self.fixture / "src/run").symlink_to("../run.sh")
        (self.fixture / "chain").symlink_to("src/run")
        self.git("add", ".")
        self.git("-c", "user.name=Luma Test", "-c", "user.email=luma@example.com", "commit", "--quiet", "-m", "fixture")
        self.commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        archive = self.git("archive", "--format=tar.gz", self.commit).stdout
        self.lock = {
            "repository": "https://git.example.test/acme/library.git",
            "commit": self.commit,
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
        }
        self.lock_path = self.primary / "runtime.json"
        self.write_lock()
        self.build_spec = {"x-luma-git-contexts": {"library": {"lock": "runtime.json"}}}
        self.primary_url = "https://git.example.test/acme/app.git"

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.fixture), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def write_lock(self):
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

    def local_clone(self, repository, destination, **kwargs):
        self.assertEqual(repository, self.lock["repository"])
        self.assertEqual(kwargs["ref"], self.commit)
        self.assertTrue(kwargs["strict_https_origin"])
        subprocess.run(
            ["git", "clone", "--quiet", "--no-local", str(self.fixture), str(destination)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", kwargs["ref"]],
            check=True, capture_output=True,
        )

    def prepare(self, **kwargs):
        return prepare_git_contexts(
            self.primary, self.build_spec, repository_url=self.primary_url,
            workspace=self.workspace, **kwargs,
        )

    def assert_no_prepared_files(self):
        self.assertEqual(list(self.workspace.glob("luma-git-*")), [])

    def test_real_git_archive_exports_exact_source_with_internal_symlinks(self):
        logs = []
        with patch("luma.build_sources.gitops.clone", side_effect=self.local_clone) as clone:
            prepared = self.prepare(token="provider-private-token", username="provider-user", progress=logs.append)
        context = prepared["library"]
        self.assertEqual(context.commit, self.commit)
        self.assertEqual(context.archive_sha256, self.lock["archive_sha256"])
        self.assertEqual((context.path / "src/main.py").read_text(), "print('pinned')\n")
        self.assertEqual((context.path / "chain").resolve(), context.path / "run.sh")
        self.assertTrue((context.path / "run.sh").stat().st_mode & 0o111)
        self.assertFalse(context.path.is_relative_to(self.primary))
        self.assertEqual(list(context.path.rglob(".git")), [])
        self.assertEqual(list(context.path.rglob("*askpass*")), [])
        self.assertEqual(list(self.workspace.glob("luma-git-fetch-*")), [])
        self.assertNotIn("provider-private-token", str(asdict(context)) + str(logs))
        self.assertEqual(clone.call_args.kwargs["token"], "provider-private-token")

    def test_plain_build_does_not_prepare_sources_or_require_https(self):
        with patch("luma.build_sources.gitops.clone") as clone:
            self.assertEqual(prepare_git_contexts(
                self.primary, {"context": "."}, repository_url="git@host:repo.git", workspace=self.workspace,
            ), {})
        clone.assert_not_called()
        self.assert_no_prepared_files()

    def test_alias_spec_and_lock_paths_are_strict(self):
        invalid = [
            {}, [], None,
            {"scratch": {"lock": "runtime.json"}},
            {"context": {"lock": "runtime.json"}},
            {"dockerfile": {"lock": "runtime.json"}},
            {"Library": {"lock": "runtime.json"}},
            {"../library": {"lock": "runtime.json"}},
            {"library": {"lock": "../runtime.json"}},
            {"library": {"lock": "/runtime.json"}},
            {"library": {"lock": "x/./runtime.json"}},
            {"library": {"lock": "x\\runtime.json"}},
            {"library": {"lock": ".git/config"}},
            {"library": {"lock": "runtime.json", "repository": "ignored"}},
        ]
        for contexts in invalid:
            with self.subTest(contexts=contexts), self.assertRaises(LumaError):
                validate_git_contexts_spec({"x-luma-git-contexts": contexts})

    def test_duplicate_alias_is_rejected_even_for_custom_mapping(self):
        class DuplicateMapping(dict):
            def items(self):
                return [("library", {"lock": "runtime.json"})] * 2
        with self.assertRaisesRegex(LumaError, "duplicate"):
            validate_git_contexts_spec({"x-luma-git-contexts": DuplicateMapping(library={})})

    def test_lock_requires_exact_fields_types_full_commit_and_hash(self):
        original = dict(self.lock)
        invalid = [
            {**original, "commit": self.commit[:8]},
            {**original, "commit": True},
            {**original, "archive_sha256": "f" * 63},
            {**original, "repository": None},
            {**original, "token": "do-not-accept"},
            {"commit": self.commit},
        ]
        for lock in invalid:
            self.lock = lock
            self.write_lock()
            with self.subTest(lock=lock), patch("luma.build_sources.gitops.clone") as clone, self.assertRaises(LumaError):
                self.prepare()
            clone.assert_not_called()
        self.lock_path.write_text('{"repository":"a","repository":"b"}')
        with self.assertRaisesRegex(LumaError, "invalid JSON"):
            self.prepare()
        self.assert_no_prepared_files()

    def test_repository_must_share_https_origin_without_credentials(self):
        invalid = [
            "https://other.example.test/acme/library.git",
            "https://git.example.test:8443/acme/library.git",
            "https://git.example.test:0/acme/library.git",
            "https://user:secret@git.example.test/acme/library.git",
            "https://@git.example.test/acme/library.git",
            "https://git.example.test/acme/library.git?token=secret",
            "https://git.example.test/acme/library.git#fragment",
            "https://git.example.test/acme/library.git?",
            "http://git.example.test/acme/library.git",
            "git@git.example.test:acme/library.git",
            "https://git.example.test/../acme/library.git",
            "https://git.example.test/%2e%2e/acme/library.git",
            "https://git.example.test/acme/lib\nrary.git",
        ]
        for repository in invalid:
            self.lock["repository"] = repository
            self.write_lock()
            with self.subTest(repository=repository), patch("luma.build_sources.gitops.clone") as clone, self.assertRaises(LumaError) as error:
                self.prepare()
            clone.assert_not_called()
            self.assertNotIn("secret", str(error.exception))
        self.lock["repository"] = "https://GIT.EXAMPLE.TEST:443/acme/library.git"
        self.write_lock()
        self.assertIn("library", parse_git_contexts(self.primary, self.build_spec, repository_url=self.primary_url))

    def test_lock_symlink_cannot_escape_primary_and_workspace_cannot_be_inside_it(self):
        outside = self.workspace / "outside.json"
        outside.write_text(json.dumps(self.lock))
        self.lock_path.unlink()
        self.lock_path.symlink_to(outside)
        with self.assertRaisesRegex(LumaError, "inside the primary"):
            self.prepare()
        self.lock_path.unlink()
        self.write_lock()
        with self.assertRaisesRegex(LumaError, "outside the primary"):
            prepare_git_contexts(self.primary, self.build_spec, repository_url=self.primary_url, workspace=self.primary)
        self.assert_no_prepared_files()

    def test_digest_mismatch_removes_checkout_archive_and_output(self):
        self.lock["archive_sha256"] = "0" * 64
        self.write_lock()
        with patch("luma.build_sources.gitops.clone", side_effect=self.local_clone), self.assertRaisesRegex(LumaError, "SHA256"):
            self.prepare()
        self.assert_no_prepared_files()

    def test_head_mismatch_never_archives_and_cleans_output(self):
        with patch("luma.build_sources.gitops.clone", side_effect=self.local_clone), patch(
            "luma.build_sources.gitops.head_commit_full", return_value="0" * 40,
        ), patch("luma.build_sources._archive_source") as archive, self.assertRaisesRegex(LumaError, "pinned commit"):
            self.prepare()
        archive.assert_not_called()
        self.assert_no_prepared_files()

    def test_cancel_before_clone_does_not_create_or_fetch_anything(self):
        canceled = threading.Event()
        canceled.set()
        with patch("luma.build_sources.gitops.clone") as clone, self.assertRaises(GitContextCanceled):
            self.prepare(cancel_event=canceled)
        clone.assert_not_called()
        self.assert_no_prepared_files()

    def test_cancel_during_bounded_clone_stops_before_archive_and_cleans(self):
        canceled = threading.Event()
        def clone_and_cancel(*args, **kwargs):
            self.local_clone(*args, **kwargs)
            canceled.set()
        with patch("luma.build_sources.gitops.clone", side_effect=clone_and_cancel), patch(
            "luma.build_sources._archive_source",
        ) as archive, self.assertRaises(GitContextCanceled):
            self.prepare(cancel_event=canceled)
        archive.assert_not_called()
        self.assert_no_prepared_files()

    def test_cancel_during_failed_clone_is_reported_as_canceled(self):
        canceled = threading.Event()
        def fail_after_cancel(*_args, **_kwargs):
            canceled.set()
            raise LumaError("git clone timed out")
        with patch("luma.build_sources.gitops.clone", side_effect=fail_after_cancel), self.assertRaises(GitContextCanceled):
            self.prepare(cancel_event=canceled)
        self.assert_no_prepared_files()

    def test_second_context_failure_cleans_already_prepared_first_context(self):
        self.build_spec["x-luma-git-contexts"]["second"] = {"lock": "runtime.json"}
        count = 0
        def clone_then_fail(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise LumaError("transport returned provider-private-token")
            self.local_clone(*args, **kwargs)
        with patch("luma.build_sources.gitops.clone", side_effect=clone_then_fail), self.assertRaises(LumaError) as error:
            self.prepare(token="provider-private-token")
        self.assertNotIn("provider-private-token", str(error.exception))
        self.assert_no_prepared_files()

    def archive_fixture(self, entries):
        archive_path = self.workspace / "malicious.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, kind, data in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = 0o6755
                if kind == tarfile.REGTYPE:
                    content = data.encode()
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    member.linkname = data
                    archive.addfile(member)
        return archive_path

    def test_unsafe_archive_paths_types_and_symlink_graphs_are_rejected(self):
        cases = [
            [("../escape", tarfile.REGTYPE, "x")],
            [("/escape", tarfile.REGTYPE, "x")],
            [("a/../escape", tarfile.REGTYPE, "x")],
            [(".git/config", tarfile.REGTYPE, "x")],
            [("a", tarfile.REGTYPE, "1"), ("a", tarfile.REGTYPE, "2")],
            [("a", tarfile.LNKTYPE, "b")],
            [("a", tarfile.CHRTYPE, "")],
            [("a", tarfile.SYMTYPE, "../escape")],
            [("a", tarfile.SYMTYPE, "/tmp/escape")],
            [("a", tarfile.SYMTYPE, "b"), ("a/file", tarfile.REGTYPE, "x")],
            [("a/file", tarfile.REGTYPE, "x"), ("a", tarfile.SYMTYPE, "b")],
            [("a", tarfile.REGTYPE, "x"), ("a/file", tarfile.REGTYPE, "x")],
            [("a", tarfile.SYMTYPE, "b"), ("b", tarfile.SYMTYPE, "a")],
            [("a", tarfile.SYMTYPE, "dir/link/../../outside"), ("dir/link", tarfile.SYMTYPE, "../real")],
        ]
        for index, entries in enumerate(cases):
            archive = self.archive_fixture(entries)
            with self.subTest(entries=entries), self.assertRaises(LumaError):
                build_sources._extract_source(archive, self.workspace / f"unsafe-{index}", None)
        self.assertFalse((self.workspace / "escape").exists())

    def test_safe_dangling_internal_symlink_and_executable_mode_are_preserved(self):
        archive = self.archive_fixture([
            ("run", tarfile.REGTYPE, "script"),
            ("links/missing", tarfile.SYMTYPE, "../not-yet-generated"),
        ])
        destination = self.workspace / "safe"
        build_sources._extract_source(archive, destination, None)
        self.assertTrue((destination / "links/missing").is_symlink())
        self.assertEqual((destination / "run").stat().st_mode & 0o7777, 0o755)

    def test_archive_limits_reject_before_export(self):
        archive = self.archive_fixture([("large", tarfile.REGTYPE, "123456")])
        with patch.object(build_sources, "MAX_EXTRACTED_BYTES", 5), self.assertRaisesRegex(LumaError, "extraction limit"):
            build_sources._extract_source(archive, self.workspace / "too-large", None)
        self.assertFalse((self.workspace / "too-large").exists())
        with patch.object(build_sources, "MAX_ARCHIVE_BYTES", 1), self.assertRaisesRegex(LumaError, "size limit"):
            build_sources._verify_archive(archive, "0" * 64, None)

    def test_extraction_failure_cleans_all_preparation_output(self):
        archive = self.archive_fixture([("a", tarfile.SYMTYPE, "../escape")])
        self.lock["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.write_lock()
        def write_unsafe_archive(_repo, destination, _commit):
            destination.write_bytes(archive.read_bytes())
        with patch("luma.build_sources.gitops.clone", side_effect=self.local_clone), patch(
            "luma.build_sources._archive_source", side_effect=write_unsafe_archive,
        ), self.assertRaisesRegex(LumaError, "escapes"):
            self.prepare()
        self.assert_no_prepared_files()


class StrictGitTransportTests(unittest.TestCase):
    def test_full_sha256_commit_initializes_matching_object_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream = Path(tmp) / "upstream"
            destination = Path(tmp) / "checkout"
            real_run = subprocess.run
            real_run(["git", "init", "--quiet", "--object-format=sha256", str(upstream)], check=True, capture_output=True)
            real_run([
                "git", "-C", str(upstream), "-c", "user.name=Luma Test", "-c", "user.email=luma@example.com",
                "commit", "--quiet", "--allow-empty", "-m", "sha256 fixture",
            ], check=True, capture_output=True)
            commit = real_run(["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(len(commit), 64)
            def use_local_transport(command, **kwargs):
                # Exercise the real init/fetch/checkout owner; only the fake
                # HTTPS origin is replaced by this local Git test fixture.
                if command[-4:-1] == ["remote", "add", "origin"]:
                    command = [*command[:-1], str(upstream)]
                return real_run(command, **kwargs)
            with patch("luma.gitops.subprocess.run", side_effect=use_local_transport):
                gitops.clone("https://git.example.test/acme/library.git", destination, ref=commit)
            self.assertEqual(gitops.head_commit_full(destination), commit)

    def test_strict_clone_isolates_configuration_and_never_exposes_token(self):
        commands = []
        auth_paths = []
        def run(command, **kwargs):
            env = kwargs["env"]
            commands.append(command)
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_DIR", "GIT_TRACE_CURL"):
                self.assertNotIn(key, env)
            self.assertNotIn("provider-private-token", str(command) + str(env))
            helper = Path(env["GIT_ASKPASS"])
            password = Path(env["LUMA_GIT_PASSWORD_FILE"])
            self.assertEqual(password.read_text(), "provider-private-token")
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)
            auth_paths.append(helper.parent)
            return Mock(returncode=0, stdout="")
        injected = {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.https://evil.test/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.test/", "GIT_CONFIG_PARAMETERS": "injected",
            "GIT_DIR": "/unrelated/.git", "GIT_TRACE_CURL": "1",
        }
        with patch.dict(os.environ, injected), patch("luma.gitops.subprocess.run", side_effect=run):
            gitops.clone(
                "https://git.example.test/acme/library.git", Path("/unused"), ref="a" * 40,
                token="provider-private-token", username="provider-user", strict_https_origin=True,
            )
        self.assertEqual(len(commands), 4)
        for command in commands:
            for option in ("http.followRedirects=false", "credential.helper=", "protocol.allow=never", "protocol.https.allow=always", "init.templateDir="):
                self.assertIn(option, command)
        self.assertTrue(all(not path.exists() for path in auth_paths))

    def test_failed_strict_clone_redacts_token_and_removes_credentials(self):
        auth_paths = []
        def run(_command, **kwargs):
            auth_paths.append(Path(kwargs["env"]["GIT_ASKPASS"]).parent)
            return Mock(returncode=1, stdout="failure: provider-private-token")
        with patch("luma.gitops.subprocess.run", side_effect=run), self.assertRaises(LumaError) as error:
            gitops.clone(
                "https://git.example.test/acme/library.git", Path("/unused"),
                token="provider-private-token", strict_https_origin=True,
            )
        self.assertNotIn("provider-private-token", str(error.exception))
        self.assertTrue(all(not path.exists() for path in auth_paths))

    def test_strict_clone_timeout_removes_credentials(self):
        auth_paths = []
        def timeout(command, **kwargs):
            auth_paths.append(Path(kwargs["env"]["GIT_ASKPASS"]).parent)
            raise subprocess.TimeoutExpired(command, gitops.GIT_NETWORK_TIMEOUT)
        with patch("luma.gitops.subprocess.run", side_effect=timeout), self.assertRaisesRegex(LumaError, "timed out"):
            gitops.clone(
                "https://git.example.test/acme/library.git", Path("/unused"),
                token="provider-private-token", strict_https_origin=True,
            )
        self.assertTrue(all(not path.exists() for path in auth_paths))

    def test_strict_clone_rejects_non_https_or_credential_urls_before_subprocess(self):
        for repository in ("http://example.test/repo", "git@example.test:repo", "https://user:token@example.test/repo", "https://example.test/repo?token=secret", "https://example.test/repo\n"):
            with self.subTest(repository=repository), patch("luma.gitops.subprocess.run") as run, self.assertRaises(LumaError):
                gitops.clone(repository, Path("/unused"), strict_https_origin=True)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
