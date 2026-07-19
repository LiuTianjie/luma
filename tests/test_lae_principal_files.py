from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import server as control_server
from luma.control.state import init_state
from luma.errors import LumaError
from luma.lae_runtime import RUNTIME_AUDIENCE, RuntimeBinding


class LaePrincipalFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.builder_token = "builder-file-principal-token-" + "b" * 32
        self.runtime_token = "runtime-file-principal-token-" + "r" * 32
        self.builder_token_path = self.root / "lae-builder.token"
        self.runtime_token_path = self.root / "lae-runtime.token"
        self.builder_config_path = self.root / "lae-builder-principals.json"
        self.runtime_config_path = self.root / "lae-runtime-principals.json"
        self.plan_signing_path = self.root / "lae-plan-signing.json"
        self.plan_signing_secret = b"p" * 32
        self._write_private(self.builder_token_path, self.builder_token + "\n")
        self._write_private(self.runtime_token_path, self.runtime_token + "\n")
        self._write_private(
            self.builder_config_path,
            json.dumps(
                {
                    "lae-builder": {
                        "tokenFile": self.builder_token_path.name,
                        "tenantRefs": ["tenant-file-test"],
                        "applicationRefs": ["application-file-test"],
                    }
                }
            ),
        )
        self._write_private(
            self.runtime_config_path,
            json.dumps(
                {
                    "lae-runtime": {
                        "tokenFile": self.runtime_token_path.name,
                        "tenantRefs": ["tenant-file-test"],
                        "applicationRefs": ["application-file-test"],
                        "builderPrincipalRefs": ["lae-builder"],
                        "scopes": ["runtime:deployments:read"],
                    }
                }
            ),
        )
        self._write_private(
            self.plan_signing_path,
            json.dumps(
                {
                    "lae-plan-primary": "base64:"
                    + base64.b64encode(self.plan_signing_secret).decode("ascii")
                }
            ),
        )
        self.environment = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_LAE_SERVICE_PRINCIPALS_FILE": str(
                    self.builder_config_path
                ),
                "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE": str(
                    self.runtime_config_path
                ),
                "LUMA_LAE_PLAN_SIGNING_KEYS_FILE": str(self.plan_signing_path),
            },
            clear=True,
        )
        self.environment.start()
        self.state = init_state(
            domain="luma.example.com",
            cluster_id="luma-principal-file-test",
            overwrite=True,
        )

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    @staticmethod
    def _write_private(path: Path, value: str):
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _binding():
        return RuntimeBinding(
            "tenant-file-test",
            "application-file-test",
            "operation-file-test",
            "revision-file-test",
            "deployment-file-test",
        )

    def test_builder_and_runtime_file_principals_are_accepted_and_separate(self):
        builder = control_server._require_lae_service_principal(
            self.state,
            self.builder_token,
        )
        self.assertEqual(builder["id"], "lae-builder")
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            control_server._require_lae_service_principal(
                self.state,
                self.runtime_token,
            )

        runtime = control_server._require_lae_runtime_principal(
            self.state,
            self.runtime_token,
            audience=RUNTIME_AUDIENCE,
            scope="runtime:deployments:read",
            binding=self._binding(),
        )
        self.assertEqual(runtime["id"], "lae-runtime")
        with self.assertRaisesRegex(Exception, "unauthorized"):
            control_server._require_lae_runtime_principal(
                self.state,
                self.builder_token,
                audience=RUNTIME_AUDIENCE,
                scope="runtime:deployments:read",
                binding=self._binding(),
            )

        state_text = self.state_dir.joinpath("control.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(self.builder_token, state_text)
        self.assertNotIn(self.runtime_token, state_text)
        self.assertNotIn(self.builder_token, self.builder_config_path.read_text())
        self.assertNotIn(self.runtime_token, self.runtime_config_path.read_text())

    def test_private_regular_config_and_token_files_are_required(self):
        self.builder_token_path.chmod(0o640)
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()
        self.builder_token_path.chmod(0o600)

        self.builder_config_path.chmod(0o644)
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()
        self.builder_config_path.chmod(0o600)

        real_config = self.root / "real-builder-principals.json"
        self._write_private(real_config, self.builder_config_path.read_text())
        self.builder_config_path.unlink()
        self.builder_config_path.symlink_to(real_config)
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()

    def test_plan_signing_keys_use_private_file_and_reject_ambiguous_inline_secret(self):
        self.assertEqual(
            control_server._lae_plan_signing_keys(),
            {"lae-plan-primary": self.plan_signing_secret},
        )
        self.assertNotIn(
            base64.b64encode(self.plan_signing_secret).decode("ascii"),
            self.state_dir.joinpath("control.json").read_text(encoding="utf-8"),
        )
        self.plan_signing_path.chmod(0o644)
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_plan_signing_keys()
        self.plan_signing_path.chmod(0o600)
        with patch.dict(
            os.environ,
            {
                "LUMA_LAE_PLAN_SIGNING_KEYS_JSON": json.dumps(
                    {"lae-plan-inline": "inline-secret-value-at-least-32-bytes"}
                )
            },
            clear=False,
        ), self.assertRaisesRegex(LumaError, "ambiguous"):
            control_server._lae_plan_signing_keys()

    def test_inline_tokens_path_escape_and_mixed_modes_fail_closed(self):
        self.builder_config_path.write_text(
            json.dumps(
                {
                    "lae-builder": {
                        "token": self.builder_token,
                        "tenantRefs": ["*"],
                        "applicationRefs": ["*"],
                    }
                }
            ),
            encoding="utf-8",
        )
        self.builder_config_path.chmod(0o600)
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()

        self._write_private(
            self.builder_config_path,
            json.dumps(
                {
                    "lae-builder": {
                        "tokenFile": "../outside.token",
                        "tenantRefs": ["*"],
                        "applicationRefs": ["*"],
                    }
                }
            ),
        )
        with self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()

        with patch.dict(
            os.environ,
            {
                "LUMA_LAE_SERVICE_PRINCIPALS_JSON": json.dumps(
                    {
                        "legacy": {
                            "token": "legacy-builder-token-value",
                            "tenantRefs": ["*"],
                            "applicationRefs": ["*"],
                        }
                    }
                )
            },
            clear=False,
        ), self.assertRaisesRegex(LumaError, "configuration is invalid"):
            control_server._lae_service_principals()

    def test_duplicate_builder_and_runtime_file_tokens_cannot_cross_planes(self):
        self._write_private(self.runtime_token_path, self.builder_token + "\n")
        with self.assertRaisesRegex(Exception, "unauthorized"):
            control_server._require_lae_runtime_principal(
                self.state,
                self.builder_token,
                audience=RUNTIME_AUDIENCE,
                scope="runtime:deployments:read",
                binding=self._binding(),
            )


if __name__ == "__main__":
    unittest.main()
