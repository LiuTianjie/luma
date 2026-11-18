"""Tests for the ``luma builder`` CLI subcommands and standalone runner helpers."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from luma.builder_runner import format_cache_reference, format_image_repository, normalize_registry_host, validate_git_ref
from luma.cli import main
from luma.errors import LumaError


class CliBuilderTests(unittest.TestCase):
    def _run_cli(self, argv: list[str], *, config_home: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "LUMA_CONFIG_HOME": str(config_home),
            "LUMA_USER_CONFIG": str(config_home / "user.json"),
            "LUMA_BUILD_URL": "",
            "LUMA_BUILD_TOKEN": "",
            "LUMA_BUILD_ADMIN_TOKEN": "",
            "LUMA_DEPLOY_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--no-env", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_luma_builder_reuses_luma_login_context_and_calls_build_service(self) -> None:
        seen_requests: list[tuple[str, str, str]] = []

        class FakeBuildServer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send_json(self, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                seen_requests.append(("GET", self.path, self.headers.get("Authorization", "")))
                if self.path == "/v1/status":
                    self._send_json(
                        {
                            "ok": True,
                            "version": "0.1.0",
                            "authMode": "control-sso",
                            "registry": {"registryHost": "crpi-x.cn-beijing.personal.cr.aliyuncs.com", "namespace": "luma", "flatten": True},
                            "controlTarget": {"configured": True, "endpoint": "https://luma.itool.tech"},
                            "runners": [{"name": "m4", "online": True, "nativePlatform": "linux/arm64", "activeBuilds": 0, "maxConcurrency": 1, "weight": 100, "cacheMode": "read-only"}],
                        }
                    )
                    return
                self._send_json({"ok": True, "builds": []})

            def do_POST(self):
                seen_requests.append(("POST", self.path, self.headers.get("Authorization", "")))
                if self.path == "/v1/runners/tokens":
                    self._send_json(
                        {
                            "ok": True,
                            "runner": {"name": "m4", "weight": 100, "cacheMode": "read-only", "runnerToken": "lbr_secret123"},
                        }
                    )
                    return
                if self.path == "/v1/builds":
                    self._send_json({"ok": True, "build": {"id": "bld-test-1", "status": "queued"}})
                    return
                self._send_json({"ok": True})

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBuildServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_port}"

        with tempfile.TemporaryDirectory() as tmp:
            cfg_home = Path(tmp)
            ctx_dir = cfg_home / "contexts"
            ctx_dir.mkdir(parents=True)
            (cfg_home / "current-context").write_text("luma-demo\n", encoding="utf-8")
            (ctx_dir / "luma-demo.json").write_text(
                json.dumps({"clusterId": "luma-demo", "endpoint": "https://luma.itool.tech", "token": "sso-borrowed-token"}),
                encoding="utf-8",
            )

            rc, out, err = self._run_cli(["builder", "use", base_url], config_home=cfg_home)
            self.assertEqual(rc, 0, err)
            self.assertIn(base_url, out)

            rc, out, err = self._run_cli(["builder", "status", "--format", "json"], config_home=cfg_home)
            self.assertEqual(rc, 0, err)
            parsed = json.loads(out)
            self.assertTrue(parsed["ok"])
            self.assertEqual(parsed["result"]["runners"][0]["name"], "m4")

            rc, out, err = self._run_cli(
                ["builder", "token", "create", "m4", "--cache-mode", "read-only"],
                config_home=cfg_home,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("LUMA_RUNNER_TOKEN=lbr_secret123", out)

            rc, out, err = self._run_cli(
                ["builder", "submit", "acme/web", "--ref", "main", "--detach"],
                config_home=cfg_home,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("bld-test-1", out)

        self.assertTrue(all(auth == "Bearer sso-borrowed-token" for _, _, auth in seen_requests))

    def test_runner_naming_and_git_ref_hardening(self) -> None:
        self.assertEqual(
            format_image_repository(repo_slug="acme/web", service_name="api", single_service=False, namespace="luma"),
            "luma/acme-web-api",
        )
        self.assertEqual(
            format_cache_reference(
                push_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                repo_slug="acme/web",
                platform="linux/arm64",
                namespace="luma",
            ),
            "crpi-x.cn-beijing.personal.cr.aliyuncs.com/luma/luma-buildcache:acme-web-linux-arm64",
        )
        with self.assertRaises(LumaError):
            validate_git_ref("--upload-pack=evil")
        with self.assertRaises(LumaError):
            normalize_registry_host('bad"\n[evil]')


if __name__ == "__main__":
    unittest.main()
