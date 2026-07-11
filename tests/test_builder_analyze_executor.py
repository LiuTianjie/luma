import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma import gitops
from luma.agent import _complete_agent_task, node_agent_capabilities
from luma.builder_executor import (
    BUILDER_ANALYZE_IMAGE_ENV,
    BUILDER_ANALYZE_DOCKER_HOST_ENV,
    BUILDER_EXTERNAL_REGISTRIES_ENV,
    BUILDER_SNAPSHOT_ROOT_ENV,
    BUILDER_TASKS_ENABLED_ENV,
    BUILDER_WORK_ROOT_ENV,
    BuilderCleanupFailed,
    BuilderTaskCanceled,
    _ProcessResult,
    _clone_source,
    _create_deterministic_snapshot,
    _download_object_source,
    _materialize_object_source,
    _harden_rootless_output_tree,
    _prepare_rootless_bind_workspace,
    _rootless_docker_host,
    _rootless_docker_identity,
    _rootless_docker_runtime,
    _run_analyzer_container,
    _run_cancellable_process,
    _remove_runner_container,
    _snapshot_id,
    _validate_runner_output,
    analyze_source,
    builder_analyze_available,
)
from luma.builder_tasks import sanitize_builder_task_result
from luma.errors import LumaError


RUNNER_IMAGE = "registry.internal/lae-agent-runner@sha256:" + ("a" * 64)
ROOTLESS_DOCKER_HOST = "unix:///run/user/1000/docker.sock"
MEDIA_TYPES = {
    "evidence": "application/vnd.lae.evidence+json",
    "deploymentPlan": "application/vnd.lae.deployment-plan+json",
    "buildPlan": "application/vnd.lae.build-plan-proposal+json",
}
FILENAMES = {
    "evidence": "evidence.json",
    "deploymentPlan": "deployment-plan.json",
    "buildPlan": "build-plan-proposal.json",
}


def _git(*args, cwd=None):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout)
    return result.stdout.strip()


