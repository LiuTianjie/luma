import base64
import hashlib
import json
import os
import stat
import struct
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma.builder_build_executor import (
    BUILDER_ALLOW_ANONYMOUS_REGISTRY_ENV,
    BUILDER_BUILD_ENABLED_ENV,
    BUILDER_EXTERNAL_REGISTRIES_ENV,
    BUILDER_REGISTRY_INSECURE_ENV,
    BUILDER_REGISTRY_PULL_HOST_ENV,
    BUILDER_REGISTRY_PUSH_HOST_ENV,
    BUILDER_TRIVY_CACHE_ENV,
    BUILDKIT_ADDR_ENV,
    _CommandResult,
    _RuntimePrerequisites,
    _rootless_buildkit_addr,
    _run_command,
    _validate_build_lease,
    build_plan,
    builder_build_available,
)
from luma.builder_executor import (
    BUILDER_SNAPSHOT_ROOT_ENV,
    BUILDER_TASKS_ENABLED_ENV,
    BuilderTaskCanceled,
    BuilderTaskTimedOut,
)
from luma.builder_tasks import builder_registry_repository, sanitize_builder_task_result
from luma.control import server as control_server
from luma.errors import LumaError


SNAPSHOT_DIGEST_PLACEHOLDER = "sha256:" + ("a" * 64)
IMAGE_DIGESTS = {
    "base": "sha256:" + ("1" * 64),
    "web": "sha256:" + ("2" * 64),
}
SECRET_SENTINEL = "registry-or-build-secret-must-not-reach-processes"


class BuilderBuildExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot_root = self.root / "snapshots"
        self.work_root = self.root / "work"
        self.work_root.mkdir()
        self.trivy_cache = self.root / "trivy-cache"
        (self.trivy_cache / "db").mkdir(parents=True)
        (self.trivy_cache / "db" / "metadata.json").write_text("{}", encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {
                BUILDER_SNAPSHOT_ROOT_ENV: str(self.snapshot_root),
                "LUMA_BUILDER_WORK_ROOT": str(self.work_root),
                BUILDER_TASKS_ENABLED_ENV: "1",
                BUILDER_BUILD_ENABLED_ENV: "1",
                BUILDER_ALLOW_ANONYMOUS_REGISTRY_ENV: "1",
                BUILDER_REGISTRY_PULL_HOST_ENV: "100.66.177.70:5000",
                BUILDER_REGISTRY_PUSH_HOST_ENV: "localhost:5000",
                BUILDER_REGISTRY_INSECURE_ENV: "1",
                BUILDER_TRIVY_CACHE_ENV: str(self.trivy_cache),
                BUILDER_EXTERNAL_REGISTRIES_ENV: json.dumps(["docker.io"]),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def _install_snapshot(self, files, *, symlinks=None):
        source_tar = self.root / f"source-{time.time_ns()}.tar"
        with tarfile.open(source_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(name)
                encoded = content.encode("utf-8") if isinstance(content, str) else bytes(content)
                info.size = len(encoded)
                info.mode = 0o644
                info.uid = info.gid = 0
                info.mtime = 0
                archive.addfile(info, fileobj=__import__("io").BytesIO(encoded))
            for name, target in sorted((symlinks or {}).items()):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                info.uid = info.gid = 0
                info.mtime = 0
                archive.addfile(info)
        digest = "sha256:" + hashlib.sha256(source_tar.read_bytes()).hexdigest()
        hexadecimal = digest.split(":", 1)[1]
        destination = self.snapshot_root / "sha256" / hexadecimal[:2] / f"{hexadecimal}.tar"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source_tar.read_bytes())
        return digest, destination

    def _payload(self, digest, builds, *, external_images=None, repositories=None, extra=None):
        principal = "lae-service"
        tenant = "tenant-build-test"
        application = "application-build-test"
        repository_map = repositories
        if repository_map is None:
            repository_map = {
                build["key"]: builder_registry_repository(principal, tenant, application, build["key"])
                for build in builds
            }
        payload = {
            "builderTaskId": "builder-task-build-test",
            "schemaVersion": "luma.builder-task/v1",
            "externalOperationId": "operation-build-test",
            "tenantRef": tenant,
            "applicationRef": application,
            "principalRef": principal,
            "sourceSnapshotId": "snapshot-build-test",
            "sourceSnapshotDigest": digest,
            "signedBuildPlan": {
                "schemaVersion": "lae.build-plan/v1",
                "sourceSnapshotDigest": digest,
                "resolvedCommit": "d" * 40,
                "policyVersion": "2026-07-11",
                "builds": builds,
                "externalImages": [],
                "signature": {"keyId": "lae-plan-test", "value": "A" * 43},
            },
            "credentialLeaseId": "credential-lease-build-test",
            "limits": {"cpu": 2, "memoryMiB": 2048, "diskMiB": 256, "timeoutSeconds": 30},
            "registry": {
                "schemaVersion": "luma.builder-registry-lease/v1",
                "pullHost": "100.66.177.70:5000",
                "pushHost": "localhost:5000",
                "repositories": repository_map,
                "externalRegistries": ["docker.io"],
                "insecure": True,
                "authMode": "anonymous",
            },
        }
        if extra:
            payload.update(extra)
        payload["signedBuildPlan"]["externalImages"] = list(external_images or [])
        return payload

    @staticmethod
    def _build(key, *, context=".", dockerfile="Dockerfile", depends=None, args=None, secrets=None, target=None):
        return {
            "key": key,
            "context": context,
            "dockerfile": dockerfile,
            "target": target,
            "platform": "linux/amd64",
            "buildArgNames": list(args or []),
            "secretMountNames": list(secrets or []),
            "dependsOnBuilds": list(depends or []),
        }

    @staticmethod
    def _external(key, ref="postgres:17", resolved_digest=None):
        return {
            "key": key,
            "ref": ref,
            "resolvedDigest": resolved_digest or "sha256:" + ("3" * 64),
            "platform": "linux/amd64",
        }

    def _prerequisites(self):
        return _RuntimePrerequisites(
            buildctl="/tools/buildctl",
            syft="/tools/syft",
            trivy="/tools/trivy",
            cosign="/tools/cosign",
            crane="/tools/crane",
            buildkit_addr="unix:///run/user/1000/buildkit/buildkitd.sock",
            trivy_cache=self.trivy_cache,
        )

    def test_multi_build_executes_topologically_and_returns_supply_chain_artifacts(self):
        digest, _snapshot = self._install_snapshot(
            {
                "Dockerfile": "FROM scratch\n",
                "services/web/Dockerfile": "FROM scratch\n",
            }
        )
        builds = [
            self._build("web", context="services/web", dockerfile="services/web/Dockerfile", depends=["base"]),
            self._build("base"),
        ]
        payload = self._payload(digest, builds)
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(list(command))
            executable = command[0]
            if executable.endswith("buildctl"):
                metadata = Path(command[command.index("--metadata-file") + 1])
                output = next(value for value in command if value.startswith("type=image,name="))
                key = "base" if "/base:" in output else "web"
                metadata.write_text(json.dumps({"containerimage.digest": IMAGE_DIGESTS[key]}), encoding="utf-8")
                return _CommandResult(0, b"")
            if executable.endswith("syft"):
                output_arg = command[command.index("--output") + 1]
                output_path = Path(output_arg.split("=", 1)[1])
                output_path.write_text(
                    json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}),
                    encoding="utf-8",
                )
                return _CommandResult(0, b"")
            if executable.endswith("trivy"):
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(json.dumps({"Results": []}), encoding="utf-8")
                return _CommandResult(0, b"")
            if executable.endswith("cosign"):
                image_digest = command[-1].rsplit("@", 1)[1]
                statement = {
                    "_type": "https://in-toto.io/Statement/v1",
                    "subject": [{"name": "image", "digest": {"sha256": image_digest.split(":", 1)[1]}}],
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {},
                }
                envelope = {
                    "payloadType": "application/vnd.in-toto+json",
                    "payload": base64.b64encode(json.dumps(statement).encode("utf-8")).decode("ascii"),
                    "signatures": [],
                }
                return _CommandResult(0, (json.dumps(envelope) + "\n").encode("utf-8"))
            raise AssertionError(command)

        with patch("luma.builder_build_executor._runtime_prerequisites", return_value=self._prerequisites()), patch(
            "luma.builder_build_executor._run_command", side_effect=fake_run
        ):
            result = build_plan(payload, cancel_event=threading.Event())

        build_commands = [command for command in commands if command[0].endswith("buildctl")]
        self.assertEqual(len(build_commands), 2)
        self.assertIn("/base:", next(value for value in build_commands[0] if value.startswith("type=image,name=")))
        self.assertIn("/web:", next(value for value in build_commands[1] if value.startswith("type=image,name=")))
        self.assertEqual(set(result["images"]), {"base", "web"})
        self.assertEqual(set(result["scanDigests"]), {"base", "web"})
        self.assertEqual(
            set(result["artifacts"]),
            {"base-sbom", "base-provenance", "base-scan", "web-sbom", "web-provenance", "web-scan"},
        )
        for key, image in result["images"].items():
            self.assertTrue(image.endswith("@" + result["imageDigests"][key]))
            self.assertNotIn("localhost:5000", image)

    def test_zero_build_plan_validates_snapshot_without_invoking_toolchain(self):
        digest, _snapshot = self._install_snapshot({"README.md": "prebuilt images only\n"})
        payload = self._payload(digest, [])
        with patch("luma.builder_build_executor._runtime_prerequisites") as prerequisites:
            result = build_plan(payload, cancel_event=threading.Event())
        prerequisites.assert_not_called()
        self.assertEqual(result["images"], {})
        self.assertEqual(result["scanDigests"], {})
        self.assertEqual(result["artifacts"], {})

    def test_external_only_plan_resolves_scans_and_returns_immutable_artifacts(self):
        digest, _snapshot = self._install_snapshot({"compose.yaml": "services: {}\n"})
        payload = self._payload(
            digest,
            [],
            external_images=[self._external("database")],
        )
        image_digest = "sha256:" + ("3" * 64)
        commands = []
        environments = []

        def fake_run(command, **kwargs):
            commands.append(list(command))
            environments.append(dict(kwargs["env"]))
            executable = command[0]
            if executable.endswith("crane"):
                self.assertEqual(command[-1], "postgres:17")
                return _CommandResult(0, (image_digest + "\n").encode("ascii"))
            if executable.endswith("syft"):
                output_path = Path(command[command.index("--output") + 1].split("=", 1)[1])
                output_path.write_text(
                    json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}),
                    encoding="utf-8",
                )
                return _CommandResult(0, b"")
            if executable.endswith("trivy"):
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(json.dumps({"Results": []}), encoding="utf-8")
                return _CommandResult(0, b"")
            raise AssertionError(command)

        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy.invalid",
                "HTTPS_PROXY": "http://proxy.invalid",
                "DOCKER_AUTH_CONFIG": SECRET_SENTINEL,
            },
            clear=False,
        ), patch(
            "luma.builder_build_executor._runtime_prerequisites",
            return_value=self._prerequisites(),
        ) as prerequisites, patch(
            "luma.builder_build_executor._run_command",
            side_effect=fake_run,
        ):
            result = build_plan(payload, cancel_event=threading.Event())

        prerequisites.assert_called_once()
        self.assertFalse(any(command[0].endswith("buildctl") for command in commands))
        self.assertFalse(any(command[0].endswith("cosign") for command in commands))
        self.assertEqual(
            result["images"]["database"],
            "docker.io/library/postgres@" + image_digest,
        )
        self.assertEqual(result["imageDigests"], {"database": image_digest})
        self.assertEqual(
            set(result["artifacts"]),
            {"database-sbom", "database-provenance", "database-scan"},
        )
        self.assertEqual(
            result["artifacts"]["database-provenance"]["mediaType"],
            "application/vnd.lae.external-resolution+json",
        )
        for environment in environments:
            self.assertNotIn("HTTP_PROXY", environment)
            self.assertNotIn("HTTPS_PROXY", environment)
            self.assertNotIn("DOCKER_AUTH_CONFIG", environment)
            self.assertNotIn(SECRET_SENTINEL, json.dumps(environment, sort_keys=True))

        provenance_digest = result["artifacts"]["database-provenance"]["digest"].split(":", 1)[1]
        provenance_path = (
            self.snapshot_root
            / "artifacts"
            / "external"
            / "resolution"
            / "sha256"
            / provenance_digest[:2]
            / f"{provenance_digest}.json"
        )
        statement = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(
            statement["predicateType"],
            "https://itool.tech/lae/external-image-resolution/v1",
        )
        self.assertEqual(statement["predicate"]["sourceReference"], "postgres:17")
        self.assertNotIn("slsa.dev", json.dumps(statement, sort_keys=True))

    def test_external_registry_and_reference_policy_fail_closed_before_network(self):
        digest, _snapshot = self._install_snapshot({"README.md": "external\n"})
        for reference in (
            "postgres",
            "postgres:latest",
            "localhost/postgres:17",
            "127.0.0.1/postgres:17",
            "http://docker.io/library/postgres:17",
            "user:password@docker.io/library/postgres:17",
            "docker.io/library/postgres:17?token=secret",
        ):
            with self.subTest(reference=reference), self.assertRaises(LumaError):
                _validate_build_lease(
                    self._payload(
                        digest,
                        [],
                        external_images=[self._external("database", reference)],
                    )
                )

        unlisted = self._payload(
            digest,
            [],
            external_images=[self._external("database", "ghcr.io/acme/postgres:17")],
        )
        runner = Mock(side_effect=AssertionError("network process must not start"))
        with patch("luma.builder_build_executor._run_command", runner):
            with self.assertRaisesRegex(LumaError, "not allowlisted"):
                build_plan(unlisted, cancel_event=threading.Event())
        runner.assert_not_called()

        mismatched_node_policy = self._payload(
            digest,
            [],
            external_images=[self._external("database")],
        )
        mismatched_node_policy["registry"]["externalRegistries"] = [
            "docker.io",
            "ghcr.io",
        ]
        runner = Mock(side_effect=AssertionError("network process must not start"))
        with patch(
            "luma.builder_build_executor._runtime_prerequisites",
            return_value=self._prerequisites(),
        ), patch("luma.builder_build_executor._run_command", runner):
            with self.assertRaisesRegex(LumaError, "does not match node policy"):
                build_plan(mismatched_node_policy, cancel_event=threading.Event())
        runner.assert_not_called()

    def test_external_resolver_failure_and_digest_mismatch_are_generic_and_fail_closed(self):
        digest, _snapshot = self._install_snapshot({"README.md": "external\n"})
        tagged = self._payload(
            digest,
            [],
            external_images=[self._external("database")],
        )
        with patch(
            "luma.builder_build_executor._runtime_prerequisites",
            return_value=self._prerequisites(),
        ), patch(
            "luma.builder_build_executor._run_command",
            return_value=_CommandResult(1, b"unauthorized: registry detail must not persist"),
        ):
            with self.assertRaisesRegex(LumaError, "anonymous external image resolution failed"):
                build_plan(tagged, cancel_event=threading.Event())

        with patch(
            "luma.builder_build_executor._runtime_prerequisites",
            return_value=self._prerequisites(),
        ), patch(
            "luma.builder_build_executor._run_command",
            return_value=_CommandResult(0, ("sha256:" + ("5" * 64) + "\n").encode("ascii")),
        ):
            with self.assertRaisesRegex(LumaError, "does not match the signed plan"):
                # The tag moved after analysis. Retrying the same signed plan
                # must fail instead of silently accepting the new digest.
                build_plan(tagged, cancel_event=threading.Event())

        expected_digest = "sha256:" + ("4" * 64)
        mismatched_pinned = self._payload(
            digest,
            [],
            external_images=[
                self._external(
                    "database",
                    "postgres@" + expected_digest,
                    resolved_digest="sha256:" + ("5" * 64),
                )
            ],
        )
        with self.assertRaisesRegex(LumaError, "must match the digest in ref"):
            _validate_build_lease(mismatched_pinned)

    def test_snapshot_digest_mismatch_is_rejected_before_build(self):
        digest, snapshot = self._install_snapshot({"Dockerfile": "FROM scratch\n"})
        snapshot.write_bytes(snapshot.read_bytes() + b"corruption")
        with self.assertRaisesRegex(LumaError, "digest mismatch"):
            build_plan(self._payload(digest, [self._build("web")]), cancel_event=threading.Event())

    def test_malicious_dockerfile_and_snapshot_symlink_escape_are_rejected(self):
        digest, _snapshot = self._install_snapshot({"Dockerfile": "FROM scratch\n"})
        malicious = self._payload(digest, [self._build("web", dockerfile="../Dockerfile")])
        with self.assertRaisesRegex(LumaError, "stay within"):
            _validate_build_lease(malicious)

        symlink_digest, _ = self._install_snapshot(
            {"Dockerfile.real": "FROM scratch\n"},
            symlinks={"Dockerfile": "../../etc/passwd"},
        )
        with self.assertRaisesRegex(LumaError, "symlink escapes"):
            build_plan(
                self._payload(symlink_digest, [self._build("web")]),
                cancel_event=threading.Event(),
            )

    def test_registry_override_and_legacy_registry_credentials_cannot_reach_lease(self):
        digest, _snapshot = self._install_snapshot({"Dockerfile": "FROM scratch\n"})
        build = self._build("web")
        wrong = self._payload(digest, [build], repositories={"web": "attacker/owned"})
        with self.assertRaisesRegex(LumaError, "repository binding"):
            _validate_build_lease(wrong)
        with self.assertRaisesRegex(LumaError, "closed schema"):
            _validate_build_lease(self._payload(digest, [build], extra={"registryHost": "attacker.invalid"}))

        request = {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "build-plan",
            "externalOperationId": "operation-build-test",
            "tenantRef": "tenant-build-test",
            "applicationRef": "application-build-test",
            "payload": {
                key: self._payload(digest, [build])[key]
                for key in ("sourceSnapshotId", "sourceSnapshotDigest", "signedBuildPlan", "credentialLeaseId", "limits")
            },
        }
        state = {
            "build": {
                "registryHost": "100.66.177.70:5000",
                "pushHost": "localhost:5000",
            },
            "registries": {
                "localhost:5000": {"username": "legacy", "password": SECRET_SENTINEL},
            },
            "builderTasks": {
                "builder-parent": {
                    "id": "builder-parent",
                    "kind": "build-plan",
                    "principalRef": "lae-service",
                    "request": request,
                }
            },
        }
        task = {
            "action": "build-plan",
            "builderTaskId": "builder-parent",
            "payload": {
                "builderTaskId": "builder-parent",
                "schemaVersion": request["schemaVersion"],
                "externalOperationId": request["externalOperationId"],
                "tenantRef": request["tenantRef"],
                "applicationRef": request["applicationRef"],
                **request["payload"],
            },
        }
        with patch.dict(
            os.environ,
            {
                "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY": "1",
                "LUMA_LAE_BUILDER_REGISTRY_INSECURE": "1",
            },
            clear=False,
        ):
            leased = control_server._agent_task_lease_payload(state, task)
        self.assertNotIn(SECRET_SENTINEL, json.dumps(leased, sort_keys=True))
        self.assertNotIn("registryAuth", leased)
        self.assertEqual(leased["registry"]["authMode"], "anonymous")

    def test_build_args_and_secret_mounts_fail_closed_without_broker(self):
        digest, _snapshot = self._install_snapshot({"Dockerfile": "FROM scratch\n"})
        payload = self._payload(
            digest,
            [self._build("web", args=["APP_VERSION"], secrets=["NPM_TOKEN"])],
        )
        runner = Mock(side_effect=AssertionError("a process must not start"))
        with patch("luma.builder_build_executor._run_command", runner):
            with self.assertRaisesRegex(LumaError, "credential lease redemption is unavailable"):
                build_plan(payload, cancel_event=threading.Event())
        runner.assert_not_called()

        with self.assertRaisesRegex(LumaError, "closed schema"):
            _validate_build_lease({**payload, "buildInputs": {"NPM_TOKEN": SECRET_SENTINEL}})

    def test_root_owned_buildkit_socket_never_enables_capability(self):
        with patch.dict(
            os.environ,
            {BUILDKIT_ADDR_ENV: "unix:///run/user/1000/buildkit/buildkitd.sock"},
            clear=False,
        ), patch.object(
            Path,
            "lstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=0),
        ):
            with self.assertRaisesRegex(LumaError, "non-root-owned"):
                _rootless_buildkit_addr()
        with patch("luma.builder_build_executor._runtime_prerequisites", side_effect=LumaError("rootful")):
            self.assertFalse(builder_build_available("linux"))

    def test_non_root_socket_path_cannot_hide_a_rootful_buildkit_peer(self):
        class FakeSocket:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def getsockopt(self, _level, _option, _size):
                return struct.pack("3i", 4321, 0, 0)

            def close(self):
                pass

        with patch.dict(
            os.environ,
            {BUILDKIT_ADDR_ENV: "unix:///run/user/1000/buildkit/buildkitd.sock"},
            clear=False,
        ), patch.object(
            Path,
            "lstat",
            return_value=SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000),
        ), patch(
            "luma.builder_build_executor.socket.socket",
            return_value=FakeSocket(),
        ), patch(
            "luma.builder_build_executor.socket.SO_PEERCRED",
            17,
            create=True,
        ):
            with self.assertRaisesRegex(LumaError, "peer credentials"):
                _rootless_buildkit_addr()

    def test_cancel_kills_running_process_group(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(BuilderTaskCanceled):
                _run_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    env={"PATH": os.environ.get("PATH", "")},
                    timeout=30,
                    cancel_event=cancel,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_kills_running_resolver_process_group(self):
        started = time.monotonic()
        with self.assertRaises(BuilderTaskTimedOut):
            _run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={"PATH": os.environ.get("PATH", "")},
                timeout=1,
                cancel_event=threading.Event(),
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_result_protocol_requires_scan_and_exact_artifact_bindings(self):
        digest = SNAPSHOT_DIGEST_PLACEHOLDER
        build = self._build("web")
        payload = self._payload(digest, [build])
        public_request = {
            "schemaVersion": payload["schemaVersion"],
            "kind": "build-plan",
            "externalOperationId": payload["externalOperationId"],
            "tenantRef": payload["tenantRef"],
            "applicationRef": payload["applicationRef"],
            "payload": {
                key: payload[key]
                for key in ("sourceSnapshotId", "sourceSnapshotDigest", "signedBuildPlan", "credentialLeaseId", "limits")
            },
        }
        image_digest = "sha256:" + ("9" * 64)
        artifact_digest = "sha256:" + ("8" * 64)
        result = {
            "sourceSnapshotDigest": digest,
            "images": {"web": "registry.invalid/repo@" + image_digest},
            "imageDigests": {"web": image_digest},
            "sbomDigests": {"web": artifact_digest},
            "provenanceDigests": {"web": artifact_digest},
            "artifacts": {},
        }
        with self.assertRaisesRegex(LumaError, "scanDigests"):
            sanitize_builder_task_result("build-plan", result, request=public_request)


if __name__ == "__main__":
    unittest.main()
