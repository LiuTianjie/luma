import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from luma.control.server import (
    _create_build_run,
    handle_local_build_complete,
    handle_local_build_fail,
    handle_local_build_prepare,
    create_app,
)
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.cli import build_parser
from luma.local_build import build_and_push_local_source


class LocalBuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_state = os.environ.get("LUMA_CONTROL_STATE_DIR")
        self.old_config = os.environ.get("LUMA_CONTROL_CONFIG")
        os.environ["LUMA_CONTROL_STATE_DIR"] = str(self.root / "state")
        os.environ["LUMA_CONTROL_CONFIG"] = str(self.root / "luma.yaml")
        (self.root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
        state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
        state["build"] = {
            "defaultNode": "builder",
            "registryHost": "100.66.177.70:5000",
            "pushHost": "localhost:5000",
        }
        save_state(state)
        self.token = state["deployToken"]

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("LUMA_CONTROL_STATE_DIR", None)
        else:
            os.environ["LUMA_CONTROL_STATE_DIR"] = self.old_state
        if self.old_config is None:
            os.environ.pop("LUMA_CONTROL_CONFIG", None)
        else:
            os.environ["LUMA_CONTROL_CONFIG"] = self.old_config
        self.temp.cleanup()

    def test_same_project_has_one_builder_or_local_build_lane(self):
        first = handle_local_build_prepare(
            self.token, {"repoUrl": "https://github.com/Acme/App.git"}
        )
        with self.assertRaisesRegex(LumaError, "already has an active build"):
            handle_local_build_prepare(
                self.token, {"repoUrl": "git@github.com:acme/app.git"}
            )
        with self.assertRaisesRegex(LumaError, "already has an active build"):
            _create_build_run(
                {"repoUrl": "https://github.com/acme/app.git"},
                source="https://github.com/acme/app.git",
                build_node="builder",
                project_key="acme/app",
            )

        build_id = first["run"]["id"]
        handle_local_build_fail(self.token, build_id, {"message": "stopped locally"})
        second = handle_local_build_prepare(
            self.token, {"repoUrl": "https://github.com/acme/app"}
        )
        self.assertNotEqual(second["run"]["id"], build_id)
        self.assertEqual(second["upload"]["repository"], "acme/app")

    def test_cli_exposes_local_build_as_a_build_subcommand(self):
        args = build_parser().parse_args(
            ["build", "local", ".", "--repo-url", "https://github.com/acme/app.git"]
        )
        self.assertEqual(args.command, "build")
        self.assertEqual(args.build_command, "local")
        self.assertEqual(args.path, Path("."))

    def test_asgi_exposes_local_build_prepare_route(self):
        with TestClient(create_app()) as client:
            response = client.post(
                "/v1/builds/local/prepare",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"repoUrl": "https://github.com/acme/app.git"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["upload"]["repository"], "acme/app")

    def test_local_compose_build_forces_the_reserved_project_namespace(self):
        project = self.root / "project"
        project.mkdir()
        (project / "luma.compose.yml").write_text(
            "name: app\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
            encoding="utf-8",
        )
        (project / "docker-compose.yml").write_text(
            "services:\n  web:\n    build: .\n",
            encoding="utf-8",
        )
        expected = {"kind": "compose", "image": "registry/acme/app:local-run"}
        with patch("luma.local_build._docker_binary", return_value="docker"), patch(
            "luma.local_build._docker_buildx_available", return_value=True
        ), patch(
            "luma.local_build._build_compose_images", return_value=expected
        ) as build:
            result = build_and_push_local_source(
                project,
                registry_host="registry",
                repository="acme/app",
                tag="local-run",
            )
        self.assertEqual(result, expected)
        self.assertFalse(build.call_args.kwargs["allow_repo_overrides"])

    def test_local_service_upload_deploys_under_reserved_project(self):
        prepared = handle_local_build_prepare(
            self.token,
            {
                "repoUrl": "https://github.com/acme/app.git",
                "region": "cn",
            },
        )
        build_id = prepared["run"]["id"]
        image = prepared["upload"]["image"]
        manifest = "name: app\nimage: placeholder\nregion: home\nexposure: none\n"

        with patch(
            "luma.control.server.handle_deployment",
            return_value={"service": "app", "steps": []},
        ) as deploy:
            result = handle_local_build_complete(
                self.token,
                build_id,
                {
                    "buildResult": {
                        "kind": "service",
                        "image": image,
                        "manifest": manifest,
                        "message": f"Built and pushed {image}",
                    }
                },
            )

        deployed = deploy.call_args.args[1]
        self.assertIn(f"image: {image}", deployed["manifest"])
        self.assertIn("region: cn", deployed["manifest"])
        self.assertEqual(deployed["gitSource"]["repoUrl"], "https://github.com/acme/app.git")
        self.assertEqual(result["buildRunId"], build_id)
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "succeeded")

    def test_local_upload_cannot_escape_reserved_repository_or_tag(self):
        prepared = handle_local_build_prepare(
            self.token, {"repoUrl": "https://github.com/acme/app.git"}
        )
        build_id = prepared["run"]["id"]
        with self.assertRaisesRegex(LumaError, "reserved project repository"):
            handle_local_build_complete(
                self.token,
                build_id,
                {
                    "buildResult": {
                        "kind": "service",
                        "image": "100.66.177.70:5000/other/app:bad",
                        "manifest": "name: app\nregion: cn\nexposure: none\n",
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
