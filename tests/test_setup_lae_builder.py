from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup-lae-builder.sh"
RUNNER_BUILD_SCRIPT = ROOT / "scripts" / "build-lae-agent-runner.sh"


class LaeBuilderSetupScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_script_is_executable_and_bash_syntax_is_valid(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True, text=True)

        self.assertTrue(os.access(RUNNER_BUILD_SCRIPT, os.X_OK))
        subprocess.run(
            ["bash", "-n", str(RUNNER_BUILD_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_runner_build_is_exact_commit_builder_only_and_digest_output(self):
        source = RUNNER_BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('[[ "$commit" =~ ^[0-9a-f]{40}$ ]]', source)
        self.assertIn('docker buildx inspect "$builder"', source)
        self.assertIn('getent passwd "$(id -u)"', source)
        self.assertIn('export HOME="$effective_home"', source)
        self.assertIn('git -C "$work" fetch -q --depth=1 origin "$commit"', source)
        self.assertIn("--platform linux/amd64", source)
        self.assertIn("--provenance=true", source)
        self.assertIn("--sbom=true", source)
        self.assertIn("containerimage.digest", source)
        self.assertIn('docker buildx imagetools inspect "$immutable"', source)
        self.assertNotIn(":latest", source)

    def test_help_is_available_without_mutating_the_host(self):
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--check", result.stdout)
        self.assertIn("--runner-image IMAGE@sha256:DIGEST", result.stdout)
        self.assertIn("--registry-host HOST[:PORT]", result.stdout)
        self.assertIn("--registry-push-host HOST[:PORT]", result.stdout)
        self.assertIn("--registry-basic-auth", result.stdout)
        self.assertIn("--buildkit-egress-proxy HTTP_URL", result.stdout)
        self.assertIn("--buildkit-sha256 SHA256", result.stdout)

    def test_tool_versions_and_checksum_roots_are_pinned(self):
        expected = {
            'BUILDKIT_VERSION="v0.31.1"',
            'SYFT_VERSION="v1.46.0"',
            'TRIVY_VERSION="v0.72.0"',
            'COSIGN_VERSION="v3.1.1"',
            'CRANE_VERSION="v0.21.7"',
            'SYFT_CHECKSUM_FILE_SHA256="2fefc202b2eccab83888cc91f5a364a75df0dd777afbbae5b5e23ebd93d81ac6"',
            'TRIVY_CHECKSUM_FILE_SHA256="ebe9d19a774b950e240b1017a038e9b5a002ea068e02023369ff6d241c10c580"',
            'COSIGN_CHECKSUM_FILE_SHA256="47ec240858ef4c4f6d214fee9ed351c9631ee8ed3e2536ce9885a41cf509be6f"',
            'CRANE_CHECKSUM_FILE_SHA256="cd15501232e498a51ef7d2d65dd2fb360f9f1086e234acef1af02343cea291f9"',
        }
        for line in expected:
            self.assertIn(line, self.source)

        self.assertIn('verify_file_sha256 "$checksum_path" "$checksum_file_sha"', self.source)
        self.assertIn('verify_file_sha256 "$asset_path" "$expected"', self.source)
        self.assertIn('verify_file_sha256 "$archive" "$BUILDKIT_SHA256"', self.source)
        self.assertNotIn("100.66.177.70", self.source)

    def test_trust_inputs_are_explicit_and_fail_closed(self):
        self.assertIn('[[ -n "$RUNNER_IMAGE" ]] || die "--runner-image is required"', self.source)
        self.assertRegex(
            self.source,
            re.compile(r"RUNNER_IMAGE.*@sha256:\[0-9a-f\]\{64\}", re.DOTALL),
        )
        self.assertIn('validate_registry_host "$REGISTRY_PULL_HOST" "--registry-host"', self.source)
        self.assertIn('validate_registry_host "$REGISTRY_PUSH_HOST" "--registry-push-host"', self.source)
        self.assertIn("loopback is not the Builder host", self.source)
        self.assertIn('validate_sha256 "$BUILDKIT_SHA256"', self.source)
        self.assertIn('die "--external-registry values must be sorted and unique"', self.source)
        self.assertNotRegex(self.source, re.compile(r"BUILDKIT_SHA256=\"[0-9a-f]{64}\""))

    def test_check_mode_branches_before_mutating_setup(self):
        check_branch = self.source.index('if [[ "$MODE" == "check" ]]')
        first_package_install = self.source.rindex("install_system_dependencies\n")
        self.assertLess(check_branch, first_package_install)
        self.assertIn("verify_all", self.source[check_branch:first_package_install])
        self.assertNotIn("init_audit", self.source[check_branch:first_package_install])

    def test_rootless_runtime_and_supply_chain_gates_are_verified(self):
        required_fragments = (
            "dockerd-rootless-setuptool.sh",
            "loginctl enable-linger",
            "rootlesskit} --net=slirp4netns",
            "Docker daemon did not report rootless security mode",
            "rootless BuildKit has no available worker",
            "Environment=HTTP_PROXY=${BUILDKIT_EGRESS_PROXY}",
            "Environment=NO_PROXY=${BUILDKIT_EGRESS_NO_PROXY}",
            "buildctl does not support attestations",
            "Trivy DB metadata is invalid",
            "local runner image does not expose the required RepoDigest",
            "registry v2 endpoint does not allow anonymous access",
            "LUMA_BUILDER_ALLOW_BASIC_REGISTRY=",
            "verified_rootless_bind_probe",
            "rootless analyzer image does not implement the current LAE result contract",
            '.schemaVersion == "lae.agent-analysis-result/v1"',
            '.verdict == "deployable"',
            "--user 0:0",
            "chmod 0500 \"$source_dir\" \"$input_dir\"",
            "chmod 0700 \"$BIND_PROBE_DIR\" \"$output_dir\"",
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.source)

    def test_rootless_docker_socket_identity_is_normalized_after_restart(self):
        self.assertIn(
            'readonly ROOTLESS_DOCKER_SOCKET_HELPER="/usr/local/lib/luma-builder/normalize-docker-socket"',
            self.source,
        )
        self.assertIn("configure_rootless_docker_socket_permissions", self.source)
        self.assertIn("ExecStartPost=${ROOTLESS_DOCKER_SOCKET_HELPER}", self.source)
        self.assertIn('chgrp "${builder_gid}" "\\$socket"', self.source)
        self.assertIn('chmod 0660 "\\$socket"', self.source)
        self.assertIn('rootless Docker socket group does not match the daemon peer GID', self.source)
        self.assertIn('rootless Docker socket mode is not 0660', self.source)

    def test_ubuntu_docker_io_rootless_tools_are_supported(self):
        self.assertIn("resolve_rootless_docker_tools()", self.source)
        self.assertIn("/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh", self.source)
        self.assertIn('ROOTLESS_DOCKER_TOOL_ROOT="/usr/local/lib/luma-builder/docker-rootless-tools"', self.source)
        self.assertIn('ln -sfn "$docker_cli" "$ROOTLESS_DOCKER_TOOL_ROOT/docker"', self.source)
        self.assertIn('[[ -x "${bin_dir}/dockerd-rootless.sh" ]]', self.source)
        self.assertIn('run_as_builder "$ROOTLESS_DOCKER_SETUP_TOOL" install --force', self.source)
        self.assertIn("ROOTLESS_DOCKER_BIN_DIR", self.source)
        self.assertIn('build_help=$(buildctl build --help 2>&1)', self.source)

    def test_generated_node_agent_environment_contains_capabilities_but_no_credentials(self):
        start = self.source.index("render_node_agent_env()")
        end = self.source.index("write_node_agent_env()")
        env_renderer = self.source[start:end]
        expected = (
            "LUMA_BUILDER_TASKS_ENABLED=1",
            "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST=",
            "LUMA_BUILDER_ANALYZE_DOCKER_HOST=",
            "LUMA_BUILDER_BUILD_ENABLED=1",
            "LUMA_BUILDER_BUILDKIT_ADDR=",
            "LUMA_BUILDER_REGISTRY_PULL_HOST=",
            "LUMA_BUILDER_REGISTRY_PUSH_HOST=",
            "LUMA_BUILDER_TRIVY_CACHE_DIR=",
        )
        for entry in expected:
            self.assertIn(entry, env_renderer)

        assignments = re.findall(r"^(LUMA_[A-Z0-9_]+)=", env_renderer, flags=re.MULTILINE)
        self.assertTrue(assignments)
        for name in assignments:
            self.assertNotRegex(name, re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL"))

    def test_audit_and_manifest_record_binary_and_configuration_hashes(self):
        self.assertIn('readonly AUDIT_LOG="${AUDIT_DIR}/lae-builder-setup.log"', self.source)
        self.assertIn('readonly MANIFEST_FILE="${BUILDER_ROOT}/toolchain-manifest.env"', self.source)
        for key in (
            "BUILDKIT_ASSET_SHA256=",
            "BUILDCTL_BINARY_SHA256=",
            "SYFT_BINARY_SHA256=",
            "TRIVY_BINARY_SHA256=",
            "TRIVY_DB_METADATA_SHA256=",
            "COSIGN_BINARY_SHA256=",
            "CRANE_BINARY_SHA256=",
            "NODE_AGENT_ENV_SHA256=",
        ):
            self.assertIn(key, self.source)


if __name__ == "__main__":
    unittest.main()
