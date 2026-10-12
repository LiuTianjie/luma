from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from luma.agent import TaskCanceled, _docker_buildx_build, build_image, node_agent_capabilities
from luma.errors import LumaError
from luma.local_build import build_and_push_local_source
from luma.service import _load_service_build, build_target


class RepositoryBuildInputsTests(unittest.TestCase):
    def write_project(self, root: Path, *, compose: bool, extra_sources: bool = True, target: object = "question-video") -> None:
        root.mkdir(parents=True, exist_ok=True)
        spec = {"context": ".", "dockerfile": "Dockerfile"}
        if target is not None:
            spec["target"] = target
        if extra_sources:
            spec["x-luma-git-contexts"] = {"teachvideo": {"lock": "deploy/teachvideo.lock.json"}}
        (root / "Dockerfile").write_text("FROM scratch AS question-video\nFROM scratch AS background\n", encoding="utf-8")
        if compose:
            (root / "luma.compose.yml").write_text("name: app\ncompose: compose.yml\nregion: home\n", encoding="utf-8")
            (root / "compose.yml").write_text(yaml.safe_dump({"services": {"worker": {"build": spec}}}), encoding="utf-8")
        else:
            (root / ".luma.yml").write_text(yaml.safe_dump({"name": "app", "region": "home", "build": spec}), encoding="utf-8")

    def payload(self) -> dict[str, object]:
        return {
            "repoUrl": "https://git.example.com/acme/app.git",
            "registryHost": "100.64.0.70:5000",
            "pushHost": "100.64.0.70:5000",
            "repo": "acme/app",
            "gitToken": "private-git-token",
            "gitUsername": "builder-user",
            "targetPlatformsByRegion": {"home": ["linux/arm64"]},
        }

    def test_remote_native_and_compose_use_prepared_directories_and_target(self) -> None:
        for compose in (False, True):
            with self.subTest(compose=compose):
                directories: list[Path] = []

                def clone(_url, dest, **_kwargs):
                    self.write_project(dest, compose=compose)

                def prepare(root, spec, **kwargs):
                    self.assertEqual(spec["target"], "question-video")
                    self.assertEqual(kwargs["repository_url"], self.payload()["repoUrl"])
                    self.assertEqual(kwargs["token"], "private-git-token")
                    self.assertEqual(kwargs["username"], "builder-user")
                    workspace = kwargs["workspace"]
                    self.assertNotIn(root, workspace.parents)
                    prepared = workspace / "contexts" / "teachvideo"
                    prepared.mkdir(parents=True)
                    directories.append(prepared)
                    kwargs["progress"]("Prepared pinned Git context teachvideo")
                    return {"teachvideo": SimpleNamespace(path=prepared)}

                commands: list[list[str]] = []

                def run(command, **kwargs):
                    commands.append(command)
                    context = command[command.index("--build-context") + 1]
                    self.assertEqual(context, f"teachvideo={directories[0]}")
                    self.assertTrue(directories[0].is_dir())
                    self.assertNotIn("private-git-token", str(command) + str(kwargs))
                    self.assertNotIn("builder-user", str(command) + str(kwargs))
                    return SimpleNamespace(code=0, output="built")

                events: list[dict[str, object]] = []
                with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
                    "luma.agent._docker_binary", return_value="docker"
                ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
                    "luma.agent._ensure_buildx_builder", return_value="luma-builder"
                ), patch("luma.build_sources.prepare_git_contexts", side_effect=prepare), patch(
                    "luma.agent._run_process_streaming", side_effect=run
                ):
                    result = build_image(self.payload(), progress=events.append)

                self.assertEqual(len(commands), 1)
                command = commands[0]
                self.assertEqual(command[command.index("--target") + 1], "question-video")
                self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")
                self.assertEqual(result["buildCapabilities"], ["repository-build-inputs-v1"])
                self.assertFalse(directories[0].exists())
                self.assertNotIn("private-git-token", str(result) + str(events))
                if compose:
                    self.assertNotIn("build", yaml.safe_load(result["composeContent"])["services"]["worker"])

    def test_source_failure_and_cancellation_never_invoke_buildkit(self) -> None:
        from luma.build_sources import GitContextCanceled

        for compose in (False, True):
            for error in (LumaError("archive checksum mismatch"), GitContextCanceled("canceled")):
                with self.subTest(compose=compose, error=error):
                    def clone(_url, dest, **_kwargs):
                        self.write_project(dest, compose=compose)

                    with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
                        "luma.agent._docker_binary", return_value="docker"
                    ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
                        "luma.agent._ensure_buildx_builder", return_value="luma-builder"
                    ), patch("luma.build_sources.prepare_git_contexts", side_effect=error), patch(
                        "luma.agent._docker_buildx_build"
                    ) as build:
                        expected = TaskCanceled if isinstance(error, GitContextCanceled) else LumaError
                        with self.assertRaises(expected):
                            build_image(self.payload(), cancel_event=threading.Event())
                    build.assert_not_called()

    def test_external_manifest_build_inputs_are_used_when_repository_has_no_manifest(self) -> None:
        prepared_paths: list[Path] = []
        spec = {
            "context": "app",
            "dockerfile": "app/Dockerfile.worker",
            "target": "question-video",
            "repo": "acme/video-worker",
            "x-luma-git-contexts": {"teachvideo": {"lock": "deploy/teachvideo.lock.json"}},
        }

        def clone(_url, dest, **_kwargs):
            (dest / "app").mkdir(parents=True)
            (dest / "app/Dockerfile.worker").write_text("FROM scratch AS question-video\n", encoding="utf-8")

        def prepare(root, received, **kwargs):
            self.assertEqual(received, spec)
            prepared = kwargs["workspace"] / "contexts/teachvideo"
            prepared.mkdir(parents=True)
            prepared_paths.append(prepared)
            self.assertNotIn(root, prepared.parents)
            return {"teachvideo": SimpleNamespace(path=prepared)}

        payload = {
            **self.payload(),
            "fallbackBuild": spec,
            "fallbackPlacement": {"region": "home", "node": "video-arm"},
            "targetPlatformsByNode": {"video-arm": "linux/arm64"},
        }
        with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.build_sources.prepare_git_contexts", side_effect=prepare) as preparation, patch(
            "luma.agent._run_process_streaming", return_value=SimpleNamespace(code=0, output="built")
        ) as run:
            result = build_image(payload)

        preparation.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--target") + 1], "question-video")
        self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")
        self.assertEqual(command[command.index("--build-context") + 1], f"teachvideo={prepared_paths[0]}")
        self.assertTrue(command[-1].endswith("/src/app"))
        self.assertTrue(command[command.index("-f") + 1].endswith("/src/app/Dockerfile.worker"))
        self.assertIn("/acme/video-worker:", result["image"])
        self.assertEqual(result["manifest"], "")
        self.assertEqual(result["buildCapabilities"], ["repository-build-inputs-v1"])
        self.assertIs(result["fallbackManifestUsed"], True)
        self.assertFalse(prepared_paths[0].exists())

    def test_repository_manifest_owns_complete_build_inputs_over_external_fallback(self) -> None:
        for compose in (False, True):
            with self.subTest(compose=compose):
                def clone(_url, dest, **_kwargs):
                    self.write_project(dest, compose=compose, extra_sources=False, target="background")

                payload = {
                    **self.payload(),
                    "fallbackBuild": {
                        "context": "missing-external-context",
                        "dockerfile": "missing-external-Dockerfile",
                        "target": "question-video",
                        "x-luma-git-contexts": {"teachvideo": {"lock": "missing-lock.json"}},
                    },
                    "fallbackPlacement": {"node": "missing-external-node", "region": "missing-region"},
                }
                with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
                    "luma.agent._docker_binary", return_value="docker"
                ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
                    "luma.agent._ensure_buildx_builder", return_value="luma-builder"
                ), patch("luma.build_sources.prepare_git_contexts") as preparation, patch(
                    "luma.agent._run_process_streaming", return_value=SimpleNamespace(code=0, output="built")
                ) as run:
                    result = build_image(payload)
                preparation.assert_not_called()
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--target") + 1], "background")
                self.assertNotIn("--build-context", command)
                self.assertTrue(command[-1].endswith("/src"))
                self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")
                self.assertTrue(result["manifest"])
                self.assertNotIn("fallbackManifestUsed", result)

    def test_external_fallback_rejects_invalid_build_inputs_instead_of_default_stage(self) -> None:
        for fallback in (
            {"fallbackBuild": "question-video"},
            {"fallbackBuild": {"target": "${TARGET}"}},
            {"fallbackBuild": {"environment": {"RUNTIME_SECRET": "must-not-enter-build"}}},
            {"fallbackPlacement": {"region": "home", "environment": {}}},
        ):
            with self.subTest(fallback=fallback):
                def clone(_url, dest, **_kwargs):
                    dest.mkdir(parents=True)
                    (dest / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

                with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
                    "luma.agent._docker_binary", return_value="docker"
                ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
                    "luma.agent._docker_buildx_build"
                ) as build:
                    with self.assertRaises(LumaError):
                        build_image({**self.payload(), **fallback})
                build.assert_not_called()

    def test_local_source_contract_is_explicitly_rejected_before_build(self) -> None:
        for compose in (False, True):
            with self.subTest(compose=compose), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_project(root, compose=compose)
                with patch("luma.local_build._docker_binary", return_value="docker"), patch(
                    "luma.local_build._docker_buildx_available", return_value=True
                ), patch("luma.local_build._expose_local_docker_runtime"), patch(
                    "luma.agent._ensure_buildx_builder"
                ) as compose_builder, patch("luma.local_build._ensure_buildx_builder") as native_builder, patch(
                    "luma.agent._docker_buildx_build"
                ) as compose_build, patch("luma.local_build._docker_buildx_build") as native_build:
                    with self.assertRaisesRegex(LumaError, "use luma import"):
                        build_and_push_local_source(root, registry_host="100.64.0.70:5000", repository="acme/app", tag="revision")
                for operation in (compose_builder, native_builder, compose_build, native_build):
                    operation.assert_not_called()

    def test_local_target_and_ordinary_default_stage(self) -> None:
        for compose in (False, True):
            for target in ("question-video", None):
                with self.subTest(compose=compose, target=target), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.write_project(root, compose=compose, extra_sources=False, target=target)
                    with patch("luma.local_build._docker_binary", return_value="docker"), patch(
                        "luma.local_build._docker_buildx_available", return_value=True
                    ), patch("luma.local_build._expose_local_docker_runtime"), patch(
                        "luma.agent._ensure_buildx_builder", return_value="luma-builder"
                    ), patch("luma.local_build._ensure_buildx_builder", return_value="luma-builder"), patch(
                        "luma.agent._run_process_streaming", return_value=SimpleNamespace(code=0, output="built")
                    ) as run:
                        build_and_push_local_source(root, registry_host="100.64.0.70:5000", repository="acme/app", tag="revision")
                    command = run.call_args.args[0]
                    if target:
                        self.assertEqual(command[command.index("--target") + 1], target)
                    else:
                        self.assertNotIn("--target", command)
                    self.assertNotIn("--build-context", command)

    def test_target_validation_and_native_schema(self) -> None:
        self.assertEqual(build_target({}), "")
        for value in (None, "", True, "--other", "${TARGET}", " stage", "a\nb", [], "a/b"):
            with self.subTest(value=value), self.assertRaisesRegex(LumaError, "Dockerfile stage"):
                build_target({"target": value})
        spec = _load_service_build({"target": "question-video", "x-luma-git-contexts": {"teachvideo": {"lock": "deploy/teachvideo.lock.json"}}}, "app")
        self.assertEqual(spec.target, "question-video")
        self.assertIn("x-luma-git-contexts", spec.raw)

    def test_agent_advertises_build_input_capability_only_with_buildx(self) -> None:
        for present in (True, False):
            with self.subTest(present=present), patch("luma.agent._docker_buildx_available", return_value=present):
                capabilities = node_agent_capabilities("linux")
                self.assertEqual("repository-build-inputs-v1" in capabilities, present)

    def test_cancellation_after_source_preparation_does_not_start_docker(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with patch("luma.agent._run_process_streaming") as run, self.assertRaises(TaskCanceled):
            _docker_buildx_build(
                docker="docker", builder="luma-builder", docker_config=Path("/tmp/auth"),
                push_host="100.64.0.70:5000", registry_host="100.64.0.70:5000",
                repo="acme/app", sha="abc123", context_dir=Path("/tmp/src"),
                dockerfile_path=Path("/tmp/src/Dockerfile"), platform="linux/arm64",
                proxy="", build_timeout=60, cancel_event=cancel_event,
            )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
