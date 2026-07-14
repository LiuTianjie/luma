from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