def _make_repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    repository.mkdir()
    _git("init", "-q", cwd=repository)
    _git("config", "user.name", "LAE Test", cwd=repository)
    _git("config", "user.email", "lae@example.invalid", cwd=repository)
    (repository / "index.html").write_text("<h1>LAE</h1>\n", encoding="utf-8")
    (repository / "bin").mkdir()
    executable = repository / "bin" / "start"
    executable.write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
    executable.chmod(0o755)
    _git("add", ".", cwd=repository)
    _git("commit", "-q", "-m", "initial", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _artifact_body(name: str, metadata: dict) -> dict:
    if name == "evidence":
        return {
            "schemaVersion": "lae.analysis-evidence/v1",
            "agentVersion": "test",
            "adapter": {"name": "static", "version": "test"},
            "source": {
                "resolvedCommit": metadata["resolvedCommit"],
                "sourceSnapshotId": metadata["sourceSnapshotId"],
                "sourceSnapshotDigest": metadata["sourceSnapshotDigest"],
            },
            "inventory": [],
            "findings": [],
            "environment": [],
            "warnings": [],
            "blockers": [],
            "verdict": "deployable",
            "unsupported": [],
            "ai": {
                "status": "diagnostic_failed",
                "mode": "deterministic_fallback",
                "code": "AI_ANALYSIS_NOT_CONFIGURED",
                "model": None,
                "knowledgeVersion": "2026-07-11.1",
                "manifestCandidate": {},
            },
        }
    if name == "deploymentPlan":
        return {
            "schemaVersion": "lae.deployment-plan/v1",
            "planId": "plan_test",
            "sourceRevisionId": "src_test",
            "sourceDigest": metadata["sourceSnapshotDigest"],
            "kind": "service",
            "services": [
                {
                    "key": "web",
                    "role": "http",
                    "image": {"source": "external", "ref": "nginx:alpine"},
                    "dependencies": [],
                    "environmentNames": [],
                    "resources": {"cpu": "1", "memoryMiB": 128},
                }
            ],
            "routes": [],
            "volumes": [],
            "environment": [],
            "warnings": [],
            "blockers": [],
            "policy": {"version": metadata["policyVersion"], "decision": "allow"},
        }
    if name == "buildPlan":
        return {
            "schemaVersion": "lae.build-plan-proposal/v1",
            "sourceSnapshotDigest": metadata["sourceSnapshotDigest"],
            "resolvedCommit": metadata["resolvedCommit"],
            "policyVersion": metadata["policyVersion"],
            "builds": [],
            "externalImages": [],
        }
    raise AssertionError(name)


def _logical_digest(name: str, body: dict) -> str:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_valid_runner_output(
    input_dir: Path,
    output_dir: Path,
    *,
    external_images: list[dict] | None = None,
) -> None:
    metadata = json.loads((input_dir / "metadata.json").read_text(encoding="utf-8"))
    descriptors = {}
    for name, filename in FILENAMES.items():
        body = _artifact_body(name, metadata)
        if name == "buildPlan" and external_images is not None:
            body["externalImages"] = external_images
        content = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (output_dir / filename).write_bytes(content)
        descriptors[name] = {
            "path": filename,
            "digest": _logical_digest(name, body),
            "mediaType": MEDIA_TYPES[name],
            "sizeBytes": len(content),
        }
    result = {
        "schemaVersion": "lae.agent-analysis-result/v1",
        "status": "succeeded",
        "decision": "allow",
        "externalOperationId": metadata["externalOperationId"],
        "tenantRef": metadata["tenantRef"],
        "applicationRef": metadata["applicationRef"],
        "resolvedCommit": metadata["resolvedCommit"],
        "sourceSnapshotId": metadata["sourceSnapshotId"],
        "sourceSnapshotDigest": metadata["sourceSnapshotDigest"],
        "policyVersion": metadata["policyVersion"],
        "verdict": "deployable",
        "diagnosticStatus": "diagnostic_failed",
        "diagnosticMode": "deterministic_fallback",
        "diagnosticCode": "AI_ANALYSIS_NOT_CONFIGURED",
        "knowledgeVersion": "2026-07-11.1",
        "blockers": [],
        "artifacts": descriptors,
    }
    (output_dir / "result.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class BuilderAnalyzeExecutorTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "builderTaskId": "builder-task-test",
            "externalOperationId": "operation-test",
            "tenantRef": "tenant-test",
            "applicationRef": "application-test",
            "sourceRef": {"repository": "https://example.invalid/acme/app.git", "ref": "main"},
            "credentialLeaseId": "public-source-no-credential",
            "agentImageDigest": RUNNER_IMAGE,
            "policyVersion": "2026-07-11",
            "limits": {"cpu": 1, "memoryMiB": 512, "diskMiB": 256, "timeoutSeconds": 30},
            "externalRegistries": ["docker.io"],
        }
        payload.update(overrides)
        return payload

    def test_local_git_full_commit_and_snapshot_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, expected_commit = _make_repository(root)
            self.assertEqual(gitops.head_commit_full(repository), expected_commit)
            self.assertEqual(len(expected_commit), 40)

            first = root / "first.tar"
            second = root / "second.tar"
            first_result = _create_deterministic_snapshot(
                repository,
                first,
                disk_limit_bytes=256 * 1024 * 1024,
                cancel_event=threading.Event(),
            )
            os.utime(repository / "index.html", (time.time() + 5000, time.time() + 5000))
            second_result = _create_deterministic_snapshot(
                repository,
                second,
                disk_limit_bytes=256 * 1024 * 1024,
                cancel_event=threading.Event(),
            )

            self.assertEqual(first_result, second_result)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertNotIn(b".git/", first.read_bytes())

            (repository / "bin" / "start").chmod(0o644)
            third = root / "third.tar"
            third_result = _create_deterministic_snapshot(
                repository,
                third,
                disk_limit_bytes=256 * 1024 * 1024,
                cancel_event=threading.Event(),
            )
            self.assertNotEqual(first_result[0], third_result[0])
            self.assertNotEqual(first_result[1], third_result[1])

    def test_snapshot_handle_is_task_scoped_while_content_digest_is_shared(self):
        digest = "sha256:" + ("f" * 64)
        first = _snapshot_id(digest, builder_task_id="builder-task-tenant-a")
        second = _snapshot_id(digest, builder_task_id="builder-task-tenant-b")
        self.assertNotEqual(first, second)
        self.assertEqual(first, _snapshot_id(digest, builder_task_id="builder-task-tenant-a"))
        self.assertNotIn("tenant-a", first)

    def test_analyze_source_persists_snapshot_and_returns_only_closed_digest_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, expected_commit = _make_repository(root)
            snapshots = root / "snapshots"
            work = root / "work"

            def local_clone(_repository, destination, **_kwargs):
                result = subprocess.run(
                    ["git", "clone", "-q", "--local", str(repository), str(destination)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self.assertEqual(result.returncode, 0, result.stdout)

            def fake_runner(**kwargs):
                _write_valid_runner_output(kwargs["input_dir"], kwargs["output_dir"])

            environment = {
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(snapshots),
                BUILDER_WORK_ROOT_ENV: str(work),
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            }
            progress = []
            with patch.dict(os.environ, environment, clear=False), patch(
                "luma.builder_executor._clone_source", side_effect=local_clone
            ), patch("luma.builder_executor._run_analyzer_container", side_effect=fake_runner):
                child_payload = self._payload()
                result = analyze_source(child_payload, cancel_event=threading.Event(), progress=progress.append)

            self.assertEqual(result["resolvedCommit"], expected_commit)
            self.assertRegex(result["sourceTreeDigest"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(result["sourceSnapshotDigest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                set(result),
                {
                    "resolvedCommit",
                    "sourceTreeDigest",
                    "sourceSnapshotId",
                    "sourceSnapshotDigest",
                    "deploymentPlanDigest",
                    "buildPlanDigest",
                    "evidenceDigest",
                    "policyVersion",
                    "agentImageDigest",
                    "verdict",
                    "diagnosticStatus",
                    "diagnosticMode",
                    "diagnosticCode",
                    "knowledgeVersion",
                    "blockers",
                    "artifacts",
                },
            )
            self.assertNotIn("example.invalid", json.dumps(result))
            self.assertFalse(any(work.iterdir()))
            stored_tar = list(snapshots.glob("sha256/*/*.tar"))
            stored_artifacts = list(snapshots.glob("artifacts/*/sha256/*/*.json"))
            self.assertEqual(len(stored_tar), 1)
            self.assertEqual(len(stored_artifacts), 3)
            self.assertEqual("sha256:" + hashlib.sha256(stored_tar[0].read_bytes()).hexdigest(), result["sourceSnapshotDigest"])
            self.assertEqual(
                [event["type"] for event in progress],
                ["source.fetch", "source.fetch", "source.snapshot", "source.snapshot", "analysis", "analysis"],
            )
            parent_request = {
                "schemaVersion": "luma.builder-task/v1",
                "kind": "analyze-source",
                "externalOperationId": child_payload["externalOperationId"],
                "tenantRef": child_payload["tenantRef"],
                "applicationRef": child_payload["applicationRef"],
                "payload": {
                    "sourceRef": child_payload["sourceRef"],
                    "credentialLeaseId": child_payload["credentialLeaseId"],
                    "agentImageDigest": child_payload["agentImageDigest"],
                    "policyVersion": child_payload["policyVersion"],
                    "limits": child_payload["limits"],
                },
            }
            self.assertEqual(
                sanitize_builder_task_result("analyze-source", result, request=parent_request),
                result,
            )

    def test_analyze_resolves_external_tag_before_persisting_canonical_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, _expected_commit = _make_repository(root)
            snapshots = root / "snapshots"
            work = root / "work"
            resolved_digest = "sha256:" + ("7" * 64)
            captured = {}

            def local_clone(_repository, destination, **_kwargs):
                subprocess.run(
                    ["git", "clone", "-q", "--local", str(repository), str(destination)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

            def fake_runner(**kwargs):
                _write_valid_runner_output(
                    kwargs["input_dir"],
                    kwargs["output_dir"],
                    external_images=[
                        {
                            "key": "database",
                            "ref": "postgres:17",
                            "platform": "linux/amd64",
                        }
                    ],
                )

            def fake_process(command, **kwargs):
                captured["command"] = list(command)
                captured["env"] = dict(kwargs["env"])
                return _ProcessResult(0, resolved_digest + "\n")

            environment = {
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(snapshots),
                BUILDER_WORK_ROOT_ENV: str(work),
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
                "HTTP_PROXY": "http://proxy.invalid",
                "DOCKER_AUTH_CONFIG": "registry-secret-must-not-reach-crane",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "luma.builder_executor._clone_source", side_effect=local_clone
            ), patch(
                "luma.builder_executor._run_analyzer_container", side_effect=fake_runner
            ), patch(
                "luma.builder_executor._require_crane_runtime", return_value="/tools/crane"
            ), patch(
                "luma.builder_executor._run_cancellable_process", side_effect=fake_process
            ):
                result = analyze_source(
                    self._payload(),
                    cancel_event=threading.Event(),
                )

            self.assertEqual(
                captured["command"],
                ["/tools/crane", "digest", "--platform", "linux/amd64", "postgres:17"],
            )
            self.assertNotIn("HTTP_PROXY", captured["env"])
            self.assertNotIn("DOCKER_AUTH_CONFIG", captured["env"])
            candidate_files = list(
                snapshots.glob("artifacts/build-plan-candidate/sha256/*/*.json")
            )
            self.assertEqual(len(candidate_files), 1)
            candidate_bytes = candidate_files[0].read_bytes()
            candidate = json.loads(candidate_bytes)
            self.assertEqual(
                candidate["externalImages"],
                [
                    {
                        "key": "database",
                        "ref": "postgres:17",
                        "resolvedDigest": resolved_digest,
                        "platform": "linux/amd64",
                    }
                ],
            )
            self.assertEqual(
                result["buildPlanDigest"],
                "sha256:" + hashlib.sha256(candidate_bytes).hexdigest(),
            )
            self.assertEqual(
                result["artifacts"]["buildPlan"]["mediaType"],
                "application/vnd.lae.build-plan-candidate+json",
            )

    def test_uploaded_html_uses_leased_url_but_returns_only_digest_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshots = root / "snapshots"
            work = root / "work"
            content = b"<!doctype html><html><body>Uploaded</body></html>"
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            leased_url = "https://objects.example.test/private/source?X-Amz-Signature=secret"

            def fake_download(url, destination, **kwargs):
                self.assertEqual(url, leased_url)
                self.assertEqual(kwargs["expected_digest"], digest)
                self.assertEqual(kwargs["expected_size"], len(content))
                destination.write_bytes(content)

            def fake_runner(**kwargs):
                self.assertEqual(
                    (kwargs["source_dir"] / "index.html").read_bytes(), content
                )
                _write_valid_runner_output(kwargs["input_dir"], kwargs["output_dir"])

            environment = {
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(snapshots),
                BUILDER_WORK_ROOT_ENV: str(work),
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            }
            payload = self._payload(
                sourceRef={
                    "kind": "object",
                    "digest": digest,
                    "mediaType": "text/html",
                    "sizeBytes": len(content),
                },
                objectUrl=leased_url,
                objectAllowedHost="objects.example.test",
            )
            with patch.dict(os.environ, environment, clear=False), patch(
                "luma.builder_executor._download_object_source",
                side_effect=fake_download,
            ), patch(
                "luma.builder_executor._run_analyzer_container",
                side_effect=fake_runner,
            ):
                result = analyze_source(payload, cancel_event=threading.Event())

            self.assertEqual(result["resolvedCommit"], digest.removeprefix("sha256:"))
            rendered = json.dumps(result, sort_keys=True)
            self.assertNotIn(leased_url, rendered)
            self.assertNotIn("objects.example.test", rendered)
            self.assertFalse(any(work.iterdir()))

    def test_uploaded_source_url_must_match_the_broker_allowed_host(self):
        payload = self._payload(
            sourceRef={
                "kind": "object",
                "digest": "sha256:" + ("b" * 64),
                "mediaType": "text/html",
                "sizeBytes": 32,
            },
            objectUrl="https://attacker.example/private/source?secret=host-canary",
            objectAllowedHost="objects.example.test",
        )
        download = Mock(side_effect=AssertionError("download must not start"))
        with patch.dict(
            os.environ,
            {
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            },
            clear=False,
        ), patch(
            "luma.builder_executor._download_object_source",
            download,
        ), self.assertRaisesRegex(LumaError, "leased object URL is invalid"):
            analyze_source(payload, cancel_event=threading.Event())
        download.assert_not_called()

    def test_uploaded_source_download_rejects_redirects_and_hides_location(self):
        redirect_url = "https://attacker.example/private?secret=redirect-canary"
        captured = {}

        class RedirectingOpener:
            def __init__(self, redirect_handler):
                self.redirect_handler = redirect_handler

            def open(self, request, timeout):
                captured["timeout"] = timeout
                redirected = self.redirect_handler.redirect_request(
                    request,
                    None,
                    302,
                    "redirect",
                    {"Location": redirect_url},
                    redirect_url,
                )
                if redirected is not None:
                    raise AssertionError("object source redirect was followed")
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": redirect_url},
                    None,
                )

        def opener_factory(*handlers):
            redirect_handler = next(
                handler
                for handler in handlers
                if handler.__class__.__name__ == "_RejectObjectRedirects"
            )
            return RedirectingOpener(redirect_handler)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "source.bin"
            with patch(
                "luma.builder_executor.urllib.request.build_opener",
                side_effect=opener_factory,
            ), self.assertRaises(LumaError) as raised:
                _download_object_source(
                    "https://objects.example.test/private/source?secret=request-canary",
                    destination,
                    expected_digest="sha256:" + ("b" * 64),
                    expected_size=32,
                    cancel_event=threading.Event(),
                    timeout=30,
                    workspace_root=root,
                    disk_limit_bytes=1024,
                )

            self.assertEqual(str(raised.exception), "uploaded source download failed")
            self.assertNotIn("redirect-canary", str(raised.exception))
            self.assertNotIn("request-canary", str(raised.exception))
            self.assertFalse(destination.exists())
            self.assertEqual(captured["timeout"], 30)

    def test_uploaded_zip_is_revalidated_before_materialization(self):
        def archive(entries):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as handle:
                for name, content in entries:
                    handle.writestr(name, content)
            return output.getvalue()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.zip"
            source.write_bytes(
                archive(
                    [
                        ("index.html", b"<!doctype html><html></html>"),
                        ("assets/app.css", b"body{}"),
                    ]
                )
            )
            destination = root / "checkout"
            _materialize_object_source(
                source,
                destination,
                media_type="application/zip",
                cancel_event=threading.Event(),
                workspace_root=root,
                disk_limit_bytes=16 * 1024 * 1024,
            )
            self.assertTrue((destination / "index.html").is_file())
            self.assertTrue((destination / "assets" / "app.css").is_file())

        for unsafe_name in ("../index.html", "/index.html", "assets\\app.js"):
            with self.subTest(path=unsafe_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.zip"
                source.write_bytes(
                    archive(
                        [
                            ("index.html", b"<!doctype html><html></html>"),
                            (unsafe_name, b"unsafe"),
                        ]
                    )
                )
                with self.assertRaises(LumaError):
                    _materialize_object_source(
                        source,
                        root / "checkout",
                        media_type="application/zip",
                        cancel_event=threading.Event(),
                        workspace_root=root,
                        disk_limit_bytes=16 * 1024 * 1024,
                    )

    def test_analyze_external_registry_policy_mismatch_fails_before_clone(self):
        runner = Mock(side_effect=AssertionError("source or network process must not start"))
        with patch.dict(
            os.environ,
            {
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            },
            clear=False,
        ), patch("luma.builder_executor._clone_source", runner):
            with self.assertRaisesRegex(LumaError, "does not match node policy"):
                analyze_source(
                    self._payload(externalRegistries=["docker.io", "ghcr.io"]),
                    cancel_event=threading.Event(),
                )
        runner.assert_not_called()

    def test_git_token_uses_askpass_paths_and_never_appears_in_argv_or_environment(self):
        captured = {}
        secret = "ghp_this-token-must-not-appear-in-argv"

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs["env"])
            self.assertTrue(Path(kwargs["env"]["LUMA_GIT_PASSWORD_FILE"]).is_file())
            self.assertEqual(Path(kwargs["env"]["LUMA_GIT_PASSWORD_FILE"]).read_text(encoding="utf-8"), secret)
            return _ProcessResult(0, "")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "luma.builder_executor._run_cancellable_process", side_effect=fake_run
        ):
            _clone_source(
                "https://github.com/acme/app.git",
                Path(temporary) / "checkout",
                ref="main",
                git_token=secret,
                git_username="octocat",
                cancel_event=threading.Event(),
                timeout=30,
            )

        self.assertNotIn(secret, captured["command"])
        self.assertNotIn(secret, captured["env"].values())
        self.assertEqual(captured["command"][-2:], ["https://github.com/acme/app.git", str(Path(temporary) / "checkout")])

    def test_full_commit_ref_is_fetched_and_checked_out_detached(self):
        commands = []
        commit = "f829276c68503f7afae195c3e3f778f085242cb0"

        def fake_run(command, **_kwargs):
            commands.append(list(command))
            return _ProcessResult(0, "")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "luma.builder_executor._run_cancellable_process", side_effect=fake_run
        ):
            destination = Path(temporary) / "checkout"
            _clone_source(
                "https://github.com/render-examples/fastapi.git",
                destination,
                ref=commit,
                git_token="",
                git_username="",
                cancel_event=threading.Event(),
                timeout=30,
            )

        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0][-3:], ["init", "--", str(destination)])
        self.assertEqual(commands[1][-4:], ["remote", "add", "origin", "https://github.com/render-examples/fastapi.git"])
        self.assertEqual(commands[2][-5:], ["--depth", "1", "--no-tags", "origin", commit])
        self.assertEqual(commands[3][-3:], ["checkout", "--detach", "FETCH_HEAD"])
        self.assertNotIn("--branch", [item for command in commands for item in command])

    def test_runner_argv_enforces_isolation_limits_and_fixed_command(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs["env"])
            return _ProcessResult(0, "")

        inherited = {
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONFIG": "/home/agent/.docker-with-credentials",
            "HTTP_PROXY": "http://proxy.invalid:3128",
            "DOCKER_CLI_HINTS": "1",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, inherited, clear=False), patch(
            "luma.builder_executor._rootless_docker_runtime",
            return_value=("/usr/bin/docker", ROOTLESS_DOCKER_HOST),
        ), patch(
            "luma.builder_executor._rootless_docker_identity",
            return_value=(1000, 1000),
        ), patch(
            "luma.builder_executor._prepare_rootless_bind_workspace"
        ) as prepare, patch(
            "luma.builder_executor._harden_rootless_output_tree"
        ) as harden, patch(
            "luma.builder_executor._run_cancellable_process", side_effect=fake_run
        ), patch(
            "luma.builder_executor._remove_runner_container", return_value=True
        ):
            root = Path(temporary)
            for name in ("source", "input", "output"):
                (root / name).mkdir()
            _run_analyzer_container(
                image=RUNNER_IMAGE,
                builder_task_id="builder-task-argv",
                source_dir=root / "source",
                input_dir=root / "input",
                output_dir=root / "output",
                limits={"cpu": 0.5, "memoryMiB": 768, "diskMiB": 256},
                cancel_event=threading.Event(),
                timeout=30,
            )

        command = captured["command"]
        self.assertEqual(
            command[:7],
            ["/usr/bin/docker", "--host", ROOTLESS_DOCKER_HOST, "run", "--rm", "--pull", "never"],
        )
        docker_environment = captured["env"]
        self.assertEqual(set(docker_environment), {"PATH", "HOME", "DOCKER_CONFIG", "LANG"})
        self.assertNotEqual(docker_environment["DOCKER_CONFIG"], inherited["DOCKER_CONFIG"])
        self.assertNotIn("DOCKER_HOST", docker_environment)
        self.assertNotIn("HTTP_PROXY", docker_environment)
        self.assertIn("none", command)
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        self.assertEqual(command[command.index("--security-opt") + 1], "no-new-privileges:true")
        self.assertEqual(command[command.index("--user") + 1], "0:0")
        self.assertEqual(command[command.index("--memory") + 1], "768m")
        self.assertEqual(command[command.index("--cpus") + 1], "0.5")
        self.assertEqual(command[command.index("--entrypoint") + 1], "lae-agent-runner")
        image_index = command.index(RUNNER_IMAGE)
        self.assertEqual(
            command[image_index + 1 :],
            [
                "analyze",
                "--source",
                "/workspace",
                "--metadata",
                "/input/metadata.json",
                "--output-dir",
                "/output",
            ],
        )
        prepare.assert_called_once_with(
            source_dir=root / "source",
            input_dir=root / "input",
            output_dir=root / "output",
            daemon_uid=1000,
            daemon_gid=1000,
        )
        harden.assert_called_once_with(root / "output", daemon_uid=1000, daemon_gid=1000)

    def test_runner_cleanup_requires_inspect_proof_and_cleanup_failure_wins_over_cancel(self):
        removed = Mock(returncode=0, stdout="")
        missing = Mock(returncode=1, stdout="Error: No such object: luma-lae-analyze-test")
        docker_environment = {"PATH": "/usr/bin", "HOME": "/tmp/empty", "DOCKER_CONFIG": "/tmp/empty"}
        with patch("luma.builder_executor.subprocess.run", side_effect=[removed, missing]) as run:
            self.assertTrue(
                _remove_runner_container(
                    "docker",
                    ROOTLESS_DOCKER_HOST,
                    "luma-lae-analyze-test",
                    env=docker_environment,
                )
            )
        self.assertEqual(run.call_count, 2)
        self.assertIn("inspect", run.call_args_list[1].args[0])
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:3], ["--host", ROOTLESS_DOCKER_HOST])
            self.assertEqual(call.kwargs["env"], docker_environment)

        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "luma.builder_executor._rootless_docker_runtime",
            return_value=("docker", ROOTLESS_DOCKER_HOST),
        ), patch(
            "luma.builder_executor._rootless_docker_identity",
            return_value=(1000, 1000),
        ), patch(
            "luma.builder_executor._prepare_rootless_bind_workspace"
        ), patch(
            "luma.builder_executor._run_cancellable_process",
            side_effect=BuilderTaskCanceled("builder task canceled"),
        ), patch("luma.builder_executor._remove_runner_container", return_value=False):
            root = Path(temporary)
            for name in ("source", "input", "output"):
                (root / name).mkdir()
            with self.assertRaises(BuilderCleanupFailed):
                _run_analyzer_container(
                    image=RUNNER_IMAGE,
                    builder_task_id="builder-task-cleanup-failure",
                    source_dir=root / "source",
                    input_dir=root / "input",
                    output_dir=root / "output",
                    limits={"cpu": 1, "memoryMiB": 512, "diskMiB": 256},
                    cancel_event=cancel,
                    timeout=30,
                )
        import luma.builder_executor as executor

        executor._BUILDER_ANALYZE_CLEANUP_HEALTHY = True

    def test_bind_workspace_is_scoped_to_verified_daemon_without_touching_snapshot_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot-store" / "source.tar"
            snapshot.parent.mkdir()
            snapshot.write_bytes(b"immutable snapshot")
            snapshot.chmod(0o600)
            snapshot_before = snapshot.stat()

            work = root / "work" / "task"
            source = work / "source"
            input_dir = work / "input"
            output = work / "output"
            (source / "nested").mkdir(parents=True)
            input_dir.mkdir()
            output.mkdir()
            (source / "nested" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "alias.py").symlink_to("nested/app.py")
            (input_dir / "metadata.json").write_text("{}", encoding="utf-8")
            work.chmod(0o700)

            uid, gid = os.getuid(), os.getgid()
            with patch("luma.builder_executor.os.fchown") as fchown:
                _prepare_rootless_bind_workspace(
                    source_dir=source,
                    input_dir=input_dir,
                    output_dir=output,
                    daemon_uid=uid,
                    daemon_gid=gid,
                )

            self.assertGreater(fchown.call_count, 0)
            self.assertTrue(all(call.args[1:] == (uid, gid) for call in fchown.call_args_list))
            self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o500)
            self.assertEqual(stat.S_IMODE((source / "nested").stat().st_mode), 0o500)
            self.assertEqual(stat.S_IMODE((source / "nested" / "app.py").stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE(input_dir.stat().st_mode), 0o500)
            self.assertEqual(stat.S_IMODE((input_dir / "metadata.json").stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertTrue((source / "alias.py").is_symlink())
            snapshot_after = snapshot.stat()
            self.assertEqual((snapshot_after.st_uid, snapshot_after.st_gid), (snapshot_before.st_uid, snapshot_before.st_gid))
            self.assertEqual(stat.S_IMODE(snapshot_after.st_mode), 0o600)

    def test_bind_workspace_syscall_failure_rolls_back_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "task"
            source = work / "source"
            input_dir = work / "input"
            output = work / "output"
            source.mkdir(parents=True)
            input_dir.mkdir()
            output.mkdir()
            source_file = source / "app.py"
            source_file.write_text("print('ok')\n", encoding="utf-8")
            metadata = input_dir / "metadata.json"
            metadata.write_text("{}", encoding="utf-8")
            original_modes = {
                path: stat.S_IMODE(path.stat().st_mode)
                for path in (work, source, source_file, input_dir, metadata, output)
            }
            real_fchmod = os.fchmod
            calls = 0

            def fail_once(fd, mode):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated ownership syscall failure")
                return real_fchmod(fd, mode)

            with patch("luma.builder_executor.os.fchmod", side_effect=fail_once):
                with self.assertRaisesRegex(LumaError, "ownership could not be prepared"):
                    _prepare_rootless_bind_workspace(
                        source_dir=source,
                        input_dir=input_dir,
                        output_dir=output,
                        daemon_uid=os.getuid(),
                        daemon_gid=os.getgid(),
                    )

            self.assertGreater(calls, 2, "rollback must invoke fchmod after the injected failure")
            for path, expected_mode in original_modes.items():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode, path)

    def test_output_hardening_rejects_symlinks_and_removes_world_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            nested = output / "nested"
            nested.mkdir(parents=True)
            result = nested / "result.json"
            result.write_text("{}", encoding="utf-8")
            uid, gid = os.getuid(), os.getgid()

            _harden_rootless_output_tree(output, daemon_uid=uid, daemon_gid=gid)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            alias = output / "result-link.json"
            alias.symlink_to("nested/result.json")
            with self.assertRaisesRegex(LumaError, "contains a symlink"):
                _harden_rootless_output_tree(output, daemon_uid=uid, daemon_gid=gid)

    def test_process_group_is_terminated_when_cancel_event_is_set(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(BuilderTaskCanceled):
                _run_cancellable_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=30,
                    cancel_event=cancel,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_process_is_terminated_when_workspace_exceeds_soft_disk_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = (
                "import pathlib,sys,time; "
                "p=pathlib.Path(sys.argv[1]); f=(p/'growth.bin').open('wb'); "
                "[(f.write(b'x'*262144),f.flush(),time.sleep(.05)) for _ in range(40)]"
            )
            started = time.monotonic()
            with self.assertRaisesRegex(LumaError, "disk budget"):
                _run_cancellable_process(
                    [sys.executable, "-c", script, str(root)],
                    timeout=30,
                    cancel_event=threading.Event(),
                    disk_watch_path=root,
                    disk_limit_bytes=1024 * 1024,
                )
            self.assertLess(time.monotonic() - started, 4)

    def test_analyze_cancellation_always_removes_checkout_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            snapshots = root / "snapshots"
            cancel = threading.Event()

            def canceled_clone(_repository, destination, **_kwargs):
                destination.mkdir(parents=True)
                (destination / "partial-secret.txt").write_text("must be removed", encoding="utf-8")
                cancel.set()
                raise BuilderTaskCanceled("builder task canceled")

            with patch.dict(
                os.environ,
                {
                    BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                    BUILDER_SNAPSHOT_ROOT_ENV: str(snapshots),
                    BUILDER_WORK_ROOT_ENV: str(work),
                    BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
                },
                clear=False,
            ), patch("luma.builder_executor._clone_source", side_effect=canceled_clone):
                with self.assertRaises(BuilderTaskCanceled):
                    analyze_source(self._payload(), cancel_event=cancel)

            self.assertTrue(work.is_dir())
            self.assertFalse(any(work.iterdir()))

    def test_busy_heartbeat_propagates_cancel_and_reports_canceled_not_success(self):
        client = Mock()
        client.heartbeat_agent.return_value = {"cancelRequested": True}

        def wait_for_cancel(_task, *, cancel_event, **_kwargs):
            self.assertTrue(cancel_event.wait(1))
            raise BuilderTaskCanceled("builder task canceled")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 0.01}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", side_effect=wait_for_cancel), patch(
                "luma.agent.node_agent_capabilities", return_value=["builder-analyze-v1"]
            ), patch("luma.agent.node_agent_metrics", return_value={}):
                _complete_agent_task(
                    client,
                    node_name="builder",
                    node_id="builder-node",
                    task={"id": "agent-task-cancel", "action": "analyze-source", "payload": {}},
                    config_path=config,
                )

        self.assertEqual(client.heartbeat_agent.call_args.kwargs["active_task_id"], "agent-task-cancel")
        self.assertEqual(client.complete_agent_task.call_args.kwargs["status"], "canceled")
        self.assertNotEqual(client.complete_agent_task.call_args.kwargs["status"], "succeeded")

    def test_cleanup_failure_is_reported_failed_even_after_cancel_request(self):
        client = Mock()
        client.heartbeat_agent.return_value = {"cancelRequested": True}

        def fail_cleanup(_task, *, cancel_event, **_kwargs):
            self.assertTrue(cancel_event.wait(1))
            raise BuilderCleanupFailed("builder runner cleanup could not be verified")

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 0.01}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", side_effect=fail_cleanup), patch(
                "luma.agent.node_agent_capabilities", return_value=[]
            ), patch("luma.agent.node_agent_metrics", return_value={}):
                _complete_agent_task(
                    client,
                    node_name="builder",
                    node_id="builder-node",
                    task={"id": "agent-task-cleanup", "action": "analyze-source", "payload": {}},
                    config_path=config,
                )

        self.assertEqual(client.complete_agent_task.call_args.kwargs["status"], "failed")
        self.assertEqual(client.complete_agent_task.call_args.kwargs["message"], "builder sandbox cleanup failed")

    def test_invalid_runner_artifact_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            metadata = {
                "builderTaskId": "builder-task-invalid",
                "externalOperationId": "operation-invalid",
                "tenantRef": "tenant-invalid",
                "applicationRef": "application-invalid",
                "resolvedCommit": "b" * 40,
                "sourceTreeDigest": "sha256:" + ("c" * 64),
                "sourceSnapshotId": "snapshot-invalid",
                "sourceSnapshotDigest": "sha256:" + ("d" * 64),
                "policyVersion": "2026-07-11",
                "agentImageDigest": RUNNER_IMAGE,
            }
            (input_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            _write_valid_runner_output(input_dir, output_dir)
            result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
            result["artifacts"]["buildPlan"]["digest"] = "sha256:" + ("0" * 64)
            (output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

            with self.assertRaisesRegex(LumaError, "artifact digest mismatch"):
                _validate_runner_output(output_dir, metadata)

    def test_rootless_docker_host_rejects_default_socket_override_and_rootful_peer(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LumaError, "must explicitly name"):
                _rootless_docker_host()
        with patch.dict(os.environ, {"DOCKER_HOST": ROOTLESS_DOCKER_HOST}, clear=True):
            with self.assertRaisesRegex(LumaError, "must explicitly name"):
                _rootless_docker_host()

        for invalid in (
            "unix:///var/run/docker.sock",
            "unix:///run/user/0/docker.sock",
            "tcp://127.0.0.1:2375",
            "unix:///run/user/1000/other.sock",
        ):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ,
                {BUILDER_ANALYZE_DOCKER_HOST_ENV: invalid},
                clear=False,
            ):
                with self.assertRaisesRegex(LumaError, "must explicitly name"):
                    _rootless_docker_host()

        class RootfulPeerSocket:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def getsockopt(self, _level, _option, _size):
                return struct.pack("3i", 4321, 0, 0)

            def close(self):
                pass

        def fake_lstat(path):
            if str(path).endswith("docker.sock"):
                return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000, st_gid=1000)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)

        with patch.dict(
            os.environ,
            {BUILDER_ANALYZE_DOCKER_HOST_ENV: ROOTLESS_DOCKER_HOST},
            clear=False,
        ), patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat), patch(
            "luma.builder_executor.socket.socket", return_value=RootfulPeerSocket()
        ), patch("luma.builder_executor.socket.SO_PEERCRED", 17, create=True):
            with self.assertRaisesRegex(LumaError, "peer credentials"):
                _rootless_docker_host()

        for socket_uid, expected_error in ((0, "non-root-owned"), (1001, "owner does not match")):
            def owner_lstat(path, uid=socket_uid):
                if str(path).endswith("docker.sock"):
                    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=uid, st_gid=1000)
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)

            with self.subTest(socket_uid=socket_uid), patch.dict(
                os.environ,
                {BUILDER_ANALYZE_DOCKER_HOST_ENV: ROOTLESS_DOCKER_HOST},
                clear=False,
            ), patch.object(Path, "lstat", autospec=True, side_effect=owner_lstat), patch(
                "luma.builder_executor._unix_socket_peer_credentials", return_value=(4321, 1000, 1000)
            ):
                with self.assertRaisesRegex(LumaError, expected_error):
                    _rootless_docker_host()

        with patch.dict(
            os.environ,
            {BUILDER_ANALYZE_DOCKER_HOST_ENV: ROOTLESS_DOCKER_HOST},
            clear=False,
        ), patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat), patch(
            "luma.builder_executor._unix_socket_peer_credentials", return_value=(4321, 1000, 1000)
        ):
            self.assertEqual(_rootless_docker_host(), ROOTLESS_DOCKER_HOST)
            self.assertEqual(_rootless_docker_identity(ROOTLESS_DOCKER_HOST), (1000, 1000))

        def wrong_group_lstat(path):
            if str(path).endswith("docker.sock"):
                return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000, st_gid=1001)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)

        with patch.object(Path, "lstat", autospec=True, side_effect=wrong_group_lstat), patch(
            "luma.builder_executor._unix_socket_peer_credentials", return_value=(4321, 1000, 1000)
        ):
            with self.assertRaisesRegex(LumaError, "peer credentials"):
                _rootless_docker_identity(ROOTLESS_DOCKER_HOST)

    def test_rootless_runtime_proves_security_options_and_local_runner_digest_with_isolated_env(self):
        rootless_info = Mock(returncode=0, stdout=json.dumps(["name=seccomp,profile=builtin", "name=rootless"]))
        image_inspect = Mock(returncode=0, stdout=json.dumps([RUNNER_IMAGE]))
        inherited = {
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONFIG": "/home/agent/.docker-with-credentials",
            "HTTPS_PROXY": "http://proxy.invalid:3128",
        }
        with patch.dict(
            os.environ,
            {**inherited, BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE},
            clear=False,
        ), patch("luma.builder_executor.sys.platform", "linux"), patch(
            "luma.builder_executor._docker_binary", return_value="/usr/bin/docker"
        ), patch(
            "luma.builder_executor._rootless_docker_host", return_value=ROOTLESS_DOCKER_HOST
        ), patch(
            "luma.builder_executor.subprocess.run", side_effect=[rootless_info, image_inspect]
        ) as run:
            self.assertEqual(
                _rootless_docker_runtime(RUNNER_IMAGE),
                ("/usr/bin/docker", ROOTLESS_DOCKER_HOST),
            )

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:3], ["--host", ROOTLESS_DOCKER_HOST])
            self.assertEqual(set(call.kwargs["env"]), {"PATH", "HOME", "DOCKER_CONFIG", "LANG"})
            self.assertNotIn("DOCKER_HOST", call.kwargs["env"])
            self.assertNotIn("HTTPS_PROXY", call.kwargs["env"])

        rootful_info = Mock(returncode=0, stdout=json.dumps(["name=seccomp,profile=builtin"]))
        with patch.dict(
            os.environ,
            {BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE},
            clear=False,
        ), patch("luma.builder_executor.sys.platform", "linux"), patch(
            "luma.builder_executor._docker_binary", return_value="/usr/bin/docker"
        ), patch(
            "luma.builder_executor._rootless_docker_host", return_value=ROOTLESS_DOCKER_HOST
        ), patch("luma.builder_executor.subprocess.run", return_value=rootful_info):
            with self.assertRaisesRegex(LumaError, "did not prove rootless"):
                _rootless_docker_runtime(RUNNER_IMAGE)

        wrong_local_digest = Mock(
            returncode=0,
            stdout=json.dumps(["registry.internal/lae-agent-runner@sha256:" + ("b" * 64)]),
        )
        with patch.dict(
            os.environ,
            {BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE},
            clear=False,
        ), patch("luma.builder_executor.sys.platform", "linux"), patch(
            "luma.builder_executor._docker_binary", return_value="/usr/bin/docker"
        ), patch(
            "luma.builder_executor._rootless_docker_host", return_value=ROOTLESS_DOCKER_HOST
        ), patch(
            "luma.builder_executor.subprocess.run", side_effect=[rootless_info, wrong_local_digest]
        ):
            with self.assertRaisesRegex(LumaError, "does not match its allowlisted digest"):
                _rootless_docker_runtime(RUNNER_IMAGE)

        with patch.dict(
            os.environ,
            {BUILDER_ANALYZE_IMAGE_ENV: "registry.internal/other-runner@sha256:" + ("c" * 64)},
            clear=False,
        ), patch("luma.builder_executor.sys.platform", "linux"):
            with self.assertRaisesRegex(LumaError, "is not allowlisted"):
                _rootless_docker_runtime(RUNNER_IMAGE)

    def test_capability_fails_closed_on_rootless_runtime_timeout_or_os_error(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                BUILDER_TASKS_ENABLED_ENV: "1",
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(Path(temporary) / "snapshots"),
            },
            clear=False,
        ), patch("luma.builder_executor.shutil.which", return_value="/usr/bin/git"):
            for failure in (
                subprocess.TimeoutExpired(["docker", "info"], 5),
                OSError("socket unavailable"),
            ):
                with self.subTest(failure=type(failure).__name__), patch(
                    "luma.builder_executor._rootless_docker_runtime", side_effect=failure
                ):
                    self.assertFalse(builder_analyze_available("linux"))
            with patch(
                "luma.builder_executor._rootless_docker_runtime",
                return_value=("/usr/bin/docker", ROOTLESS_DOCKER_HOST),
            ):
                self.assertFalse(builder_analyze_available("darwin"))

    def test_capability_fails_closed_without_resolver_or_valid_external_policy(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                BUILDER_TASKS_ENABLED_ENV: "1",
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(Path(temporary) / "snapshots"),
                BUILDER_WORK_ROOT_ENV: str(Path(temporary) / "work"),
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            },
            clear=False,
        ), patch(
            "luma.builder_executor._require_git_runtime", return_value="/tools/git"
        ), patch(
            "luma.builder_executor._rootless_docker_runtime",
            return_value=("/tools/docker", ROOTLESS_DOCKER_HOST),
        ):
            with patch(
                "luma.builder_executor._require_crane_runtime",
                side_effect=LumaError("missing crane"),
            ):
                self.assertFalse(builder_analyze_available("linux"))
            with patch(
                "luma.builder_executor._require_crane_runtime",
                return_value="/tools/crane",
            ), patch(
                "luma.builder_executor._local_external_registry_allowlist",
                side_effect=LumaError("invalid policy"),
            ):
                self.assertFalse(builder_analyze_available("linux"))

    def test_capability_is_opt_in_pinned_and_does_not_claim_aggregate_or_build(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                BUILDER_TASKS_ENABLED_ENV: "1",
                BUILDER_ANALYZE_IMAGE_ENV: RUNNER_IMAGE,
                BUILDER_SNAPSHOT_ROOT_ENV: str(Path(temporary) / "snapshots"),
            },
            clear=False,
        ), patch("luma.builder_executor.shutil.which", return_value="/usr/bin/git"), patch(
            "luma.builder_executor._rootless_docker_runtime",
            return_value=("/usr/bin/docker", ROOTLESS_DOCKER_HOST),
        ), patch("luma.agent._docker_buildx_available", return_value=False):
            capabilities = node_agent_capabilities("linux")

        self.assertIn("builder-analyze-v1", capabilities)
        self.assertNotIn("builder-task-v1", capabilities)
        self.assertNotIn("builder-build-v1", capabilities)

        with patch.dict(os.environ, {BUILDER_TASKS_ENABLED_ENV: "0"}, clear=False):
            self.assertNotIn("builder-analyze-v1", node_agent_capabilities("linux"))


if __name__ == "__main__":
    unittest.main()
