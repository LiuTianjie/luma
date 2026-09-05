from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cli_reference", ROOT / "scripts/generate-cli-reference.py")
REFERENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFERENCE)


class SourceGateTests(unittest.TestCase):
    def test_pr_and_publish_workflows_use_same_source_gate(self):
        for name in ("ci.yml", "control-image.yml", "pypi.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
                validation_jobs = []
                for job in workflow["jobs"].values():
                    steps = job.get("steps", [])
                    if any("bash scripts/check-luma.sh" in step.get("run", "") for step in steps):
                        validation_jobs.append(job)
                self.assertEqual(len(validation_jobs), 1)
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("pull_request", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})

    def test_gate_builds_assets_before_python_tests_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            trace = directory / "trace"
            for command in ("python", "npm", "node", "git"):
                stub = directory / command
                stub.write_text(
                    '#!/bin/bash\n'
                    'echo "${0##*/} $*" >> "$GATE_TEST_TRACE"\n'
                    'if [[ "$1" == "-m" && "$2" == "unittest" ]]; then exit 7; fi\n'
                )
                stub.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/check-luma.sh")],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "GATE_TEST_TRACE": str(trace)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            invoked = trace.read_text()
            self.assertIn("generate-cli-reference.py --check", invoked)
            self.assertIn("-m unittest discover -s tests -p test_*.py", invoked)
            self.assertLess(invoked.index("npm run build:dashboard"), invoked.index("python -m unittest"))
            self.assertNotIn("node ", invoked)
            self.assertNotIn("git ", invoked)


class CliReferenceTests(unittest.TestCase):
    def test_reference_excludes_hidden_internal_commands(self):
        rendered = REFERENCE.render_reference()
        self.assertIn("## `luma doctor`", rendered)
        self.assertIn("## `luma service restart`", rendered)
        self.assertNotIn("node-agent", rendered)
        self.assertNotIn("==SUPPRESS==", rendered)

    def test_reference_is_independent_of_terminal_width(self):
        with patch.dict(os.environ, {"COLUMNS": "40"}):
            narrow = REFERENCE.render_reference()
        with patch.dict(os.environ, {"COLUMNS": "180"}):
            wide = REFERENCE.render_reference()
        self.assertEqual(narrow, wide)


if __name__ == "__main__":
    unittest.main()
