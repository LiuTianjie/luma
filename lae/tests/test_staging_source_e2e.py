from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT_PATH = SCRIPTS / "staging_source_e2e.py"
SPEC = importlib.util.spec_from_file_location("staging_source_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ZipFixtureTest(unittest.TestCase):
    def test_zip_fixture_has_root_entry_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "index.html").write_text("<h1>LAE</h1>", encoding="utf-8")
            (root / "assets" / "site.css").write_text("body{}", encoding="utf-8")

            content = MODULE._zip_fixture(root)
            with MODULE.zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertEqual(
                    sorted(archive.namelist()), ["assets/site.css", "index.html"]
                )

    def test_zip_fixture_requires_root_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MODULE.AcceptanceFailure):
                MODULE._zip_fixture(Path(directory))


class StaticTopologyTest(unittest.TestCase):
    def test_single_http_topology_returns_hostname(self) -> None:
        hostname = MODULE._assert_single_http(
            {
                "services": [{"key": "web", "role": "http"}],
                "routes": [{"serviceKey": "web", "hostname": "abc.itool.tech"}],
                "volumes": [],
            }
        )
        self.assertEqual(hostname, "abc.itool.tech")

    def test_rejects_extra_service(self) -> None:
        with self.assertRaises(MODULE.AcceptanceFailure):
            MODULE._assert_single_http(
                {
                    "services": [
                        {"key": "web", "role": "http"},
                        {"key": "worker", "role": "worker"},
                    ],
                    "routes": [{"hostname": "abc.itool.tech"}],
                    "volumes": [],
                }
            )


class WorkerRecoveryInjectionTest(unittest.TestCase):
    def test_restarts_only_worker_after_operation_is_running(self) -> None:
        class Client:
            def request(self, method, path, body=None, **kwargs):
                self.call = (method, path, body, kwargs)
                return SimpleNamespace(body={"status": "running"})

        client = Client()
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            MODULE._restart_worker_when_operation_runs(
                client,
                "op_test",
                stack="lae-platform-staging",
                deadline=MODULE.time.monotonic() + 2,
            )

        self.assertEqual(client.call[1], "/operations/op_test")
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "luma",
                "service",
                "restart",
                "lae-platform-staging",
                "--service",
                "worker",
                "--mode",
                "task",
                "--timeout",
                "120",
            ],
        )


if __name__ == "__main__":
    unittest.main()
