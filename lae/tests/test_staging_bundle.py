from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LAE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = LAE_ROOT / "deploy/luma/generate-staging-bundle.py"
ANALYZER = "100.66.177.70:5000/lae/agent-runner@sha256:" + "a" * 64


def _dotenv(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }


class StagingBundleTests(unittest.TestCase):
    def test_bundle_is_private_complete_separated_and_never_prints_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bundle"
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
            required = {
                line.split("=", 1)[0]
                for line in (LAE_ROOT / "deploy/luma/.env.example")
                .read_text(encoding="utf-8")
                .splitlines()
                if line and not line.startswith("#")
            }
            self.assertTrue(required.issubset(environment))
            self.assertTrue(all(environment[name] for name in required))
            self.assertEqual(environment["LAE_ANALYZER_IMAGE_DIGEST"], ANALYZER)
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
                "LUMA_LAE_RUNTIME_STORAGE_CLASS=lae-staging-runtime-nfs",
                control_environment,
            )
            self.assertIn(
                "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON='[\"manager\",\"tecent\"]'",
                control_environment,
            )
            self.assertNotIn("LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON", environment)

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
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
