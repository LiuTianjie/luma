from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import Mock, patch

from luma.bootstrap import (
    CONTROL_ENV_FILE,
    _parse_control_environment_file,
    _persisted_control_environment,
    deploy_control_stack,
)
from luma.config import LumaConfig
from luma.errors import LumaError


class ControlEnvironmentPersistenceTests(unittest.TestCase):
    def test_parser_accepts_only_strict_allowlisted_name_value_lines(self) -> None:
        parsed = _parse_control_environment_file(
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=600\n"
            b"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON=[\"manager\",\"tecent\"]\n"
        )

        self.assertEqual(parsed["LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS"], "600")
        self.assertEqual(
            parsed["LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON"],
            '["manager","tecent"]',
        )

        invalid_files = (
            b"export LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=600\n",
            b"# shell comments are not part of this format\n",
            b"UNRELATED_MANAGER_SECRET=secret\n",
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=\n",
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=600\n"
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=900\n",
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=600\r\n",
            b"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=29\n",
        )
        for content in invalid_files:
            with self.subTest(content=content), self.assertRaises(LumaError):
                _parse_control_environment_file(content)

    def test_remote_reader_requires_fixed_private_regular_file(self) -> None:
        remote = Mock()
        remote.sudo.side_effect = LumaError(
            "Luma Control environment file mode must be 0400 or 0600"
        )

        with self.assertRaisesRegex(LumaError, "0400 or 0600"):
            _persisted_control_environment(remote)

        command = remote.sudo.call_args.args[0]
        self.assertIn(CONTROL_ENV_FILE, command)
        self.assertIn('[ ! -L "$directory" ]', command)
        self.assertIn('8#$directory_mode & 022', command)
        self.assertIn('[ ! -L "$path" ]', command)
        self.assertIn('[ -f "$path" ]', command)
        self.assertIn('"$uid" = 0', command)
        self.assertIn('400|600', command)

    def test_deploy_merges_persisted_environment_with_invocation_priority(self) -> None:
        image = "ghcr.io/liutianjie/luma-control@sha256:" + "a" * 64
        config = LumaConfig(
            {"defaults": {"engine": "nomad", "images": {"lumaControl": image}}},
            None,
        )
        persisted = (
            "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS=600\n"
            "LUMA_LAE_RUNTIME_STORAGE_CLASS=cn-nfs\n"
        ).encode()
        remote = Mock()
        remote.sudo.return_value = base64.b64encode(persisted).decode()
        submitted: dict[str, object] = {}

        def capture(_remote: object, job_json: str, job_id: str) -> str:
            submitted["job"] = json.loads(job_json)
            submitted["jobId"] = job_id
            return f"Nomad job deployed: {job_id}"

        with patch.dict(
            os.environ,
            {"LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS": "900"},
            clear=True,
        ), patch(
            "luma.bootstrap._ensure_control_image",
            return_value=f"Control image pulled: {image}",
        ), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status",
            return_value="Nomad tmpfs compatibility ok",
        ), patch(
            "luma.bootstrap._deploy_nomad_job",
            side_effect=capture,
        ), patch(
            "luma.bootstrap._wait_nomad_job",
            return_value="Nomad job running: luma-control",
        ):
            deploy_control_stack(
                remote,
                config,
                "luma.example.com",
                require_pull_egress=False,
                node_name="manager",
            )

        self.assertEqual(submitted["jobId"], "luma-control")
        job = submitted["job"]
        assert isinstance(job, dict)
        environment = job["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(environment["LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS"], "900")
        self.assertEqual(environment["LUMA_LAE_RUNTIME_STORAGE_CLASS"], "cn-nfs")


if __name__ == "__main__":
    unittest.main()
