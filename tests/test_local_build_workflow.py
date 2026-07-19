import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from luma.control.server import (
    _create_build_run,
    _resolve_target_build_platform,
    handle_local_build_complete,
    handle_local_build_base_images,
    handle_local_build_fail,
    handle_local_build_prepare,
    create_app,
)
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.cli import build_parser
from luma.agent import _deployment_target_build_platform
from luma.local_build import (
    _expose_local_docker_runtime,
    build_and_push_local_source,
    _dockerfile_base_images,
    local_deployment_target,
)


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
        state["nodes"] = {
            "cn-amd64": {
                "name": "cn-amd64",
                "region": "cn",
                "nomadStatus": "ready",
                "agent": {"status": "ready", "os": "linux", "arch": "amd64"},
            }
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
            self.token, {"repoUrl": "https://github.com/Acme/App.git", "targetRegion": "cn"}
        )
        with self.assertRaisesRegex(LumaError, "already has an active build"):
            handle_local_build_prepare(
                self.token, {"repoUrl": "git@github.com:acme/app.git", "targetRegion": "cn"}
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
            self.token, {"repoUrl": "https://github.com/acme/app", "targetRegion": "cn"}
        )
        self.assertNotEqual(second["run"]["id"], build_id)
        self.assertEqual(second["upload"]["repository"], "acme/app")

    def test_cli_exposes_local_build_as_a_build_subcommand(self):
        args = build_parser().parse_args(
            [
                "build", "local", ".",
                "--repo-url", "https://github.com/acme/app.git",
                "--builder", "desktop-linux",
                "--proxy", "http://host.docker.internal:7890",
            ]
        )
        self.assertEqual(args.command, "build")
        self.assertEqual(args.build_command, "local")
        self.assertEqual(args.path, Path("."))
        self.assertEqual(args.builder, "desktop-linux")
        self.assertEqual(args.proxy, "http://host.docker.internal:7890")

    def test_dockerfile_base_images_resolve_global_args_and_skip_stages(self):
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text(
            "ARG NODE_IMAGE=node:22-bookworm-slim\n"
            "ARG PYTHON_IMAGE=python:3.12-slim\n"
            "FROM ${NODE_IMAGE} AS frontend\n"
            "FROM ${PYTHON_IMAGE} AS runtime\n"
            "COPY --from=frontend /app /app\n",
            encoding="utf-8",
        )
        self.assertEqual(
            _dockerfile_base_images(dockerfile),
            ["node:22-bookworm-slim", "python:3.12-slim"],
        )

    def test_local_base_images_are_cached_by_builder_registry(self):
        prepared = handle_local_build_prepare(
            self.token,
            {"repoUrl": "https://github.com/acme/app.git", "targetRegion": "cn"},
        )
        with patch(
            "luma.control.server._cache_runtime_image_on_builder",
            side_effect=[
                {"deployed": "100.66.177.70:5000/luma-cache/node@sha256:" + "a" * 64},
                {"deployed": "100.66.177.70:5000/luma-cache/python@sha256:" + "b" * 64},
            ],
        ) as cache:
            result = handle_local_build_base_images(
                self.token,
                prepared["run"]["id"],
                {
                    "images": ["node:22-bookworm-slim", "python:3.12-slim"],
                    "platform": "linux/amd64",
                },
            )
        self.assertEqual(cache.call_count, 2)
        self.assertEqual(set(result["images"]), {"node:22-bookworm-slim", "python:3.12-slim"})

    def test_local_docker_config_keeps_buildx_plugin_and_context_visible(self):
        user_home = self.root / "home"
        plugin = user_home / ".docker" / "cli-plugins" / "docker-buildx"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("buildx", encoding="utf-8")
        context = user_home / ".docker" / "contexts" / "meta" / "context" / "meta.json"
        context.parent.mkdir(parents=True)
        context.write_text('{"Name":"desktop"}', encoding="utf-8")
        docker_config = self.root / "docker-config"
        docker_config.mkdir()
        with patch("luma.local_build.Path.home", return_value=user_home):
            _expose_local_docker_runtime(docker_config)
        exposed = docker_config / "cli-plugins" / "docker-buildx"
        self.assertTrue(exposed.is_file())
        self.assertEqual(exposed.read_text(encoding="utf-8"), "buildx")
        exposed_context = docker_config / "contexts" / "meta" / "context" / "meta.json"
        self.assertEqual(exposed_context.read_text(encoding="utf-8"), '{"Name":"desktop"}')

    def test_asgi_exposes_local_build_prepare_route(self):
        with TestClient(create_app()) as client:
            response = client.post(
                "/v1/builds/local/prepare",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"repoUrl": "https://github.com/acme/app.git", "targetRegion": "cn"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["upload"]["repository"], "acme/app")
        self.assertEqual(response.json()["upload"]["platform"], "linux/amd64")

    def test_target_architecture_uses_node_and_builds_mixed_region_as_multi_platform(self):
        state = load_state()
        state["nodes"]["home-arm"] = {
            "name": "home-arm",
            "region": "home",
            "nomadStatus": "ready",
            "agent": {"status": "ready", "os": "darwin", "arch": "arm64"},
        }
        state["nodes"]["home-amd"] = {
            "name": "home-amd",
            "region": "home",
            "nomadStatus": "ready",
            "agent": {"status": "ready", "os": "linux", "arch": "x86_64"},
        }
        save_state(state)
        self.assertEqual(
            _resolve_target_build_platform(
                state, target_nodes=["home-arm"], target_regions=[], requested=""
            ),
            "linux/arm64",
        )
        self.assertEqual(
            _resolve_target_build_platform(
                state, target_nodes=[], target_regions=["home"], requested=""
            ),
            "linux/amd64,linux/arm64",
        )
        with self.assertRaisesRegex(LumaError, "does not cover"):
            _resolve_target_build_platform(
                state,
                target_nodes=[],
                target_regions=["home"],
                requested="linux/amd64",
            )

    def test_builder_resolves_manifest_target_from_control_inventory(self):
        payload = {
            "targetPlatformsByNode": {"m4": "linux/arm64"},
            "targetPlatformsByRegion": {
                "home": ["linux/amd64", "linux/arm64"]
            },
        }
        self.assertEqual(
            _deployment_target_build_platform(payload, node="m4", region="home"),
            "linux/arm64",
        )
        self.assertEqual(
            _deployment_target_build_platform(payload, region="home"),
            "linux/amd64,linux/arm64",
        )

    def test_local_compose_target_collects_service_architecture_scopes(self):
        project = self.root / "targets"
        project.mkdir()
        (project / "luma.compose.yml").write_text(
            "name: app\ncompose: docker-compose.yml\nregion: home\nservices:\n"
            "  web:\n    node: m4\n  worker:\n    region: cn\n",
            encoding="utf-8",
        )
        (project / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: app\n  worker:\n    image: worker\n",
            encoding="utf-8",
        )
        self.assertEqual(
            local_deployment_target(project),
            {"targetNodes": ["m4"], "targetRegions": ["cn"]},
        )

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
        self.assertEqual(build.call_args.kwargs["buildx_builder"], "")

    def test_local_single_platform_compose_can_reuse_existing_builder(self):
        project = self.root / "single-platform"
        project.mkdir()
        (project / "luma.compose.yml").write_text(
            "name: app\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web: {}\n",
            encoding="utf-8",
        )
        (project / "docker-compose.yml").write_text(
            "services:\n  web:\n    build: .\n", encoding="utf-8"
        )
        with patch("luma.local_build._docker_binary", return_value="docker"), patch(
            "luma.local_build._docker_buildx_available", return_value=True
        ), patch(
            "luma.local_build._build_compose_images", return_value={"kind": "compose"}
        ) as build, patch(
            "luma.local_build._current_docker_context_builder", return_value="desktop-linux"
        ):
            build_and_push_local_source(
                project,
                registry_host="registry",
                repository="acme/app",
                tag="local-run",
                platform="linux/amd64",
                builder="desktop-linux",
                proxy="http://host.docker.internal:7890",
            )
        self.assertEqual(build.call_args.kwargs["buildx_builder"], "desktop-linux")
        self.assertEqual(
            build.call_args.kwargs["proxy"], "http://host.docker.internal:7890"
        )

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
            self.token, {"repoUrl": "https://github.com/acme/app.git", "targetRegion": "cn"}
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
