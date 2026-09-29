"""User-facing behaviour of the CLI surface: help, errors, init and .env handling."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from luma import __version__
from luma.cli import build_parser, main
from luma.deploy_workflow import parse_recipe, replay_argv
from luma.service import load_service

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], *, keep_environment: bool = False) -> tuple[int, str, str]:
    """Run the CLI; unless asked otherwise, never read or leak the real user's settings."""
    out, err = io.StringIO(), io.StringIO()
    isolation = contextlib.nullcontext() if keep_environment else patch.dict(
        os.environ, {"LUMA_USER_CONFIG": str(ROOT / "tests" / "missing-user-config.json")}
    )
    with isolation, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def public_parsers(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            summaries = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                if name in summaries:
                    yield path + (name,), summaries[name], child
                    yield from public_parsers(child, path + (name,))


class CliSurfaceTests(unittest.TestCase):
    def test_every_public_command_has_a_summary_and_documented_options(self):
        commands = list(public_parsers(build_parser()))
        self.assertGreater(len(commands), 60)
        for path, summary, parser in commands:
            with self.subTest(command=" ".join(path)):
                self.assertTrue(summary)
                for action in parser._actions:
                    if action.help == argparse.SUPPRESS or isinstance(action, argparse._SubParsersAction):
                        continue
                    self.assertTrue(action.help, f"{' '.join(path)} {action.option_strings or action.dest} has no help")

    def test_internal_node_agent_command_is_hidden(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertNotIn("node-agent", out)
        self.assertIn("Cluster administration:", out)

    def test_no_arguments_prints_help(self):
        code, out, err = run([])
        self.assertEqual(code, 0)
        self.assertIn("Get started:", out)
        self.assertEqual(err, "")

    def test_version_flag(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"luma {__version__}")

    def test_mistyped_commands_suggest_the_closest_one(self):
        code, _, err = run(["deplyo", "app.yaml"])
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'deploy'?", err)
        code, _, err = run(["app", "lgos", "web"])
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'logs'?", err)

    def test_python_module_entry_point_used_by_node_agents(self):
        result = subprocess.run(
            [sys.executable, "-m", "luma.cli", "node-agent", "run", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--poll-interval", result.stdout)

    def test_ctrl_c_exits_130_without_traceback(self):
        with patch.dict("luma.cli.main.COMMANDS", {"validate": Mock(side_effect=KeyboardInterrupt)}):
            code, _, err = run(["validate", "app.yaml"])
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)

    def test_unexpected_errors_are_reported_without_traceback(self):
        with patch.dict("luma.cli.main.COMMANDS", {"validate": Mock(side_effect=RuntimeError("boom"))}), patch.dict(os.environ, {"LUMA_DEBUG": ""}):
            code, _, err = run(["validate", "app.yaml"])
        self.assertEqual(code, 1)
        self.assertIn("unexpected error: RuntimeError: boom", err)
        self.assertIn("LUMA_DEBUG=1", err)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.cwd)

    def test_non_interactive_init_writes_a_valid_manifest_with_limits_and_health(self):
        with patch("sys.stdin.isatty", return_value=False):
            code, out, _ = run(["--no-env", "init", "--name", "web", "--region", "global", "--port", "8080"])
        self.assertEqual(code, 0, out)
        service = load_service(Path("web.yaml"))
        self.assertEqual(service.exposure, "external-edge")
        self.assertEqual(service.domain, "web.example.com")
        self.assertEqual(service.resources["limits"]["memory"], "512M")
        self.assertTrue(service.healthcheck)
        self.assertIn("luma validate web.yaml", out)
        code, _, _ = run(["--no-env", "validate", "web.yaml"])
        self.assertEqual(code, 0)

    def test_init_refuses_to_overwrite_without_force(self):
        Path("web.yaml").write_text("keep: me\n")
        with patch("sys.stdin.isatty", return_value=False):
            code, _, err = run(["--no-env", "init", "--name", "web"])
            self.assertEqual(code, 1)
            self.assertIn("--force", err)
            self.assertEqual(Path("web.yaml").read_text(), "keep: me\n")
            code, _, _ = run(["--no-env", "init", "--name", "web", "--force"])
        self.assertEqual(code, 0)

    def test_internal_exposure_has_no_domain(self):
        with patch("sys.stdin.isatty", return_value=False):
            code, _, _ = run(["--no-env", "init", "--name", "worker", "--exposure", "none"])
        self.assertEqual(code, 0)
        self.assertIsNone(load_service(Path("worker.yaml")).domain)


class EnvironmentLoadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.cwd)
        self.env = patch.dict(os.environ, {"LUMA_USER_CONFIG": str(Path(self.directory.name) / "user.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("DATABASE_URL", "LUMA_DEPLOY_TOKEN", "LUMA_CONTROL_URL", "CLOUDFLARE_API_TOKEN"):
            os.environ.pop(name, None)
        Path("app.yaml").write_text("name: app\nimage: nginx:1.27\nregion: cn\nexposure: none\n")

    def test_application_env_file_cannot_break_commands_or_redirect_control(self):
        Path(".env").write_text(
            "DATABASE_URL=postgres://app\nLUMA_DEPLOY_TOKEN=other-cluster\nLUMA_CONTROL_URL=https://other\n"
            "MULTI LINE VALUE\nCLOUDFLARE_API_TOKEN=cf-token\n"
        )
        code, out, _ = run(["validate", "app.yaml"], keep_environment=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("DATABASE_URL", os.environ)
        self.assertNotIn("LUMA_DEPLOY_TOKEN", os.environ)
        self.assertNotIn("LUMA_CONTROL_URL", os.environ)
        self.assertEqual(os.environ.get("CLOUDFLARE_API_TOKEN"), "cf-token")

    def test_explicit_env_file_is_loaded_completely_and_must_exist(self):
        Path("ci.env").write_text("LUMA_CONTROL_URL=https://ci.example.com\n")
        code, _, _ = run(["--env-file", "ci.env", "validate", "app.yaml"], keep_environment=True)
        self.assertEqual(code, 0)
        self.assertEqual(os.environ.get("LUMA_CONTROL_URL"), "https://ci.example.com")
        code, _, err = run(["--env-file", "missing.env", "validate", "app.yaml"])
        self.assertEqual(code, 1)
        self.assertIn("env file not found", err)

    def test_secret_import_does_not_load_the_imported_file_into_the_cli_environment(self):
        Path("secrets.env").write_text("DATABASE_URL=postgres://app\n")
        args = build_parser().parse_args(["secret", "import", "secrets.env", "--scope", "app"])
        self.assertIsNone(args.env_file)
        self.assertEqual(args.secrets_file, Path("secrets.env"))


class RecordedWorkflowCompatibilityTests(unittest.TestCase):
    def test_recipes_recorded_by_older_clients_still_parse_and_replay(self):
        legacy = ["--env-file", ".env", "deploy", "app.yaml", "--engine", "nomad"]
        self.assertEqual(parse_recipe(legacy).command, "deploy")
        self.assertEqual(replay_argv(legacy), ["deploy", "app.yaml", "--engine", "nomad"])
        self.assertEqual(replay_argv(["--no-env", "deploy", "app.yaml"]), ["--no-env", "deploy", "app.yaml"])


if __name__ == "__main__":
    unittest.main()
