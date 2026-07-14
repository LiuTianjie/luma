from __future__ import annotations

import base64
import json
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LAE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = LAE_ROOT / "deploy/luma/generate-staging-bundle.py"
PREPARE_SCRIPT = LAE_ROOT / "deploy/luma/prepare-staging-release.py"
ANALYZER = "100.66.177.70:5000/lae/agent-runner@sha256:" + "a" * 64


def _dotenv(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }


class StagingBundleTests(unittest.TestCase):
    def test_release_preflight_reuses_credentials_and_rejects_cluster_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bundle"
            api_key = Path(temporary) / "ark-api-key"
            api_key.write_text("test-provider-key", encoding="utf-8")
            api_key.chmod(0o600)
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--analyzer-image-digest",
                    ANALYZER,
                    "--cluster-id",
                    "old-cluster",
                    "--llm-model",
                    "test-model",
                    "--llm-api-key-file",
                    str(api_key),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            before = _dotenv(output / "lae-platform-staging.env")
            (output / "builder-agent-ai.env").unlink()
            control_path = output / "lae-control.env"
            control_path.write_text(
                "".join(
                    "export " + line + "\n"
                    for line in control_path.read_text(encoding="utf-8").splitlines()
                ),
                encoding="utf-8",
            )
            control_path.chmod(0o600)
            replacement = "100.66.177.70:5000/lae/agent-runner@sha256:" + "b" * 64
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE_SCRIPT),
                    "--bundle-dir",
                    str(output),
                    "--cluster-id",
                    "live-cluster",
                    "--analyzer-image-digest",
                    replacement,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertNotIn(before["LAE_LUMA_SERVICE_TOKEN"], rejected.stderr)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE_SCRIPT),
                    "--bundle-dir",
                    str(output),
                    "--cluster-id",
                    "live-cluster",
                    "--analyzer-image-digest",
                    replacement,
                    "--update",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)
            self.assertTrue(result["ok"])
            after = _dotenv(output / "lae-platform-staging.env")
            self.assertEqual(after["LAE_LUMA_CLUSTER_ID"], "live-cluster")
            self.assertEqual(after["LAE_ANALYZER_IMAGE_DIGEST"], replacement)
            normalized_control = _dotenv(output / "lae-control.env")
            self.assertEqual(
                normalized_control["LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON"],
                '["manager","tecent"]',
            )
            self.assertEqual(
                normalized_control["LUMA_LAE_RUNTIME_STORAGE_CLASS"],
                "lae-staging-runtime-nfs",
            )
            for name in (
                "LAE_LUMA_SERVICE_TOKEN",
                "LAE_LUMA_RUNTIME_SERVICE_TOKEN",
                "LAE_CREDENTIAL_BROKER_TOKEN",
                "LAE_OBJECT_SOURCE_BROKER_TOKEN",
                "LAE_ADMIN_API_TOKEN",
                "LAE_AUTH_HMAC_KEY",
                "LAE_DATABASE_URL",
            ):
                self.assertEqual(after[name], before[name])

    def test_bundle_is_private_complete_separated_and_never_prints_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bundle"
            api_key = Path(temporary) / "ark-api-key"
            api_key.write_text("test-provider-key", encoding="utf-8")
            api_key.chmod(0o600)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--analyzer-image-digest",
                    ANALYZER,
                    "--cluster-id",
                    "luma-staging-test",
                    "--llm-base-url",
                    "https://llm.example.test/v1",
                    "--llm-model",
                    "test-model",
                    "--llm-api-key-file",
                    str(api_key),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            public_result = json.loads(completed.stdout)
            self.assertEqual(public_result["outputDir"], str(output.resolve()))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for path in output.iterdir():
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            environment = _dotenv(output / "lae-platform-staging.env")
            global_names = {
                line.split("=", 1)[0]
                for line in (LAE_ROOT / "deploy/luma/.global-secrets.example")
                .read_text(encoding="utf-8")
                .splitlines()
                if line and not line.startswith("#")
            }
            compose_text = (
                LAE_ROOT / "deploy/luma/docker-compose.staging.yml"
            ).read_text(encoding="utf-8")
            required = set(
                re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)\}", compose_text)
            ) - global_names
            self.assertTrue(required.issubset(environment))
            self.assertTrue(all(environment[name] for name in required))
            self.assertTrue(global_names.isdisjoint(environment))
            self.assertEqual(environment["LAE_ANALYZER_IMAGE_DIGEST"], ANALYZER)
            self.assertEqual(
                environment["LAE_AGENT_LLM_BASE_URL"],
                "https://llm.example.test/v1",
            )
            self.assertEqual(environment["LAE_AGENT_LLM_MODEL"], "test-model")
            self.assertEqual(
                environment["LAE_AGENT_LLM_API_KEY"], "test-provider-key"
            )
            self.assertEqual(
                environment["LAE_DATABASE_URL"],
                "postgresql+asyncpg://lae:"
                + environment["LAE_POSTGRES_PASSWORD"]
                + "@postgres:5432/lae",
            )

            secret_names = (
                "LAE_LUMA_SERVICE_TOKEN",
                "LAE_LUMA_RUNTIME_SERVICE_TOKEN",
                "LAE_CREDENTIAL_BROKER_TOKEN",
                "LAE_OBJECT_SOURCE_BROKER_TOKEN",
                "LAE_ADMIN_API_TOKEN",
            )
            secret_values = [environment[name] for name in secret_names]
            self.assertEqual(len(secret_values), len(set(secret_values)))
            self.assertEqual(
                (output / "lae-builder.token").read_text().strip(),
                environment["LAE_LUMA_SERVICE_TOKEN"],
            )
            self.assertEqual(
                (output / "lae-runtime.token").read_text().strip(),
                environment["LAE_LUMA_RUNTIME_SERVICE_TOKEN"],
            )
            self.assertEqual(
                (output / "credential-broker.token").read_text().strip(),
                environment["LAE_CREDENTIAL_BROKER_TOKEN"],
            )
            self.assertEqual(
                (output / "object-broker.token").read_text().strip(),
                environment["LAE_OBJECT_SOURCE_BROKER_TOKEN"],
            )
            builder_ai = _dotenv(output / "builder-agent-ai.env")
            self.assertEqual(
                builder_ai["LUMA_BUILDER_ANALYZE_CONTROLLER_TOKEN"],
                environment["LAE_AGENT_CONTROLLER_TOKEN"],
            )
            self.assertEqual(
                builder_ai["LUMA_BUILDER_ANALYZE_AI_REQUIRED"],
                "1",
            )

            signing = json.loads(
                (output / "lae-plan-signing.json").read_text(encoding="utf-8")
            )["lae-plan-primary"]
            self.assertTrue(signing.startswith("base64:"))
            self.assertEqual(
                base64.b64decode(signing.removeprefix("base64:"), validate=True),
                base64.b64decode(
                    environment["LAE_BUILD_PLAN_SIGNING_HMAC_KEY"], validate=True
                ),
            )
            self.assertEqual(
                json.loads(
                    (output / "lae-builder-principals.json").read_text()
                )["lae-builder"]["tenantRefs"],
                ["*"],
            )
            runtime = json.loads(
                (output / "lae-runtime-principals.json").read_text()
            )["lae-runtime"]
            self.assertIn("runtime:secrets:issue", runtime["scopes"])
            self.assertEqual(runtime["builderPrincipalRefs"], ["lae-builder"])

            safe_output = completed.stdout + completed.stderr
            control_environment = (output / "lae-control.env").read_text()
            for secret in secret_values + [environment["LAE_POSTGRES_PASSWORD"]]:
                self.assertNotIn(secret, safe_output)
                self.assertNotIn(secret, control_environment)
            self.assertIn("LUMA_LAE_PLAN_SIGNING_KEYS_FILE", control_environment)
            self.assertNotIn("LUMA_LAE_PLAN_SIGNING_KEYS_JSON", control_environment)
            self.assertIn(
                "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY=0",
                control_environment,
            )
            self.assertIn(
                "LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY=1",
                control_environment,
            )
            self.assertIn(
                "LUMA_LAE_BUILDER_REGISTRY_INSECURE=0",
                control_environment,
            )
            self.assertIn(
                "LUMA_LAE_RUNTIME_STORAGE_CLASS=lae-staging-runtime-nfs",
                control_environment,
            )
            self.assertIn(
                'LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON=["manager","tecent"]',
                control_environment,
            )
            self.assertNotIn("export ", control_environment)
            self.assertNotIn("LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON", environment)
            manifest = json.loads(
                (output / "bundle-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["llmBaseUrl"], "https://llm.example.test/v1")
            self.assertEqual(manifest["llmModel"], "test-model")
            self.assertNotIn("test-provider-key", json.dumps(manifest))

            repeated = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--analyzer-image-digest",
                    ANALYZER,
                    "--cluster-id",
                    "luma-staging-test",
                    "--llm-model",
                    "test-model",
                    "--llm-api-key-file",
                    str(api_key),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(repeated.returncode, 2)
            for secret in secret_values:
                self.assertNotIn(secret, repeated.stderr)

    def test_invalid_mutable_analyzer_is_rejected_before_creating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bundle"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--analyzer-image-digest",
                    "registry.invalid/lae-agent:latest",
                    "--cluster-id",
                    "luma-staging-test",
                    "--llm-model",
                    "test-model",
                    "--llm-api-key-file",
                    str(Path(temporary) / "missing-key"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
