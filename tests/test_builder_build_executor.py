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
    _TRUSTED_NODE_ADAPTER_DOCKERFILE,
    _TRUSTED_NODE_ADAPTER_ENTRYPOINT,
    _TRUSTED_NODE_ADAPTER_ENTRYPOINT_PATH,
    _TRUSTED_NODE_ADAPTER_PATH,
    _TRUSTED_PYTHON_ADAPTER_DOCKERFILE,
    _TRUSTED_PYTHON_ADAPTER_PATH,
    _TRUSTED_PYTHON_ADAPTER_RUNTIME,
    _TRUSTED_PYTHON_ADAPTER_RUNTIME_PATH,
    _TRUSTED_STATIC_ADAPTER_DOCKERFILE,
    _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE,
    _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE_PATH,
    _TRUSTED_STATIC_ADAPTER_PATH,
    _materialize_trusted_build_adapters,
    _retrieve_buildkit_provenance,
    _rootless_buildkit_addr,
    _run_command,
    _syft_environment,
    _validate_build_lease,
    _validate_scan_report,
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
SECRET_SENTINEL = "registry-or-build-secret-must-not-reach-processes"


def _json_bytes(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _buildkit_fixture(key, *, descriptor_subject=None, statement_subject=None):
    runnable_digest = _sha256_bytes(f"runnable-manifest:{key}".encode("utf-8"))
    statement_subject_digest = statement_subject or runnable_digest
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": f"lae/{key}",
                "digest": {"sha256": statement_subject_digest.split(":", 1)[1]},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {"buildDefinition": {"buildType": "https://mobyproject.org/buildkit@v1"}},
    }
    statement_bytes = _json_bytes(statement)
    blob_digest = _sha256_bytes(statement_bytes)
    attestation_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [
            {
                "mediaType": "application/vnd.in-toto+json",
                "digest": blob_digest,
                "size": len(statement_bytes),
                "annotations": {"in-toto.io/predicate-type": "https://slsa.dev/provenance/v1"},
            }
        ],
    }
    attestation_bytes = _json_bytes(attestation_manifest)
    attestation_digest = _sha256_bytes(attestation_bytes)
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": runnable_digest,
                "size": 512,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": attestation_digest,
                "size": len(attestation_bytes),
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": descriptor_subject or runnable_digest,
                },
            },
        ],
    }
    index_bytes = _json_bytes(index)
    return {
        "runnableDigest": runnable_digest,
        "statement": statement,
        "statementBytes": statement_bytes,
        "blobDigest": blob_digest,
        "attestationBytes": attestation_bytes,
        "attestationDigest": attestation_digest,
        "indexBytes": index_bytes,
        "indexDigest": _sha256_bytes(index_bytes),
    }


