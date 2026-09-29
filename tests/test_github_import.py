"""Repository import, git providers and build execution."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

from luma.config import LumaConfig
from luma.control.server import handle_git_provider_list, handle_git_provider_refs, handle_git_provider_remove, handle_git_provider_repositories, handle_git_provider_set
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from tests.support import _restore_env, _JsonResponse, _set_env


class GithubImportTests(unittest.TestCase):
    def test_git_provider_credentials_support_multiple_accounts_without_returning_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                saved_personal = handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )
                saved_work = handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "work", "username": "octo-work", "token": "ghp_work"},
                )
                saved_gitea = handle_git_provider_set(
                    state["deployToken"],
                    {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    },
                )

                self.assertEqual(saved_personal["id"], "github:personal")
                self.assertEqual(saved_work["id"], "github:work")
                self.assertEqual(saved_gitea["id"], "gitea:lin")
                listed = handle_git_provider_list(state["deployToken"])
                serialized = json.dumps(listed)
                self.assertIn("github:personal", serialized)
                self.assertIn("github:work", serialized)
                self.assertIn("gitea:lin", serialized)
                self.assertIn("octo-work", serialized)
                self.assertNotIn("ghp_personal", serialized)
                self.assertNotIn("ghp_work", serialized)
                self.assertNotIn("gitea_secret", serialized)
                self.assertEqual(load_state()["gitProviders"]["github:work"]["token"], "ghp_work")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_provider_remove_deletes_only_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(state["deployToken"], {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"})
                handle_git_provider_set(state["deployToken"], {"type": "github", "account": "work", "username": "octo-work", "token": "ghp_work"})

                removed = handle_git_provider_remove(state["deployToken"], {"id": "github:personal"})

                self.assertTrue(removed["removed"])
                providers = load_state().get("gitProviders", {})
                self.assertNotIn("github:personal", providers)
                self.assertIn("github:work", providers)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_gitea_provider_repository_list_uses_selected_account_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    },
                )

                def fake_urlopen(request, **_kwargs):
                    self.assertEqual(request.full_url, "https://gcode.gaojiua.com:3000/api/v1/user/repos?limit=100&page=1")
                    self.assertEqual(request.headers.get("Authorization"), "token gitea_secret")
                    return _JsonResponse(
                        [
                            {
                                "full_name": "gaojiuatech/price",
                                "clone_url": "https://gcode.gaojiua.com:3000/gaojiuatech/price.git",
                                "default_branch": "main",
                                "private": True,
                            }
                        ]
                    )

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_repositories(state["deployToken"], "gitea:lin")

                self.assertEqual(
                    result["repositories"],
                    [
                        {
                            "fullName": "gaojiuatech/price",
                            "cloneUrl": "https://gcode.gaojiua.com:3000/gaojiuatech/price.git",
                            "defaultBranch": "main",
                            "private": True,
                        }
                    ],
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_github_provider_refs_include_branches_and_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )

                def fake_urlopen(request, **_kwargs):
                    self.assertEqual(request.headers.get("Authorization"), "Bearer ghp_personal")
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=1":
                        return _JsonResponse([{"name": "main"}])
                    if request.full_url == "https://api.github.com/repos/acme/app/tags?per_page=100&page=1":
                        return _JsonResponse([{"name": "v1.2.3"}])
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_refs(state["deployToken"], "github:personal", "acme/app")

                self.assertEqual(result["refs"], [{"name": "main", "type": "branch"}, {"name": "v1.2.3", "type": "tag"}])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_clone_builds_shallow_command_with_ref(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref="main")
        self.assertEqual(captured["cmd"][:4], ["git", "clone", "--depth", "1"])
        self.assertIn("--branch", captured["cmd"])
        self.assertIn("main", captured["cmd"])

    def test_clone_fetches_full_commit_without_treating_it_as_a_branch(self):
        from luma import gitops

        captured = []
        commit = "45b76e581e894f5d66ef142944c781073c35f5ef"

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref=commit)

        self.assertEqual([command[1] for command in captured], ["init", "-C", "-C", "-C"])
        self.assertEqual(captured[2][-2:], ["origin", commit])
        self.assertEqual(captured[3][-2:], ["--detach", "FETCH_HEAD"])
        self.assertNotIn("--branch", [item for command in captured for item in command])

    def test_clone_accepts_uppercase_full_commit(self):
        from luma import gitops

        captured = []
        commit = "45B76E581E894F5D66EF142944C781073C35F5EF"

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref=commit)

        self.assertEqual(captured[2][-2:], ["origin", commit])
        self.assertEqual(captured[3][-2:], ["--detach", "FETCH_HEAD"])

    def test_clone_injects_token_and_proxy(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["username"] = Path(captured["env"]["LUMA_GIT_USERNAME_FILE"]).read_text(encoding="utf-8")
            captured["password"] = Path(captured["env"]["LUMA_GIT_PASSWORD_FILE"]).read_text(encoding="utf-8")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), proxy="http://127.0.0.1:7890", token="ghp_secret")
        clone_url = captured["cmd"][-2]
        self.assertEqual(clone_url, "https://github.com/acme/app")
        self.assertNotIn("ghp_secret", " ".join(captured["cmd"]))
        self.assertNotIn("ghp_secret", json.dumps(captured["env"]))
        self.assertEqual(captured["username"], "x-access-token")
        self.assertEqual(captured["password"], "ghp_secret")
        self.assertEqual(captured["env"]["GIT_ASKPASS_REQUIRE"], "force")
        self.assertEqual(captured["env"]["HTTPS_PROXY"], "http://127.0.0.1:7890")

    def test_clone_redacts_token_on_failure(self):
        from luma import gitops
        from luma.errors import LumaError

        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=1, stdout="fatal: https://x-access-token:ghp_secret@github.com/acme/app not found")):
            with self.assertRaises(LumaError) as ctx:
                gitops.clone("https://github.com/acme/app", Path("/tmp/x"), token="ghp_secret")
        self.assertNotIn("ghp_secret", str(ctx.exception))
        self.assertIn("***@", str(ctx.exception))

    def test_clone_rejects_repository_urls_that_can_leak_credentials(self):
        from luma import gitops

        rejected = (
            "https://user:gitea-super-secret@gcode.example.com/acme/app.git",
            "https://gcode.example.com/acme/app.git?access_token=gitea-super-secret",
            "https://gcode.example.com/acme/app.git#token=gitea-super-secret",
            "ssh://user:password@gcode.example.com/acme/app.git",
        )
        with patch("luma.gitops.subprocess.run") as run:
            for url in rejected:
                with self.subTest(url=url), self.assertRaisesRegex(LumaError, "credentials|query parameters"):
                    gitops.clone(url, Path("/tmp/x"))
            run.assert_not_called()

    def test_clone_removes_inherited_git_trace_environment(self):
        from luma import gitops

        captured = {}

        def fake_run(_cmd, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return Mock(returncode=0, stdout="")

        trace_env = {
            "GIT_TRACE": "1",
            "GIT_TRACE2": "/tmp/git-trace",
            "GIT_TRACE_PACKET": "1",
            "GIT_TRACE_CURL": "1",
            "GIT_CURL_VERBOSE": "1",
        }
        with patch.dict(os.environ, trace_env, clear=False), patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), token="gitea-super-secret")

        for name in trace_env:
            self.assertNotIn(name, captured["env"])

    def test_image_repo_from_repo_url(self):
        from luma.control.server import _image_repo_from_repo_url, normalize_import_repo_url

        self.assertEqual(_image_repo_from_repo_url("https://github.com/Acme/App"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app.git"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("git@github.com:acme/app.git"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("github.com/acme/My_Repo"), "acme/my_repo")
        # query string / fragment must not leak into the image name
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app?token=x"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app#readme"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app/"), "acme/app")
        self.assertEqual(normalize_import_repo_url("LiuTianjie/luxe-monitor"), "https://github.com/LiuTianjie/luxe-monitor.git")
        self.assertEqual(normalize_import_repo_url("LiuTianjie/luxe-monitor.git"), "https://github.com/LiuTianjie/luxe-monitor.git")
        self.assertEqual(normalize_import_repo_url("https://gcode.example.com/acme/app.git"), "https://gcode.example.com/acme/app.git")
        self.assertEqual(normalize_import_repo_url("github.com/acme"), "github.com/acme")

    def test_build_image_credentials_injected_at_lease_not_stored(self):
        # Security: gitToken / registryAuth must never be persisted in the build
        # payload; they are added only when the task is leased to the agent.
        from luma.control.server import _agent_task_lease_payload

        state = {
            "gitProviders": {
                "github:personal": {"type": "github", "account": "personal", "username": "octo", "token": "ghp_secret"}
            },
            "registries": {"build-1:5000": {"username": "u", "password": "p", "serverAddress": "build-1:5000"}},
        }
        stored_payload = {"repoUrl": "https://github.com/acme/app", "gitProviderId": "github:personal", "pushHost": "build-1:5000", "repo": "acme/app"}
        # the stored payload itself carries no credentials
        self.assertNotIn("gitToken", stored_payload)
        self.assertNotIn("registryAuth", stored_payload)
        leased = _agent_task_lease_payload(state, {"action": "build-image", "payload": stored_payload})
        self.assertEqual(leased.get("gitToken"), "ghp_secret")
        self.assertEqual((leased.get("registryAuth") or {}).get("username"), "u")
        # original stored payload is untouched (no mutation back into state)
        self.assertNotIn("gitToken", stored_payload)
        self.assertNotIn("registryAuth", stored_payload)

    def test_mirror_registry_credentials_injected_at_lease_not_stored(self):
        from luma.control.server import _agent_task_lease_payload

        state = {
            "registries": {
                "registry.example.com": {
                    "username": "lae",
                    "password": "registry-secret",
                    "serverAddress": "registry.example.com",
                }
            }
        }
        stored_payload = {
            "sourceImage": "ghcr.io/liutianjie/luma-control:v1",
            "pushImage": "registry.example.com/luma-control:v1",
        }

        leased = _agent_task_lease_payload(
            state,
            {"action": "mirror-control-image", "payload": stored_payload},
        )

        self.assertEqual(leased["registryAuth"]["password"], "registry-secret")
        self.assertNotIn("registryAuth", stored_payload)

    def test_runtime_cache_credentials_are_leased_for_source_and_destination(self):
        from luma.control.server import _agent_task_lease_payload

        state = {
            "registries": {
                "private.example.com": {
                    "username": "source-user",
                    "password": "source-secret",
                    "serverAddress": "private.example.com",
                },
                "builder:5000": {
                    "username": "cache-user",
                    "password": "cache-secret",
                    "serverAddress": "builder:5000",
                },
            }
        }
        stored_payload = {
            "sourceImage": "private.example.com/acme/api:latest",
            "pushImage": "builder:5000/luma-cache/private.example.com/acme/api:cache",
        }

        leased = _agent_task_lease_payload(
            state,
            {"action": "cache-runtime-image", "payload": stored_payload},
        )

        self.assertEqual(leased["sourceRegistryAuth"]["password"], "source-secret")
        self.assertEqual(leased["destinationRegistryAuth"]["password"], "cache-secret")
        self.assertNotIn("sourceRegistryAuth", stored_payload)
        self.assertNotIn("destinationRegistryAuth", stored_payload)

    def test_build_image_credentials_fall_back_to_legacy_github_token_secret(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"secrets": {"GITHUB_TOKEN": "ghp_legacy"}}
        leased = _agent_task_lease_payload(
            state,
            {"action": "build-image", "payload": {"repoUrl": "https://github.com/acme/app", "pushHost": "build-1:5000", "repo": "acme/app"}},
        )
        self.assertEqual(leased.get("gitToken"), "ghp_legacy")

    def test_build_image_credentials_do_not_fallback_when_provider_selected_but_missing(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"secrets": {"GITHUB_TOKEN": "ghp_legacy"}}
        leased = _agent_task_lease_payload(
            state,
            {
                "action": "build-image",
                "payload": {"repoUrl": "https://github.com/acme/app", "gitProviderId": "github:missing", "pushHost": "build-1:5000", "repo": "acme/app"},
            },
        )
        self.assertNotIn("gitToken", leased)

    def test_build_image_credentials_inject_provider_username(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"gitProviders": {"gitea:lin": {"type": "gitea", "account": "lin", "username": "lin", "token": "gitea_secret"}}}
        leased = _agent_task_lease_payload(
            state,
            {
                "action": "build-image",
                "payload": {"repoUrl": "https://gcode.gaojiua.com:3000/acme/app", "gitProviderId": "gitea:lin", "pushHost": "build-1:5000", "repo": "acme/app"},
            },
        )
        self.assertEqual(leased.get("gitToken"), "gitea_secret")
        self.assertEqual(leased.get("gitUsername"), "lin")

    def test_clone_uses_configured_git_username_for_https_token(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            captured["username"] = Path(captured["env"]["LUMA_GIT_USERNAME_FILE"]).read_text(encoding="utf-8")
            captured["password"] = Path(captured["env"]["LUMA_GIT_PASSWORD_FILE"]).read_text(encoding="utf-8")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://gcode.gaojiua.com:3000/acme/app", Path("/tmp/x"), token="gitea_secret", username="lin")

        clone_url = captured["cmd"][-2]
        self.assertEqual(clone_url, "https://gcode.gaojiua.com:3000/acme/app")
        self.assertEqual(captured["username"], "lin")
        self.assertEqual(captured["password"], "gitea_secret")
        self.assertNotIn("gitea_secret", " ".join(captured["cmd"]))
        self.assertNotIn("gitea_secret", json.dumps(captured["env"]))

    def test_head_commit_full_requires_complete_object_id(self):
        from luma import gitops

        full = "0123456789abcdef0123456789abcdef01234567"
        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout=full + "\n")) as run:
            self.assertEqual(gitops.head_commit_full(Path("/tmp/repo")), full)
        self.assertEqual(run.call_args.args[0][-1], "HEAD")

        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout="abc123\n")):
            with self.assertRaisesRegex(LumaError, "invalid full object id"):
                gitops.head_commit_full(Path("/tmp/repo"))
        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout=("a" * 41) + "\n")):
            with self.assertRaisesRegex(LumaError, "invalid full object id"):
                gitops.head_commit_full(Path("/tmp/repo"))

    def test_git_provider_repositories_fetch_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "gitea", "account": "lin", "baseUrl": "https://gcode.gaojiua.com:3000", "username": "lin", "token": "gitea_secret"},
                )
                calls = []

                def fake_urlopen(request, **_kwargs):
                    calls.append(request.full_url)
                    if request.full_url.endswith("page=1"):
                        return _JsonResponse(
                            [
                                {
                                    "full_name": f"acme/repo-{index}",
                                    "clone_url": f"https://gcode.gaojiua.com:3000/acme/repo-{index}.git",
                                    "default_branch": "main",
                                    "private": True,
                                }
                                for index in range(100)
                            ]
                        )
                    if request.full_url.endswith("page=2"):
                        return _JsonResponse(
                            [
                                {
                                    "full_name": "acme/repo-100",
                                    "clone_url": "https://gcode.gaojiua.com:3000/acme/repo-100.git",
                                    "default_branch": "main",
                                    "private": True,
                                }
                            ]
                        )
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_repositories(state["deployToken"], "gitea:lin")

                self.assertEqual(len(result["repositories"]), 101)
                self.assertTrue(calls[0].endswith("page=1"))
                self.assertTrue(calls[1].endswith("page=2"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_provider_refs_fetch_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )
                calls = []

                def fake_urlopen(request, **_kwargs):
                    calls.append(request.full_url)
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=1":
                        return _JsonResponse([{"name": f"branch-{index}"} for index in range(100)])
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=2":
                        return _JsonResponse([{"name": "branch-100"}])
                    if request.full_url == "https://api.github.com/repos/acme/app/tags?per_page=100&page=1":
                        return _JsonResponse([{"name": "v1"}])
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_refs(state["deployToken"], "github:personal", "acme/app")

                self.assertIn("https://api.github.com/repos/acme/app/branches?per_page=100&page=2", calls)
                self.assertEqual(len([item for item in result["refs"] if item["type"] == "branch"]), 101)
                self.assertIn({"name": "v1", "type": "tag"}, result["refs"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_deploy_accepts_provider_repository_mode(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["gitProviders"] = {
                    "gitea:lin": {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "cloneBaseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                captured = {}

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    captured.update({"node": node_name, "action": action, "payload": payload})
                    return {
                        "image": "100.64.0.70:5000/gaojiuatech/price:abc123",
                        "manifest": "name: price\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task), patch(
                    "luma.control.server.handle_deployment", return_value={"service": "price", "steps": []}
                ) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {"providerId": "gitea:lin", "repository": "gaojiuatech/price", "ref": "main"},
                    )

                self.assertEqual(captured["node"], "builder")
                self.assertEqual(captured["action"], "build-image")
                self.assertEqual(captured["payload"]["repoUrl"], "https://gcode.gaojiua.com:3000/gaojiuatech/price.git")
                self.assertEqual(captured["payload"]["gitProviderId"], "gitea:lin")
                self.assertEqual(captured["payload"]["registryHost"], "100.64.0.70:5000")
                self.assertEqual(captured["payload"]["pushHost"], "localhost:5000")
                self.assertEqual(captured["payload"]["repo"], "gaojiuatech/price")
                self.assertEqual(captured["payload"]["ref"], "main")
                deployed_manifest = deploy.call_args.args[1]["manifest"]
                self.assertIn("image: 100.64.0.70:5000/gaojiuatech/price:abc123", deployed_manifest)
                self.assertEqual(result["image"], "100.64.0.70:5000/gaojiuatech/price:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_requires_declared_builder_node(self):
        from luma.control.server import handle_build_deploy
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                    "home-2": {
                        "name": "home-2",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                with self.assertRaisesRegex(LumaError, "declared, ready builder node"):
                    handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app", "buildNode": "home-2"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_config_declares_builder_nodes(self):
        from luma.control.server import handle_build_config_set, handle_control_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                save_state(state)

                result = handle_build_config_set(
                    state["deployToken"],
                    {"nodes": ["builder"], "defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000", "directEgressNodes": ["builder"]},
                )

                self.assertEqual(result["build"]["defaultNode"], "builder")
                self.assertEqual(result["build"]["nodes"][0]["name"], "builder")
                self.assertEqual(result["build"]["directEgressNodes"], ["builder"])
                status = handle_control_status(state["deployToken"])
                self.assertEqual(status["build"]["registryHost"], "100.64.0.70:5000")
                self.assertEqual(status["build"]["directEgressNodes"], ["builder"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_direct_egress_builder_bypasses_manager_proxy(self):
        from luma.control.server import _egress_proxy_for_node

        state = {
            "managerAddr": "100.106.154.3",
            "build": {"directEgressNodes": ["builder"]},
            "nodes": {
                "builder": {"name": "builder", "region": "home", "aliases": ["build-1"]},
                "home-worker": {"name": "home-worker", "region": "home"},
            },
        }
        config = LumaConfig({"defaults": {"nomadServer": "100.106.154.3:4647"}}, None)

        self.assertEqual(_egress_proxy_for_node(config, state, "build-1"), "")
        self.assertEqual(_egress_proxy_for_node(config, state, "home-worker"), "http://100.106.154.3:7890")

    def test_runtime_config_marks_manager_for_local_ingress_addressing(self):
        from luma.control.server import _config_with_state_nodes

        config = LumaConfig({"nodes": {}}, None)
        state = {
            "nodes": {
                "manager": {
                    "name": "manager",
                    "status": "manager",
                    "region": "cn",
                    "tailscaleIP": "100.106.154.3",
                }
            }
        }

        runtime = _config_with_state_nodes(config, state)

        self.assertTrue(runtime.nodes["manager"].raw["lumaLocalIngress"])

    def test_build_run_records_failed_import_events(self):
        from luma.control.server import handle_build_deploy, handle_build_run_list, handle_build_run_get
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                def fake_run_task(*_args, **_kwargs):
                    raise LumaError("git clone failed: 403")

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task):
                    with self.assertRaisesRegex(LumaError, "git clone failed"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                listed = handle_build_run_list(state["deployToken"])["runs"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["status"], "failed")
                detail = handle_build_run_get(state["deployToken"], listed[0]["id"])["run"]
                self.assertEqual(detail["request"]["buildNode"], "builder")
                self.assertNotIn("envSecrets", detail["request"])
                self.assertIn("git clone failed: 403", detail["events"][-1]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_event_history_is_retained_and_paginated(self):
        from luma.control.server import (
            _append_build_run_event,
            _create_build_run,
            handle_build_run_get,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                for index in range(125):
                    _append_build_run_event(
                        run_id,
                        {
                            "name": "Build image",
                            "status": "progress",
                            "message": f"line-{index}",
                        },
                    )

                first = handle_build_run_get(state["deployToken"], run_id, {"limit": 100})
                self.assertEqual(len(first["run"]["events"]), 100)
                self.assertTrue(first["eventsPage"]["hasMore"])
                second = handle_build_run_get(state["deployToken"], run_id, {"limit": 100, "cursor": first["eventsPage"]["nextCursor"]})
                self.assertFalse(second["eventsPage"]["hasMore"])
                events = first["run"]["events"] + second["run"]["events"]
                self.assertEqual(len(events), 125)
                self.assertEqual(events[0]["message"], "line-0")
                self.assertEqual(events[-1]["message"], "line-124")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_history_preserves_legacy_events_with_bounded_messages(self):
        from luma.control.server import (
            _prune_build_runs,
            handle_build_run_get,
            handle_build_run_list,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                state["buildRuns"] = {
                    "build-legacy": {
                        "id": "build-legacy",
                        "status": "failed",
                        "message": "x" * 10_000,
                        "events": [
                            {"name": "Build image", "status": "progress", "message": f"line-{index}"}
                            for index in range(500)
                        ],
                        "createdAt": 1,
                        "updatedAt": 2,
                    }
                }
                _prune_build_runs(state)
                save_state(state)

                page = handle_build_run_get(state["deployToken"], "build-legacy", {"limit": 100})
                detail = page["run"]
                events = list(detail["events"])
                while page["eventsPage"]["hasMore"]:
                    page = handle_build_run_get(state["deployToken"], "build-legacy", {"limit": 100, "cursor": page["eventsPage"]["nextCursor"]})
                    events.extend(page["run"]["events"])
                summary = handle_build_run_list(state["deployToken"])["runs"][0]
                self.assertEqual(len(detail["events"]), 100)
                self.assertEqual(len(events), 500)
                self.assertEqual(events[0]["message"], "line-0")
                self.assertEqual(events[-1]["message"], "line-499")
                self.assertEqual(len(detail["message"]), 4000)
                self.assertEqual(len(summary["message"]), 500)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_history_records_events_with_origin(self):
        from luma.control.server import _record_deployment_event, _deployment_origin, handle_deployment_history, handle_deployment_history_get

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                token = state["deployToken"]

                # origin defaults to cli when unspecified, dashboard when tagged.
                self.assertEqual(_deployment_origin(None), "cli")
                self.assertEqual(_deployment_origin({}), "cli")
                self.assertEqual(_deployment_origin({"origin": "dashboard"}), "dashboard")
                self.assertEqual(_deployment_origin({"origin": "anything-else"}), "cli")

                _record_deployment_event(kind="service", name="web", slug="web", source_name="service.yaml", origin="cli", status="active", steps=[{"name": "x", "status": "ok"}])
                _record_deployment_event(kind="compose", name="stack", slug="stack", source_name="luma.compose.yml", origin="dashboard", status="failed_partial", error="boom")

                events = handle_deployment_history(token)["events"]
                self.assertEqual(len(events), 2)
                # newest first
                self.assertEqual(events[0]["name"], "stack")
                self.assertEqual(events[0]["origin"], "dashboard")
                self.assertEqual(events[0]["status"], "failed_partial")
                self.assertEqual(events[0]["error"], "boom")
                self.assertEqual(events[1]["name"], "web")
                self.assertEqual(events[1]["origin"], "cli")
                self.assertEqual(events[1]["stepCount"], 1)
                # list omits the heavy steps array; detail endpoint returns it
                self.assertNotIn("steps", events[1])
                detail = handle_deployment_history_get(token, events[1]["id"])["event"]
                self.assertEqual(detail["steps"], [{"name": "x", "status": "ok"}])
                from luma.errors import LumaError
                with self.assertRaises(LumaError):
                    handle_deployment_history_get(token, "deploy-missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_retry_creates_linked_attempt_and_preserves_failure(self):
        from luma.control.server import handle_build_deploy, handle_build_run_get, handle_build_run_list, handle_build_run_retry
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                def fail_task(*_args, **_kwargs):
                    raise LumaError("docker buildx build failed")

                with patch("luma.control.server._run_node_agent_task", side_effect=fail_task):
                    with self.assertRaisesRegex(LumaError, "docker buildx build failed"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                failed = handle_build_run_list(state["deployToken"])["runs"][0]
                original_detail = handle_build_run_get(state["deployToken"], failed["id"])["run"]

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "image": "100.64.0.70:5000/acme/app:abc123",
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\n",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "app", "steps": []}):
                    result = handle_build_run_retry(state["deployToken"], failed["id"])

                listed = handle_build_run_list(state["deployToken"])["runs"]
                self.assertEqual(len(listed), 2)
                self.assertNotEqual(result["buildRunId"], failed["id"])
                self.assertEqual(handle_build_run_get(state["deployToken"], failed["id"])["run"], original_detail)
                detail = handle_build_run_get(state["deployToken"], result["buildRunId"])["run"]
                self.assertEqual(detail["status"], "succeeded")
                self.assertEqual(detail["retryOf"], failed["id"])
                self.assertEqual(detail["retryRootId"], failed["id"])
                self.assertEqual(detail["events"][0]["name"], "Build image")

                retry_progress = []
                with patch("luma.control.server._run_node_agent_task", side_effect=fail_task):
                    with self.assertRaisesRegex(LumaError, "docker buildx build failed"):
                        handle_build_run_retry(state["deployToken"], failed["id"], progress=retry_progress.append)
                failed_retry_id = retry_progress[0]["buildRunId"]
                self.assertNotEqual(failed_retry_id, failed["id"])
                self.assertNotEqual(failed_retry_id, result["buildRunId"])
                self.assertEqual(handle_build_run_get(state["deployToken"], failed_retry_id)["run"]["status"], "failed")
                self.assertEqual(handle_build_run_get(state["deployToken"], failed["id"])["run"], original_detail)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_cancel_signals_legacy_agent_task_and_fences_success(self):
        from luma.control.server import (
            _complete_build_run,
            _create_build_run,
            handle_build_run_cancel,
            handle_build_run_get,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                current = load_state()
                created_at = current["buildRuns"][run_id]["createdAt"]
                current["agentTasks"] = {
                    "task-legacy": {
                        "id": "task-legacy",
                        "nodeName": "builder",
                        "action": "build-image",
                        "payload": {"repoUrl": "https://github.com/acme/app"},
                        "status": "running",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                    }
                }
                save_state(current)

                canceled = handle_build_run_cancel(state["deployToken"], run_id, {})

                self.assertFalse(canceled["replayed"])
                self.assertEqual(canceled["run"]["status"], "canceling")
                persisted = load_state()
                self.assertEqual(persisted["buildRuns"][run_id]["agentTaskId"], "task-legacy")
                self.assertEqual(persisted["agentTasks"]["task-legacy"]["buildRunId"], run_id)
                self.assertTrue(persisted["agentTasks"]["task-legacy"]["cancelRequestedAt"])

                # A late success response cannot revive a run after the user
                # requested cancellation.
                _complete_build_run(run_id, "succeeded", result={"service": "app"})
                detail = handle_build_run_get(state["deployToken"], run_id)["run"]
                self.assertEqual(detail["status"], "canceled")
                self.assertEqual(detail["message"], "build canceled")
                self.assertEqual(detail["result"], {})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_cancel_stops_queued_task_immediately(self):
        from luma.control.server import _create_build_run, handle_build_run_cancel

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                current = load_state()
                created_at = current["buildRuns"][run_id]["createdAt"]
                current["buildRuns"][run_id]["agentTaskId"] = "task-queued"
                current["agentTasks"] = {
                    "task-queued": {
                        "id": "task-queued",
                        "nodeName": "builder",
                        "action": "build-image",
                        "payload": {"repoUrl": "https://github.com/acme/app"},
                        "status": "queued",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "buildRunId": run_id,
                    }
                }
                save_state(current)

                result = handle_build_run_cancel(state["deployToken"], run_id, {})

                self.assertEqual(result["run"]["status"], "canceled")
                self.assertEqual(load_state()["agentTasks"]["task-queued"]["status"], "canceled")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_import_records_source_and_application_update_rebuilds_it(self):
        from luma.control.server import handle_application_update, handle_build_deploy, handle_deployment_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                build_images = iter([
                    "100.64.0.70:5000/acme/app:first",
                    "100.64.0.70:5000/acme/app:second",
                ])

                def fake_build(*_args, **_kwargs):
                    return {
                        "image": next(build_images),
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                def fake_deploy(token, body, **_kwargs):
                    from luma.control.server import _load_service_manifest, _mark_service_deployment

                    service = _load_service_manifest(body["manifest"])
                    _mark_service_deployment(
                        service,
                        body["manifest"],
                        str(body.get("sourceName") or ""),
                        status="active",
                        steps=[],
                        git_source=body.get("gitSource"),
                    )
                    return {"service": service.name, "steps": []}

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_build), patch("luma.control.server.handle_deployment", side_effect=fake_deploy):
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "ref": "main",
                            "buildNode": "builder",
                            "proxyMode": "direct",
                        },
                    )

                config = handle_deployment_config(state["deployToken"], "app")
                self.assertEqual(config["gitSource"]["repoUrl"], "https://github.com/acme/app")
                self.assertEqual(config["gitSource"]["ref"], "main")
                self.assertEqual(config["gitSource"]["buildNode"], "builder")
                self.assertEqual(config["gitSource"]["proxyMode"], "direct")
                self.assertEqual(config["gitSource"]["buildRunId"], result["buildRunId"])

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_build), patch("luma.control.server.handle_deployment", side_effect=fake_deploy) as deploy:
                    update = handle_application_update(state["deployToken"], {"name": "app"})

                self.assertEqual(update["service"], "app")
                self.assertNotEqual(update["buildRunId"], result["buildRunId"])
                update_body = deploy.call_args.args[1]
                self.assertEqual(update_body["gitSource"]["repoUrl"], "https://github.com/acme/app")
                self.assertEqual(update_body["gitSource"]["ref"], "main")
                self.assertEqual(update_body["gitSource"]["proxyMode"], "direct")
                self.assertIn("image: 100.64.0.70:5000/acme/app:second", update_body["manifest"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_retry_accepts_env_secret_overrides_without_storing_values(self):
        from luma.control.server import handle_build_deploy, handle_build_run_get, handle_build_run_list, handle_build_run_retry
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                with patch("luma.control.server._run_node_agent_task", side_effect=LumaError("missing deployment secret")):
                    with self.assertRaisesRegex(LumaError, "missing deployment secret"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                failed = handle_build_run_list(state["deployToken"])["runs"][0]

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "image": "100.64.0.70:5000/acme/app:abc123",
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "app", "steps": []}) as deploy:
                    result = handle_build_run_retry(
                        state["deployToken"],
                        failed["id"],
                        {"envSecrets": {"DATABASE_URL": "postgres://secret"}},
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["envSecrets"], {"DATABASE_URL": "postgres://secret"})
                self.assertNotEqual(result["buildRunId"], failed["id"])
                detail = handle_build_run_get(state["deployToken"], result["buildRunId"])["run"]
                self.assertEqual(detail["request"]["envSecretNames"], ["DATABASE_URL"])
                self.assertNotIn("postgres://secret", json.dumps(detail))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_build_progress_is_forwarded(self):
        from luma.control.server import _wait_node_agent_task

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["agentTasks"] = {
                    "task-1": {
                        "id": "task-1",
                        "nodeName": "builder",
                        "action": "build-image",
                        "status": "succeeded",
                        "progressOffset": 2,
                        "progress": [
                            {"type": "output", "line": "Buildx builder is missing; recreating it"},
                            {"type": "output", "line": "final build output"},
                        ],
                        "result": {"image": "100.64.0.70:5000/acme/app:abc123"},
                    }
                }
                save_state(state)
                events: list[dict[str, str]] = []

                result = _wait_node_agent_task("task-1", "builder", "build-image", timeout=1, progress=lambda event: events.append(event))

                self.assertEqual(result["image"], "100.64.0.70:5000/acme/app:abc123")
                self.assertEqual([event["name"] for event in events], ["Build image"] * 3)
                self.assertTrue(all(event["status"] == "progress" for event in events))
                self.assertIn("discarded", events[0]["message"])
                self.assertIn("recreating", events[1]["message"])
                self.assertEqual(events[2]["message"], "final build output")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_agent_build_progress_continues_after_retained_window_rolls(self):
        from luma.control.server import _wait_node_agent_task

        original = [{"type": "output", "line": f"line-{index}"} for index in range(300)]
        completed = {
            "id": "task-1",
            "status": "succeeded",
            "progressOffset": 1,
            "progress": original[1:] + [{"type": "output", "line": "line-300"}],
            "result": {"image": "100.64.0.70:5000/acme/app:abc123"},
        }
        events: list[dict[str, str]] = []

        with patch(
            "luma.control.state.load_entity",
            side_effect=[{"id": "task-1", "status": "running", "progress": original}, completed],
        ), patch("luma.control.server.time.sleep"):
            result = _wait_node_agent_task(
                "task-1",
                "builder",
                "build-image",
                timeout=1,
                progress=lambda event: events.append(event),
            )

        self.assertEqual(result["image"], "100.64.0.70:5000/acme/app:abc123")
        self.assertEqual([event["message"] for event in events], [f"line-{index}" for index in range(301)])

    def test_build_deploy_expands_owner_repo_shortcut_to_github_url(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                captured = {}

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    captured.update({"node": node_name, "action": action, "payload": payload})
                    return {
                        "image": "100.64.0.70:5000/liutianjie/luxe-monitor:abc123",
                        "manifest": "name: luxe-monitor\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task), patch(
                    "luma.control.server.handle_deployment", return_value={"service": "luxe-monitor", "steps": []}
                ) as deploy:
                    result = handle_build_deploy(state["deployToken"], {"repoUrl": "LiuTianjie/luxe-monitor"})

                self.assertEqual(captured["payload"]["repoUrl"], "https://github.com/LiuTianjie/luxe-monitor.git")
                self.assertEqual(captured["payload"]["repo"], "liutianjie/luxe-monitor")
                self.assertEqual(deploy.call_args.args[1]["sourceName"], "https://github.com/LiuTianjie/luxe-monitor.git")
                self.assertEqual(result["image"], "100.64.0.70:5000/liutianjie/luxe-monitor:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_accepts_manual_manifest_when_repo_has_none(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                manual_manifest = "name: price\nimage: placeholder\nregion: cn\nexposure: none\n"

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"image": "100.64.0.70:5000/gaojiuatech/price:abc123", "manifest": ""},
                ), patch("luma.control.server.handle_deployment", return_value={"service": "price", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {"repoUrl": "https://github.com/gaojiuatech/price", "manifest": manual_manifest},
                    )

                deployed_manifest = deploy.call_args.args[1]["manifest"]
                self.assertIn("name: price", deployed_manifest)
                self.assertIn("image: 100.64.0.70:5000/gaojiuatech/price:abc123", deployed_manifest)
                self.assertEqual(result["image"], "100.64.0.70:5000/gaojiuatech/price:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_routes_compose_import_to_compose_handler(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                sidecar = "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n"
                compose = (
                    "services:\n"
                    "  web:\n"
                    "    image: 100.64.0.70:5000/acme/app/web:abc123\n"
                    "  worker:\n"
                    "    image: acme/app:local\n"
                )
                source_compose = (
                    "services:\n"
                    "  web:\n"
                    "    image: acme/app:local\n"
                    "    build: .\n"
                    "  worker:\n"
                    "    image: acme/app:local\n"
                )

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "kind": "compose",
                        "manifest": sidecar,
                        "composeContent": compose,
                        "images": {"web": "100.64.0.70:5000/acme/app/web:abc123"},
                        "imageAliases": {
                            "acme/app:local": "100.64.0.70:5000/acme/app/web:abc123"
                        },
                        "image": "100.64.0.70:5000/acme/app/web:abc123",
                    },
                ), patch("luma.control.server.handle_compose_deployment", return_value={"deployment": "app-stack", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "ref": "main",
                            "domain": "ignored.example.com",
                            "exposure": "cn-edge",
                            "port": 3000,
                            # Repository-import callers may submit the source
                            # Compose for preview, but the Builder copy has the
                            # immutable image rewrites and must win.
                            "composeContent": source_compose,
                        },
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["manifest"], sidecar)
                deployed_compose = yaml.safe_load(deploy_body["composeContent"])
                self.assertEqual(
                    deployed_compose["services"]["web"]["image"],
                    "100.64.0.70:5000/acme/app/web:abc123",
                )
                self.assertEqual(
                    deployed_compose["services"]["worker"]["image"],
                    "100.64.0.70:5000/acme/app/web:abc123",
                )
                self.assertEqual(deploy_body["sourceName"], "https://github.com/acme/app")
                self.assertNotIn("envSecrets", deploy_body)
                self.assertEqual(result["deployment"], "app-stack")
                self.assertEqual(result["images"]["web"], "100.64.0.70:5000/acme/app/web:abc123")
                self.assertIn("Compose import ignores service-level override(s): exposure, domain, port", result["steps"][1]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_passes_env_secrets_to_final_service_deploy(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.64.0.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                repo_manifest = "name: api\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n"

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "kind": "service",
                        "manifest": repo_manifest,
                        "image": "100.64.0.70:5000/acme/app:abc123",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "api", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "envSecrets": {"DATABASE_URL": "postgres://secret"},
                        },
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["envSecrets"], {"DATABASE_URL": "postgres://secret"})
                self.assertIn("image: 100.64.0.70:5000/acme/app:abc123", deploy_body["manifest"])
                self.assertEqual(result["service"], "api")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_image_lease_without_secrets(self):
        from luma.control.server import _agent_task_lease_payload

        leased = _agent_task_lease_payload({}, {"action": "build-image", "payload": {"repoUrl": "https://github.com/acme/app", "pushHost": "build-1:5000", "repo": "acme/app"}})
        self.assertNotIn("gitToken", leased)
        self.assertNotIn("registryAuth", leased)

    def test_safe_image_repo_rejects_bad_input(self):
        from luma.agent import _safe_image_repo
        from luma.errors import LumaError

        self.assertEqual(_safe_image_repo("acme/app"), "acme/app")
        for bad in ("../etc", "acme/app;rm", "UP PER"):
            with self.assertRaises(LumaError):
                _safe_image_repo(bad)

    def test_safe_registry_host(self):
        from luma.agent import _safe_registry_host
        from luma.errors import LumaError

        self.assertEqual(_safe_registry_host("node-1:5000"), "node-1:5000")
        self.assertEqual(_safe_registry_host("localhost:5000"), "localhost:5000")
        with self.assertRaises(LumaError):
            _safe_registry_host("bad host:5000")

    def test_safe_repo_subpath_blocks_escape(self):
        from luma.agent import _safe_repo_subpath
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            self.assertEqual(_safe_repo_subpath(root, "sub"), (root / "sub").resolve())
            with self.assertRaises(LumaError):
                _safe_repo_subpath(root, "../../etc/passwd")

    def test_build_image_discovers_luma_manifest_outside_repo_root(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "deploy").mkdir(parents=True)
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            (dest / "deploy" / "app.luma.yml").write_text(
                "name: nested-app\nimage: placeholder\nregion: cn\nexposure: none\n",
                encoding="utf-8",
            )

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        self.assertIn("name: nested-app", result["manifest"])
        self.assertRegex(result["image"], r"^100\.64\.0\.70:5000/acme/app:abc123-[a-f0-9]{16}$")

    def test_remote_build_attempts_do_not_overwrite_same_commit_images(self):
        from luma.agent import build_image

        for kind in ("service", "compose"):
            with self.subTest(kind=kind):
                def fake_clone(_url, dest, **_kwargs):
                    dest.mkdir(parents=True)
                    (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
                    if kind == "compose":
                        (dest / "luma.compose.yml").write_text(
                            "name: app\ncompose: docker-compose.yml\nregion: cn\n", encoding="utf-8"
                        )
                        (dest / "docker-compose.yml").write_text(
                            "services:\n  web:\n    build: .\n    image: acme/app:local\n"
                            "  worker:\n    image: acme/app:local\n", encoding="utf-8"
                        )
                    else:
                        (dest / ".luma.yml").write_text(
                            "name: app\nimage: placeholder\nregion: cn\nexposure: none\n", encoding="utf-8"
                        )

                with patch("luma.gitops.clone", side_effect=fake_clone), patch(
                    "luma.gitops.head_commit", return_value="abc123"
                ), patch("luma.agent._docker_binary", return_value="docker"), patch(
                    "luma.agent._docker_buildx_available", return_value=True
                ), patch("luma.agent._ensure_buildx_builder", return_value="luma-builder"), patch(
                    "luma.agent._run_process_streaming", return_value=Mock(code=0, output="pushed\n")
                ) as build:
                    results = [build_image({
                        "repoUrl": "https://github.com/acme/app", "ref": ref,
                        "registryHost": "registry:5000", "pushHost": "registry:5000", "repo": "acme/app",
                    }) for ref in ("main", "dev", "main")]

                self.assertEqual(len({result["image"] for result in results}), 3)
                for result, call in zip(results, build.call_args_list):
                    self.assertEqual(result["sha"], "abc123")
                    self.assertRegex(result["image"], r"^registry:5000/acme/app:abc123-[a-f0-9]{16}$")
                    command = call.args[0]
                    self.assertEqual(command[command.index("-t") + 1], result["image"])
                    if kind == "compose":
                        services = yaml.safe_load(result["composeContent"])["services"]
                        self.assertEqual(services["web"]["image"], result["image"])
                        self.assertEqual(services["worker"]["image"], result["image"])

    def test_build_image_prefers_root_manifest_over_nested_compose_manifest(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "examples").mkdir(parents=True)
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            (dest / ".luma.yml").write_text(
                "name: root-app\nimage: placeholder\nregion: cn\nexposure: none\n",
                encoding="utf-8",
            )
            (dest / "examples" / "luma.compose.yml").write_text(
                "name: example-stack\ncompose: docker-compose.yml\nregion: cn\n",
                encoding="utf-8",
            )
            (dest / "examples" / "docker-compose.yml").write_text(
                "services:\n  web:\n    image: nginx:alpine\n",
                encoding="utf-8",
            )

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        self.assertEqual(result["kind"], "service")
        self.assertIn("name: root-app", result["manifest"])

    def test_build_image_discovers_nested_compose_manifest_and_rewrites_build_services(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            deploy = dest / "deploy"
            (deploy / "web").mkdir(parents=True)
            (deploy / "prod.luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (deploy / "docker-compose.yml").write_text(
                "services:\n  web:\n    build:\n      context: ./web\n      dockerfile: Dockerfile\n      platform: linux/amd64\n  redis:\n    image: redis:7-alpine\n",
                encoding="utf-8",
            )
            (deploy / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        self.assertEqual(result["kind"], "compose")
        self.assertIn("name: app-stack", result["manifest"])
        self.assertRegex(compose["services"]["web"]["image"], r"^100\.64\.0\.70:5000/acme/app:abc123-[a-f0-9]{16}$")
        self.assertNotIn("build", compose["services"]["web"])
        self.assertEqual(compose["services"]["redis"]["image"], "redis:7-alpine")

    def test_build_image_passes_literal_compose_build_args_to_buildx(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "services:\n"
                "  web:\n"
                "    build:\n"
                "      context: ./web\n"
                "      args:\n"
                "        NEXT_PUBLIC_UPLOAD_ORIGIN: https://uploads.example.com\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text(
                "FROM busybox\n", encoding="utf-8"
            )

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch(
            "luma.gitops.head_commit", return_value="abc123"
        ), patch("luma.agent._docker_binary", return_value="docker"), patch(
            "luma.agent._docker_buildx_available", return_value=True
        ), patch("luma.agent._ensure_buildx_builder", return_value="luma-builder"), patch(
            "luma.agent._run_process_streaming", return_value=completed
        ) as run:
            build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        command = run.call_args.args[0]
        index = command.index("--build-arg")
        self.assertEqual(
            command[index + 1],
            "NEXT_PUBLIC_UPLOAD_ORIGIN=https://uploads.example.com",
        )

    def test_build_image_rejects_environment_inherited_compose_build_args(self):
        from luma.agent import build_image
        from luma.errors import LumaError

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "services:\n"
                "  web:\n"
                "    build:\n"
                "      context: ./web\n"
                "      args:\n"
                "        PRIVATE_VALUE:\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text(
                "FROM busybox\n", encoding="utf-8"
            )

        with patch("luma.gitops.clone", side_effect=fake_clone), patch(
            "luma.gitops.head_commit", return_value="abc123"
        ), patch("luma.agent._docker_binary", return_value="docker"), patch(
            "luma.agent._docker_buildx_available", return_value=True
        ), patch("luma.agent._ensure_buildx_builder", return_value="luma-builder"):
            with self.assertRaisesRegex(LumaError, "must declare a literal value"):
                build_image(
                    {
                        "repoUrl": "https://github.com/acme/app",
                        "registryHost": "100.64.0.70:5000",
                        "pushHost": "localhost:5000",
                        "repo": "acme/app",
                    }
                )

    def test_build_image_rewrites_services_reusing_a_built_compose_image(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: home\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "x-app-image: &app-image registry.example/acme/app:latest\n"
                "services:\n"
                "  web:\n"
                "    image: *app-image\n"
                "    build:\n"
                "      context: ./web\n"
                "      dockerfile: Dockerfile\n"
                "      x-luma-repo: acme/app\n"
                "  worker:\n"
                "    image: *app-image\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch(
            "luma.gitops.head_commit", return_value="abc123"
        ), patch("luma.agent._docker_binary", return_value="docker"), patch(
            "luma.agent._docker_buildx_available", return_value=True
        ), patch("luma.agent._ensure_buildx_builder", return_value="luma-builder"), patch(
            "luma.agent._run_process_streaming", return_value=completed
        ):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        expected = result["image"]
        self.assertRegex(expected, r"^100\.64\.0\.70:5000/acme/app:abc123-[a-f0-9]{16}$")
        self.assertEqual(compose["services"]["web"]["image"], expected)
        self.assertEqual(compose["services"]["worker"]["image"], expected)
        self.assertEqual(
            result["imageAliases"],
            {"registry.example/acme/app:latest": expected},
        )

    def test_build_image_treats_docker_compose_luma_yml_as_compose_manifest(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "docker-compose.luma.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "services:\n  web:\n    build:\n      context: ./web\n      dockerfile: Dockerfile\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.64.0.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        self.assertEqual(result["kind"], "compose")
        self.assertIn("name: app-stack", result["manifest"])
        self.assertRegex(compose["services"]["web"]["image"], r"^100\.64\.0\.70:5000/acme/app:abc123-[a-f0-9]{16}$")
        self.assertNotIn("build", compose["services"]["web"])

    def test_build_image_rejects_ambiguous_same_priority_luma_manifests(self):
        from luma.agent import build_image
        from luma.errors import LumaError

        def fake_clone(_url, dest, **_kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".luma.yml").write_text("name: app\nregion: cn\nbuild: {}\n", encoding="utf-8")
            (dest / "luma.compose.yml").write_text("name: app-stack\ncompose: docker-compose.yml\nregion: cn\n", encoding="utf-8")
            (dest / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx:alpine\n", encoding="utf-8")
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True):
            with self.assertRaisesRegex(LumaError, "multiple Luma deployment manifests"):
                build_image(
                    {
                        "repoUrl": "https://github.com/acme/app",
                        "registryHost": "100.64.0.70:5000",
                        "pushHost": "localhost:5000",
                        "repo": "acme/app",
                    }
                )

    def test_buildx_capability_advertised_when_present(self):
        import luma.agent as agent

        agent._BUILDX_AVAILABLE = None
        with patch("luma.agent.shutil.which", return_value="/usr/bin/docker-buildx"):
            caps = agent.node_agent_capabilities("linux")
        agent._BUILDX_AVAILABLE = None
        self.assertIn("docker-build", caps)

    def test_buildx_capability_absent_when_missing(self):
        import luma.agent as agent

        agent._BUILDX_AVAILABLE = None
        with patch("luma.agent.shutil.which", return_value=None), patch("luma.agent.os.path.exists", return_value=False):
            caps = agent.node_agent_capabilities("linux")
        agent._BUILDX_AVAILABLE = None
        self.assertNotIn("docker-build", caps)

    def test_buildx_builder_quotes_no_proxy_commas_for_driver_opt(self):
        from luma.agent import _ensure_buildx_builder, _buildx_driver_opt

        self.assertEqual(_buildx_driver_opt("env.NO_PROXY=localhost,127.0.0.1"), '"env.NO_PROXY=localhost,127.0.0.1"')

        calls = []
        inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                inspect_count += 1
                return Mock(returncode=1 if inspect_count == 1 else 0, stdout="")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            "HTTP_PROXY=http://127.0.0.1:7890",
                            "HTTPS_PROXY=http://127.0.0.1:7890",
                            "NO_PROXY=localhost,127.0.0.1,100.64.0.70:5000",
                        ]
                    ),
                )
            return Mock(returncode=0, stdout="created\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://127.0.0.1:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.70:5000",
            )

        self.assertEqual(builder, "luma-builder-egress")
        create_cmd = calls[1]
        self.assertIn('"env.NO_PROXY=localhost,127.0.0.1,100.64.0.70:5000"', create_cmd)
        self.assertNotIn("env.NO_PROXY=localhost\\,127.0.0.1\\,100.64.0.70:5000", create_cmd)
        self.assertNotIn("env.NO_PROXY=localhost,127.0.0.1,100.64.0.70:5000", create_cmd)

    def test_buildx_builder_uses_docker_config_env_for_every_step(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        inspect_count = 0
        buildx_env = {"DOCKER_CONFIG": "/tmp/luma-docker-config"}

        def fake_run(cmd, **kwargs):
            nonlocal inspect_count
            calls.append((cmd, kwargs))
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                inspect_count += 1
                return Mock(returncode=1 if inspect_count == 1 else 0, stdout="")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(returncode=0, stdout="[]")
            return Mock(returncode=0, stdout="created\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder("docker", proxy="", no_proxy="localhost", env=buildx_env)

        self.assertEqual(builder, "luma-builder")
        self.assertEqual([kwargs.get("env") for _, kwargs in calls], [buildx_env, buildx_env, buildx_env, buildx_env])

    def test_buildx_builder_configures_internal_registry_for_base_image_resolution(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        config_text = ""

        def fake_run(cmd, **_kwargs):
            nonlocal config_text
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=1 if len(calls) == 1 else 0, stdout="")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(
                    returncode=0,
                    stdout=json.dumps(["LUMA_INSECURE_REGISTRY_HOST=100.64.0.70:5000"]),
                )
            if cmd[:3] == ["docker", "buildx", "create"]:
                config_path = Path(cmd[cmd.index("--buildkitd-config") + 1])
                config_text = config_path.read_text(encoding="utf-8")
            return Mock(returncode=0, stdout="created\n")

        with tempfile.TemporaryDirectory() as tmp, patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder(
                "docker",
                proxy="",
                no_proxy="localhost,100.64.0.70:5000",
                registry_host="100.64.0.70:5000",
                env={"BUILDX_CONFIG": tmp},
            )

        self.assertEqual(builder, "luma-builder")
        self.assertEqual(calls[1][:3], ["docker", "rm", "-f"])
        create_cmd = calls[2]
        self.assertIn("--buildkitd-config", create_cmd)
        self.assertEqual(
            config_text,
            '[registry."100.64.0.70:5000"]\n  http = true\n  insecure = true\n',
        )

    def test_buildx_builder_recreates_when_internal_registry_config_is_missing(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        docker_inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal docker_inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder\n")
            if cmd[:2] == ["docker", "inspect"]:
                docker_inspect_count += 1
                return Mock(returncode=0, stdout=json.dumps([]))
            return Mock(returncode=0, stdout="ok\n")

        with tempfile.TemporaryDirectory() as tmp, patch("luma.agent.subprocess.run", side_effect=fake_run):
            Path(tmp, ".luma-builder.luma-registry").write_text("old.registry:5000\n", encoding="utf-8")
            builder = _ensure_buildx_builder(
                "docker",
                proxy="",
                no_proxy="localhost,100.64.0.70:5000",
                registry_host="100.64.0.70:5000",
                env={"BUILDX_CONFIG": tmp},
            )

        self.assertEqual(builder, "luma-builder")
        self.assertEqual(calls[2], ["docker", "buildx", "rm", "-f", "luma-builder"])
        self.assertEqual(calls[3][:3], ["docker", "rm", "-f"])
        self.assertEqual(calls[4][:5], ["docker", "buildx", "create", "--name", "luma-builder"])

    def test_buildx_environment_separates_ephemeral_auth_from_persistent_state(self):
        from luma.agent import _buildx_environment

        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp, "task-auth")
            state = Path(tmp, "persistent-buildx")
            with patch.dict(os.environ, {"LUMA_BUILDX_CONFIG": str(state)}):
                env = _buildx_environment(auth)

            self.assertEqual(env["DOCKER_CONFIG"], str(auth))
            self.assertEqual(env["BUILDX_CONFIG"], str(state))
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_buildx_builder_reuses_matching_proxy_driver_options(self):
        from luma.agent import _ensure_buildx_builder

        def fake_run(cmd, **_kwargs):
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder-egress\n")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            "HTTP_PROXY=http://manager:7890",
                            "HTTPS_PROXY=http://manager:7890",
                            "NO_PROXY=localhost,127.0.0.1,100.64.0.0/10",
                        ]
                    ),
                )
            raise AssertionError(cmd)

        with patch("luma.agent.subprocess.run", side_effect=fake_run) as run:
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertEqual(builder, "luma-builder-egress")
        self.assertEqual(run.call_count, 2)

    def test_buildx_builder_proxy_match_rejects_changed_no_proxy(self):
        from luma.agent import _buildx_builder_proxy_matches

        container_env = json.dumps(
            [
                "HTTP_PROXY=http://manager:7890",
                "HTTPS_PROXY=http://manager:7890",
                "NO_PROXY=localhost,127.0.0.1",
            ]
        )
        with patch(
            "luma.agent.subprocess.run",
            return_value=Mock(returncode=0, stdout=container_env),
        ):
            matches = _buildx_builder_proxy_matches(
                "docker",
                "luma-builder-egress",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertFalse(matches)

    def test_buildx_builder_recreates_when_proxy_url_changes(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        docker_inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal docker_inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder-egress\n")
            if cmd[:2] == ["docker", "inspect"]:
                docker_inspect_count += 1
                proxy = "http://aly:7890" if docker_inspect_count == 1 else "http://manager:7890"
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [f"HTTP_PROXY={proxy}", f"HTTPS_PROXY={proxy}", "NO_PROXY=localhost,127.0.0.1"]
                    ),
                )
            return Mock(returncode=0, stdout="ok\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1",
            )

        self.assertEqual(builder, "luma-builder-egress")
        self.assertEqual(calls[2], ["docker", "buildx", "rm", "-f", "luma-builder-egress"])
        self.assertEqual(calls[3][:5], ["docker", "buildx", "create", "--name", "luma-builder-egress"])

    def test_buildx_builder_recreates_no_proxy_builder_with_proxy_options(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        docker_inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal docker_inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder\n")
            if cmd[:2] == ["docker", "inspect"]:
                docker_inspect_count += 1
                container_env = [
                    "HTTP_PROXY=http://aly:7890",
                    "HTTPS_PROXY=http://aly:7890",
                    "NO_PROXY=localhost",
                ] if docker_inspect_count == 1 else []
                return Mock(returncode=0, stdout=json.dumps(container_env))
            return Mock(returncode=0, stdout="ok\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder("docker", proxy="", no_proxy="localhost")

        self.assertEqual(builder, "luma-builder")
        self.assertEqual(calls[2], ["docker", "buildx", "rm", "-f", "luma-builder"])
        create_cmd = calls[3]
        self.assertNotIn("env.HTTP_PROXY=", " ".join(create_cmd))

    def test_buildx_build_recreates_missing_builder_and_retries(self):
        from luma.agent import _docker_buildx_build
        from luma.local import LocalResult

        progress: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docker_config = root / "docker-config"
            docker_config.mkdir()
            context_dir = root / "src"
            context_dir.mkdir()
            dockerfile = context_dir / "Dockerfile"
            dockerfile.write_text("FROM busybox\n", encoding="utf-8")

            with patch.dict(os.environ, {"LUMA_BUILDX_CONFIG": str(root / "buildx")}), patch(
                "luma.agent._run_process_streaming",
                side_effect=[
                    LocalResult(code=1, output='ERROR: no builder "luma-builder-egress" found\n'),
                    LocalResult(code=0, output="pushed\n"),
                ],
            ) as run, patch("luma.agent._ensure_buildx_builder", return_value="luma-builder-egress") as ensure:
                image = _docker_buildx_build(
                    docker="docker",
                    builder="luma-builder-egress",
                    docker_config=docker_config,
                    push_host="localhost:5000",
                    registry_host="100.64.0.70:5000",
                    repo="acme/app",
                    sha="abc123",
                    context_dir=context_dir,
                    dockerfile_path=dockerfile,
                    platform="linux/amd64",
                    proxy="http://127.0.0.1:7890",
                    build_timeout=1800,
                    progress=lambda event: progress.append(event),
                )

        self.assertEqual(image, "100.64.0.70:5000/acme/app:abc123")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(ensure.call_args.args, ("docker",))
        self.assertEqual(ensure.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(ensure.call_args.kwargs["no_proxy"], "localhost,127.0.0.1,::1,localhost:5000,100.64.0.70:5000")
        self.assertTrue(ensure.call_args.kwargs["recreate"])
        self.assertEqual(ensure.call_args.kwargs["env"]["DOCKER_CONFIG"], str(docker_config))
        self.assertEqual(ensure.call_args.kwargs["env"]["BUILDX_CONFIG"], str(root / "buildx"))
        self.assertIn("recreating it and retrying once", progress[0]["line"])

    def test_buildx_build_streams_output_to_progress(self):
        from luma.agent import _docker_buildx_build
        from luma.local import LocalResult

        progress: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docker_config = root / "docker-config"
            docker_config.mkdir()
            context_dir = root / "src"
            context_dir.mkdir()
            dockerfile = context_dir / "Dockerfile"
            dockerfile.write_text("FROM busybox\n", encoding="utf-8")

            captured_command = []

            def fake_stream(command, **kwargs):
                captured_command.extend(command)
                kwargs["on_line"]("#1 [internal] load build definition")
                kwargs["on_line"]("#2 pushing layers")
                return LocalResult(code=0, output="#1 [internal] load build definition\n#2 pushing layers\n")

            with patch.dict(os.environ, {"LUMA_BUILDX_CONFIG": str(root / "buildx")}), patch(
                "luma.agent._run_process_streaming", side_effect=fake_stream
            ) as stream:
                image = _docker_buildx_build(
                    docker="docker",
                    builder="luma-builder",
                    docker_config=docker_config,
                    push_host="localhost:5000",
                    registry_host="100.64.0.70:5000",
                    repo="acme/app",
                    sha="abc123",
                    context_dir=context_dir,
                    dockerfile_path=dockerfile,
                    platform="linux/amd64",
                    proxy="",
                    build_timeout=1800,
                    progress=lambda event: progress.append(event),
                )

        self.assertEqual(image, "100.64.0.70:5000/acme/app:abc123")
        self.assertEqual(stream.call_args.kwargs["heartbeat_interval"], 15.0)
        self.assertEqual(
            stream.call_args.kwargs["heartbeat_message"],
            "Docker image build is still running",
        )
        self.assertNotIn("--push", captured_command)
        output_index = captured_command.index("--output")
        self.assertEqual(
            captured_command[output_index + 1],
            "type=image,push=true,registry.insecure=true",
        )
        self.assertEqual([event["line"] for event in progress], ["#1 [internal] load build definition", "#2 pushing layers"])

    def test_streaming_process_emits_idle_heartbeats(self):
        import sys

        from luma.agent import _run_process_streaming

        progress: list[str] = []
        result = _run_process_streaming(
            [sys.executable, "-c", "import time; time.sleep(0.35)"],
            timeout=2,
            on_line=progress.append,
            heartbeat_interval=0.05,
            heartbeat_message="Long operation is still running",
        )

        self.assertEqual(result.code, 0)
        self.assertTrue(progress)
        self.assertTrue(
            all(line.startswith("Long operation is still running (") for line in progress)
        )
        self.assertLessEqual(len(progress), 4)

    def test_registry_serve_configures_insecure_registry_and_docker_no_proxy(self):
        from luma.control.server import handle_registry_serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "cn",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                    "cn-2": {
                        "name": "cn-2",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {
                            "status": "online",
                            "lastSeen": now,
                            "os": "linux",
                            "capabilities": [],
                            "diagnostics": {"docker": {"proxy": {"http": "http://10.0.0.2:7890", "noProxy": "localhost,127.0.0.1"}}},
                        },
                    },
                }
                save_state(state)
                calls = []
                events = []

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    calls.append((node_name, action, payload))
                    events.append(("agent", node_name, action))
                    return {"message": f"{action} ok"}

                def fake_deployment(_token, _body, **_kwargs):
                    events.append(("deploy", "luma-registry"))
                    return {"service": "luma-registry", "steps": []}

                with patch("luma.control.server.handle_deployment", side_effect=fake_deployment), patch(
                    "luma.control.server._run_node_agent_task", side_effect=fake_run_task
                ):
                    result = handle_registry_serve(state["deployToken"], {"node": "builder"})

                self.assertEqual(load_state()["managedRegistryTransports"]["100.64.0.70:5000"], "http")
                self.assertEqual(result["registryHost"], "100.64.0.70:5000")
                tecent_no_proxy = [
                    payload["noProxy"]
                    for node_name, action, payload in calls
                    if node_name == "cn-2" and action == "configure-docker-egress-proxy"
                ][0]
                self.assertIn("100.64.0.70:5000", tecent_no_proxy)
                self.assertIn("100.64.0.70", tecent_no_proxy)
                self.assertIn("100.64.0.0/10", tecent_no_proxy)
                tecent_proxy = [
                    payload["proxy"]
                    for node_name, action, payload in calls
                    if node_name == "cn-2" and action == "configure-docker-egress-proxy"
                ][0]
                self.assertEqual(tecent_proxy, "http://10.0.0.2:7890")
                self.assertIn(("cn-2", "configure-insecure-registry", {"registry": "100.64.0.70:5000"}), calls)
                deploy_index = events.index(("deploy", "luma-registry"))
                self.assertTrue(events[:deploy_index])
                self.assertTrue(all(event[0] == "agent" for event in events[:deploy_index]))
                self.assertFalse(any(event[0] == "agent" for event in events[deploy_index + 1 :]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_docker_restart_recreates_and_waits_for_affected_nomad_allocations(self):
        from luma.control.server import _reconcile_allocations_after_docker_restart

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("defaults:\n  nomadAddr: http://127.0.0.1:4646\n", encoding="utf-8")
                state = {
                    "clusterId": "luma-test",
                    "nomadToken": "nomad-token",
                    "nodes": {
                        "cn-2": {
                            "name": "cn-2",
                            "nomadNodeId": "node-cn-2",
                            "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": []},
                        }
                    },
                }
                job_reads = 0
                calls = []

                def request(_client, method, path, body=None):
                    nonlocal job_reads
                    calls.append((method, path, body))
                    if (method, path) == ("GET", "/v1/allocation/alloc-old"):
                        return {
                            "ID": "alloc-old",
                            "JobID": "api",
                            "TaskGroup": "api",
                            "NodeID": "node-cn-2",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                        }
                    if (method, path) == ("GET", "/v1/job/api/allocations"):
                        job_reads += 1
                        if job_reads == 1:
                            return [{"ID": "alloc-old", "TaskGroup": "api", "ClientStatus": "running", "DesiredStatus": "run"}]
                        return [{"ID": "alloc-new", "TaskGroup": "api", "ClientStatus": "running", "DesiredStatus": "run"}]
                    if (method, path) == ("POST", "/v1/allocation/alloc-old/stop"):
                        return {}
                    raise AssertionError((method, path, body))

                with patch("luma.control.server.NomadApi.request", request):
                    result = _reconcile_allocations_after_docker_restart(state, "cn-2", {"alloc-old"}, timeout=2)

                self.assertEqual(result["recreatedAllocationIds"], ["alloc-old"])
                self.assertEqual(result["jobs"], ["api"])
                self.assertIn(("POST", "/v1/allocation/alloc-old/stop", None), calls)
                self.assertGreaterEqual(job_reads, 2)
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_docker_restart_reconcile_rejects_allocation_from_another_node(self):
        from luma.control.server import _reconcile_allocations_after_docker_restart

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("defaults:\n  nomadAddr: http://127.0.0.1:4646\n", encoding="utf-8")
                state = {
                    "nomadToken": "nomad-token",
                    "nodes": {
                        "cn-2": {"name": "cn-2", "nomadNodeId": "node-cn-2"},
                        "home-2": {"name": "home-2", "nomadNodeId": "node-home-2"},
                    },
                }
                allocation = {
                    "ID": "alloc-other",
                    "JobID": "api",
                    "TaskGroup": "api",
                    "NodeID": "node-home-2",
                    "ClientStatus": "running",
                    "DesiredStatus": "run",
                }
                with patch("luma.control.server.NomadApi.request", return_value=allocation), self.assertRaisesRegex(
                    LumaError, "not cn-2"
                ):
                    _reconcile_allocations_after_docker_restart(state, "cn-2", {"alloc-other"}, timeout=1)
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_configure_insecure_registry_only_restarts_docker_when_config_changes(self):
        from luma.agent import configure_insecure_registry

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=1\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-b", "alloc-a"}) as active_allocs:
            result = configure_insecure_registry(registry="100.64.0.70:5000")

        script = executor.sudo.call_args.args[0]
        self.assertIn("changed=$(python3", script)
        self.assertIn("changed = host not in regs", script)
        self.assertIn('if [ "$changed" = "1" ]; then systemctl restart docker; fi', script)
        self.assertTrue(result["changed"])
        self.assertTrue(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], ["alloc-a", "alloc-b"])
        active_allocs.assert_called_once_with(executor)

    def test_configure_insecure_registry_reports_no_restart_when_unchanged(self):
        from luma.agent import configure_insecure_registry

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=0\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-a"}):
            result = configure_insecure_registry(registry="100.64.0.70:5000")

        self.assertFalse(result["changed"])
        self.assertFalse(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], [])
        self.assertIn("already configured", result["message"])

    def test_configure_docker_egress_proxy_only_restarts_when_content_changes(self):
        from luma.agent import configure_docker_egress_proxy

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=1\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-b", "alloc-a"}) as active_allocs:
            result = configure_docker_egress_proxy(
                proxy="http://10.0.0.2:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        script = executor.sudo.call_args.args[0]
        self.assertIn('cmp -s "$tmp" "$f"', script)
        self.assertIn('if [ -f "$f" ] && cmp -s "$tmp" "$f"', script)
        self.assertEqual(script.count("systemctl restart docker"), 1)
        self.assertTrue(result["changed"])
        self.assertTrue(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], ["alloc-a", "alloc-b"])
        active_allocs.assert_called_once_with(executor)

    def test_configure_docker_egress_proxy_reports_no_restart_when_content_matches(self):
        from luma.agent import configure_docker_egress_proxy

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=0\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-a"}):
            result = configure_docker_egress_proxy(
                proxy="http://10.0.0.2:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertFalse(result["changed"])
        self.assertFalse(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], [])
        self.assertIn("already configured", result["message"])

    def test_registry_serve_requires_ready_linux_docker_node(self):
        from luma.control.server import handle_registry_serve
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "cn",
                        "tailscaleIP": "100.64.0.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    },
                    "home-2": {
                        "name": "home-2",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    },
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"]}
                save_state(state)

                with self.assertRaisesRegex(LumaError, "docker-image capability"):
                    handle_registry_serve(state["deployToken"], {"node": "home-2"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_registry_serve_secure_domain_uses_tls_auth_without_docker_restarts(self):
        from luma.control.server import handle_registry_serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                now = int(time.time())
                state["nodes"] = {
                    "manager": {
                        "name": "manager",
                        "region": "cn",
                        "tailscaleIP": "100.106.154.3",
                        "labels": {"role.nomad-manager": "true"},
                        "agent": {
                            "status": "online",
                            "lastSeen": now,
                            "os": "linux",
                            "capabilities": ["docker-image"],
                        },
                    }
                }
                save_state(state)
                password = "p" * 64
                deployed = {}

                def fake_deployment(_token, body, **_kwargs):
                    deployed.update(yaml.safe_load(body["manifest"]))
                    return {"service": "registry", "steps": []}

                with patch(
                    "luma.control.server._mirror_registry_runtime_image",
                    return_value="100.64.0.70:5000/luma-system/registry-runtime:test@sha256:"
                    + "a" * 64,
                ), patch(
                    "luma.control.server.handle_deployment",
                    side_effect=fake_deployment,
                ), patch(
                    "luma.control.server._verify_authenticated_registry",
                    return_value={"status": 200, "authenticated": True},
                ), patch(
                    "luma.control.server.handle_registry_set"
                ) as registry_set, patch(
                    "luma.control.server.handle_build_config_set"
                ) as build_config, patch(
                    "luma.control.server._run_node_agent_task"
                ) as agent_task:
                    result = handle_registry_serve(
                        state["deployToken"],
                        {
                            "node": "manager",
                            "domain": "registry.example.com",
                            "username": "lae",
                            "password": password,
                        },
                    )

                self.assertEqual(deployed["exposure"], "cn-edge")
                self.assertEqual(deployed["domain"], "registry.example.com")
                self.assertNotIn("publishPort", deployed)
                serialized = yaml.safe_dump(deployed)
                self.assertIn("basicauth.users=lae:{SHA}", serialized)
                self.assertNotIn(password, serialized)
                agent_task.assert_not_called()
                registry_set.assert_called_once_with(
                    state["deployToken"],
                    {
                        "host": "registry.example.com",
                        "username": "lae",
                        "password": password,
                    },
                )
                build_config.assert_called_once_with(
                    state["deployToken"],
                    {
                        "registryHost": "registry.example.com",
                        "pushHost": "registry.example.com",
                    },
                )
                self.assertTrue(result["secure"])
                self.assertTrue(result["authenticated"])
                self.assertTrue(result["activated"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_egress_proxy_for_cn_build_node_uses_manager_gateway(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        state = {"nodes": {"build-1": {"region": "cn", "agent": {"capabilities": ["docker-build"]}}}}
        self.assertEqual(_egress_proxy_for_node(config, state, "build-1"), "http://100.64.0.1:7890")

    def test_egress_proxy_for_global_build_node_is_empty(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        state = {"nodes": {"build-2": {"region": "global", "agent": {"capabilities": ["docker-build"]}}}}
        self.assertEqual(_egress_proxy_for_node(config, state, "build-2"), "")

    def test_egress_proxy_for_unknown_node_is_empty(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        self.assertEqual(_egress_proxy_for_node(config, {"nodes": {}}, "missing"), "")

    def test_build_proxy_request_missing_uses_auto_policy(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node", return_value="http://manager:7890") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {})

        self.assertEqual(proxy, "http://manager:7890")
        auto.assert_called_once()

    def test_build_proxy_request_explicit_empty_is_direct(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {"proxy": ""})

        self.assertEqual(proxy, "")
        auto.assert_not_called()

    def test_build_proxy_request_direct_mode_is_direct_and_validated(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {"proxyMode": "direct"})

        self.assertEqual(proxy, "")
        auto.assert_not_called()
        with self.assertRaisesRegex(LumaError, "proxyMode must be auto or direct"):
            _build_proxy_for_request(Mock(), {}, "builder", {"proxyMode": "invalid"})



if __name__ == "__main__":
    unittest.main()
