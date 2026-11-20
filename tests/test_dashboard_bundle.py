"""Tests for the hot-swappable Luma Dashboard static-bundle core addon."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from luma.cli.parser import build_parser
from luma.control import dashboard_bundle, server
from luma.control.state import init_state
from luma.errors import LumaError


def _make_bundle_bytes(files: dict[str, bytes], *, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=f"{prefix}{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class DashboardBundleUnitTests(unittest.TestCase):
    def test_install_rollback_and_prune_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "dashboard"
            v1 = _make_bundle_bytes(
                {
                    "index.html": b"<!doctype html><title>V1</title><script src=\"/dashboard/app-11111111.js\"></script>",
                    "app-11111111.js": b"console.log('v1');",
                }
            )
            res1 = dashboard_bundle.install_dashboard_bundle_bytes(v1, version="1.0.0", store=store)
            self.assertEqual(res1["release"]["version"], "1.0.0")
            self.assertEqual(res1["status"]["mode"], "bundle")
            self.assertEqual(res1["status"]["activeRelease"]["version"], "1.0.0")
            self.assertTrue((dashboard_bundle.active_bundle_root(store) / "app-11111111.js").is_file())

            # Wrapper directory in archive is automatically stripped
            v2 = _make_bundle_bytes(
                {
                    "index.html": b"<!doctype html><title>V2</title><script src=\"/dashboard/app-22222222.js\"></script>",
                    "app-22222222.js": b"console.log('v2');",
                },
                prefix="dist/",
            )
            res2 = dashboard_bundle.install_dashboard_bundle_bytes(v2, version="2.0.0", store=store)
            self.assertEqual(res2["release"]["version"], "2.0.0")
            self.assertTrue((dashboard_bundle.active_bundle_root(store) / "app-22222222.js").is_file())
            self.assertEqual(len(res2["status"]["releases"]), 2)

            # Default rollback switches back to v1
            rb = dashboard_bundle.rollback_dashboard_bundle(store=store)
            self.assertEqual(rb["release"]["id"], res1["release"]["id"])
            self.assertEqual(rb["status"]["activeRelease"]["version"], "1.0.0")
            self.assertTrue((dashboard_bundle.active_bundle_root(store) / "app-11111111.js").is_file())

            # Explicit rollback switches forward to v2 again
            rb2 = dashboard_bundle.rollback_dashboard_bundle(res2["release"]["id"], store=store)
            self.assertEqual(rb2["release"]["id"], res2["release"]["id"])

    def test_rejects_tar_slip_symlinks_missing_index_and_bad_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "dashboard"

            # Path traversal
            bad_traversal = _make_bundle_bytes({"../evil.txt": b"nope", "index.html": b"<html/>"})
            with self.assertRaises(LumaError):
                dashboard_bundle.install_dashboard_bundle_bytes(bad_traversal, store=store)

            # Symlink rejection
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                sym = tarfile.TarInfo(name="index.html")
                sym.type = tarfile.SYMTYPE
                sym.linkname = "/etc/passwd"
                tf.addfile(sym)
            with self.assertRaises(LumaError):
                dashboard_bundle.install_dashboard_bundle_bytes(buf.getvalue(), store=store)

            # Missing index.html
            no_index = _make_bundle_bytes({"app.js": b"console.log(1);"})
            with self.assertRaises(LumaError):
                dashboard_bundle.install_dashboard_bundle_bytes(no_index, store=store)

            # SHA-256 mismatch
            valid = _make_bundle_bytes({"index.html": b"<!doctype html><title>OK</title>"})
            with self.assertRaises(LumaError):
                dashboard_bundle.install_dashboard_bundle_bytes(valid, expected_sha256="0" * 64, store=store)

    def test_package_dashboard_directory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "built"
            src.mkdir()
            (src / "index.html").write_text("<!doctype html><title>Packaged</title>", encoding="utf-8")
            (src / "app-abcdef12.js").write_text("window.__LUMA__ = 1;", encoding="utf-8")
            (src / ".DS_Store").write_bytes(b"ignored")

            archive, manifest = dashboard_bundle.package_dashboard_directory(src, version="0.2.99", commit="deadbeef")
            self.assertEqual(manifest["version"], "0.2.99")
            self.assertEqual(manifest["fileCount"], 2)
            self.assertEqual(manifest["sha256"], hashlib.sha256(archive).hexdigest())

            store = Path(tmp) / "store"
            res = dashboard_bundle.install_dashboard_bundle_bytes(
                archive,
                version=manifest["version"],
                expected_sha256=manifest["sha256"],
                store=store,
            )
            self.assertEqual(res["release"]["fileCount"], 2)


class DashboardAddonApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.env_patch = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_DASHBOARD_AUTO_FETCH": "0",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
        self.token = self.state["deployToken"]
        self.client = TestClient(server.create_app())
        self.addCleanup(self.client.close)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_hot_swap_upload_serves_new_assets_without_restart_and_blocks_uninstall(self):
        # Catalog lists dashboard as required static-bundle addon
        addons_resp = self.client.get("/v1/addons", headers=self._headers())
        self.assertEqual(addons_resp.status_code, 200, addons_resp.text)
        by_id = {item["id"]: item for item in addons_resp.json()["addons"]}
        self.assertIn("dashboard", by_id)
        dash = by_id["dashboard"]
        self.assertEqual(dash["kind"], "static-bundle")
        self.assertTrue(dash["required"])
        self.assertEqual(dash["url"], "https://luma.example.com/dashboard/")

        # Upload v1 bundle -> immediately served by /dashboard/ without restarting Control
        v1 = _make_bundle_bytes(
            {
                "index.html": b"<!doctype html><title>HotSwap V1</title><script src=\"/dashboard/main-11111111.js\"></script>",
                "main-11111111.js": b"console.log('hotswap-v1');",
            }
        )
        up1 = self.client.post(
            "/v1/addons/dashboard/upload?version=1.0.0&source=test",
            headers={**self._headers(), "Content-Type": "application/gzip"},
            content=v1,
        )
        self.assertEqual(up1.status_code, 200, up1.text)
        self.assertEqual(up1.json()["bundleStatus"]["mode"], "bundle")

        page1 = self.client.get("/dashboard/")
        self.assertEqual(page1.status_code, 200)
        self.assertIn("HotSwap V1", page1.text)
        asset1 = self.client.get("/dashboard/main-11111111.js")
        self.assertEqual(asset1.status_code, 200)
        self.assertIn("hotswap-v1", asset1.text)
        # Release metadata file must never be exposed over HTTP
        self.assertEqual(self.client.get(f"/dashboard/{dashboard_bundle.RELEASE_META_FILE}").status_code, 404)

        # Upload v2 bundle -> immediately switches over
        v2 = _make_bundle_bytes(
            {
                "index.html": b"<!doctype html><title>HotSwap V2</title><script src=\"/dashboard/main-22222222.js\"></script>",
                "main-22222222.js": b"console.log('hotswap-v2');",
            }
        )
        up2 = self.client.post(
            "/v1/addons/dashboard/upload?version=2.0.0&source=test",
            headers={**self._headers(), "Content-Type": "application/gzip"},
            content=v2,
        )
        self.assertEqual(up2.status_code, 200, up2.text)
        self.assertIn("HotSwap V2", self.client.get("/dashboard/").text)

        # Rollback returns immediately to V1
        rb = self.client.post("/v1/addons/dashboard/rollback", headers=self._headers(), json={})
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertEqual(rb.json()["release"]["version"], "1.0.0")
        self.assertIn("HotSwap V1", self.client.get("/dashboard/").text)

        # Required core addon refuses uninstall
        uninst = self.client.post("/v1/addons/dashboard/uninstall", headers=self._headers(), json={})
        self.assertEqual(uninst.status_code, 400)
        self.assertIn("required core addon", uninst.json()["error"])

    def test_remote_apply_stream_and_check_update(self):
        bundle = _make_bundle_bytes(
            {
                "index.html": b"<!doctype html><title>Remote Bundle 3.0</title>",
                "app-33333333.js": b"console.log('remote-3');",
            }
        )
        digest = hashlib.sha256(bundle).hexdigest()
        manifest = json.dumps(
            {
                "schemaVersion": "luma.dashboard-bundle/v1",
                "version": "3.0.0",
                "sha256": digest,
                "commit": "abc12345",
                "builtAt": 1795000000,
            }
        ).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/manifest.json":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(manifest)
                elif self.path == "/luma-dashboard.tar.gz":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.end_headers()
                    self.wfile.write(bundle)
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(timeout=2)))

        base = f"http://127.0.0.1:{srv.server_port}"
        cfg = self.client.post(
            "/v1/addons/dashboard/configure",
            headers=self._headers(),
            json={"config": {"bundleUrl": f"{base}/luma-dashboard.tar.gz", "manifestUrl": f"{base}/manifest.json"}},
        )
        self.assertEqual(cfg.status_code, 200, cfg.text)

        check_before = self.client.get("/v1/addons/dashboard/check-update", headers=self._headers())
        self.assertEqual(check_before.status_code, 200, check_before.text)
        self.assertTrue(check_before.json()["updateAvailable"])

        stream_resp = self.client.post("/v1/addons/dashboard/apply/stream", headers=self._headers(), json={})
        self.assertEqual(stream_resp.status_code, 200, stream_resp.text)
        events = [json.loads(line) for line in stream_resp.text.splitlines() if line.strip()]
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual(events[-1]["result"]["release"]["version"], "3.0.0")
        self.assertIn("Remote Bundle 3.0", self.client.get("/dashboard/").text)

        check_after = self.client.get("/v1/addons/dashboard/check-update", headers=self._headers())
        self.assertEqual(check_after.status_code, 200)
        self.assertFalse(check_after.json()["updateAvailable"])

    def test_bootstrap_page_served_when_no_bundle_and_no_embedded_assets(self):
        with patch.object(dashboard_bundle, "embedded_dashboard_root", return_value=None):
            resp = self.client.get("/dashboard/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Luma Dashboard", resp.text)
            self.assertIn("luma addon install dashboard", resp.text)
            self.assertEqual(self.client.get("/dashboard/missing.js").status_code, 404)

    def test_cli_parser_supports_dashboard_bundle_and_rollback(self):
        parser = build_parser()
        inst = parser.parse_args(["addon", "install", "dashboard", "--bundle", "dist.tar.gz", "--version", "0.3.0"])
        self.assertEqual(inst.addon_command, "install")
        self.assertEqual(inst.addon_id, "dashboard")
        self.assertEqual(str(inst.bundle), "dist.tar.gz")
        self.assertEqual(inst.version, "0.3.0")

        rb = parser.parse_args(["addon", "rollback", "dashboard", "--release", "rel-1"])
        self.assertEqual(rb.addon_command, "rollback")
        self.assertEqual(rb.addon_id, "dashboard")
        self.assertEqual(rb.release, "rel-1")

    def test_cli_stream_addon_apply_uses_control_client_stream_post(self):
        from luma.cli import addons as cli_addons

        seen = []

        class FakeClient:
            def stream(self, method, path, body, *, timeout=30):
                seen.append((method, path, body, timeout))
                yield {"status": "ok", "name": "Verify and activate bundle", "message": "done"}
                yield {"status": "done", "result": {"release": {"version": "0.2.93"}}}

        result = cli_addons._stream_addon_apply(
            FakeClient(),
            "dashboard",
            {"origin": "cli"},
            timeout=120,
            quiet=True,
        )
        self.assertEqual(result, {"release": {"version": "0.2.93"}})
        self.assertEqual(seen, [("POST", "/v1/addons/dashboard/apply/stream", {"origin": "cli"}, 120)])
