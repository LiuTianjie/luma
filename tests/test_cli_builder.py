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
                            "controlTarget": {"configured": True, "endpoint": "https://luma.example.com"},
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
                json.dumps({"clusterId": "luma-demo", "endpoint": "https://luma.example.com", "token": "sso-borrowed-token"}),
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

    def test_runner_parity_between_luma_cli_and_standalone_builder_package(self) -> None:
        import sys

        builder_root = Path(__file__).resolve().parents[1] / "builder"
        if str(builder_root) not in sys.path:
            sys.path.insert(0, str(builder_root))

        from luma import builder_runner as cli_runner
        from luma_build import deployer as pkg_deployer
        from luma_build import registry_naming as pkg_naming
        from luma_build import runner as pkg_runner

        cases = [
            {"single_service": True},
            {"manifest_path": "luma.prod.api.yml", "context": "apps/api", "single_service": True},
            {"compose_sidecar": "apps/billing/luma.billing.compose.yml", "service_name": "web", "single_service": True},
            {"compose_sidecar": "luma.compose.yml", "service_name": "worker", "single_service": False},
        ]
        for kwargs in cases:
            self.assertEqual(cli_runner.derive_cache_variant(**kwargs), pkg_naming.derive_cache_variant(**kwargs))

        acr = "crpi-abc.cn-beijing.personal.cr.aliyuncs.com"
        self.assertEqual(cli_runner.registry_no_proxy_entries(acr), pkg_naming.registry_no_proxy_entries(acr))
        self.assertIn(".aliyuncs.com", cli_runner.registry_no_proxy_entries(acr))
        self.assertTrue(cli_runner.uses_inline_registry_cache(acr))
        self.assertEqual(cli_runner.uses_inline_registry_cache(acr), pkg_naming.uses_inline_registry_cache(acr))
        self.assertFalse(cli_runner.uses_inline_registry_cache("100.64.0.70:5000"))

        with tempfile.TemporaryDirectory() as td:
            rt = {
                "dockerHost": "",
                "resolvedEndpoint": "unix:///tmp/orb.sock",
                "tlsVerify": "",
                "certPath": "",
                "currentContext": "orbstack",
                "contextFiles": {"contexts/meta/h1/meta.json": b'{"Name":"orbstack"}'},
            }
            c_dir = Path(td) / "cli-cfg"
            p_dir = Path(td) / "pkg-cfg"
            cli_runner._write_docker_config(c_dir, [], docker_runtime=rt)
            pkg_runner._write_docker_config(p_dir, [], docker_runtime=rt)
            self.assertEqual(
                json.loads((c_dir / "config.json").read_text(encoding="utf-8")),
                json.loads((p_dir / "config.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(
                (c_dir / "contexts/meta/h1/meta.json").read_bytes(),
                (p_dir / "contexts/meta/h1/meta.json").read_bytes(),
            )

            # Both CLI runner and standalone runner strip scoped gitAuth when x-luma-git-contexts points outside pathPrefix!
            import hashlib
            import subprocess as sp

            shared_repo = Path(td) / "shared-src"
            shared_repo.mkdir()
            (shared_repo / "x.txt").write_text("ok\n", encoding="utf-8")
            sp.run(["git", "-C", str(shared_repo), "init"], check=True, stdout=sp.DEVNULL)
            sp.run(["git", "-C", str(shared_repo), "config", "user.email", "ci@example.com"], check=True)
            sp.run(["git", "-C", str(shared_repo), "config", "user.name", "ci"], check=True)
            sp.run(["git", "-C", str(shared_repo), "add", "."], check=True)
            sp.run(["git", "-C", str(shared_repo), "commit", "-m", "init"], check=True, stdout=sp.DEVNULL)
            commit = sp.check_output(["git", "-C", str(shared_repo), "rev-parse", "HEAD"], text=True).strip()
            archive_sha = hashlib.sha256(
                sp.check_output(["git", "-C", str(shared_repo), "archive", "--format=tar.gz", commit])
            ).hexdigest()

            primary_repo = Path(td) / "primary-src"
            primary_repo.mkdir()
            (primary_repo / "team_b.lock.json").write_text(
                json.dumps(
                    {
                        "repository": "https://git.example.com/corp/team-b/shared.git",
                        "commit": commit,
                        "archive_sha256": archive_sha,
                    }
                ),
                encoding="utf-8",
            )
            work = Path(td) / "ctx-work"
            work.mkdir()
            seen_cli_tokens: list[str | None] = []
            seen_pkg_auths: list[dict | None] = []

            def fake_gitops_clone(url, dest, *, ref=None, token=None, **kw):
                import shutil

                seen_cli_tokens.append(token)
                shutil.copytree(shared_repo, dest)

            def fake_pkg_clone(*, repo_url, ref, dest, git_auth, proxy="", **kw):
                import shutil

                seen_pkg_auths.append(git_auth)
                shutil.copytree(shared_repo, dest)
                return commit[:12], commit

            scoped_auth = {"username": "bot", "token": "team-a-secret", "pathPrefix": "corp/team-a"}
            spec = {"x-luma-git-contexts": {"shared": {"lock": "team_b.lock.json"}}}
            with patch("luma.build_sources.gitops.clone", side_effect=fake_gitops_clone):
                cli_runner._prepare_runner_git_contexts(
                    primary_repo,
                    spec,
                    repo_url="https://git.example.com/corp/team-a/app.git",
                    workspace=work,
                    git_auth=scoped_auth,
                    proxy="",
                    on_line=None,
                    cancel_event=threading.Event(),
                )
            with patch("luma_build.runner._clone_repo", side_effect=fake_pkg_clone):
                pkg_runner._prepare_git_contexts(
                    primary_repo,
                    spec,
                    primary_repo_url="https://git.example.com/corp/team-a/app.git",
                    workspace=work,
                    git_auth=scoped_auth,
                    proxy="",
                    on_line=None,
                    cancel_event=threading.Event(),
                )
            self.assertEqual(seen_cli_tokens, [None])
            self.assertEqual(seen_pkg_auths, [None])

        manifest_samples = [
            "name: svc\nregion: cn\nnode: arm-1\n",
            "name: svc\nbuild:\n  platform: linux/arm64\n",
            "name: stack\nregion: hk\nservices:\n  api:\n    node: arm-2\n  web:\n    exposure: public\n",
        ]
        for sample in manifest_samples:
            self.assertEqual(
                cli_runner.extract_manifest_target_hints(sample),
                pkg_deployer.extract_manifest_target_hints(sample),
            )

        task = {
            "platformExplicit": False,
            "nodePlatforms": {"arm-1": "linux/arm64"},
            "regionPlatforms": {"cn": ["linux/amd64"]},
        }
        c_plats = cli_runner._resolve_post_clone_platforms(task, manifest_text=manifest_samples[0], fallback=["linux/amd64"])
        p_plats = pkg_runner._resolve_post_clone_platforms(task, manifest_text=manifest_samples[0], fallback=["linux/amd64"])
        self.assertEqual(c_plats, ["linux/arm64"])
        self.assertEqual(c_plats, p_plats)
        with self.assertRaises(cli_runner.PlatformRequeueRequired):
            cli_runner._check_runner_platform_compatibility(c_plats, native_platform="linux/amd64", task=task)
        with self.assertRaises(pkg_runner.PlatformRequeueRequired):
            pkg_runner._check_runner_platform_compatibility(p_plats, native_platform="linux/amd64", task=task)

    def test_deploy_workflow_supports_builder_submit_and_registry_namespace(self) -> None:
        from luma.deploy_workflow import comparison_fields, make_recipe, parse_recipe, validate_recipe

        args = parse_recipe(
            [
                "builder",
                "submit",
                "acme/web",
                "--ref",
                "main",
                "--manifest-path",
                "luma.prod.api.yml",
                "--region",
                "cn",
                "--node",
                "manager",
            ]
        )
        recipe = validate_recipe(make_recipe(args))
        self.assertEqual(recipe["method"], "builder-submit")
        fields = comparison_fields(recipe)
        self.assertEqual(fields["manifest_path"], "luma.prod.api.yml")
        self.assertEqual(fields["node"], "manager")

        # Detached, build-only, or custom --url invocations must not be persisted as shared recipes
        for rejected in (
            ["builder", "submit", "acme/web", "--no-deploy"],
            ["builder", "submit", "acme/web", "--detach"],
        ):
            with self.assertRaises(LumaError):
                parse_recipe(rejected)
        with self.assertRaises(LumaError):
            validate_recipe({"schemaVersion": 1, "method": "builder-submit", "argv": ["builder", "submit", "acme/web", "--url", "https://evil.example.com"]})

        # build local --registry-namespace must be preserved and compared
        local_a = make_recipe(parse_recipe(["build", "local", ".", "--registry-namespace", "luma"]))
        local_b = make_recipe(parse_recipe(["build", "local", ".", "--registry-namespace", "other"]))
        self.assertEqual(comparison_fields(local_a)["registry_namespace"], "luma")
        self.assertNotEqual(comparison_fields(local_a), comparison_fields(local_b))

    def test_control_registry_no_proxy_and_node_native_platform_summary(self) -> None:
        from luma.control.resources import EGRESS_NO_PROXY, _no_proxy_entries_for_registry
        from luma.control.server import _registered_nodes_summary, _state_private_registry_no_proxy

        self.assertIn(".aliyuncs.com", EGRESS_NO_PROXY)
        acr_entries = _no_proxy_entries_for_registry("crpi-test.cn-beijing.personal.cr.aliyuncs.com")
        self.assertIn(".aliyuncs.com", acr_entries)

        merged = _state_private_registry_no_proxy(
            {"registries": {"acr": {"server": "crpi-test.cn-beijing.personal.cr.aliyuncs.com"}, "corp": {"server": "reg.corp.example.com:5000"}}}
        )
        self.assertIn("reg.corp.example.com:5000", merged)
        self.assertIn(".aliyuncs.com", merged)

        summary = _registered_nodes_summary(
            {
                "arm-edge": {
                    "region": "hk",
                    "status": "labeled",
                    "agent": {"os": "darwin", "arch": "arm64", "lastSeen": 1},
                }
            }
        )
        self.assertEqual(summary[0]["agentArch"], "arm64")
        self.assertEqual(summary[0]["nativePlatform"], "linux/arm64")


if __name__ == "__main__":
    unittest.main()
