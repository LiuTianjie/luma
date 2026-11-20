from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BUILD_ROOT = Path(__file__).resolve().parents[1]
if str(BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(BUILD_ROOT))

from starlette.testclient import TestClient  # noqa: E402

from luma_build.cli import BuildApiClient, main as cli_main  # noqa: E402
from luma_build.deployer import ControlAuthVerifier, extract_manifest_platforms  # noqa: E402
from luma_build.errors import BuildCanceled, BuildError  # noqa: E402
from luma_build.registry_naming import (  # noqa: E402
    derive_cache_variant,
    derive_repo_slug,
    format_cache_reference,
    format_image_repository,
    is_http_registry,
    merge_no_proxy,
    normalize_registry_host,
    normalize_repo_url,
    registry_no_proxy_entries,
    validate_git_ref,
)
from luma_build.runner import (  # noqa: E402
    PlatformRequeueRequired,
    _clone_repo,
    _discover_manifest,
    _ensure_runner_buildx,
    _prepare_git_contexts,
    _render_buildkitd_toml,
    _run_buildx_command,
    _url_bypasses_proxy,
    execute_build_task,
)
from luma_build.server import create_app  # noqa: E402


ADMIN_TOKEN = "admin-secret-token-1234567890"


class RegistryAndCacheNamingTests(unittest.TestCase):
    def test_standalone_runner_stops_on_deleted_token(self) -> None:
        import contextlib
        import io
        import urllib.error
        from unittest.mock import Mock
        from luma_build.runner import RunnerDaemon
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://build.test", 401, "Unauthorized", {}, None)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("luma_build.runner.find_docker_binary", return_value="docker"), \
             patch("luma_build.runner.capture_docker_runtime", return_value={}), \
             patch("luma_build.runner.detect_native_engine_platform", return_value="linux/amd64"), \
             patch("luma_build.runner.urllib.request.build_opener", return_value=opener), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner = RunnerDaemon(server_url="https://build.test", runner_token="lbr_test", state_dir=Path(tmp))
            self.assertEqual(runner.run_once_or_forever(), 0)
            self.assertTrue(runner.credential_rejected)
            self.assertTrue(runner.stop_event.is_set())
            self.assertEqual(opener.open.call_count, 1)

    def test_http_vs_https_registry_detection(self) -> None:
        self.assertTrue(is_http_registry("localhost:5000"))
        self.assertTrue(is_http_registry("127.0.0.1:5000"))
        self.assertTrue(is_http_registry("100.66.177.70:5000"))
        self.assertTrue(is_http_registry("192.168.1.20:5000"))
        self.assertFalse(is_http_registry("crpi-abc123.cn-beijing.personal.cr.aliyuncs.com"))
        self.assertFalse(is_http_registry("ghcr.io"))
        self.assertTrue(is_http_registry("registry.internal.example.com", explicit_insecure=True))

    def test_aliyun_acr_two_level_flattening_and_single_cache_repo(self) -> None:
        slug = derive_repo_slug("https://git.example.com/MyOrg/Web_App.git")
        self.assertEqual(slug, "myorg/web_app")

        # Legacy internal registry preserves multi-segment path when namespace is empty
        self.assertEqual(
            format_image_repository(repo_slug=slug, service_name="api", single_service=False),
            "myorg/web_app/api",
        )

        # Aliyun ACR with namespace='luma' strictly flattens to 2 segments (<ns>/<repo>)
        acr_repo = format_image_repository(
            repo_slug=slug, service_name="api", single_service=False, namespace="luma"
        )
        self.assertEqual(acr_repo, "luma/myorg-web-app-api")
        self.assertEqual(acr_repo.count("/"), 1)

        # All project caches share a single luma-buildcache repository to save ACR repo quota
        cache_ref = format_cache_reference(
            push_host="crpi-abc.cn-beijing.personal.cr.aliyuncs.com",
            repo_slug=slug,
            service_name="api",
            platform="linux/arm64",
            namespace="luma",
        )
        self.assertEqual(
            cache_ref,
            "crpi-abc.cn-beijing.personal.cr.aliyuncs.com/luma/luma-buildcache:myorg-web-app-api-linux-arm64",
        )


class DecoupledBuildServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.app = create_app(
            data_dir=self.data_dir,
            admin_token=ADMIN_TOKEN,
            control_url="https://luma.example.com",
            control_deploy_token="ctl-deploy-token",
            default_registry_host="crpi-test.cn-beijing.personal.cr.aliyuncs.com",
            default_push_host="crpi-test.cn-beijing.personal.cr.aliyuncs.com",
            default_registry_namespace="luma",
            affinity_wait_seconds=20,
            start_worker=False,
        )
        self.client = TestClient(self.app)
        self.admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def create_runner(self, name: str, *, weight: int = 100, cache_mode: str = "read-write") -> str:
        resp = self.client.post(
            "/v1/runners/tokens",
            headers=self.admin_headers,
            json={"name": name, "weight": weight, "cacheMode": cache_mode},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return str(resp.json()["runner"]["runnerToken"])

    def poll(self, token: str, *, platform: str = "linux/amd64") -> dict:
        resp = self.client.post(
            "/v1/runner/lease",
            headers={"Authorization": f"Bearer {token}"},
            json={"nativePlatform": platform, "os": "linux", "arch": platform.split("/")[1], "waitSeconds": 0},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_runner_token_is_strictly_isolated_from_admin_and_credentials(self) -> None:
        rtok = self.create_runner("m4")
        for method, path in (
            ("GET", "/v1/status"),
            ("GET", "/v1/credentials/git"),
            ("POST", "/v1/runners/tokens"),
            ("GET", "/v1/builds"),
        ):
            res = self.client.request(method, path, headers={"Authorization": f"Bearer {rtok}"})
            self.assertEqual(res.status_code, 401)

        # Revoking immediately blocks runner lease polling
        rev = self.client.post("/v1/runners/m4/revoke", headers=self.admin_headers)
        self.assertEqual(rev.status_code, 200)
        denied = self.client.post(
            "/v1/runner/lease",
            headers={"Authorization": f"Bearer {rtok}"},
            json={"nativePlatform": "linux/arm64"},
        )
        self.assertEqual(denied.status_code, 401)

    def test_join_authenticates_without_leasing_or_revealing_credentials(self) -> None:
        token = self.create_runner("join-existing")
        store = self.app.state.store
        build = store.enqueue_build({"repoUrl": "https://git.example.com/acme/join.git", "platform": "linux/amd64"})
        identity = self.client.post("/v1/runner/join", headers={"Authorization": f"Bearer {token}"}, json={})
        self.assertEqual(identity.status_code, 200, identity.text)
        self.assertEqual(identity.json()["runner"]["name"], "join-existing")
        self.assertFalse(identity.json()["runner"]["online"])
        self.assertEqual(store.get_build(build["id"])["status"], "queued")
        self.assertNotIn(token, identity.text)
        self.assertNotIn("task", identity.json())
        self.assertEqual(self.client.post("/v1/runner/join", headers=self.admin_headers).status_code, 401)

    def test_delete_removes_runner_cancels_affected_builds_and_keeps_history(self) -> None:
        token = self.create_runner("delete-me")
        other = self.create_runner("keep-me")
        store = self.app.state.store
        building = store.enqueue_build({"repoUrl": "https://git.example.com/acme/active.git", "platform": "linux/amd64", "runner": "delete-me"})
        self.assertEqual(self.poll(token)["task"]["buildId"], building["id"])
        pinned = store.enqueue_build({"repoUrl": "https://git.example.com/acme/pinned.git", "platform": "linux/amd64", "runner": "delete-me"})
        generic = store.enqueue_build({"repoUrl": "https://git.example.com/acme/generic.git", "platform": "linux/amd64"})
        history = store.enqueue_build({"repoUrl": "https://git.example.com/acme/history.git", "platform": "linux/amd64"})
        deployment = store.enqueue_build({"repoUrl": "https://git.example.com/acme/deploy.git", "platform": "linux/amd64"})
        with store._conn(write=True) as conn:
            conn.execute("UPDATE builds SET status='succeeded', runner_name='delete-me' WHERE id=?", (history["id"],))
            conn.execute("UPDATE builds SET status='queued_deploy', runner_name='delete-me' WHERE id=?", (deployment["id"],))
        self.assertEqual(self.client.delete("/v1/runners/delete-me", headers={"Authorization": f"Bearer {other}"}).status_code, 401)
        deleted = self.client.delete("/v1/runners/delete-me", headers=self.admin_headers)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual([r["name"] for r in store.list_runners()], ["keep-me"])
        self.assertEqual(store.get_build(building["id"])["status"], "canceled")
        self.assertEqual(store.get_build(pinned["id"])["status"], "canceled")
        self.assertEqual(store.get_build(generic["id"])["status"], "queued")
        self.assertEqual(store.get_build(history["id"])["status"], "succeeded")
        self.assertEqual(store.get_build(deployment["id"])["status"], "queued_deploy")
        self.assertEqual(self.client.post("/v1/runner/join", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_old_revoked_rows_are_removed_on_upgrade(self) -> None:
        self.create_runner("old-revoked")
        self.create_runner("still-present")
        store = self.app.state.store
        with store._conn(write=True) as conn:
            conn.execute("UPDATE runners SET revoked=1 WHERE name='old-revoked'")
        store._init_schema()
        self.assertEqual([r["name"] for r in store.list_runners()], ["still-present"])

    def test_credentials_leased_only_in_memory_and_env_secrets_never_sent_to_runner(self) -> None:
        self.client.post(
            "/v1/credentials/git",
            headers=self.admin_headers,
            json={"id": "gitea:corp", "host": "git.example.com", "username": "bot", "token": "git-secret-pat"},
        )
        self.client.post(
            "/v1/credentials/registry",
            headers=self.admin_headers,
            json={
                "host": "crpi-test.cn-beijing.personal.cr.aliyuncs.com",
                "username": "aliyun-user",
                "password": "aliyun-push-password",
            },
        )
        rtok = self.create_runner("cloud-1", weight=200)

        enq = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={
                "repoUrl": "https://git.example.com/acme/orders.git",
                "ref": "main",
                "platform": "linux/amd64",
                "envSecrets": {"DATABASE_URL": "postgres://super-secret-prod"},
            },
        )
        self.assertEqual(enq.status_code, 200, enq.text)
        build_id = enq.json()["build"]["id"]

        # Public build detail never exposes secrets
        detail_text = json.dumps(self.client.get(f"/v1/builds/{build_id}", headers=self.admin_headers).json())
        self.assertNotIn("super-secret-prod", detail_text)
        self.assertNotIn("git-secret-pat", detail_text)

        leased = self.poll(rtok, platform="linux/amd64")
        task = leased["task"]
        self.assertIsNotNone(task)
        self.assertEqual(task["gitAuth"], {"username": "bot", "token": "git-secret-pat"})
        self.assertEqual(task["registryAuths"][0]["password"], "aliyun-push-password")
        self.assertFalse(task["pushInsecure"])
        # Runtime application secrets MUST NOT be sent to the runner
        self.assertNotIn("super-secret-prod", json.dumps(task))

        # Runner finishes image push -> immediately released while deploy runs on server!
        comp = self.client.post(
            "/v1/runner/complete",
            headers={"Authorization": f"Bearer {rtok}"},
            json={
                "buildId": build_id,
                "status": "succeeded",
                "result": {
                    "kind": "service",
                    "image": "crpi-test.cn-beijing.personal.cr.aliyuncs.com/luma/acme-orders@sha256:" + "a" * 64,
                    "commit": "b" * 40,
                    "manifest": "name: orders\nregion: cn\nexposure: none\nbuild:\n  context: .\n",
                },
            },
        )
        self.assertEqual(comp.status_code, 200)
        self.assertEqual(comp.json()["build"]["status"], "queued_deploy")

        captured_deploy: dict = {}

        def fake_deploy(**kwargs):
            captured_deploy.update(kwargs)
            kwargs["on_step"]("[ok] Nomad rollout: healthy")
            return {"service": "orders", "revision": 7}

        with patch.object(self.app.state.deployer, "deploy_built_artifact", side_effect=fake_deploy):
            self.assertTrue(self.app.state.deploy_worker.run_pending_once())

        self.assertEqual(captured_deploy["env_secrets"], {"DATABASE_URL": "postgres://super-secret-prod"})
        final = self.client.get(f"/v1/builds/{build_id}", headers=self.admin_headers).json()["build"]
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["result"]["deployment"]["service"], "orders")

    def test_native_arm_routing_cache_affinity_and_branch_supersede(self) -> None:
        amd_1 = self.create_runner("amd-low", weight=50)
        amd_2 = self.create_runner("amd-high", weight=200)
        arm_m4 = self.create_runner("m4", weight=100, cache_mode="read-only")

        # Bring all three online with initial heartbeats
        self.assertIsNone(self.poll(amd_1, platform="linux/amd64")["task"])
        self.assertIsNone(self.poll(amd_2, platform="linux/amd64")["task"])
        self.assertIsNone(self.poll(arm_m4, platform="linux/arm64")["task"])

        # 1) ARM build can ONLY be leased by native ARM runner (m4), never amd_2
        arm_id = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/ios-worker", "platform": "linux/arm64", "deploy": False},
        ).json()["build"]["id"]
        self.assertIsNone(self.poll(amd_2, platform="linux/amd64")["task"])
        leased_arm = self.poll(arm_m4, platform="linux/arm64")["task"]
        self.assertEqual(leased_arm["buildId"], arm_id)
        self.assertEqual(leased_arm["cacheMode"], "read-only")

        # 2) Cold AMD64 build prefers higher-weight idle runner (amd_2 over amd_1)
        b1 = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/web", "ref": "main", "platform": "linux/amd64", "deploy": False},
        ).json()["build"]["id"]
        self.assertIsNone(self.poll(amd_1, platform="linux/amd64")["task"])
        self.assertEqual(self.poll(amd_2, platform="linux/amd64")["task"]["buildId"], b1)

        # Complete on amd_1 for another repo to establish warm cache affinity on amd_1
        b_warm = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/cache-demo", "runner": "amd-low", "platform": "linux/amd64", "deploy": False},
        ).json()["build"]["id"]
        self.assertEqual(self.poll(amd_1, platform="linux/amd64")["task"]["buildId"], b_warm)
        self.client.post(
            "/v1/runner/complete",
            headers={"Authorization": f"Bearer {amd_1}"},
            json={"buildId": b_warm, "status": "succeeded", "result": {"image": "x"}},
        )

        # Next unpinned build of acme/cache-demo prefers warm amd-low even though amd-high has higher weight!
        b_next = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/cache-demo", "platform": "linux/amd64", "deploy": False},
        ).json()["build"]["id"]
        self.assertIsNone(self.poll(amd_2, platform="linux/amd64")["task"])
        self.assertEqual(self.poll(amd_1, platform="linux/amd64")["task"]["buildId"], b_next)

        # While warm amd-low is BUSY, another idle runner claims a new branch build IMMEDIATELY (zero queueing!)
        amd_3 = self.client.post(
            "/v1/runners/tokens",
            headers=self.admin_headers,
            json={"name": "amd-spillover", "weight": 150},
        ).json()["runner"]["runnerToken"]
        self.poll(amd_3, platform="linux/amd64")
        b_spill = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/cache-demo", "ref": "feat-fast", "platform": "linux/amd64", "deploy": False},
        ).json()["build"]["id"]
        self.assertEqual(self.poll(amd_3, platform="linux/amd64")["task"]["buildId"], b_spill)

        # 3) Submitting a newer commit on the same branch requests cancellation of the active build
        b_newer = self.client.post(
            "/v1/builds",
            headers=self.admin_headers,
            json={"repository": "acme/cache-demo", "platform": "linux/amd64", "deploy": False},
        ).json()["build"]["id"]
        hb = self.client.post(
            "/v1/runner/heartbeat",
            headers={"Authorization": f"Bearer {amd_1}"},
            json={"buildId": b_next},
        ).json()
        self.assertTrue(hb["cancelRequested"])
        self.assertNotEqual(b_newer, b_next)

    def test_builds_list_pagination_search_and_summary_blob_stripping(self) -> None:
        rtok = self.create_runner("pager-runner")
        ids: list[str] = []
        for idx, (repo, ref) in enumerate(
            [
                ("acme/alpha", "main"),
                ("acme/beta", "release-v1"),
                ("acme/gamma", "main"),
                ("acme/delta", "feat-search-ref"),
                ("acme/epsilon", "main"),
            ]
        ):
            res = self.client.post(
                "/v1/builds",
                headers=self.admin_headers,
                json={"repository": repo, "ref": ref, "platform": "linux/amd64", "deploy": False},
            )
            self.assertEqual(res.status_code, 200, res.text)
            ids.append(res.json()["build"]["id"])

        # Complete the oldest build with large manifest/composeContent blobs in result
        leased = self.poll(rtok, platform="linux/amd64")["task"]
        self.assertEqual(leased["buildId"], ids[0])
        comp = self.client.post(
            "/v1/runner/complete",
            headers={"Authorization": f"Bearer {rtok}"},
            json={
                "buildId": ids[0],
                "leaseId": leased["leaseId"],
                "status": "succeeded",
                "result": {
                    "kind": "compose",
                    "shortSha": "abc1234",
                    "image": "registry.test/acme-alpha:1",
                    "manifest": "name: alpha\n" + ("# padding\n" * 200),
                    "composeContent": "services:\n  web:\n    image: x\n" + ("# compose\n" * 200),
                },
            },
        )
        self.assertEqual(comp.status_code, 200, comp.text)

        # Page 1 via offset
        p1 = self.client.get("/v1/builds?limit=2&offset=0", headers=self.admin_headers)
        self.assertEqual(p1.status_code, 200, p1.text)
        body1 = p1.json()
        self.assertEqual(body1["total"], 5)
        self.assertEqual(body1["limit"], 2)
        self.assertEqual(body1["offset"], 0)
        self.assertTrue(body1["hasMore"])
        self.assertEqual([b["id"] for b in body1["builds"]], [ids[4], ids[3]])

        # Page 2 via page number
        p2 = self.client.get("/v1/builds?limit=2&page=2", headers=self.admin_headers)
        self.assertEqual(p2.status_code, 200, p2.text)
        body2 = p2.json()
        self.assertEqual(body2["total"], 5)
        self.assertEqual(body2["offset"], 2)
        self.assertTrue(body2["hasMore"])
        self.assertEqual([b["id"] for b in body2["builds"]], [ids[2], ids[1]])

        # Page 3 (last page) strips large blobs in list summary while detail retains them
        p3 = self.client.get("/v1/builds?limit=2&page=3", headers=self.admin_headers)
        self.assertEqual(p3.status_code, 200, p3.text)
        body3 = p3.json()
        self.assertEqual(body3["total"], 5)
        self.assertFalse(body3["hasMore"])
        self.assertEqual([b["id"] for b in body3["builds"]], [ids[0]])
        list_result = body3["builds"][0]["result"]
        self.assertEqual(list_result["shortSha"], "abc1234")
        self.assertNotIn("manifest", list_result)
        self.assertNotIn("composeContent", list_result)

        detail = self.client.get(f"/v1/builds/{ids[0]}", headers=self.admin_headers).json()["build"]
        self.assertIn("manifest", detail["result"])
        self.assertIn("composeContent", detail["result"])

        # Search by branch ref as well as repo slug
        by_ref = self.client.get("/v1/builds?repo=feat-search-ref", headers=self.admin_headers).json()
        self.assertEqual(by_ref["total"], 1)
        self.assertEqual([b["id"] for b in by_ref["builds"]], [ids[3]])

        # Invalid pagination parameters rejected cleanly
        self.assertEqual(self.client.get("/v1/builds?offset=-1", headers=self.admin_headers).status_code, 400)
        self.assertEqual(self.client.get("/v1/builds?page=0", headers=self.admin_headers).status_code, 400)


