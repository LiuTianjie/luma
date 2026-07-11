from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-agent-core/src",
    "packages/python/lae-core/src",
    "services/agent-runner/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_agent_core import AnalysisError, analyze_source  # noqa: E402
from lae_agent_runner.__main__ import main as runner_main  # noqa: E402
from lae_contracts import validate_instance  # noqa: E402


def metadata(**overrides: str) -> dict[str, str]:
    value = {
        "externalOperationId": "op-analyze-1",
        "tenantRef": "tenant-test",
        "applicationRef": "application-test",
        "resolvedCommit": "0123456789abcdef0123456789abcdef01234567",
        "sourceSnapshotId": "snapshot-test",
        "sourceSnapshotDigest": "sha256:" + "a" * 64,
        "policyVersion": "2026-07-11",
    }
    value.update(overrides)
    return value


class AgentRunnerTests(unittest.TestCase):
    def _run(
        self,
        files: dict[str, str],
        *,
        source_parent: Path | None = None,
    ) -> tuple[dict[str, object], dict[str, bytes]]:
        owned = tempfile.TemporaryDirectory() if source_parent is None else None
        root = Path(owned.name) if owned is not None else source_parent
        assert root is not None
        source = root / "source"
        output = root / "output"
        source.mkdir(parents=True)
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        result = analyze_source(source, metadata(), output)
        artifacts = {
            path.name: path.read_bytes()
            for path in sorted(output.iterdir())
            if path.is_file()
        }
        if owned is not None:
            owned.cleanup()
        return result, artifacts

    def test_static_html_generates_valid_atomic_artifacts(self) -> None:
        result, artifacts = self._run(
            {"index.html": "<!doctype html><title>LAE</title>"}
        )

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(
            set(artifacts),
            {
                "evidence.json",
                "deployment-plan.json",
                "build-plan-proposal.json",
                "result.json",
            },
        )
        deployment = json.loads(artifacts["deployment-plan.json"])
        build_proposal = json.loads(artifacts["build-plan-proposal.json"])
        self.assertFalse(
            validate_instance("deployment-plan.v1.schema.json", deployment)
        )
        self.assertFalse(
            validate_instance("build-plan-proposal.v1.schema.json", build_proposal)
        )
        self.assertEqual(deployment["kind"], "service")
        self.assertEqual(deployment["routes"][0]["containerPort"], 8080)
        self.assertEqual(
            build_proposal["builds"][0]["dockerfile"],
            ".lae/adapters/static-v1.Dockerfile",
        )
        self.assertEqual(build_proposal["schemaVersion"], "lae.build-plan-proposal/v1")
        self.assertEqual(build_proposal["externalImages"], [])
        self.assertNotIn("signature", build_proposal)
        for name, filename in (
            ("evidence", "evidence.json"),
            ("deploymentPlan", "deployment-plan.json"),
            ("buildPlan", "build-plan-proposal.json"),
        ):
            descriptor = result["artifacts"][name]
            self.assertEqual(
                set(descriptor), {"path", "digest", "mediaType", "sizeBytes"}
            )
            self.assertEqual(descriptor["sizeBytes"], len(artifacts[filename]))

    def test_compose_supports_two_public_http_services_postgres_and_named_volume(
        self,
    ) -> None:
        compose = """
services:
  web:
    build:
      context: ./services/web
    expose: [3000]
    depends_on: [postgres]
    environment:
      DATABASE_URL: ${DATABASE_URL}
  admin:
    image: nginx:1.27-alpine
    expose: [8080]
  postgres:
    image: postgres:17
    expose: [5432]
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pg-data:/var/lib/postgresql/data
volumes:
  pg-data: {}
"""
        files = {
            "compose.yaml": compose,
            "services/web/Dockerfile": "FROM node:22-alpine\nEXPOSE 3000\n",
            "services/web/server.js": "console.log(process.env.DATABASE_URL)\n",
        }
        result, artifacts = self._run(files)
        deployment = json.loads(artifacts["deployment-plan.json"])
        build_proposal = json.loads(artifacts["build-plan-proposal.json"])

        self.assertEqual(result["decision"], "needs_configuration")
        self.assertEqual(len(deployment["routes"]), 2)
        self.assertEqual(sum(route["primary"] for route in deployment["routes"]), 1)
        self.assertEqual(
            {
                route["serviceKey"]: route["containerPort"]
                for route in deployment["routes"]
            },
            {"admin": 8080, "web": 3000},
        )
        postgres = next(
            service
            for service in deployment["services"]
            if service["key"] == "postgres"
        )
        self.assertEqual(postgres["role"], "datastore")
        self.assertNotIn(
            "postgres", {route["serviceKey"] for route in deployment["routes"]}
        )
        self.assertEqual(deployment["volumes"][0]["serviceKeys"], ["postgres"])
        self.assertEqual(build_proposal["builds"][0]["dependsOnBuilds"], [])
        self.assertEqual(
            build_proposal["externalImages"],
            [
                {
                    "key": "admin",
                    "ref": "nginx:1.27-alpine",
                    "platform": "linux/amd64",
                },
                {
                    "key": "postgres",
                    "ref": "postgres:17",
                    "platform": "linux/amd64",
                },
            ],
        )
        self.assertEqual(
            {item["key"] for item in build_proposal["builds"]}
            & {item["key"] for item in build_proposal["externalImages"]},
            set(),
        )

    def test_compose_external_images_require_public_explicit_versions(self) -> None:
        digest = "sha256:" + "b" * 64
        valid_references = (
            "postgres:17",
            "ghcr.io/acme/app:v1.2.3",
            f"docker.io/library/postgres@{digest}",
            "registry.example.com/team/app:2026-07-11",
        )
        for reference in valid_references:
            with self.subTest(valid=reference):
                result, artifacts = self._run(
                    {
                        "compose.yaml": (
                            "services:\n"
                            "  web:\n"
                            f"    image: {json.dumps(reference)}\n"
                            "    expose: [8080]\n"
                            "    platform: linux/amd64\n"
                        )
                    }
                )
                self.assertEqual(result["decision"], "allow")
                proposal = json.loads(artifacts["build-plan-proposal.json"])
                expected_image = {
                    "key": "web",
                    "ref": reference,
                    "platform": "linux/amd64",
                }
                if "@" in reference:
                    expected_image["resolvedDigest"] = digest
                self.assertEqual(proposal["externalImages"], [expected_image])
                if "@" in reference:
                    self.assertEqual(
                        proposal["externalImages"][0]["resolvedDigest"], digest
                    )
                else:
                    self.assertNotIn("resolvedDigest", proposal["externalImages"][0])

        invalid_references = (
            "postgres",
            "postgres:latest",
            "localhost:5000/team/app:v1",
            "registry.example.com:5000/team/app:v1",
            "127.0.0.1/team/app:v1",
            "10.0.0.1/team/app:v1",
            "registry.local/team/app:v1",
            "https://ghcr.io/acme/app:v1",
            "user@ghcr.io/acme/app:v1",
            "ghcr.io/acme/app:v1?token=value",
            "ghcr.io/acme/app:v1#fragment",
            f"ghcr.io/acme/app:v1@{digest}",
        )
        for reference in invalid_references:
            with self.subTest(invalid=reference):
                result, artifacts = self._run(
                    {
                        "compose.yaml": (
                            "services:\n"
                            "  web:\n"
                            f"    image: {json.dumps(reference)}\n"
                            "    expose: [8080]\n"
                        )
                    }
                )
                self.assertEqual(result["decision"], "deny")
                proposal = json.loads(artifacts["build-plan-proposal.json"])
                self.assertEqual(proposal["externalImages"], [])
                deployment = json.loads(artifacts["deployment-plan.json"])
                self.assertTrue(
                    any(
                        "COMPOSE_IMAGE_REFERENCE_INVALID" in blocker
                        for blocker in deployment["blockers"]
                    )
                )

    def test_compose_rejects_non_amd64_external_image_platform(self) -> None:
        result, artifacts = self._run(
            {
                "compose.yaml": """
services:
  web:
    image: nginx:1.27-alpine
    expose: [8080]
    platform: linux/arm64
"""
            }
        )
        self.assertEqual(result["decision"], "deny")
        blockers = json.loads(artifacts["deployment-plan.json"])["blockers"]
        self.assertTrue(
            any("COMPOSE_PLATFORM_UNSUPPORTED" in blocker for blocker in blockers)
        )

    def test_environment_values_and_private_dotenv_are_never_emitted(self) -> None:
        canaries = (
            "literal-compose-secret",
            "private-dotenv-secret",
            "example-secret-value",
        )
        files = {
            "compose.yaml": """
services:
  web:
    image: nginx:alpine
    expose: [8080]
    environment:
      API_TOKEN: literal-compose-secret
      APP_MODE: production
""",
            ".env": "API_TOKEN=private-dotenv-secret\n",
            ".env.example": "API_TOKEN=example-secret-value\nPUBLIC_URL=\n",
            "config.ts": "process.env.API_TOKEN; process.env.PUBLIC_URL;\n",
        }
        _, artifacts = self._run(files)
        serialized = b"\n".join(artifacts.values()).decode("utf-8")
        for canary in canaries:
            self.assertNotIn(canary, serialized)
        deployment = json.loads(artifacts["deployment-plan.json"])
        environment = {item["name"]: item for item in deployment["environment"]}
        self.assertTrue(environment["API_TOKEN"]["sensitive"])
        self.assertFalse(environment["API_TOKEN"]["public"])
        self.assertTrue(environment["APP_MODE"]["required"])
        self.assertFalse(environment["APP_MODE"]["configured"])
        self.assertIn("PUBLIC_URL", environment)

    def test_compose_denies_privilege_host_access_and_public_non_http(self) -> None:
        compose = """
services:
  web:
    image: example/web:1
    ports: ["127.0.0.1:8080:8080", "5353:5353/udp"]
    labels:
      lae.public.protocol: tcp
    privileged: true
    network_mode: host
    pid: host
    ipc: host
    cap_add: [SYS_ADMIN]
    devices: [/dev/kvm:/dev/kvm]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/data
"""
        result, artifacts = self._run({"compose.yaml": compose})
        deployment = json.loads(artifacts["deployment-plan.json"])
        blockers = "\n".join(deployment["blockers"])

        self.assertEqual(result["decision"], "deny")
        for code in (
            "COMPOSE_PRIVILEGED",
            "COMPOSE_HOST_PORT",
            "PUBLIC_UDP_UNSUPPORTED",
            "PUBLIC_TCP_UNSUPPORTED",
            "COMPOSE_NETWORK_MODE_HOST",
            "COMPOSE_PID_HOST",
            "COMPOSE_IPC_HOST",
            "COMPOSE_CAP_ADD",
            "COMPOSE_DEVICES",
            "COMPOSE_DOCKER_SOCKET",
            "COMPOSE_HOST_BIND",
        ):
            self.assertIn(code, blockers)

    def test_shared_network_namespace_rejects_duplicate_ports(self) -> None:
        compose = """
services:
  web:
    image: example/web:1
    expose: [8080]
  admin:
    image: example/admin:1
    expose: [8080]
"""
        result, artifacts = self._run({"compose.yaml": compose})
        blockers = json.loads(artifacts["deployment-plan.json"])["blockers"]
        self.assertEqual(result["decision"], "deny")
        self.assertTrue(
            any("COMPOSE_SHARED_NETWORK_PORT_CONFLICT" in item for item in blockers)
        )

    def test_container_only_ports_are_route_evidence_but_host_publish_is_denied(
        self,
    ) -> None:
        allowed, allowed_artifacts = self._run(
            {
                "compose.yaml": """
services:
  web:
    image: example/web:1
    ports: ["8080"]
"""
            }
        )
        self.assertEqual(allowed["decision"], "allow")
        deployment = json.loads(allowed_artifacts["deployment-plan.json"])
        self.assertEqual(deployment["routes"][0]["containerPort"], 8080)

        denied, denied_artifacts = self._run(
            {
                "compose.yaml": """
services:
  web:
    image: example/web:1
    ports: ["18080:8080"]
"""
            }
        )
        self.assertEqual(denied["decision"], "deny")
        self.assertTrue(
            any(
                "COMPOSE_HOST_PORT" in blocker
                for blocker in json.loads(denied_artifacts["deployment-plan.json"])[
                    "blockers"
                ]
            )
        )

    def test_duplicate_yaml_keys_are_rejected_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "compose.yaml").write_text(
                "services:\n  web:\n    image: first:1\n  web:\n    image: canary-secret:2\n",
                encoding="utf-8",
            )
            with self.assertRaises(AnalysisError) as error:
                analyze_source(source, metadata(), root / "output")
            self.assertEqual(error.exception.code, "LAE_COMPOSE_INVALID")
            self.assertNotIn("canary-secret", str(error.exception))

    def test_artifacts_are_reproducible_across_paths_and_mtimes(self) -> None:
        files = {
            "Dockerfile": "FROM python:3.12-slim\nARG APP_VERSION\nEXPOSE 8000\n",
            "app.py": "import os\nprint(os.getenv('DATABASE_URL'))\n",
        }
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            _, first_artifacts = self._run(files, source_parent=Path(first))
            os.utime(Path(second), (1_000_000, 1_000_000))
            _, second_artifacts = self._run(files, source_parent=Path(second))
        self.assertEqual(first_artifacts, second_artifacts)

    def test_result_uses_actual_internal_proposal_file_digest(self) -> None:
        result, artifacts = self._run({"index.html": "<h1>hello</h1>"})
        proposal_bytes = artifacts["build-plan-proposal.json"]
        proposal = json.loads(proposal_bytes)
        expected = "sha256:" + hashlib.sha256(proposal_bytes).hexdigest()
        self.assertEqual(result["artifacts"]["buildPlan"]["digest"], expected)
        self.assertEqual(
            result["artifacts"]["buildPlan"]["mediaType"],
            "application/vnd.lae.build-plan-proposal+json",
        )
        self.assertEqual(proposal["schemaVersion"], "lae.build-plan-proposal/v1")
        self.assertNotIn("signature", proposal)

    def test_metadata_rejects_secret_fields_and_non_full_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_text("ok", encoding="utf-8")
            with self.assertRaises(AnalysisError) as secret_error:
                analyze_source(
                    source, {**metadata(), "authorization": "canary"}, root / "out"
                )
            self.assertEqual(secret_error.exception.code, "LAE_METADATA_FORBIDDEN")
            with self.assertRaises(AnalysisError) as commit_error:
                analyze_source(source, metadata(resolvedCommit="a" * 41), root / "out")
            self.assertEqual(commit_error.exception.code, "LAE_METADATA_INVALID")

    def test_cli_interface_writes_result_last_and_prints_only_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
            metadata_path = root / "metadata.json"
            metadata_path.write_text(json.dumps(metadata()), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = runner_main(
                    [
                        "analyze",
                        "--source",
                        str(source),
                        "--metadata",
                        str(metadata_path),
                        "--output-dir",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue()),
                json.loads((output / "result.json").read_text()),
            )

    def test_cli_argument_errors_are_structured_and_do_not_echo_input(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = runner_main(["analyze", "--unknown", "canary-secret-value"])
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["code"], "LAE_ARGUMENT_INVALID")
        self.assertNotIn("canary-secret-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