BUILDKIT_FIXTURES = {key: _buildkit_fixture(key) for key in ("base", "web")}
IMAGE_DIGESTS = {key: fixture["indexDigest"] for key, fixture in BUILDKIT_FIXTURES.items()}


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

    def test_scan_report_keeps_high_findings_without_rejecting_deployment(self):
        report = self.root / "scan.json"
        report.write_text(
            json.dumps(
                {
                    "Results": [
                        {
                            "Target": "application-image",
                            "Vulnerabilities": [
                                {"VulnerabilityID": "CVE-TEST", "Severity": "CRITICAL"}
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        _validate_scan_report(report)

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

    def _retrieve_fixture(self, fixture, *, index_output=None):
        def fake_run(command, **_kwargs):
            reference = command[-1]
            if command[1] == "manifest" and reference.endswith("@" + fixture["indexDigest"]):
                return _CommandResult(0, fixture["indexBytes"] if index_output is None else index_output)
            if command[1] == "manifest" and reference.endswith("@" + fixture["attestationDigest"]):
                return _CommandResult(0, fixture["attestationBytes"])
            if command[1] == "blob" and reference.endswith("@" + fixture["blobDigest"]):
                return _CommandResult(0, fixture["statementBytes"])
            raise AssertionError(command)

        with patch("luma.builder_build_executor._run_command", side_effect=fake_run):
            return _retrieve_buildkit_provenance(
                "/tools/crane",
                "localhost:5000/lae/provenance-test@" + fixture["indexDigest"],
                expected_index_digest=fixture["indexDigest"],
                insecure=True,
                env={"PATH": "/tools"},
                timeout=30,
                cancel_event=threading.Event(),
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
                self.assertEqual(command[command.index("--exit-code") + 1], "0")
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(json.dumps({"Results": []}), encoding="utf-8")
                return _CommandResult(0, b"")
            if executable.endswith("crane"):
                reference = command[-1]
                key = next(key for key in BUILDKIT_FIXTURES if f"/{key}@" in reference)
                fixture = BUILDKIT_FIXTURES[key]
                self.assertIn("--insecure", command)
                if command[1] == "manifest" and reference.endswith("@" + fixture["indexDigest"]):
                    return _CommandResult(0, fixture["indexBytes"])
                if command[1] == "manifest" and reference.endswith("@" + fixture["attestationDigest"]):
                    return _CommandResult(0, fixture["attestationBytes"])
                if command[1] == "blob" and reference.endswith("@" + fixture["blobDigest"]):
                    return _CommandResult(0, fixture["statementBytes"])
                raise AssertionError(command)
            raise AssertionError(command)

        with patch("luma.builder_build_executor._runtime_prerequisites", return_value=self._prerequisites()), patch(
            "luma.builder_build_executor._run_command", side_effect=fake_run
        ):
            result = build_plan(payload, cancel_event=threading.Event())

        build_commands = [command for command in commands if command[0].endswith("buildctl")]
        self.assertEqual(len(build_commands), 2)
        for command in build_commands:
            provenance_option = command.index("attest:provenance=mode=max")
            self.assertEqual(command[provenance_option - 1], "--opt")
            self.assertNotIn("--attest=type=provenance,mode=max", command)
        self.assertFalse(any(command[0].endswith("cosign") for command in commands))
        self.assertFalse(
            any(
                "--registry-insecure-skip-tls-verify" in command
                for command in commands
                if command[0].endswith("syft")
            )
        )
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
            artifact_digest = result["artifacts"][f"{key}-provenance"]["digest"].split(":", 1)[1]
            artifact_path = (
                self.snapshot_root
                / "artifacts"
                / "build"
                / "provenance"
                / "sha256"
                / artifact_digest[:2]
                / f"{artifact_digest}.json"
            )
            envelopes = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(len(envelopes), 1)
            statement = json.loads(base64.b64decode(envelopes[0]["payload"]).decode("utf-8"))
            self.assertEqual(
                statement["subject"][0]["digest"]["sha256"],
                BUILDKIT_FIXTURES[key]["runnableDigest"].split(":", 1)[1],
            )

    def test_syft_uses_supported_http_registry_environment(self):
        original = {"PATH": "/tools", "DOCKER_CONFIG": "/tmp/docker"}
        secure = _syft_environment(original, insecure=False)
        insecure = _syft_environment(original, insecure=True)

        self.assertEqual(secure, original)
        self.assertEqual(insecure["SYFT_REGISTRY_INSECURE_SKIP_TLS_VERIFY"], "true")
        self.assertEqual(insecure["SYFT_REGISTRY_INSECURE_USE_HTTP"], "true")
        self.assertEqual(original, {"PATH": "/tools", "DOCKER_CONFIG": "/tmp/docker"})

    def test_python_adapter_uses_scanned_alpine_runtime(self):
        dockerfile = _TRUSTED_PYTHON_ADAPTER_DOCKERFILE.decode("utf-8")
        self.assertIn("python:3.12.13-alpine3.23@sha256:efc8538b", dockerfile)
        self.assertIn("adduser -D -u 10001", dockerfile)
        self.assertIn("chmod 0555 /usr/local/lib/lae", dockerfile)
        self.assertLess(
            dockerfile.index("chmod 0555 /usr/local/lib/lae"),
            dockerfile.index("COPY --chmod=0444 .lae/adapters/python-v1-runtime.py"),
        )
        self.assertNotIn("slim-bookworm", dockerfile)

    def test_buildkit_provenance_rejects_descriptor_subject_mismatch(self):
        fixture = _buildkit_fixture(
            "descriptor-mismatch",
            descriptor_subject="sha256:" + ("d" * 64),
        )
        with self.assertRaisesRegex(LumaError, "descriptor is not bound"):
            self._retrieve_fixture(fixture)

    def test_buildkit_provenance_rejects_statement_subject_mismatch(self):
        fixture = _buildkit_fixture(
            "statement-mismatch",
            statement_subject="sha256:" + ("e" * 64),
        )
        with self.assertRaisesRegex(LumaError, "not bound to the linux/amd64 manifest"):
            self._retrieve_fixture(fixture)

    def test_buildkit_provenance_rejects_root_index_content_digest_mismatch(self):
        fixture = _buildkit_fixture("root-content-mismatch")
        with self.assertRaisesRegex(LumaError, "does not match its OCI descriptor digest"):
            self._retrieve_fixture(fixture, index_output=fixture["indexBytes"] + b"\n")

    def test_zero_build_plan_validates_snapshot_without_invoking_toolchain(self):
        digest, _snapshot = self._install_snapshot({"README.md": "prebuilt images only\n"})
        payload = self._payload(digest, [])
        with patch("luma.builder_build_executor._runtime_prerequisites") as prerequisites:
            result = build_plan(payload, cancel_event=threading.Event())
        prerequisites.assert_not_called()
        self.assertEqual(result["images"], {})
        self.assertEqual(result["scanDigests"], {})
        self.assertEqual(result["artifacts"], {})

    def test_exact_static_adapter_is_materialized_before_build(self):
        digest, _snapshot = self._install_snapshot(
            {
                ".dockerignore": "*\n",
                "index.html": "<!doctype html><title>LAE</title>\n",
            }
        )
        payload = self._payload(
            digest,
            [self._build("web", dockerfile=_TRUSTED_STATIC_ADAPTER_PATH)],
        )
        fixture = BUILDKIT_FIXTURES["web"]
        build_commands = []

        def fake_run(command, **_kwargs):
            executable = command[0]
            if executable.endswith("buildctl"):
                build_commands.append(list(command))
                dockerfile_root = Path(
                    next(value for value in command if value.startswith("dockerfile=")).split("=", 1)[1]
                )
                context_root = Path(
                    next(value for value in command if value.startswith("context=")).split("=", 1)[1]
                )
                self.assertEqual(
                    (dockerfile_root / Path(_TRUSTED_STATIC_ADAPTER_PATH).name).read_bytes(),
                    _TRUSTED_STATIC_ADAPTER_DOCKERFILE,
                )
                self.assertEqual(
                    (context_root / _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE_PATH).read_bytes(),
                    _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE,
                )
                self.assertTrue((context_root / "index.html").is_file())
                metadata = Path(command[command.index("--metadata-file") + 1])
                metadata.write_text(
                    json.dumps({"containerimage.digest": fixture["indexDigest"]}),
                    encoding="utf-8",
                )
                return _CommandResult(0, b"")
            if executable.endswith("syft"):
                output_path = Path(command[command.index("--output") + 1].split("=", 1)[1])
                output_path.write_text(
                    json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}),
                    encoding="utf-8",
                )
                return _CommandResult(0, b"")
            if executable.endswith("trivy"):
                self.assertEqual(command[command.index("--exit-code") + 1], "0")
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(json.dumps({"Results": []}), encoding="utf-8")
                return _CommandResult(0, b"")
            raise AssertionError(command)

        with patch(
            "luma.builder_build_executor._runtime_prerequisites",
            return_value=self._prerequisites(),
        ), patch(
            "luma.builder_build_executor._run_command",
            side_effect=fake_run,
        ), patch(
            "luma.builder_build_executor._retrieve_buildkit_provenance",
            return_value=b"[]",
        ):
            result = build_plan(payload, cancel_event=threading.Event())

        self.assertEqual(set(result["images"]), {"web"})
        self.assertEqual(len(build_commands), 1)
        command = build_commands[0]
        self.assertIn(f"filename={Path(_TRUSTED_STATIC_ADAPTER_PATH).name}", command)
        provenance_option = command.index("attest:provenance=mode=max")
        self.assertEqual(command[provenance_option - 1], "--opt")
        self.assertIn(
            "FROM golang:1.26.5-bookworm@sha256:18aedc16aa19b3fd7ded7245fc14b109e054d65d22ed53c355c899582bbb2113",
            _TRUSTED_STATIC_ADAPTER_DOCKERFILE.decode("utf-8"),
        )
        self.assertIn("FROM scratch", _TRUSTED_STATIC_ADAPTER_DOCKERFILE.decode("utf-8"))
        self.assertIn('USER 10001:10001', _TRUSTED_STATIC_ADAPTER_DOCKERFILE.decode("utf-8"))
        self.assertIn('request.URL.Path == "/healthz"', _TRUSTED_STATIC_ADAPTER_DOCKERFILE.decode("utf-8"))
        self.assertIn("EXPOSE 8080", _TRUSTED_STATIC_ADAPTER_DOCKERFILE.decode("utf-8"))

    def test_exact_node_and_python_adapters_are_materialized(self):
        fixtures = (
            (
                _TRUSTED_NODE_ADAPTER_PATH,
                _TRUSTED_NODE_ADAPTER_DOCKERFILE,
                _TRUSTED_NODE_ADAPTER_ENTRYPOINT_PATH,
                _TRUSTED_NODE_ADAPTER_ENTRYPOINT,
            ),
            (
                _TRUSTED_PYTHON_ADAPTER_PATH,
                _TRUSTED_PYTHON_ADAPTER_DOCKERFILE,
                _TRUSTED_PYTHON_ADAPTER_RUNTIME_PATH,
                _TRUSTED_PYTHON_ADAPTER_RUNTIME,
            ),
        )
        for adapter_path, dockerfile, support_path, support_content in fixtures:
            with self.subTest(adapter=adapter_path), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary)
                (source / "README.md").write_text("tenant source\n", encoding="utf-8")
                _materialize_trusted_build_adapters(
                    source,
                    [self._build("web", dockerfile=adapter_path)],
                )
                self.assertEqual((source / adapter_path).read_bytes(), dockerfile)
                self.assertEqual((source / support_path).read_bytes(), support_content)
                self.assertTrue((source / f"{adapter_path}.dockerignore").is_file())

        node = _TRUSTED_NODE_ADAPTER_DOCKERFILE.decode("utf-8")
        python = _TRUSTED_PYTHON_ADAPTER_DOCKERFILE.decode("utf-8")
        self.assertIn("USER 10001:10001", node)
        self.assertIn("npm run build", node)
        self.assertIn("ENTRYPOINT", node)
        self.assertIn("USER 10001:10001", python)
        self.assertIn("requirements.txt", python)
        self.assertIn("lae_python_runtime", python)
        self.assertIn('scope.get("path") == "/healthz"', _TRUSTED_PYTHON_ADAPTER_RUNTIME.decode("utf-8"))

    def test_static_adapter_rejects_reserved_snapshot_path_and_symlink_conflicts(self):
        occupied_digest, _snapshot = self._install_snapshot(
            {
                _TRUSTED_STATIC_ADAPTER_PATH: "FROM attacker.invalid/image\n",
                "index.html": "<!doctype html><title>LAE</title>\n",
            }
        )
        symlink_digest, _snapshot = self._install_snapshot(
            {"index.html": "<!doctype html><title>LAE</title>\n"},
            symlinks={".lae": "tenant-controlled"},
        )
        prerequisites = Mock(side_effect=AssertionError("toolchain must not start"))
        with patch("luma.builder_build_executor._runtime_prerequisites", prerequisites):
            for snapshot_digest in (occupied_digest, symlink_digest):
                with self.subTest(snapshot_digest=snapshot_digest), self.assertRaisesRegex(
                    LumaError,
                    "reserved platform adapter path conflicts",
                ):
                    build_plan(
                        self._payload(
                            snapshot_digest,
                            [self._build("web", dockerfile=_TRUSTED_STATIC_ADAPTER_PATH)],
                        ),
                        cancel_event=threading.Event(),
                    )
        prerequisites.assert_not_called()

    def test_ordinary_missing_dockerfile_is_not_platform_materialized(self):
        digest, _snapshot = self._install_snapshot(
            {"index.html": "<!doctype html><title>LAE</title>\n"}
        )
        prerequisites = Mock(side_effect=AssertionError("toolchain must not start"))
        with patch(
            "luma.builder_build_executor._runtime_prerequisites",
            prerequisites,
        ), self.assertRaisesRegex(LumaError, "build Dockerfile is not present"):
            build_plan(
                self._payload(digest, [self._build("web", dockerfile="Dockerfile")]),
                cancel_event=threading.Event(),
            )
        prerequisites.assert_not_called()

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
                self.assertEqual(command[command.index("--exit-code") + 1], "0")
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
