from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.agent import (
    _run_process_streaming,
    _select_luma_compose_manifest,
    build_image,
    execute_agent_task,
)
from luma.cli import build_parser
from luma.control.client import ControlClient
from luma.control.server import create_app, handle_build_deploy
from luma.control.state import init_state, save_state
from luma.errors import LumaError
from luma.repo_paths import normalize_repo_relative_path
from starlette.testclient import TestClient


VALID_SIDECAR = """\
name: app-staging
compose: docker-compose.staging.yml
region: cn
volumes:
  data:
    storageClass: cn-nfs
    path: app/staging/data
services:
  web:
    exposure: none
"""

VALID_COMPOSE = """\
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - data:/data
volumes:
  data: {}
"""


class ImportComposeSidecarTests(unittest.TestCase):
    def test_build_image_dispatch_receives_agent_cancellation(self) -> None:
        cancel_event = threading.Event()
        captured: dict[str, object] = {}

        def fake_build(
            payload: dict[str, object],
            *,
            progress: object,
            cancel_event: threading.Event,
        ) -> dict[str, object]:
            captured.update(payload=payload, progress=progress, cancel_event=cancel_event)
            return {"message": "ok"}

        with patch("luma.agent.build_image", side_effect=fake_build):
            result = execute_agent_task(
                {"action": "build-image", "payload": {"repoUrl": "https://example.invalid/repo.git"}},
                cancel_event=cancel_event,
            )

        self.assertEqual(result, {"message": "ok"})
        self.assertIs(captured["cancel_event"], cancel_event)

    def test_streaming_build_process_is_killed_when_canceled(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.1, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            result = _run_process_streaming(
                ["python3", "-c", "import time; time.sleep(30)"],
                timeout=60,
                cancel_event=cancel_event,
            )
        finally:
            timer.cancel()

        self.assertEqual(result.code, 130)
        self.assertIn("process canceled", result.output)
        self.assertLess(time.monotonic() - started, 2)

    def test_production_only_runbooks_do_not_reference_removed_staging_sidecar(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deployment_readme = (
            root / "lae" / "deploy" / "luma" / "README.md"
        ).read_text(encoding="utf-8")
        runbook = (
            root / "docs" / "lae" / "11-deployment-and-upgrade.md"
        ).read_text(encoding="utf-8")
        removed_sidecar = "lae/deploy/luma/luma.compose.staging.itool.yml"
        self.assertFalse((root / removed_sidecar).exists())
        self.assertNotIn(removed_sidecar, deployment_readme)
        self.assertNotIn(removed_sidecar, runbook)
        self.assertIn("repository-compose-sidecar-v1", runbook)

    def test_cli_and_client_send_explicit_sidecar_path(self) -> None:
        args = build_parser().parse_args(
            [
                "import",
                "https://github.com/acme/app.git",
                "--compose-sidecar",
                "deploy/luma.compose.staging.yml",
            ]
        )
        self.assertEqual(args.compose_sidecar, "deploy/luma.compose.staging.yml")

        body = ControlClient._build_body(
            {
                "repo_url": "https://github.com/acme/app.git",
                "compose_sidecar": "deploy/luma.compose.staging.yml",
            }
        )
        self.assertEqual(
            body["composeSidecar"], "deploy/luma.compose.staging.yml"
        )
        with TestClient(create_app()) as client:
            self.assertIn(
                "repository-compose-sidecar-v1",
                client.get("/v1/health").json()["capabilities"],
            )

    def test_path_must_be_canonical_and_repository_relative(self) -> None:
        self.assertEqual(
            normalize_repo_relative_path(
                "deploy/luma.compose.staging.yml", label="composeSidecar"
            ),
            "deploy/luma.compose.staging.yml",
        )
        for value in (
            "",
            "/deploy/luma.compose.yml",
            "../luma.compose.yml",
            "deploy/../luma.compose.yml",
            "./luma.compose.yml",
            "deploy//luma.compose.yml",
            "deploy\\luma.compose.yml",
            " deploy/luma.compose.yml",
        ):
            with self.subTest(value=value), self.assertRaises(LumaError):
                normalize_repo_relative_path(value, label="composeSidecar")

    def test_client_fails_before_build_when_control_lacks_capability(self) -> None:
        client = ControlClient("https://luma.example.com", "token")
        with patch.object(
            client, "health", return_value={"capabilities": []}
        ), patch.object(client, "request") as request, self.assertRaisesRegex(
            LumaError, "update the manager"
        ):
            client.build_deploy(
                repo_url="https://github.com/acme/app.git",
                compose_sidecar="deploy/luma.compose.staging.yml",
            )
        request.assert_not_called()

    def test_selected_sidecar_is_structurally_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deploy = root / "deploy"
            deploy.mkdir()
            (deploy / "luma.compose.staging.yml").write_text(
                VALID_SIDECAR, encoding="utf-8"
            )
            (deploy / "docker-compose.staging.yml").write_text(
                VALID_COMPOSE, encoding="utf-8"
            )
            (deploy / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

            selected = _select_luma_compose_manifest(
                root, "deploy/luma.compose.staging.yml"
            )
            self.assertEqual(
                selected, (deploy / "luma.compose.staging.yml").resolve()
            )

            (deploy / "invalid.yml").write_text(
                "compose: docker-compose.staging.yml\nregion: cn\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "selected composeSidecar is invalid"):
                _select_luma_compose_manifest(root, "deploy/invalid.yml")

    def test_missing_or_symlink_escape_never_falls_back_to_auto_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / "luma.compose.yml").write_text(
                "name: production\ncompose: docker-compose.yml\nregion: cn\n",
                encoding="utf-8",
            )
            (root / "docker-compose.yml").write_text(
                "services:\n  web:\n    image: nginx\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(LumaError, "does not exist"):
                _select_luma_compose_manifest(root, "staging.luma.compose.yml")

            external = Path(outside) / "staging.yml"
            external.write_text(VALID_SIDECAR, encoding="utf-8")
            (root / "escape.yml").symlink_to(external)
            with self.assertRaisesRegex(LumaError, "escapes the repository"):
                _select_luma_compose_manifest(root, "escape.yml")

            external_compose = Path(outside) / "docker-compose.yml"
            external_compose.write_text(
                "services:\n  web:\n    image: nginx\n", encoding="utf-8"
            )
            (root / "compose-link.yml").symlink_to(external_compose)
            (root / "internal-sidecar.yml").write_text(
                "name: staging\ncompose: compose-link.yml\nregion: cn\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LumaError, "path escapes repository"):
                _select_luma_compose_manifest(root, "internal-sidecar.yml")

    def test_build_image_uses_selected_sidecar_instead_of_production(self) -> None:
        def fake_clone(_url: str, destination: Path, **_kwargs: object) -> None:
            deploy = destination / "deploy"
            deploy.mkdir(parents=True)
            (destination / "luma.compose.yml").write_text(
                "name: production\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (destination / "docker-compose.yml").write_text(
                VALID_COMPOSE, encoding="utf-8"
            )
            (destination / "Dockerfile").write_text(
                "FROM scratch\n", encoding="utf-8"
            )
            (deploy / "luma.compose.staging.yml").write_text(
                VALID_SIDECAR, encoding="utf-8"
            )
            (deploy / "docker-compose.staging.yml").write_text(
                VALID_COMPOSE, encoding="utf-8"
            )
            (deploy / "Dockerfile").write_text(
                "FROM scratch\n", encoding="utf-8"
            )

        with patch("luma.gitops.clone", side_effect=fake_clone), patch(
            "luma.gitops.head_commit", return_value="abc123"
        ), patch("luma.agent._docker_binary", return_value="docker"), patch(
            "luma.agent._docker_buildx_available", return_value=True
        ), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch(
            "luma.agent._docker_buildx_build",
            return_value="registry.internal/acme/app:abc123",
        ):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app.git",
                    "registryHost": "registry.internal",
                    "pushHost": "registry.internal",
                    "repo": "acme/app",
                    "composeSidecar": "deploy/luma.compose.staging.yml",
                }
            )

        self.assertEqual(result["kind"], "compose")
        self.assertEqual(
            result["composeSidecar"], "deploy/luma.compose.staging.yml"
        )
        self.assertIn("name: app-staging", result["manifest"])
        self.assertNotIn("name: production", result["manifest"])

    def test_control_passes_sidecar_to_builder_and_rejects_manifest_mix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                "LUMA_CONTROL_STATE_DIR": str(root / "state"),
                "LUMA_CONTROL_CONFIG": str(root / "luma.yaml"),
            }
            (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
            with patch.dict(os.environ, environment, clear=False):
                state = init_state(
                    domain="luma.example.com", cluster_id="luma-test", overwrite=True
                )
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {
                            "status": "ready",
                            "os": "linux",
                            "capabilities": ["docker-build"],
                        },
                    }
                }
                state["build"] = {
                    "defaultNode": "builder",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                }
                save_state(state)
                captured: dict[str, object] = {}

                def fake_task(
                    _state: dict[str, object],
                    _node: str,
                    _action: str,
                    payload: dict[str, object],
                    **_kwargs: object,
                ) -> dict[str, object]:
                    captured.update(payload)
                    return {
                        "kind": "compose",
                        "manifest": "name: app-staging\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                        "composeContent": "services:\n  web:\n    image: registry.internal/acme/app:abc123\n",
                        "images": {"web": "registry.internal/acme/app:abc123"},
                        "imageAliases": {},
                        "image": "registry.internal/acme/app:abc123",
                        "composeSidecar": "deploy/luma.compose.staging.yml",
                    }

                with patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=fake_task,
                ), patch(
                    "luma.control.server.handle_compose_deployment",
                    return_value={"deployment": "app-staging", "steps": []},
                ):
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app.git",
                            "composeSidecar": "deploy/luma.compose.staging.yml",
                        },
                    )

                self.assertEqual(
                    captured["composeSidecar"],
                    "deploy/luma.compose.staging.yml",
                )
                self.assertEqual(
                    result["composeSidecar"],
                    "deploy/luma.compose.staging.yml",
                )

                stale_builder_result = {
                    "kind": "compose",
                    "manifest": "name: production\ncompose: docker-compose.yml\nregion: cn\n",
                    "composeContent": "services:\n  web:\n    image: registry.internal/acme/app:abc123\n",
                    "images": {"web": "registry.internal/acme/app:abc123"},
                    "imageAliases": {},
                    "image": "registry.internal/acme/app:abc123",
                }
                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value=stale_builder_result,
                ), self.assertRaisesRegex(LumaError, "update the builder node agent"):
                    handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app.git",
                            "composeSidecar": "deploy/luma.compose.staging.yml",
                        },
                    )

                with self.assertRaisesRegex(
                    LumaError, "cannot be combined with manifest"
                ):
                    handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app.git",
                            "composeSidecar": "deploy/luma.compose.staging.yml",
                            "manifest": "name: not-allowed\n",
                        },
                    )


if __name__ == "__main__":
    unittest.main()