class RunnerBuildKitExecutionTests(unittest.TestCase):
    def test_buildkitd_toml_and_two_tier_cache_flags(self) -> None:
        # HTTPS Aliyun ACR emits NO insecure registry stanza
        https_toml = _render_buildkitd_toml(http_hosts=[], cache_keep_gb=25, max_parallelism=4)
        self.assertIn("keepBytes = 26843545600", https_toml)
        self.assertIn("max-parallelism = 4", https_toml)
        self.assertNotIn("http = true", https_toml)

        # Tailscale HTTP registry gets explicit http = true stanza
        http_toml = _render_buildkitd_toml(http_hosts=["100.66.177.70:5000"], cache_keep_gb=10)
        self.assertIn('[registry."100.66.177.70:5000"]', http_toml)
        self.assertIn("http = true", http_toml)
        self.assertNotIn('[registry."docker.io"]', http_toml)

        # Explicit docker.io mirrors are normalized, deduplicated, and rendered safely
        mirror_toml = _render_buildkitd_toml(
            http_hosts=[],
            docker_mirrors=[
                "https://docker.m.daocloud.io/",
                "docker.1ms.run",
                "docker.m.daocloud.io",
                "docker.io",
                "bad mirror/path",
            ],
        )
        self.assertIn('[registry."docker.io"]', mirror_toml)
        self.assertIn('mirrors = ["docker.m.daocloud.io", "docker.1ms.run"]', mirror_toml)

    def test_configured_docker_mirrors_opt_in_and_buildx_no_proxy(self) -> None:
        import os
        import subprocess as sp

        from luma import builder_runner as canonical_runner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_daemon_json = root / "missing-daemon.json"
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    canonical_runner._configured_docker_mirrors(daemon_json_path=missing_daemon_json),
                    [],
                )

            daemon_json = root / "daemon.json"
            daemon_json.write_text(
                json.dumps(
                    {
                        "registry-mirrors": [
                            "https://mirror.ccs.tencentyun.com",
                            "https://docker.m.daocloud.io/",
                            "invalid/with/path",
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LUMA_RUNNER_DOCKER_MIRRORS": "https://docker.m.daocloud.io, docker.1ms.run:443"},
                clear=True,
            ):
                self.assertEqual(
                    canonical_runner._configured_docker_mirrors(daemon_json_path=daemon_json),
                    ["docker.m.daocloud.io", "docker.1ms.run:443", "mirror.ccs.tencentyun.com"],
                )

            bx_dir = root / "bx"
            dc_dir = root / "dc"
            dc_dir.mkdir()
            calls: list[list[str]] = []
            exists = False

            def fake_run(cmd, **kwargs):
                nonlocal exists
                calls.append(list(cmd))
                if cmd[1:3] == ["buildx", "inspect"]:
                    return sp.CompletedProcess(cmd, 0 if exists else 1, stdout="", stderr="")
                if cmd[1:3] == ["buildx", "create"]:
                    exists = True
                    return sp.CompletedProcess(cmd, 0, stdout="luma-runner-egress\n", stderr="")
                return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch.object(canonical_runner, "_DOCKER_DAEMON_JSON_PATH", missing_daemon_json), patch(
                "luma.builder_runner.subprocess.run", side_effect=fake_run
            ):
                with patch.dict(os.environ, {}, clear=True):
                    _ensure_runner_buildx(
                        docker_bin="docker",
                        buildx_state_dir=bx_dir,
                        docker_config=dc_dir,
                        proxy="http://127.0.0.1:7890",
                        no_proxy="localhost,127.0.0.1",
                        http_hosts=[],
                        cache_keep_gb=20,
                        max_parallelism=0,
                    )
                toml_without = (bx_dir / "luma-runner-egress.buildkitd.toml").read_text(encoding="utf-8")
                self.assertNotIn('[registry."docker.io"]', toml_without)

                calls.clear()
                with patch.dict(
                    os.environ,
                    {"LUMA_RUNNER_DOCKER_MIRRORS": "https://docker.m.daocloud.io,mirror.internal:5000"},
                    clear=True,
                ):
                    _ensure_runner_buildx(
                        docker_bin="docker",
                        buildx_state_dir=bx_dir,
                        docker_config=dc_dir,
                        proxy="http://127.0.0.1:7890",
                        no_proxy="localhost,127.0.0.1",
                        http_hosts=[],
                        cache_keep_gb=20,
                        max_parallelism=0,
                    )
                self.assertTrue(any("--keep-state" in c for c in calls))
                create_cmd = [c for c in calls if c[1:3] == ["buildx", "create"]][-1]
                no_proxy_args = [arg for arg in create_cmd if "env.NO_PROXY=" in arg]
                self.assertEqual(len(no_proxy_args), 1)
                self.assertIn("docker.m.daocloud.io", no_proxy_args[0])
                self.assertIn("mirror.internal", no_proxy_args[0])
                toml_with = (bx_dir / "luma-runner-egress.buildkitd.toml").read_text(encoding="utf-8")
                self.assertIn('[registry."docker.io"]', toml_with)
                self.assertIn('mirrors = ["docker.m.daocloud.io", "mirror.internal:5000"]', toml_with)

    def test_execute_build_task_injects_remote_cache_and_pins_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured_cmds: list[list[str]] = []

            def fake_clone(*, repo_url, ref, dest, git_auth, proxy=""):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
                (dest / ".luma.yml").write_text("name: demo\nregion: cn\nexposure: none\n", encoding="utf-8")
                return "123456abcdef", "123456abcdef" + "0" * 28

            def fake_buildx(**kwargs):
                captured_cmds.append(
                    [
                        kwargs["cache_mode"],
                        kwargs["cache_ref"],
                        str(kwargs["push_insecure"]),
                        kwargs["image_repo"],
                    ]
                )
                return (
                    f"{kwargs['pull_host']}/{kwargs['image_repo']}@sha256:" + "f" * 64,
                    f"{kwargs['pull_host']}/{kwargs['image_repo']}:{kwargs['tag']}",
                )

            with patch("luma_build.runner._clone_repo", side_effect=fake_clone), patch(
                "luma_build.runner._ensure_runner_buildx", return_value="luma-runner"
            ), patch("luma_build.runner._run_buildx_command", side_effect=fake_buildx):
                res = execute_build_task(
                    {
                        "repoUrl": "https://github.com/acme/demo.git",
                        "repoSlug": "acme/demo",
                        "ref": "main",
                        "registryHost": "crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                        "pushHost": "crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                        "registryNamespace": "luma",
                        "platforms": ["linux/amd64"],
                        "cacheMode": "read-write",
                        "pushInsecure": False,
                        "pullInsecure": False,
                    },
                    docker_bin="docker",
                    native_platform="linux/amd64",
                    buildx_state_dir=root / "buildx",
                )
            self.assertEqual(res["kind"], "service")
            self.assertTrue(res["image"].endswith("@sha256:" + "f" * 64))
            self.assertEqual(
                captured_cmds[0],
                [
                    "read-write",
                    "crpi-x.cn-beijing.personal.cr.aliyuncs.com/luma/luma-buildcache:acme-demo-linux-amd64",
                    "False",
                    "luma/acme-demo",
                ],
            )

            # ARM build rejected on amd64 runner before touching Docker
            with self.assertRaisesRegex(BuildError, "ARM QEMU emulation is disabled"):
                execute_build_task(
                    {
                        "repoUrl": "https://github.com/acme/demo.git",
                        "repoSlug": "acme/demo",
                        "registryHost": "100.66.177.70:5000",
                        "platforms": ["linux/arm64"],
                    },
                    docker_bin="docker",
                    native_platform="linux/amd64",
                    buildx_state_dir=root / "buildx",
                )

    def test_compose_multi_service_build_and_shared_image_alias_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_clone(*, repo_url, ref, dest, git_auth, proxy=""):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
                (dest / "luma.compose.yml").write_text(
                    "name: stack-demo\ncompose: docker-compose.yml\nregion: cn\n", encoding="utf-8"
                )
                (dest / "docker-compose.yml").write_text(
                    "services:\n"
                    "  web:\n"
                    "    build: .\n"
                    "    image: app:local\n"
                    "  worker:\n"
                    "    image: app:local\n"
                    "  redis:\n"
                    "    image: redis:7\n",
                    encoding="utf-8",
                )
                return "abcdef123456", "abcdef123456" + "0" * 28

            def fake_buildx(**kwargs):
                digest_ref = f"{kwargs['pull_host']}/{kwargs['image_repo']}@sha256:" + "d" * 64
                return digest_ref, f"{kwargs['pull_host']}/{kwargs['image_repo']}:{kwargs['tag']}"

            with patch("luma_build.runner._clone_repo", side_effect=fake_clone), patch(
                "luma_build.runner._ensure_runner_buildx", return_value="luma-runner"
            ), patch("luma_build.runner._run_buildx_command", side_effect=fake_buildx):
                res = execute_build_task(
                    {
                        "repoUrl": "https://github.com/acme/stack.git",
                        "repoSlug": "acme/stack",
                        "ref": "main",
                        "registryHost": "crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                        "pushHost": "crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                        "registryNamespace": "luma",
                        "platforms": ["linux/amd64"],
                        "cacheMode": "read-only",
                    },
                    docker_bin="docker",
                    native_platform="linux/amd64",
                    buildx_state_dir=root / "buildx",
                )
            self.assertEqual(res["kind"], "compose")
            expected_digest = "crpi-x.cn-beijing.personal.cr.aliyuncs.com/luma/acme-stack@sha256:" + "d" * 64
            self.assertEqual(res["images"]["web"], expected_digest)
            # Sibling 'worker' sharing 'app:local' is rewritten to the exact immutable digest; 'redis:7' untouched
            self.assertIn(f"image: {expected_digest}", res["composeContent"])
            self.assertIn("image: redis:7", res["composeContent"])


class LumaNativeCredentialsAndSSOTests(unittest.TestCase):
    def test_control_sso_without_separate_admin_token_and_luma_scoped_env_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            env_patch = {
                "LUMA_BUILD_REGISTRY_USERNAME": "acr-push-user",
                "LUMA_BUILD_REGISTRY_PASSWORD": "acr-push-secret-value",
                "GITEA_HOST": "git.example.com",
                "GITEA_USERNAME": "ci-bot",
                "GITEA_TOKEN": "gitea-scoped-secret",
                "GITHUB_TOKEN": "ghp-scoped-secret",
            }
            verify_calls: list[str] = []

            class FakeResp:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self) -> bytes:
                    return b'{"clusterId": "luma-demo1234", "endpoint": "luma.example.com"}'

            class FakeOpener:
                def open(self, req, timeout=8):
                    auth = req.get_header("Authorization") or ""
                    verify_calls.append(auth)
                    if auth == "Bearer valid-luma-deploy-token":
                        return FakeResp()
                    import urllib.error

                    raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", req.headers, None)

            with patch.dict("os.environ", env_patch, clear=False), patch.object(
                ControlAuthVerifier, "_opener", return_value=FakeOpener()
            ):
                app = create_app(
                    data_dir=data_dir,
                    admin_token="",
                    control_url="https://luma.example.com",
                    control_deploy_token="valid-luma-deploy-token",
                    default_registry_host="crpi-bj.cn-beijing.personal.cr.aliyuncs.com",
                    default_push_host="crpi-bj.cn-beijing.personal.cr.aliyuncs.com",
                    default_registry_namespace="luma",
                    start_worker=False,
                )
                client = TestClient(app)

                # Invalid token is rejected with 401
                bad = client.get("/v1/status", headers={"Authorization": "Bearer wrong-token"})
                self.assertEqual(bad.status_code, 401)

                # Valid Luma login token succeeds via SSO and caches the SHA-256 digest!
                ok1 = client.get("/v1/status", headers={"Authorization": "Bearer valid-luma-deploy-token"})
                self.assertEqual(ok1.status_code, 200, ok1.text)
                self.assertEqual(ok1.json()["authMode"], "control-sso")
                ok2 = client.get("/v1/status", headers={"Authorization": "Bearer valid-luma-deploy-token"})
                self.assertEqual(ok2.status_code, 200)
                # Second call hit the in-memory digest cache without calling Control again
                self.assertEqual(verify_calls, ["Bearer wrong-token", "Bearer valid-luma-deploy-token"])

                # Issue a runner token and enqueue a Gitea build WITHOUT manually calling git-cred or registry-cred!
                rtok = client.post(
                    "/v1/runners/tokens",
                    headers={"Authorization": "Bearer valid-luma-deploy-token"},
                    json={"name": "m4"},
                ).json()["runner"]["runnerToken"]

                # Runner token is rejected on admin endpoints immediately without even calling Control
                self.assertEqual(
                    client.get("/v1/status", headers={"Authorization": f"Bearer {rtok}"}).status_code,
                    401,
                )

                client.post(
                    "/v1/builds",
                    headers={"Authorization": "Bearer valid-luma-deploy-token"},
                    json={
                        "providerId": "gitea:corp",
                        "repository": "acme/backend",
                        "platform": "linux/arm64",
                        "deploy": False,
                    },
                )
                leased = client.post(
                    "/v1/runner/lease",
                    headers={"Authorization": f"Bearer {rtok}"},
                    json={"nativePlatform": "linux/arm64", "os": "darwin", "arch": "arm64"},
                ).json()["task"]
                self.assertEqual(leased["repoUrl"], "https://git.example.com/acme/backend.git")
                self.assertEqual(leased["gitAuth"], {"username": "ci-bot", "token": "gitea-scoped-secret"})
                self.assertEqual(
                    leased["registryAuths"],
                    [
                        {
                            "host": "crpi-bj.cn-beijing.personal.cr.aliyuncs.com",
                            "username": "acr-push-user",
                            "password": "acr-push-secret-value",
                        }
                    ],
                )

    def test_cli_automatically_reuses_luma_login_context_and_saved_build_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_home = Path(tmp) / "luma-cfg"
            (cfg_home / "contexts").mkdir(parents=True)
            (cfg_home / "current-context").write_text("luma-demo1234\n", encoding="utf-8")
            (cfg_home / "contexts" / "luma-demo1234.json").write_text(
                json.dumps(
                    {
                        "clusterId": "luma-demo1234",
                        "endpoint": "https://luma.example.com",
                        "token": "context-borrowed-deploy-token",
                        "insecure": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "LUMA_CONFIG_HOME": str(cfg_home),
                    "LUMA_BUILD_URL": "",
                    "LUMA_BUILD_ADMIN_TOKEN": "",
                    "LUMA_DEPLOY_TOKEN": "",
                },
                clear=False,
            ):
                rc = cli_main(["use", "https://build.example.com/"])
                self.assertEqual(rc, 0)
                api = BuildApiClient("", "")
                self.assertEqual(api.url, "https://build.example.com")
                self.assertEqual(api.token, "context-borrowed-deploy-token")


class SecurityAndReliabilityRegressionTests(unittest.TestCase):
    def test_input_hardening_rejects_git_option_and_toml_injection(self) -> None:
        for bad_ref in ("--upload-pack=touch", "-f", "feat..bar", "branch name", "ref\x00bad", "foo.lock"):
            with self.assertRaises(BuildError):
                validate_git_ref(bad_ref)
        self.assertEqual(validate_git_ref("refs/heads/release/v1.2"), "release/v1.2")

        for bad_url in ("--upload-pack=evil", "https://user:pass@github.com/a/b.git", "https://github.com/a/b?x=1"):
            with self.assertRaises(BuildError):
                normalize_repo_url(bad_url)

        for bad_host in ('100.66.177.70:5000"\n[evil]', "host/with/slash", "host:99999"):
            with self.assertRaises(BuildError):
                normalize_registry_host(bad_host)

    def test_monorepo_distinct_contexts_do_not_supersede_each_other_and_restart_recovers_deploying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            app = create_app(
                data_dir=data_dir,
                admin_token=ADMIN_TOKEN,
                default_registry_host="100.66.177.70:5000",
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

            # Invalid integer returns clean 400 (not 500) + Cache-Control: no-store
            bad_int = client.get("/v1/builds?limit=notanint", headers=hdr)
            self.assertEqual(bad_int.status_code, 400)
            self.assertEqual(bad_int.headers.get("cache-control"), "no-store")

            # Monorepo: apps/api and apps/worker on same branch must NOT cancel each other
            b_api = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/mono", "ref": "main", "context": "apps/api", "deploy": False},
            ).json()["build"]["id"]
            b_worker = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/mono", "ref": "main", "context": "apps/worker", "deploy": False},
            ).json()["build"]["id"]
            self.assertEqual(client.get(f"/v1/builds/{b_api}", headers=hdr).json()["build"]["status"], "queued")
            self.assertEqual(client.get(f"/v1/builds/{b_worker}", headers=hdr).json()["build"]["status"], "queued")

            # Runner name validation blocks quotes / slashes / Prometheus label injection
            for bad_runner in ('bad"name', "bad/name", "-flag"):
                resp = client.post("/v1/runners/tokens", headers=hdr, json={"name": bad_runner})
                self.assertEqual(resp.status_code, 400)

            # Simulate an interrupted 'deploying' row left before a server restart
            rtok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "r1"}).json()["runner"]["runnerToken"]
            b_dep = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/svc", "ref": "main", "runner": "r1", "deploy": True},
            ).json()["build"]["id"]
            leased = client.post(
                "/v1/runner/lease",
                headers={"Authorization": f"Bearer {rtok}"},
                json={"nativePlatform": "linux/amd64"},
            )
            self.assertEqual(leased.status_code, 200)
            self.assertEqual(leased.json()["task"]["buildId"], b_dep)
            done = client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {rtok}"},
                json={"buildId": b_dep, "status": "succeeded", "result": {"image": "100.66.177.70:5000/acme/svc:1"}},
            )
            self.assertEqual(done.status_code, 200)
            claimed = app.state.store.claim_next_deploy()
            self.assertIsNotNone(claimed)
            self.assertEqual(client.get(f"/v1/builds/{b_dep}", headers=hdr).json()["build"]["status"], "deploying")

            # Restarting create_app on the same data_dir marks the interrupted deploy failed and unblocks target!
            app2 = create_app(
                data_dir=data_dir,
                admin_token=ADMIN_TOKEN,
                default_registry_host="100.66.177.70:5000",
                start_worker=False,
            )
            after = app2.state.store.get_build(b_dep)
            self.assertEqual(after["status"], "failed")
            self.assertIn("interrupted by luma-build restart", after["message"])

    def test_silent_build_subprocess_cancels_promptly_without_blocking_on_readline(self) -> None:
        import threading
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_docker = root / "fake-docker.py"
            fake_docker.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
            fake_docker.chmod(0o755)
            df = root / "Dockerfile"
            df.write_text("FROM scratch\n", encoding="utf-8")

            cancel_ev = threading.Event()
            threading.Timer(0.2, cancel_ev.set).start()
            t0 = time.monotonic()
            with self.assertRaises(BuildCanceled):
                _run_buildx_command(
                    docker_bin=str(fake_docker),
                    builder_name="luma-runner",
                    buildx_state_dir=root / "bx",
                    docker_config=root / "dc",
                    push_host="100.66.177.70:5000",
                    pull_host="100.66.177.70:5000",
                    image_repo="acme/app",
                    tag="t1",
                    context_dir=root,
                    dockerfile_path=df,
                    platforms=["linux/amd64"],
                    push_insecure=True,
                    cache_mode="local-only",
                    cache_ref="",
                    no_cache=False,
                    proxy="",
                    no_proxy="",
                    build_args={},
                    target="",
                    timeout=120,
                    metadata_file=root / "meta.json",
                    on_line=None,
                    cancel_event=cancel_ev,
                )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 3.0)

    def test_extract_manifest_platforms_and_default_platform_env(self) -> None:
        self.assertEqual(
            extract_manifest_platforms("name: web\nbuild:\n  platform: linux/arm64\n"),
            ["linux/arm64"],
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"LUMA_BUILD_DEFAULT_PLATFORM": "linux/arm64"}, clear=False
        ):
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="100.66.177.70:5000",
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            resp = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repoUrl": "https://git.example.com/acme/worker.git", "ref": "main"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["build"]["platforms"], ["linux/arm64"])

    def test_buildx_reconfigure_preserves_cache_state_and_unions_known_hosts(self) -> None:
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            bx_dir = Path(tmp) / "bx"
            dc_dir = Path(tmp) / "dc"
            dc_dir.mkdir()
            calls: list[list[str]] = []
            exists = False

            def fake_run(cmd, **kwargs):
                nonlocal exists
                calls.append(list(cmd))
                if cmd[1:3] == ["buildx", "inspect"]:
                    return sp.CompletedProcess(cmd, 0 if exists else 1, stdout="", stderr="")
                if cmd[1:3] == ["buildx", "create"]:
                    exists = True
                    return sp.CompletedProcess(cmd, 0, stdout="luma-runner\n", stderr="")
                return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("luma_build.runner.subprocess.run", side_effect=fake_run):
                # 1) Initial creation with HTTP registry
                _ensure_runner_buildx(
                    docker_bin="docker",
                    buildx_state_dir=bx_dir,
                    docker_config=dc_dir,
                    proxy="",
                    no_proxy="localhost,100.66.177.70:5000",
                    http_hosts=["100.66.177.70:5000"],
                    cache_keep_gb=20,
                    max_parallelism=0,
                )
                # 2) Subsequent build to HTTPS ACR: http_hosts=[] is a subset of known hosts,
                #    only no_proxy expands once and uses `buildx rm --keep-state` so cache volume survives!
                calls.clear()
                _ensure_runner_buildx(
                    docker_bin="docker",
                    buildx_state_dir=bx_dir,
                    docker_config=dc_dir,
                    proxy="",
                    no_proxy="localhost,crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                    http_hosts=[],
                    cache_keep_gb=20,
                    max_parallelism=0,
                )
                self.assertTrue(any("--keep-state" in c for c in calls))

                # 3) Switching back to 100.66.177.70:5000 is now a 100% cache-warm no-op (only inspect)!
                calls.clear()
                _ensure_runner_buildx(
                    docker_bin="docker",
                    buildx_state_dir=bx_dir,
                    docker_config=dc_dir,
                    proxy="",
                    no_proxy="localhost,100.66.177.70:5000",
                    http_hosts=["100.66.177.70:5000"],
                    cache_keep_gb=20,
                    max_parallelism=0,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1:3], ["buildx", "inspect"])

    def test_web_dashboard_serves_kumo_ui_with_strict_per_request_csp_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="100.66.177.70:5000",
                start_worker=False,
            )
            client = TestClient(app)
            r1 = client.get("/")
            r2 = client.get("/dashboard")
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            self.assertIn("构建与推送时间线", r1.text)
            self.assertIn('<meta name="color-scheme" content="light dark">', r1.text)
            self.assertIn('closedby="any"', r1.text)
            self.assertIn('role="alert"', r1.text)
            self.assertIn("overscroll-behavior: contain", r1.text)
            self.assertIn('id="sub-manifest-path"', r1.text)
            self.assertIn('id="sub-compose-sidecar"', r1.text)
            self.assertIn('id="sub-provider-id"', r1.text)
            self.assertIn('class="select-wrap select-wrap-sm"', r1.text)
            self.assertIn("appearance: base-select", r1.text)
            self.assertIn('id="builds-pages"', r1.text)
            self.assertIn("computePaginationItems", r1.text)
            self.assertNotIn("style=", r1.text)

            csp1 = r1.headers.get("content-security-policy", "")
            csp2 = r2.headers.get("content-security-policy", "")
            self.assertIn("strict-dynamic", csp1)
            self.assertIn("frame-ancestors 'none'", csp1)
            self.assertNotEqual(csp1, csp2)  # Fresh per-request cryptographic nonce!
            self.assertEqual(r1.headers.get("x-frame-options"), "DENY")
            self.assertEqual(r1.headers.get("x-content-type-options"), "nosniff")
            self.assertIn('<link rel="icon" type="image/png" href="/favicon.png">', r1.text)
            png = client.get("/favicon.png")
            self.assertEqual(png.status_code, 200)
            self.assertEqual(png.headers.get("content-type"), "image/png")
            self.assertTrue(png.content.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(client.get("/favicon.ico").status_code, 200)
            self.assertEqual(client.get("/favicon.svg").status_code, 200)

    def test_explicit_manifest_platforms_route_build_deterministically_without_remote_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                control_url="https://luma.example.com",
                control_deploy_token="ctl-tok",
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            resp = client.post(
                "/v1/builds",
                headers=hdr,
                json={
                    "repoUrl": "https://git.example.com/acme/service.git",
                    "ref": "main",
                    "manifest": "name: service\nbuild:\n  platforms:\n    - linux/arm64\n",
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["build"]["platforms"], ["linux/arm64"])

    def test_manifest_path_and_x_luma_git_contexts_end_to_end(self) -> None:
        import hashlib
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create a real local git repo to archive as pinned shared context
            shared = root / "shared-src"
            shared.mkdir()
            (shared / "lib.py").write_text("VALUE = 42\n", encoding="utf-8")
            sp.run(["git", "-C", str(shared), "init"], check=True, stdout=sp.DEVNULL)
            sp.run(["git", "-C", str(shared), "config", "user.email", "ci@example.com"], check=True)
            sp.run(["git", "-C", str(shared), "config", "user.name", "ci"], check=True)
            sp.run(["git", "-C", str(shared), "add", "."], check=True)
            sp.run(["git", "-C", str(shared), "commit", "-m", "init"], check=True, stdout=sp.DEVNULL)
            commit = sp.check_output(["git", "-C", str(shared), "rev-parse", "HEAD"], text=True).strip()
            archive_bytes = sp.check_output(["git", "-C", str(shared), "archive", "--format=tar.gz", commit])
            archive_sha = hashlib.sha256(archive_bytes).hexdigest()

            primary = root / "primary"
            primary.mkdir()
            (primary / "luma.prod.api.yml").write_text("name: api-prod\nregion: cn\n", encoding="utf-8")
            (primary / "shared.lock.json").write_text(
                json.dumps(
                    {
                        "repository": "https://git.example.com/acme/shared.git",
                        "commit": commit,
                        "archive_sha256": archive_sha,
                    }
                ),
                encoding="utf-8",
            )
            discovered = _discover_manifest(primary, manifest_path="luma.prod.api.yml")
            assert discovered is not None
            self.assertEqual(discovered[0], "service")
            self.assertEqual(discovered[1].name, "luma.prod.api.yml")

            def fake_clone(*, repo_url, ref, dest, git_auth, proxy=""):
                import shutil

                shutil.copytree(shared, dest)
                return commit[:12], commit

            work = root / "work"
            work.mkdir()
            with patch("luma_build.runner._clone_repo", side_effect=fake_clone):
                prepared = _prepare_git_contexts(
                    primary,
                    {"x-luma-git-contexts": {"shared": {"lock": "shared.lock.json"}}},
                    primary_repo_url="https://git.example.com/acme/primary.git",
                    workspace=work,
                    git_auth={"username": "bot", "token": "pat"},
                    proxy="",
                    on_line=None,
                    cancel_event=__import__("threading").Event(),
                )
            self.assertEqual((prepared["shared"] / "lib.py").read_text(encoding="utf-8"), "VALUE = 42\n")

            # Cross-origin repository must be rejected so gitAuth never leaks
            (primary / "bad.lock.json").write_text(
                json.dumps(
                    {
                        "repository": "https://evil.example.com/acme/shared.git",
                        "commit": commit,
                        "archive_sha256": archive_sha,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BuildError, "must use the primary repository's HTTPS origin"):
                _prepare_git_contexts(
                    primary,
                    {"x-luma-git-contexts": {"shared": {"lock": "bad.lock.json"}}},
                    primary_repo_url="https://git.example.com/acme/primary.git",
                    workspace=work,
                    git_auth={"username": "bot", "token": "pat"},
                    proxy="",
                    on_line=None,
                    cancel_event=__import__("threading").Event(),
                )

    def test_auto_arm_routing_via_control_placement_and_manifest_handoff_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(
                data_dir=root / "data",
                admin_token=ADMIN_TOKEN,
                control_url="https://luma.example.com",
                control_deploy_token="ctl-tok",
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                affinity_wait_seconds=0,
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

            amd_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "cloud-x86", "weight": 200}).json()["runner"]["runnerToken"]
            arm_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "m4-arm", "weight": 100}).json()["runner"]["runnerToken"]
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {amd_tok}"}, json={"nativePlatform": "linux/amd64"})
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {arm_tok}"}, json={"nativePlatform": "linux/arm64"})

            fake_status = {
                "nodes": {
                    "items": [
                        {"name": "manager", "region": "cn", "nativePlatform": "linux/amd64", "agentStatus": "online"},
                        {"name": "arm-box", "aliases": ["arm-edge-1"], "region": "hk-arm", "nativePlatform": "linux/arm64", "agentStatus": "online"},
                    ]
                }
            }
            with patch.object(app.state.deployer, "request_json", return_value=fake_status):
                # 1) Passing --node arm-box (even alongside mixed --region cn) resolves immediately to linux/arm64!
                direct_arm = client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"repository": "acme/direct-arm", "node": "arm-box", "region": "cn", "deploy": False},
                ).json()["build"]
                self.assertEqual(direct_arm["platforms"], ["linux/arm64"])
                # Drain direct_arm on arm_tok
                self.assertEqual(
                    client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {arm_tok}"}, json={"nativePlatform": "linux/arm64"}).json()["task"]["buildId"],
                    direct_arm["id"],
                )
                client.post(
                    "/v1/runner/complete",
                    headers={"Authorization": f"Bearer {arm_tok}"},
                    json={"buildId": direct_arm["id"], "status": "succeeded", "result": {"image": "x"}},
                )

                # 2) Un-annotated submission where .luma.yml inside Git targets node: arm-box
                b1 = client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"repository": "acme/git-arm-svc", "ref": "main", "deploy": False},
                ).json()["build"]
                # First-ever build starts as default linux/amd64 before Git repo is inspected
                self.assertEqual(b1["platforms"], ["linux/amd64"])

                leased_x86 = client.post(
                    "/v1/runner/lease",
                    headers={"Authorization": f"Bearer {amd_tok}"},
                    json={"nativePlatform": "linux/amd64"},
                ).json()["task"]
                self.assertEqual(leased_x86["buildId"], b1["id"])

                def fake_clone(*, repo_url, ref, dest, git_auth, proxy="", **kwargs):
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
                    (dest / ".luma.yml").write_text("name: git-arm-svc\nregion: cn\nnode: arm-box\n", encoding="utf-8")
                    return "111111222222", "1" * 40

                # x86 runner inspects .luma.yml, detects node: arm-box -> linux/arm64, and raises PlatformRequeueRequired BEFORE buildx!
                with patch("luma_build.runner._clone_repo", side_effect=fake_clone), patch(
                    "luma_build.runner._ensure_runner_buildx"
                ) as mock_bx:
                    with self.assertRaises(PlatformRequeueRequired) as ctx:
                        execute_build_task(
                            leased_x86,
                            docker_bin="docker",
                            native_platform="linux/amd64",
                            buildx_state_dir=root / "bx",
                        )
                    self.assertEqual(ctx.exception.platforms, ["linux/arm64"])
                    mock_bx.assert_not_called()

                # Runner reports requeue -> server hands off to linux/arm64 and learns target_platforms!
                req_res = client.post(
                    "/v1/runner/complete",
                    headers={"Authorization": f"Bearer {amd_tok}"},
                    json={
                        "buildId": b1["id"],
                        "status": "requeue",
                        "result": {"requiredPlatforms": ["linux/arm64"]},
                    },
                ).json()["build"]
                self.assertEqual(req_res["status"], "queued")
                self.assertEqual(req_res["platforms"], ["linux/arm64"])

                # x86 runner cannot lease it now; ARM64 runner leases and completes it!
                self.assertIsNone(
                    client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {amd_tok}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
                )
                leased_arm = client.post(
                    "/v1/runner/lease",
                    headers={"Authorization": f"Bearer {arm_tok}"},
                    json={"nativePlatform": "linux/arm64"},
                ).json()["task"]
                self.assertEqual(leased_arm["buildId"], b1["id"])
                client.post(
                    "/v1/runner/complete",
                    headers={"Authorization": f"Bearer {arm_tok}"},
                    json={"buildId": b1["id"], "status": "succeeded", "result": {"image": "arm-img"}},
                )

                # 3) Second build of acme/git-arm-svc routes to linux/arm64 IMMEDIATELY at queue time (zero handoff)!
                b2 = client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"repository": "acme/git-arm-svc", "ref": "main", "deploy": False},
                ).json()["build"]
                self.assertEqual(b2["platforms"], ["linux/arm64"])

    def test_ci_commit_sha_supersede_and_out_of_order_deploy_rollback_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                control_url="https://luma.example.com",
                control_deploy_token="ctl-tok",
                default_registry_host="100.66.177.70:5000",
                affinity_wait_seconds=0,
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            r_slow = client.post("/v1/runners/tokens", headers=hdr, json={"name": "slow-home", "weight": 50}).json()["runner"]["runnerToken"]
            r_fast = client.post("/v1/runners/tokens", headers=hdr, json={"name": "fast-cloud", "weight": 200}).json()["runner"]["runnerToken"]

            sha1 = "1" * 40
            sha2 = "2" * 40
            # 1) Default supersede=True: sequential 40-hex commit SHAs for deploy=True supersede older queued SHA!
            c1 = client.post("/v1/builds", headers=hdr, json={"repository": "acme/api", "ref": sha1, "deploy": True}).json()["build"]["id"]
            # A --no-deploy PR check must NOT cancel an active deploy=True build
            c_pr = client.post("/v1/builds", headers=hdr, json={"repository": "acme/api", "ref": sha1, "deploy": False}).json()["build"]["id"]
            self.assertEqual(client.get(f"/v1/builds/{c1}", headers=hdr).json()["build"]["status"], "queued")
            self.assertEqual(client.get(f"/v1/builds/{c_pr}", headers=hdr).json()["build"]["status"], "queued")
            # Next CI push sha2 (deploy=True) supersedes c1 (deploy=True), leaving c_pr (deploy=False) intact!
            c2 = client.post("/v1/builds", headers=hdr, json={"repository": "acme/api", "ref": sha2, "deploy": True}).json()["build"]["id"]
            self.assertEqual(client.get(f"/v1/builds/{c1}", headers=hdr).json()["build"]["status"], "canceled")
            self.assertEqual(client.get(f"/v1/builds/{c_pr}", headers=hdr).json()["build"]["status"], "queued")
            client.post(f"/v1/builds/{c_pr}/cancel", headers=hdr)
            client.post(f"/v1/builds/{c2}/cancel", headers=hdr)

            # 2) Concurrent builds (supersede=False): fast newer build finishes & deploys BEFORE slow older build!
            sha_old = "a" * 40
            sha_new = "b" * 40
            b_old = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": sha_old, "runner": "slow-home", "supersede": False, "deploy": True},
            ).json()["build"]["id"]
            b_new = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": sha_new, "runner": "fast-cloud", "supersede": False, "deploy": True},
            ).json()["build"]["id"]

            self.assertEqual(client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_slow}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]["buildId"], b_old)
            self.assertEqual(client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_fast}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]["buildId"], b_new)

            # Fast runner finishes b_new FIRST and deploys to production
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {r_fast}"},
                json={"buildId": b_new, "status": "succeeded", "result": {"kind": "service", "image": "img:new", "manifest": "name: orders\nregion: cn\n"}},
            )
            deployed_ids: list[str] = []

            def fake_deploy(**kwargs):
                deployed_ids.append(str(kwargs["build_record"]["id"]))
                return {"service": "orders", "revision": len(deployed_ids)}

            with patch.object(app.state.deployer, "deploy_built_artifact", side_effect=fake_deploy):
                self.assertTrue(app.state.deploy_worker.run_pending_once())
            self.assertEqual(deployed_ids, [b_new])
            self.assertEqual(client.get(f"/v1/builds/{b_new}", headers=hdr).json()["build"]["status"], "succeeded")

            # Slow runner finishes b_old 60s LATER -> must be skipped (NEVER roll back b_new)!
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {r_slow}"},
                json={"buildId": b_old, "status": "succeeded", "result": {"kind": "service", "image": "img:old", "manifest": "name: orders\nregion: cn\n"}},
            )
            with patch.object(app.state.deployer, "deploy_built_artifact", side_effect=fake_deploy):
                self.assertFalse(app.state.deploy_worker.run_pending_once())
            self.assertEqual(deployed_ids, [b_new])
            old_final = client.get(f"/v1/builds/{b_old}", headers=hdr).json()["build"]
            self.assertEqual(old_final["status"], "canceled")
            self.assertEqual(old_final["supersededBy"], b_new)

            # Intentional manual rollback submitted AFTER b_new (higher seq) MUST deploy cleanly!
            b_manual_rollback = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": sha_old, "runner": "fast-cloud", "deploy": True},
            ).json()["build"]["id"]
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_fast}"}, json={"nativePlatform": "linux/amd64"})
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {r_fast}"},
                json={"buildId": b_manual_rollback, "status": "succeeded", "result": {"kind": "service", "image": "img:rollback", "manifest": "name: orders\nregion: cn\n"}},
            )
            with patch.object(app.state.deployer, "deploy_built_artifact", side_effect=fake_deploy):
                self.assertTrue(app.state.deploy_worker.run_pending_once())
            self.assertEqual(deployed_ids, [b_new, b_manual_rollback])

    def test_terminal_states_clear_encrypted_env_secrets_and_retry_accepts_fresh_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                control_url="https://luma.example.com",
                control_deploy_token="ctl-tok",
                default_registry_host="100.66.177.70:5000",
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            rtok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "r1"}).json()["runner"]["runnerToken"]

            # 1) Failed deploy clears encrypted_env_secrets from SQLite immediately
            b1 = client.post(
                "/v1/builds",
                headers=hdr,
                json={
                    "repository": "acme/pay",
                    "ref": "main",
                    "runner": "r1",
                    "envSecrets": {"STRIPE_SECRET": "sk_live_keep_me"},
                },
            ).json()["build"]["id"]
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {rtok}"}, json={"nativePlatform": "linux/amd64"})
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {rtok}"},
                json={"buildId": b1, "status": "succeeded", "result": {"kind": "service", "image": "img:1", "manifest": "name: pay\nregion: cn\n"}},
            )
            with patch.object(app.state.deployer, "deploy_built_artifact", side_effect=BuildError("Control 503")):
                self.assertTrue(app.state.deploy_worker.run_pending_once())
            self.assertEqual(client.get(f"/v1/builds/{b1}", headers=hdr).json()["build"]["status"], "failed")
            with app.state.store._conn(write=False) as conn:
                self.assertEqual(conn.execute("SELECT encrypted_env_secrets FROM builds WHERE id = ?", (b1,)).fetchone()[0], "")

            # 2) Canceled queued build also clears encrypted_env_secrets
            b_cancel = client.post(
                "/v1/builds",
                headers=hdr,
                json={
                    "repository": "acme/cancel-pay",
                    "ref": "main",
                    "envSecrets": {"API_KEY": "secret-canceled"},
                },
            ).json()["build"]["id"]
            client.post(f"/v1/builds/{b_cancel}/cancel", headers=hdr)
            with app.state.store._conn(write=False) as conn:
                self.assertEqual(conn.execute("SELECT encrypted_env_secrets FROM builds WHERE id = ?", (b_cancel,)).fetchone()[0], "")

            # 3) Retry with freshly supplied envSecrets deploys cleanly and clears after success
            b2 = client.post(
                f"/v1/builds/{b1}/retry",
                headers=hdr,
                json={"envSecrets": {"STRIPE_SECRET": "sk_live_fresh"}},
            ).json()["build"]["id"]
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {rtok}"}, json={"nativePlatform": "linux/amd64"})
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {rtok}"},
                json={"buildId": b2, "status": "succeeded", "result": {"kind": "service", "image": "img:2", "manifest": "name: pay\nregion: cn\n"}},
            )
            captured_secrets: dict = {}

            def ok_deploy(**kwargs):
                captured_secrets.update(kwargs["env_secrets"] or {})
                return {"service": "pay", "revision": 2}

            with patch.object(app.state.deployer, "deploy_built_artifact", side_effect=ok_deploy):
                self.assertTrue(app.state.deploy_worker.run_pending_once())
            self.assertEqual(captured_secrets, {"STRIPE_SECRET": "sk_live_fresh"})
            with app.state.store._conn(write=False) as conn:
                self.assertEqual(conn.execute("SELECT encrypted_env_secrets FROM builds WHERE id = ?", (b2,)).fetchone()[0], "")

    def test_monorepo_cache_variants_and_cloud_registry_no_proxy(self) -> None:
        # Default single-service repo keeps existing tag unchanged
        self.assertEqual(derive_cache_variant(single_service=True), "")
        # Distinct monorepo manifests & sidecars produce non-colliding cache variants
        v_api = derive_cache_variant(manifest_path="luma.prod.api.yml", context="apps/api", single_service=True)
        v_worker = derive_cache_variant(manifest_path="luma.prod.worker.yml", context="apps/worker", single_service=True)
        self.assertNotEqual(v_api, v_worker)
        self.assertNotEqual(
            format_cache_reference(push_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com", repo_slug="acme/mono", service_name=v_api, namespace="luma"),
            format_cache_reference(push_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com", repo_slug="acme/mono", service_name=v_worker, namespace="luma"),
        )
        c_bill = derive_cache_variant(compose_sidecar="apps/billing/luma.billing.compose.yml", service_name="web", single_service=True)
        c_crm = derive_cache_variant(compose_sidecar="apps/crm/luma.crm.compose.yml", service_name="web", single_service=True)
        self.assertNotEqual(c_bill, c_crm)

        # Aliyun ACR automatically adds .aliyuncs.com so 307 OSS layer redirects bypass Mihomo!
        acr_entries = registry_no_proxy_entries("crpi-x.cn-beijing.personal.cr.aliyuncs.com")
        self.assertIn(".aliyuncs.com", acr_entries)
        merged = merge_no_proxy("100.64.0.0/10,git.corp.internal", *acr_entries)
        self.assertTrue(_url_bypasses_proxy("https://bucket.oss-cn-beijing.aliyuncs.com/layer", merged))
        self.assertTrue(_url_bypasses_proxy("http://100.66.177.70:3000/org/repo.git", merged))
        self.assertTrue(_url_bypasses_proxy("https://git.corp.internal/org/repo.git", merged))
        self.assertFalse(_url_bypasses_proxy("https://github.com/org/repo.git", merged))

        # _clone_repo strips HTTP_PROXY when cloning a Tailscale/bypassed Git URL
        import subprocess as sp

        seen_envs: list[dict] = []

        def fake_git_run(cmd, **kwargs):
            seen_envs.append(dict(kwargs.get("env") or {}))
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch("luma_build.runner.subprocess.run", side_effect=fake_git_run), patch(
            "luma_build.runner.subprocess.check_output", side_effect=["abcdef123456\n", "a" * 40 + "\n"]
        ):
            _clone_repo(
                repo_url="http://100.66.177.70:3000/acme/internal.git",
                ref="main",
                dest=Path(tmp) / "repo",
                git_auth=None,
                proxy="http://127.0.0.1:7890",
                no_proxy=merged,
            )
        self.assertNotIn("HTTP_PROXY", seen_envs[0])
        self.assertIn(".aliyuncs.com", seen_envs[0]["NO_PROXY"])

    def test_git_credential_strictly_validates_origin_port_and_path_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "ghp-env-token",
                "GITEA_HOST": "git.example.com",
                "GITEA_TOKEN": "gitea-env-token",
            },
            clear=False,
        ):
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

            client.post(
                "/v1/credentials/git",
                headers=hdr,
                json={
                    "id": "gitea:scoped",
                    "host": "git.example.com:8443",
                    "pathPrefix": "corp/team-a",
                    "token": "scoped-secret-token",
                },
            )

            # Wrong domain, wrong port, wrong path prefix, or SSH scheme with explicit providerId MUST fail (HTTP 400)
            for bad_url in (
                "https://evil.example.com:8443/corp/team-a/service.git",
                "https://git.example.com/corp/team-a/service.git",
                "https://git.example.com:8443/corp/team-b/service.git",
                "ssh://git@git.example.com:8443/corp/team-a/service.git",
            ):
                bad = client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"providerId": "gitea:scoped", "repoUrl": bad_url, "deploy": False},
                )
                self.assertEqual(bad.status_code, 400, f"expected 400 for {bad_url}: {bad.text}")

            # Env-backed GITHUB_TOKEN and GITEA_TOKEN also validate target host strictly
            bad_gh = client.post(
                "/v1/builds",
                headers=hdr,
                json={"providerId": "github", "repoUrl": "https://evil.example.com/a/b.git", "deploy": False},
            )
            self.assertEqual(bad_gh.status_code, 400)
            bad_gt = client.post(
                "/v1/builds",
                headers=hdr,
                json={"providerId": "gitea", "repoUrl": "https://github.com/a/b.git", "deploy": False},
            )
            self.assertEqual(bad_gt.status_code, 400)

            # Matching host + port + path_prefix succeeds
            ok = client.post(
                "/v1/builds",
                headers=hdr,
                json={
                    "providerId": "gitea:scoped",
                    "repoUrl": "https://git.example.com:8443/corp/team-a/service.git",
                    "deploy": False,
                },
            )
            self.assertEqual(ok.status_code, 200, ok.text)

            # Saving https://git.example.com:443 MUST reject plaintext http:// on port 80 or 443!
            client.post(
                "/v1/credentials/git",
                headers=hdr,
                json={"id": "https-443", "host": "https://git.example.com:443", "token": "tls-only-pat"},
            )
            for downgrade_url in (
                "http://git.example.com:80/acme/app.git",
                "http://git.example.com/acme/app.git",
                "http://git.example.com:443/acme/app.git",
                "https://git.example.com:80/acme/app.git",
            ):
                bad_dg = client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"providerId": "https-443", "repoUrl": downgrade_url, "deploy": False},
                )
                self.assertEqual(bad_dg.status_code, 400, f"expected 400 for {downgrade_url}")
            self.assertEqual(
                client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"providerId": "https-443", "repoUrl": "https://git.example.com/acme/app.git", "deploy": False},
                ).status_code,
                200,
            )

            # Explicit http:// internal Git host preserves scheme for shorthand and rejects https:// mismatch
            client.post(
                "/v1/credentials/git",
                headers=hdr,
                json={"id": "http-lan", "host": "http://100.66.177.70:3000", "token": "lan-pat"},
            )
            ok_lan = client.post(
                "/v1/builds",
                headers=hdr,
                json={"providerId": "http-lan", "repository": "acme/lan-svc", "deploy": False},
            )
            self.assertEqual(ok_lan.status_code, 200, ok_lan.text)
            self.assertEqual(ok_lan.json()["build"]["repoUrl"], "http://100.66.177.70:3000/acme/lan-svc.git")
            self.assertEqual(
                client.post(
                    "/v1/builds",
                    headers=hdr,
                    json={"providerId": "http-lan", "repoUrl": "https://100.66.177.70:3000/acme/lan-svc.git", "deploy": False},
                ).status_code,
                400,
            )

        # GITEA_TOKEN without GITEA_HOST must refuse rather than defaulting to github.com
        with tempfile.TemporaryDirectory() as tmp2, patch.dict(
            "os.environ", {"GITEA_HOST": "", "GITEA_TOKEN": "orphan-gitea-token"}, clear=False
        ):
            app2 = create_app(
                data_dir=Path(tmp2) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                start_worker=False,
            )
            c2 = TestClient(app2)
            res_no_host = c2.post(
                "/v1/builds",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                json={"providerId": "gitea", "repository": "acme/app", "deploy": False},
            )
            self.assertEqual(res_no_host.status_code, 400)

    def test_canonical_app_identity_prevents_override_bypass_and_concurrent_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                control_url="https://luma.example.com",
                control_deploy_token="ctl-tok",
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                affinity_wait_seconds=0,
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            r_slow = client.post("/v1/runners/tokens", headers=hdr, json={"name": "r-slow"}).json()["runner"]["runnerToken"]
            r_fast = client.post("/v1/runners/tokens", headers=hdr, json={"name": "r-fast"}).json()["runner"]["runnerToken"]

            # Distinct apps from the same repo (--manifest name: orders-prod vs orders-staging) MUST NOT cancel each other!
            m_prod = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": "main", "manifest": "name: orders-prod\nregion: cn\n"},
            ).json()["build"]["id"]
            m_stg = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": "main", "manifest": "name: orders-staging\nregion: cn\n"},
            ).json()["build"]["id"]
            self.assertEqual(client.get(f"/v1/builds/{m_prod}", headers=hdr).json()["build"]["status"], "queued")
            self.assertEqual(client.get(f"/v1/builds/{m_stg}", headers=hdr).json()["build"]["status"], "queued")
            client.post(f"/v1/builds/{m_prod}/cancel", headers=hdr)
            client.post(f"/v1/builds/{m_stg}/cancel", headers=hdr)

            # Build 1 omits --node/--domain/--manifest-path; Build 2 passes explicit overrides for the SAME app!
            b_old = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/orders", "ref": "1" * 40, "runner": "r-slow", "supersede": False, "deploy": True},
            ).json()["build"]["id"]
            b_new = client.post(
                "/v1/builds",
                headers=hdr,
                json={
                    "repository": "acme/orders",
                    "ref": "2" * 40,
                    "runner": "r-fast",
                    "node": "manager",
                    "domain": "orders.example.com",
                    "manifestPath": ".luma.yml",
                    "supersede": False,
                    "deploy": True,
                },
            ).json()["build"]["id"]

            t_old = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_slow}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
            t_new = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_fast}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
            self.assertEqual(t_old["buildId"], b_old)
            self.assertEqual(t_new["buildId"], b_new)

            # Fast build completes & deploys first
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {r_fast}"},
                json={
                    "buildId": b_new,
                    "leaseId": t_new["leaseId"],
                    "status": "succeeded",
                    "result": {"kind": "service", "image": "img:new", "manifest": "name: orders\nnode: manager\n"},
                },
            )
            with patch.object(app.state.deployer, "deploy_built_artifact", return_value={"service": "orders", "revision": 1}):
                self.assertTrue(app.state.deploy_worker.run_pending_once())
            self.assertEqual(client.get(f"/v1/builds/{b_new}", headers=hdr).json()["build"]["status"], "succeeded")

            # Slow earlier build finishes later -> blocked by canonical app identity ('service:orders')!
            client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {r_slow}"},
                json={
                    "buildId": b_old,
                    "leaseId": t_old["leaseId"],
                    "status": "succeeded",
                    "result": {"kind": "service", "image": "img:old", "manifest": "name: orders\nnode: manager\n"},
                },
            )
            with patch.object(app.state.deployer, "deploy_built_artifact", return_value={"service": "orders", "revision": 2}) as mock_dep:
                self.assertFalse(app.state.deploy_worker.run_pending_once())
                mock_dep.assert_not_called()
            old_rec = client.get(f"/v1/builds/{b_old}", headers=hdr).json()["build"]
            self.assertEqual(old_rec["status"], "canceled")
            self.assertEqual(old_rec["supersededBy"], b_new)

            # Deploy mutex: while 'service:billing' is actively deploying, a newer build for 'service:billing' waits in queued_deploy
            d1 = client.post("/v1/builds", headers=hdr, json={"repository": "acme/billing", "ref": "3" * 40, "runner": "r-slow", "supersede": False}).json()["build"]["id"]
            d2 = client.post("/v1/builds", headers=hdr, json={"repository": "acme/billing", "ref": "4" * 40, "runner": "r-fast", "node": "worker-1", "manifestPath": ".luma.yml", "supersede": False}).json()["build"]["id"]
            l1 = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_slow}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
            l2 = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {r_fast}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
            client.post("/v1/runner/complete", headers={"Authorization": f"Bearer {r_slow}"}, json={"buildId": d1, "leaseId": l1["leaseId"], "status": "succeeded", "result": {"kind": "service", "image": "b:1", "manifest": "name: billing\n"}})
            claimed1 = app.state.store.claim_next_deploy()
            self.assertIsNotNone(claimed1)
            self.assertEqual(claimed1[0]["id"], d1)
            client.post("/v1/runner/complete", headers={"Authorization": f"Bearer {r_fast}"}, json={"buildId": d2, "leaseId": l2["leaseId"], "status": "succeeded", "result": {"kind": "service", "image": "b:2", "manifest": "name: billing\n"}})
            # Must wait while d1 is still 'deploying'!
            self.assertIsNone(app.state.store.claim_next_deploy())
            app.state.store.complete_deploy(d1, succeeded=True, deploy_result={"service": "billing"})
            claimed2 = app.state.store.claim_next_deploy()
            self.assertIsNotNone(claimed2)
            self.assertEqual(claimed2[0]["id"], d2)

    def test_requeue_completion_receipt_replay_is_idempotent_and_runner_drops_409(self) -> None:
        import urllib.error
        from luma_build.runner import RunnerDaemon

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                affinity_wait_seconds=0,
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            amd_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "amd-1"}).json()["runner"]["runnerToken"]
            arm_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "m4"}).json()["runner"]["runnerToken"]

            bid = client.post("/v1/builds", headers=hdr, json={"repository": "acme/handoff", "deploy": False}).json()["build"]["id"]
            lease1 = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {amd_tok}"}, json={"nativePlatform": "linux/amd64"}).json()["task"]
            self.assertEqual(lease1["buildId"], bid)
            self.assertTrue(lease1["leaseId"].startswith("lse-"))

            # First requeue succeeds and clears runner_name
            r1 = client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {amd_tok}"},
                json={
                    "buildId": bid,
                    "leaseId": lease1["leaseId"],
                    "status": "requeue",
                    "result": {"requiredPlatforms": ["linux/arm64"]},
                },
            )
            self.assertEqual(r1.status_code, 200)
            self.assertFalse(r1.json()["replayed"])

            # Even AFTER m4 has already leased the requeued build, amd-1 replaying its lost ACK gets 200 replayed=True!
            lease2 = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {arm_tok}"}, json={"nativePlatform": "linux/arm64"}).json()["task"]
            self.assertEqual(lease2["buildId"], bid)
            self.assertNotEqual(lease2["leaseId"], lease1["leaseId"])

            replay = client.post(
                "/v1/runner/complete",
                headers={"Authorization": f"Bearer {amd_tok}"},
                json={
                    "buildId": bid,
                    "leaseId": lease1["leaseId"],
                    "status": "requeue",
                    "result": {"requiredPlatforms": ["linux/arm64"]},
                },
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.json()["replayed"])
            # m4 still owns the active build undisturbed!
            self.assertEqual(client.get(f"/v1/builds/{bid}", headers=hdr).json()["build"]["runner"], "m4")

            # RunnerDaemon drops non-retryable HTTP 409 immediately instead of wedging the worker thread
            with patch("luma_build.runner.find_docker_binary", return_value="docker"), patch(
                "luma_build.runner.capture_docker_runtime", return_value={}
            ), patch("luma_build.runner.detect_native_engine_platform", return_value="linux/amd64"), patch(
                "luma_build.runner.execute_build_task", return_value={"image": "x"}
            ):
                daemon = RunnerDaemon(
                    server_url="http://127.0.0.1:9",
                    runner_token=amd_tok,
                    state_dir=Path(tmp) / "rstate",
                )
                calls = 0

                def fake_post(path: str, body: dict, *, timeout: int = 35):
                    nonlocal calls
                    if path == "/v1/runner/complete":
                        calls += 1
                        raise urllib.error.HTTPError(path, 409, "conflict", None, None)  # type: ignore[arg-type]
                    return {}

                with patch.object(daemon, "_post", side_effect=fake_post):
                    daemon._run_leased_task({"buildId": "bld-stale", "leaseId": "lse-stale"})
                self.assertEqual(calls, 1)

    def test_pinned_arm_runner_first_build_leases_immediately_and_rejects_explicit_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                data_dir=Path(tmp) / "data",
                admin_token=ADMIN_TOKEN,
                default_registry_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                default_registry_namespace="luma",
                affinity_wait_seconds=0,
                start_worker=False,
            )
            client = TestClient(app)
            hdr = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            amd_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "amd-cloud", "weight": 200}).json()["runner"]["runnerToken"]
            arm_tok = client.post("/v1/runners/tokens", headers=hdr, json={"name": "m4", "weight": 100}).json()["runner"]["runnerToken"]
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {amd_tok}"}, json={"nativePlatform": "linux/amd64"})
            client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {arm_tok}"}, json={"nativePlatform": "linux/arm64"})

            # 1) First build with --runner m4 (no --platform) adopts m4's native linux/arm64 and leases immediately!
            res = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/first-arm-repo", "runner": "m4", "deploy": False},
            )
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["build"]["platforms"], ["linux/arm64"])
            leased = client.post("/v1/runner/lease", headers={"Authorization": f"Bearer {arm_tok}"}, json={"nativePlatform": "linux/arm64"}).json()["task"]
            self.assertEqual(leased["buildId"], res.json()["build"]["id"])
            self.assertEqual(leased["pinnedRunner"], "m4")

            # 2) Explicit incompatible platform (--runner m4 --platform linux/amd64) fails fast with HTTP 400!
            mismatch = client.post(
                "/v1/builds",
                headers=hdr,
                json={"repository": "acme/first-arm-repo", "runner": "m4", "platform": "linux/amd64", "deploy": False},
            )
            self.assertEqual(mismatch.status_code, 400)
            self.assertIn("incompatible", mismatch.json()["error"])

    def test_isolated_docker_config_preserves_pinned_docker_context_and_acr_inline_cache(self) -> None:
        import subprocess as sp
        from luma_build.runner import _write_docker_config, capture_docker_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig_docker = root / "orig-docker"
            meta_dir = orig_docker / "contexts" / "meta" / "hash123"
            meta_dir.mkdir(parents=True)
            meta_bytes = b'{"Name":"orbstack","Endpoints":{"docker":{"Host":"unix:///tmp/orb.sock"}}}'
            (meta_dir / "meta.json").write_bytes(meta_bytes)
            (orig_docker / "config.json").write_text('{"currentContext":"orbstack"}\n', encoding="utf-8")

            inspect_json = json.dumps(
                [
                    {
                        "Name": "orbstack",
                        "Endpoints": {"docker": {"Host": "unix:///tmp/orb.sock"}},
                        "Storage": {"MetadataPath": str(meta_dir), "TLSPath": ""},
                    }
                ]
            )
            with patch(
                "luma_build.runner.subprocess.run",
                return_value=sp.CompletedProcess(["docker"], 0, stdout=inspect_json, stderr=""),
            ):
                rt = capture_docker_runtime("docker", base_env={"DOCKER_CONFIG": str(orig_docker)})

            # Even if original config changes afterwards, isolated job config keeps pinned orbstack context!
            (orig_docker / "config.json").write_text('{"currentContext":"other"}\n', encoding="utf-8")
            job_cfg = root / "job-docker-cfg"
            _write_docker_config(
                job_cfg,
                [{"host": "crpi-x.cn-beijing.personal.cr.aliyuncs.com", "username": "u", "password": "p"}],
                docker_runtime=rt,
            )
            written_cfg = json.loads((job_cfg / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(written_cfg["currentContext"], "orbstack")
            self.assertEqual((job_cfg / "contexts" / "meta" / "hash123" / "meta.json").read_bytes(), meta_bytes)

            # Verify ACR buildx invocation uses BUILDX_NO_DEFAULT_ATTESTATIONS=1, oci-mediatypes=false, and inline cache!
            df = root / "Dockerfile"
            df.write_text("FROM scratch\n", encoding="utf-8")
            seen_popen: list[tuple[list[str], dict[str, str]]] = []

            class FakeProc:
                def __init__(self, cmd, env, **kwargs):
                    seen_popen.append((list(cmd), dict(env)))
                    import io

                    self.stdout = io.StringIO("")
                    self.pid = 99999

                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            with patch("luma_build.runner.subprocess.Popen", side_effect=FakeProc):
                _run_buildx_command(
                    docker_bin="docker",
                    builder_name="luma-runner",
                    buildx_state_dir=root / "bx",
                    docker_config=job_cfg,
                    push_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                    pull_host="crpi-x.cn-beijing.personal.cr.aliyuncs.com",
                    image_repo="luma-acr/acme-app",
                    tag="sha-1",
                    context_dir=root,
                    dockerfile_path=df,
                    platforms=["linux/amd64"],
                    push_insecure=False,
                    cache_mode="read-write",
                    cache_ref="crpi-x.cn-beijing.personal.cr.aliyuncs.com/luma-acr/luma-buildcache:acme-app-linux-amd64",
                    no_cache=False,
                    proxy="",
                    no_proxy="",
                    build_args={},
                    target="",
                    docker_runtime=rt,
                    timeout=120,
                    metadata_file=root / "meta.json",
                    on_line=None,
                    cancel_event=__import__("threading").Event(),
                )
            cmd, env = seen_popen[0]
            self.assertEqual(env["BUILDX_NO_DEFAULT_ATTESTATIONS"], "1")
            self.assertEqual(env["DOCKER_CONTEXT"], "orbstack")
            self.assertIn("type=image,push=true,oci-mediatypes=false", cmd)
            self.assertIn("type=inline", cmd)
            self.assertIn("crpi-x.cn-beijing.personal.cr.aliyuncs.com/luma-acr/acme-app:cache-acme-app-linux-amd64", cmd)

    def test_compose_dockerfile_path_traversal_rejected(self) -> None:
        for bad_dockerfile in ("/etc/passwd", "../../../etc/passwd", "../outside/Dockerfile"):
            with self.subTest(dockerfile=bad_dockerfile), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)

                def fake_clone(*, repo_url, ref, dest, git_auth, proxy="", _df=bad_dockerfile):
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / "luma.compose.yml").write_text(
                        "name: stack-evil\ncompose: docker-compose.yml\nregion: cn\n", encoding="utf-8"
                    )
                    (dest / "docker-compose.yml").write_text(
                        f"services:\n  web:\n    build:\n      context: .\n      dockerfile: {_df}\n",
                        encoding="utf-8",
                    )
                    return "abcdef123456", "abcdef123456" + "0" * 28

                with patch("luma_build.runner._clone_repo", side_effect=fake_clone), patch(
                    "luma_build.runner._ensure_runner_buildx", return_value="luma-runner"
                ), patch("luma_build.runner._run_buildx_command") as mock_bx:
                    with self.assertRaisesRegex(BuildError, "escapes repository root"):
                        execute_build_task(
                            {
                                "repoUrl": "https://github.com/acme/stack.git",
                                "repoSlug": "acme/stack",
                                "ref": "main",
                                "registryHost": "100.66.177.70:5000",
                                "pushHost": "100.66.177.70:5000",
                                "platforms": ["linux/amd64"],
                            },
                            docker_bin="docker",
                            native_platform="linux/amd64",
                            buildx_state_dir=root / "buildx",
                        )
                    mock_bx.assert_not_called()

    def test_deployer_enforces_authorized_app_name_and_allows_auto_discovery(self) -> None:
        from luma_build.deployer import ControlDeployClient

        client = ControlDeployClient("https://luma.example.com", "deploy-token")
        with patch.object(client, "_stream_or_post", return_value={"service": "orders"}) as mock_post:
            # Matching appName succeeds
            client.deploy_built_artifact(
                build_record={
                    "repoUrl": "https://github.com/acme/orders.git",
                    "ref": "main",
                    "request": {"appName": "orders"},
                    "result": {
                        "kind": "service",
                        "image": "registry.test/acme/orders@sha256:" + "a" * 64,
                        "manifest": "name: orders\nregion: cn\n",
                    },
                }
            )
            self.assertEqual(mock_post.call_count, 1)

            # Compromised runner overriding name against request.appName is rejected
            with self.assertRaisesRegex(BuildError, "does not match authorized app"):
                client.deploy_built_artifact(
                    build_record={
                        "repoUrl": "https://github.com/acme/orders.git",
                        "ref": "main",
                        "request": {"appName": "orders"},
                        "result": {
                            "kind": "service",
                            "image": "registry.test/acme/orders@sha256:" + "a" * 64,
                            "manifest": "name: prod-auth\nregion: cn\n",
                        },
                    }
                )

            # Compromised runner overriding compose sidecar name against request.manifest is rejected
            with self.assertRaisesRegex(BuildError, "does not match authorized app"):
                client.deploy_built_artifact(
                    build_record={
                        "repoUrl": "https://github.com/acme/stack.git",
                        "ref": "main",
                        "request": {"manifest": "name: staging-stack\ncompose: docker-compose.yml\n"},
                        "result": {
                            "kind": "compose",
                            "composeContent": "services:\n  web:\n    image: x\n",
                            "manifest": "name: prod-stack\ncompose: docker-compose.yml\n",
                        },
                    }
                )

            # Auto-discovered manifest without appName or request.manifest still succeeds
            client.deploy_built_artifact(
                build_record={
                    "repoUrl": "https://github.com/acme/auto.git",
                    "ref": "main",
                    "request": {},
                    "result": {
                        "kind": "service",
                        "image": "registry.test/acme/auto@sha256:" + "b" * 64,
                        "manifest": "name: auto-svc\nregion: cn\n",
                    },
                }
            )
            self.assertEqual(mock_post.call_count, 2)

    def test_secret_box_pbkdf2_derivation_with_legacy_sha256_fallback(self) -> None:
        from cryptography.fernet import Fernet
        from luma_build.crypto import SecretBox, _derive_fernet_key, _derive_legacy_fernet_key

        master = "super-secret-master-key-1234"
        pbkdf2_key = _derive_fernet_key(master)
        legacy_key = _derive_legacy_fernet_key(master)
        self.assertNotEqual(pbkdf2_key, legacy_key)

        legacy_token = Fernet(legacy_key).encrypt(b"legacy-credential").decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"LUMA_BUILD_MASTER_KEY": master}, clear=False
        ):
            box = SecretBox.from_env_or_file(Path(tmp))
            # Reads legacy SHA-256 encrypted values seamlessly
            self.assertEqual(box.decrypt(legacy_token), "legacy-credential")
            # Writes new values using PBKDF2-derived key
            new_token = box.encrypt("new-credential")
            self.assertEqual(box.decrypt(new_token), "new-credential")
            self.assertEqual(Fernet(pbkdf2_key).decrypt(new_token.encode("ascii")), b"new-credential")

    def test_idle_runner_poll_throttles_last_seen_writes(self) -> None:
        from luma_build.store import BuildStore

        with tempfile.TemporaryDirectory() as tmp:
            store = BuildStore(Path(tmp) / "data", default_registry_host="100.66.177.70:5000")
            rtok = store.create_runner_token(name="idle-r1")["runnerToken"]
            first = store.poll_and_lease(rtok, {"nativePlatform": "linux/amd64"}, now=1000)
            self.assertIsNone(first["task"])
            self.assertEqual(first["runner"]["lastSeenAt"], 1000)

            # Polling 2 seconds later with no queued builds does NOT update last_seen_at
            second = store.poll_and_lease(rtok, {"nativePlatform": "linux/amd64"}, now=1002)
            self.assertIsNone(second["task"])
            self.assertEqual(second["runner"]["lastSeenAt"], 1000)

            # Polling 15 seconds later refreshes last_seen_at
            third = store.poll_and_lease(rtok, {"nativePlatform": "linux/amd64"}, now=1015)
            self.assertIsNone(third["task"])
            self.assertEqual(third["runner"]["lastSeenAt"], 1015)

    def test_runner_and_http_shims_delegate_to_canonical_luma_implementation(self) -> None:
        from luma import builder_runner as canonical_runner
        from luma import runner_http as canonical_http
        from luma_build import runner as compat_runner
        from luma_build import runner_http as compat_http

        self.assertIs(compat_http.interruptible_urlopen, canonical_http.interruptible_urlopen)
        self.assertTrue(issubclass(compat_runner.RunnerDaemon, canonical_runner.RunnerDaemon))
        self.assertTrue(issubclass(compat_runner.PlatformRequeueRequired, BuildError))

        with patch("luma_build.runner.find_docker_binary", side_effect=canonical_runner.LumaError("no docker")):
            with self.assertRaisesRegex(BuildError, "no docker"):
                compat_runner.RunnerDaemon(
                    server_url="https://build.test",
                    runner_token="lbr_test",
                    state_dir=Path("/tmp/unused-runner-state"),
                )


if __name__ == "__main__":
    unittest.main()
