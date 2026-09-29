"""Control API handlers: deployments, nodes, agents, registry, storage and dashboard."""
import base64
import errno
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, call, patch

import yaml

from luma.assets import asset_path
from luma.config import LumaConfig
from luma.compose import load_compose_deployment
from luma.control.server import _node_record_for_name, _run_host_prep_container, _state_nodes, ensure_image_present, ensure_image_pull_egress_proxy, ensure_image_pull_network, handle_application_restart, handle_certificate_retry, handle_compose_deployment, handle_compose_deployment_preview, handle_control_status, handle_dashboard, handle_dashboard_logs, handle_deployment, handle_deployment_config, handle_deployment_preview, handle_fleet_update, handle_node_agent_complete, handle_node_agent_lease, handle_node_agent_token, handle_node_label, handle_node_nomad_join, handle_node_register, handle_node_unregister, handle_registry_list, handle_registry_remove, handle_registry_set, handle_secret_list, handle_secret_remove, handle_secret_set, handle_service_history, handle_service_pull_diagnostics, handle_service_remove, handle_service_rollback, handle_storage_apply, handle_storage_list, handle_storage_remove, handle_storage_set, image_pull_requires_egress, resolve_registry_image_digest, resolve_service_image, resolve_service_node_pin
from luma.compose import DEFAULT_NFS_MOUNT_OPTIONS
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.registry import DEFAULT_DOCKER_REGISTRY, registry_host_from_image
from luma.service import ServiceSpec, load_service
from tests.support import _restore_env, _set_env


class ControlApiTests(unittest.TestCase):
    def test_agent_manager_update_runs_in_independent_systemd_unit_and_reports_status(self):
        from luma.agent import manager_control_update_status, start_manager_control_update

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "manager-updates"
            executable = Path(tmp) / "home" / "tao" / ".local" / "bin" / "luma"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            old_root = _set_env("LUMA_MANAGER_UPDATE_ROOT", str(root))
            completed = Mock(returncode=0, stdout="")
            try:
                with patch("luma.agent.node_agent_os", return_value="linux"), patch(
                    "luma.agent.shutil.which", return_value="/usr/bin/systemd-run"
                ), patch("luma.agent._installed_luma_executable", return_value=str(executable)), patch(
                    "luma.agent._current_install_layout",
                    return_value=(Path(tmp) / "home" / "tao", Path(tmp) / "home" / "tao" / ".local" / "share" / "luma", executable.parent),
                ), patch("luma.agent.subprocess.run", return_value=completed) as run:
                    result = start_manager_control_update(
                        install_ref="v0.1.173",
                        control_image="ghcr.io/liutianjie/luma-control:v0.1.173",
                        domain="luma.example.com",
                        watchdog_peers=["100.69.154.50"],
                    )
                invocation = run.call_args.args[0]
                self.assertEqual(invocation[0], "systemd-run")
                self.assertIn("--property=Type=exec", invocation)
                self.assertIn(str(executable), invocation)
                self.assertIn("--setenv=LUMA_USER_HOME=" + str(Path(tmp) / "home" / "tao"), invocation)
                self.assertIn(
                    "--setenv=LUMA_TAILSCALE_WATCHDOG_PEERS=100.69.154.50",
                    invocation,
                )
                self.assertNotIn("management-token", " ".join(invocation))

                update_id = str(result["updateId"])
                (root / f"{update_id}.status").write_text("0\n", encoding="utf-8")
                (root / f"{update_id}.log").write_text("control healthy\n", encoding="utf-8")
                status = manager_control_update_status(update_id=update_id)
                self.assertEqual(status["status"], "succeeded")
                self.assertEqual(status["log"], ["control healthy"])
            finally:
                _restore_env("LUMA_MANAGER_UPDATE_ROOT", old_root)

    def test_manager_update_handler_uses_manager_agent_transient_update_capability(self):
        from luma.control.server import handle_manager_update_start


        state = {
            "clusterId": "luma-test",
            "domain": "luma.example.com",
            "deployToken": "management-token",
            "nodes": {
                "manager": {
                    "labels": {"role.nomad-manager": "true"},
                    "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": ["manager-update-v1"]},
                },
                "lab": {
                    "tailscaleIP": "100.69.154.50",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": [],
                    },
                },
            },
        }
        result = {
            "taskId": "task-1",
            "updateId": "manager-1783835000000-aabbccdd",
            "status": "running",
            "installRef": "v0.1.173",
            "controlImage": "ghcr.io/liutianjie/luma-control:v0.1.173",
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server._run_node_agent_task", return_value=result
        ) as run, patch(
            "luma.control.server._prepared_control_image",
            return_value="ghcr.io/liutianjie/luma-control:v0.1.173",
        ), patch(
            "luma.control.server._manager_update_baseline",
            return_value={"routeOk": True, "controlVersion": "0.1.173"},
        ), patch(
            "luma.control.server._set_control_maintenance"
        ), patch(
            "luma.control.server._manager_update_operation_write"
        ):
            response = handle_manager_update_start(
                "management-token",
                {
                    "installRef": "v0.1.173",
                },
            )

        self.assertEqual(response["updateId"], "manager-1783835000000-aabbccdd")
        self.assertEqual(response["managerNode"], "manager")
        self.assertEqual(run.call_args.args[2], "start-manager-update")
        self.assertEqual(run.call_args.kwargs["required_capability"], "manager-update-v1")
        self.assertEqual(run.call_args.args[3]["domain"], "luma.example.com")
        self.assertEqual(
            run.call_args.args[3]["tailscaleWatchdogPeers"],
            ["100.69.154.50"],
        )


    def test_agent_mirrors_control_image_with_proxy_and_verifies_digest(self):
        from luma.agent import mirror_control_image
        from luma.local import LocalResult

        digest = "sha256:" + "a" * 64
        events = []
        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming",
            side_effect=[LocalResult(0, "copy complete"), LocalResult(0, digest + "\n")],
        ) as run:
            result = mirror_control_image(
                source_image="ghcr.io/liutianjie/luma-control:v0.1.175",
                push_image="localhost:5000/luma-control:v0.1.175",
                destination_image="100.64.0.70:5000/luma-control:v0.1.175",
                proxy="http://100.106.154.3:7890",
                insecure=True,
                progress=events.append,
            )

        copy_command = run.call_args_list[0].args[0]
        self.assertEqual(copy_command[:2], ["/usr/local/bin/crane", "copy"])
        self.assertIn("--insecure", copy_command)
        self.assertEqual(run.call_args_list[0].kwargs["env"]["HTTPS_PROXY"], "http://100.106.154.3:7890")
        self.assertIn("localhost:5000", run.call_args_list[0].kwargs["env"]["NO_PROXY"])
        self.assertEqual(result["destinationImage"], "100.64.0.70:5000/luma-control:v0.1.175")
        self.assertEqual(result["digest"], digest)
        self.assertTrue(any("verified" in str(event.get("line") or "").lower() for event in events))

    def test_agent_mirror_uses_ephemeral_registry_auth_config(self):
        from luma.agent import mirror_control_image
        from luma.local import LocalResult

        digest = "sha256:" + "c" * 64
        observed: list[dict[str, Any]] = []

        def run(_command, **kwargs):
            docker_config = Path(kwargs["env"]["DOCKER_CONFIG"])
            observed.append(json.loads((docker_config / "config.json").read_text(encoding="utf-8")))
            return LocalResult(0, "copy complete" if len(observed) == 1 else digest + "\n")

        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming", side_effect=run
        ) as process:
            mirror_control_image(
                source_image="ghcr.io/liutianjie/luma-control:v0.1.212",
                push_image="registry.example.com/luma-control:v0.1.212",
                destination_image="registry.example.com/luma-control:v0.1.212",
                registry_auth={"username": "lae", "password": "registry-secret", "serveraddress": "registry.example.com"},
            )

        self.assertEqual(len(observed), 2)
        self.assertIn("registry.example.com", observed[0]["auths"])
        docker_config_path = Path(process.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"])
        self.assertFalse(docker_config_path.exists())
        self.assertNotIn("registry-secret", repr(process.call_args_list))

    def test_agent_mirrors_system_image_for_one_runtime_platform(self):
        from luma.agent import mirror_system_image
        from luma.local import LocalResult

        digest = "sha256:" + "b" * 64
        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming",
            side_effect=[LocalResult(0, "copy complete"), LocalResult(0, digest + "\n")],
        ) as run:
            result = mirror_system_image(
                source_image="registry:2",
                push_image="localhost:5000/luma-system/registry-runtime:test",
                destination_image="100.64.0.70:5000/luma-system/registry-runtime:test",
                platform="linux/amd64",
                insecure=True,
            )

        copy_command = run.call_args_list[0].args[0]
        self.assertIn("--platform", copy_command)
        self.assertEqual(copy_command[copy_command.index("--platform") + 1], "linux/amd64")
        self.assertEqual(result["digest"], digest)
        self.assertIn("System image", result["message"])

    def test_agent_caches_runtime_image_with_separate_source_and_destination_auth(self):
        from luma.agent import cache_runtime_image
        from luma.local import LocalResult

        digest = "sha256:" + "d" * 64
        observed: list[dict[str, Any]] = []

        def run(_command, **kwargs):
            docker_config = Path(kwargs["env"]["DOCKER_CONFIG"])
            observed.append(json.loads((docker_config / "config.json").read_text(encoding="utf-8")))
            return LocalResult(0, "copy complete" if len(observed) == 1 else digest + "\n")

        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming", side_effect=run
        ) as process:
            result = cache_runtime_image(
                source_image="private.example.com/acme/api:latest",
                push_image="builder:5000/luma-cache/private.example.com/acme/api:cache",
                destination_image="100.64.0.70:5000/luma-cache/private.example.com/acme/api:cache",
                source_registry_auth={
                    "username": "source-user",
                    "password": "source-secret",
                    "serveraddress": "private.example.com",
                },
                destination_registry_auth={
                    "username": "cache-user",
                    "password": "cache-secret",
                    "serveraddress": "builder:5000",
                },
                insecure=True,
            )

        self.assertEqual(result["digest"], digest)
        self.assertIn("private.example.com", observed[0]["auths"])
        self.assertIn("builder:5000", observed[0]["auths"])
        self.assertFalse(Path(process.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"]).exists())
        self.assertNotIn("source-secret", repr(process.call_args_list))
        self.assertNotIn("cache-secret", repr(process.call_args_list))

    def test_control_routes_external_runtime_image_through_builder_cache(self):
        from luma.config import LumaConfig
        from luma.control.server import _cache_runtime_image_on_builder

        digest = "sha256:" + "e" * 64
        state = {
            "build": {
                "defaultNode": "builder",
                "registryHost": "100.64.0.70:5000",
                "pushHost": "100.64.0.70:5000",
            }
        }
        with patch(
            "luma.control.server._require_build_node", return_value="builder"
        ), patch(
            "luma.control.server._run_node_agent_task",
            return_value={"digest": digest},
        ) as task:
            result = _cache_runtime_image_on_builder(
                LumaConfig({}, None),
                state,
                "ghcr.io/acme/private-api:latest",
                platform="linux/amd64",
            )

        self.assertEqual(result["builderNode"], "builder")
        self.assertTrue(result["cached"])
        self.assertEqual(
            result["deployed"],
            "100.64.0.70:5000/luma-cache/ghcr.io/acme/private-api@" + digest,
        )
        self.assertEqual(task.call_args.args[2], "cache-runtime-image")
        payload = task.call_args.args[3]
        self.assertEqual(payload["sourceImage"], "ghcr.io/acme/private-api:latest")
        self.assertEqual(payload["platform"], "linux/amd64")
        self.assertNotIn("sourceRegistryAuth", payload)
        self.assertNotIn("destinationRegistryAuth", payload)

    def test_control_keeps_existing_builder_registry_image_without_copy(self):
        from luma.config import LumaConfig
        from luma.control.server import _cache_runtime_image_on_builder

        state = {
            "build": {
                "defaultNode": "builder",
                "registryHost": "100.64.0.70:5000",
                "pushHost": "100.64.0.70:5000",
            }
        }
        with patch(
            "luma.control.server._require_build_node", return_value="builder"
        ), patch("luma.control.server._run_node_agent_task") as task:
            result = _cache_runtime_image_on_builder(
                LumaConfig({}, None),
                state,
                "100.64.0.70:5000/acme/api:abc123",
            )

        self.assertFalse(result["cached"])
        self.assertEqual(result["deployed"], "100.64.0.70:5000/acme/api:abc123")
        task.assert_not_called()

    def test_compose_runtime_cache_reuses_duplicate_image_copy(self):
        from luma.compose import ComposeDeploymentSpec
        from luma.config import LumaConfig
        from luma.control.server import _cache_compose_images_on_builder

        deployment = ComposeDeploymentSpec(
            source=Path("luma.compose.yml"),
            compose_path=Path("docker-compose.yml"),
            compose={
                "services": {
                    "api": {"image": "ghcr.io/acme/api:1"},
                    "worker": {"image": "ghcr.io/acme/api:1"},
                }
            },
            name="stack",
            region="cn",
            storage_classes={},
            volumes={},
            services={},
        )
        state = {
            "build": {
                "defaultNode": "builder",
                "registryHost": "100.64.0.70:5000",
                "pushHost": "100.64.0.70:5000",
            }
        }
        cached = {
            "deployed": "100.64.0.70:5000/luma-cache/ghcr.io/acme/api@sha256:" + "f" * 64,
            "cacheImage": "100.64.0.70:5000/luma-cache/ghcr.io/acme/api:cache",
            "builderNode": "builder",
            "cached": True,
        }
        with patch(
            "luma.control.server._cache_runtime_image_on_builder",
            return_value=cached,
        ) as cache:
            resolved, result = _cache_compose_images_on_builder(
                LumaConfig({}, None), state, deployment
            )

        self.assertEqual(cache.call_count, 1)
        self.assertEqual(
            resolved.compose["services"]["api"]["image"], cached["deployed"]
        )
        self.assertEqual(
            resolved.compose["services"]["worker"]["image"], cached["deployed"]
        )
        self.assertEqual(len(result["images"]), 2)

    def test_async_control_image_preparation_persists_progress_and_internal_ref(self):
        from luma.control.server import handle_control_image_prepare_get, handle_control_image_prepare_start
        from luma.control.state import save_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                save_state(
                    {
                        "clusterId": "luma-test",
                        "deployToken": "management-token",
                        "build": {
                            "defaultNode": "builder",
                            "nodes": ["builder"],
                            "registryHost": "100.64.0.70:5000",
                            "pushHost": "localhost:5000",
                        },
                        "nodes": {
                            "manager": {
                                "labels": {"role.nomad-manager": "true"},
                                "agent": {
                                    "status": "online",
                                    "lastSeen": int(time.time()),
                                    "os": "linux",
                                    "arch": "amd64",
                                    "capabilities": ["manager-update-v1"],
                                },
                            },
                            "builder": {
                                "roles": ["builder"],
                                "agent": {
                                    "status": "online",
                                    "lastSeen": int(time.time()),
                                    "capabilities": ["docker-build", "control-image-mirror-v1"],
                                },
                            }
                        },
                    }
                )

                def mirror(_state, node, action, payload, **kwargs):
                    self.assertEqual(node, "builder")
                    self.assertEqual(action, "mirror-control-image")
                    self.assertEqual(payload["pushImage"], "localhost:5000/luma-control:v0.1.175")
                    self.assertEqual(payload["platform"], "linux/amd64")
                    self.assertEqual(kwargs["required_capability"], "control-image-mirror-v1")
                    kwargs["progress"]({"line": "copying layers"})
                    return {
                        "taskId": "task-1",
                        "destinationImage": payload["destinationImage"],
                        "digest": "sha256:" + "b" * 64,
                        "message": "cached",
                    }

                with patch("luma.control.server.load_config", return_value=Mock()), patch(
                    "luma.control.server._egress_proxy_for_node", return_value="http://proxy:7890"
                ), patch("luma.control.server._run_node_agent_task", side_effect=mirror):
                    started = handle_control_image_prepare_start(
                        "management-token",
                        {
                            "installRef": "v0.1.175",
                            "controlImage": "ghcr.io/liutianjie/luma-control:v0.1.175",
                        },
                    )
                    deadline = time.time() + 3
                    current = started
                    while current.get("status") in {"queued", "running"} and time.time() < deadline:
                        time.sleep(0.02)
                        current = handle_control_image_prepare_get("management-token", str(started["id"]))

                self.assertEqual(current["status"], "succeeded")
                self.assertEqual(current["result"]["destinationImage"], "100.64.0.70:5000/luma-control:v0.1.175")
                self.assertIn("copying layers", current["log"])
                self.assertNotIn("proxy", current["plan"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_async_fleet_update_persists_node_progress_and_recovers_by_id(self):
        from luma.control.server import handle_fleet_update_operation_get, handle_fleet_update_operation_start
        from luma.control.state import save_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                save_state({"clusterId": "luma-test", "deployToken": "management-token"})

                def run_fleet(_token, request, *, progress=None):
                    self.assertEqual(request["nodeNames"], ["lab"])
                    if progress:
                        progress({"nodeName": "lab", "status": "pending", "agentVersionBefore": "0.1.172"})
                        progress({"nodeName": "lab", "status": "succeeded", "message": "updated"})
                    return {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0, "results": []}

                with patch("luma.control.server.handle_fleet_update", side_effect=run_fleet):
                    started = handle_fleet_update_operation_start(
                        "management-token",
                        {"installRef": "v0.1.173", "nodeNames": ["lab"]},
                    )
                    deadline = time.time() + 3
                    current = started
                    while current.get("status") in {"queued", "running"} and time.time() < deadline:
                        time.sleep(0.02)
                        current = handle_fleet_update_operation_get("management-token", str(started["id"]))

                self.assertEqual(current["status"], "succeeded")
                self.assertEqual(current["nodes"][0]["nodeName"], "lab")
                self.assertEqual(current["nodes"][0]["status"], "succeeded")
                self.assertNotIn("output", current["nodes"][0])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_dashboard_route_sentinel_reports_structured_route_health(self):
        from luma.control.server import handle_route_sentinel

        state = {"clusterId": "luma-test", "deployToken": "management-token"}
        routes = {
            "app": {"kind": "http", "domain": "app.example.com"},
            "tcp": {"kind": "tcp", "domain": "tcp.example.com"},
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server.load_config", return_value=Mock()
        ), patch("luma.control.server._dashboard_route_files", return_value=routes), patch(
            "luma.control.server._sentinel_active_http_domains",
            return_value={"app.example.com"},
        ), patch(
            "luma.control.server._sentinel_probe_public_route",
            return_value={"domain": "app.example.com", "status": 200, "ok": True, "latencyMs": 12, "error": ""},
        ):
            result = handle_route_sentinel("management-token", {})

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)

    def test_route_sentinel_domain_probe_is_independent_from_service_probe(self):
        from luma.control.server import _sentinel_probe_public_route

        unauthorized = urllib.error.HTTPError(
            "https://app.example.com/", 401, "unauthorized", {}, None
        )
        missing = urllib.error.HTTPError(
            "https://missing.example.com/", 404, "missing", {}, None
        )
        with patch(
            "luma.control.server.urllib.request.urlopen",
            side_effect=[unauthorized, missing],
        ):
            published = _sentinel_probe_public_route("app.example.com")
            unpublished = _sentinel_probe_public_route("missing.example.com")

        self.assertTrue(published["ok"])
        self.assertEqual(published["status"], 401)
        self.assertFalse(unpublished["ok"])
        self.assertEqual(unpublished["status"], 404)

    def test_route_sentinel_accepts_application_owned_json_404(self):
        import io
        from luma.control.server import _sentinel_probe_public_route

        application_404 = urllib.error.HTTPError(
            "https://api.example.com/",
            404,
            "missing",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"detail":"Not Found"}'),
        )
        with patch(
            "luma.control.server.urllib.request.urlopen",
            side_effect=application_404,
        ):
            result = _sentinel_probe_public_route("api.example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 404)

    def test_route_sentinel_excludes_stale_route_files_from_default_inventory(self):
        from luma.control.server import handle_route_sentinel

        state = {"clusterId": "luma-test", "deployToken": "management-token"}
        routes = {
            "active": {"kind": "http", "domain": "active.example.com"},
            "stale": {"kind": "http", "domain": "stale.example.com"},
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server.load_config", return_value=Mock()
        ), patch("luma.control.server._dashboard_route_files", return_value=routes), patch(
            "luma.control.server._sentinel_active_http_domains",
            return_value={"active.example.com"},
        ), patch(
            "luma.control.server._sentinel_probe_public_route",
            return_value={"domain": "active.example.com", "status": 200, "ok": True, "latencyMs": 4, "error": ""},
        ) as probe:
            result = handle_route_sentinel("management-token", {})

        probe.assert_called_once_with("active.example.com")
        self.assertEqual(result["total"], 1)

    def test_route_sentinel_inventory_joins_active_deployments_not_route_files(self):
        from luma.control.server import _sentinel_active_http_domains

        state = {
            "deployments": {
                "services": {
                    "app": {
                        "status": "active",
                        "manifest": "name: app\nexposure: tailscale-relay\ndomain: active.example.com\n",
                    }
                }
            }
        }
        routes = {
            "app": {"kind": "http", "domain": "active.example.com"},
            "old-app": {"kind": "http", "domain": "stale.example.com"},
        }
        errors = []
        with patch("luma.control.server.nomad_services_summary", return_value=[]):
            domains = _sentinel_active_http_domains(Mock(), state, routes, errors)

        self.assertEqual(domains, {"active.example.com"})
        self.assertEqual(errors, [])

    def test_prune_agent_tasks_drops_old_terminal_keeps_active_and_recent(self):
        from luma.control.server import (
            AGENT_TASK_PROGRESS_LIMIT,
            AGENT_TASK_RETENTION_SECONDS,
            _prune_agent_tasks,
        )

        now = 1_000_000
        old = now - AGENT_TASK_RETENTION_SECONDS - 10
        recent = now - 5
        state = {
            "agentTasks": {
                "old-done": {"status": "succeeded", "completedAt": old},
                "old-failed": {"status": "failed", "completedAt": old},
                "old-timeout": {"status": "timeout", "updatedAt": old},
                "recent-done": {
                    "status": "succeeded",
                    "completedAt": recent,
                    "message": "x" * 10_000,
                    "progress": [{"line": str(index)} for index in range(500)],
                },
                "old-queued": {"status": "queued", "createdAt": old},      # active: keep
                "old-running": {"status": "running", "updatedAt": old},    # active: keep
            }
        }
        _prune_agent_tasks(state, now=now)
        survivors = set(state["agentTasks"])
        # old terminal tasks gone; active tasks and recent terminal survive
        self.assertEqual(survivors, {"recent-done", "old-queued", "old-running"})
        recent_task = state["agentTasks"]["recent-done"]
        self.assertEqual(len(recent_task["progress"]), AGENT_TASK_PROGRESS_LIMIT)
        self.assertEqual(recent_task["progress"][0]["line"], "200")
        self.assertEqual(recent_task["progressOffset"], 200)
        self.assertEqual(len(recent_task["message"]), 4000)

        with patch("luma.control.server.AGENT_TASK_PROGRESS_LIMIT", 50):
            _prune_agent_tasks(state, now=now)
        self.assertEqual(len(recent_task["progress"]), 50)
        self.assertEqual(recent_task["progress"][0]["line"], "450")
        self.assertEqual(recent_task["progressOffset"], 450)

    def test_agent_idle_long_poll_persists_only_initial_heartbeat(self):
        from luma.control import database as control_database

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker": {
                        "nodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(
                    state["deployToken"],
                    {"nodeName": "worker", "nodeId": "worker-node-id"},
                )
                original_save = control_database.write_state
                with patch("luma.control.database.write_state", wraps=original_save) as save:
                    lease = handle_node_agent_lease(
                        issued["agentToken"],
                        {
                            "nodeName": "worker",
                            "nodeId": "worker-node-id",
                            "os": "linux",
                            "capabilities": [],
                            "waitSeconds": 2,
                        },
                    )
                self.assertIsNone(lease["task"])
                self.assertEqual(save.call_count, 1)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)


        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                management_result = handle_node_register(state["deployToken"], {"nodeName": "b", "region": "global"})
                join_result = handle_node_register(state["joinToken"], {"nodeName": "c", "region": "home"})
                self.assertEqual(management_result["nodeName"], "b")
                self.assertEqual(management_result["region"], "global")
                self.assertEqual(join_result["nodeName"], "c")
                self.assertEqual(join_result["region"], "home")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_node_register_returns_rpc_addr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "aly": {
                        "status": "manager",
                        "tailscaleIP": "100.64.0.125",
                        "labels": {"role.nomad-manager": "true", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")

                result = handle_node_register(state["joinToken"], {"nodeName": "bot", "region": "global"})

                self.assertEqual(result["nomadRpcAddr"], "100.64.0.125:4647")
                self.assertEqual(result["nomadServerAddr"], "100.64.0.125:4647")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_management_and_join_tokens_can_label_node_after_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                result = handle_node_label(state["joinToken"], {"nodeName": "b", "nodeId": "node-id-b", "region": "global"})
                labels = result["labels"]
                self.assertEqual(labels["region"], "global")
                self.assertEqual(labels["luma.node.name"], "b")
                self.assertNotIn("egress", labels)
                self.assertNotIn("role.global-worker", labels)
                self.assertEqual(result["nodeName"], "b")
                self.assertEqual(result["nomadNodeId"], "node-id-b")
                management_result = handle_node_label(state["deployToken"], {"nodeName": "b", "nodeId": "node-id-b", "region": "global"})
                self.assertEqual(management_result["nodeName"], "b")
                self.assertEqual(management_result["nomadNodeId"], "node-id-b")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_join_token_labels_node_without_swarm_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"m4": {"region": "home", "status": "registered"}}
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")

                result = handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "m4-host",
                        "registeredName": "m4",
                        "nodeId": "nomad-node-id",
                        "region": "home",
                        "tailscaleIP": "100.121.94.123",
                    },
                )

                self.assertEqual(result["nodeName"], "m4")
                self.assertEqual(result["nomadNodeId"], "nomad-node-id")
                saved = load_state()
                self.assertEqual(saved["nodes"]["m4"]["nodeId"], "nomad-node-id")
                self.assertEqual(saved["nodes"]["m4"]["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nodes"]["m4"]["tailscaleIP"], "100.121.94.123")
                self.assertTrue(saved["nodes"]["m4"]["agent"]["tokenHash"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_label_node_keeps_requested_name_as_luma_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                handle_node_register(state["joinToken"], {"nodeName": "global-sg-1", "region": "global"})
                result = handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "docker-hostname",
                        "nodeId": "node-id-1",
                        "registeredName": "global-sg-1",
                        "region": "global",
                        "tailscaleIP": "100.64.0.30",
                        "tailscaleName": "global-sg-1.ts.net",
                    },
                )
                saved = load_state()
                self.assertEqual(result["nodeName"], "global-sg-1")
                self.assertEqual(result["displayName"], "global-sg-1")
                self.assertEqual(result["tailscaleIP"], "100.64.0.30")
                self.assertEqual(result["tailscaleName"], "global-sg-1.ts.net")
                self.assertIn("global-sg-1", saved["nodes"])
                self.assertEqual(saved["nodes"]["global-sg-1"]["nomadHostname"], "docker-hostname")
                self.assertEqual(saved["nodes"]["global-sg-1"]["nomadNodeId"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleIP"], "100.64.0.30")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleName"], "global-sg-1.ts.net")
                self.assertEqual(saved["nodes"]["global-sg-1"]["labels"]["luma.node.id"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["status"], "labeled")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_rejoin_updates_nomad_identity_without_swarm_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "m4-host",
                        "nodeId": "old-node-id",
                        "registeredName": "m4",
                        "region": "home",
                    },
                )
                with patch("luma.control.server.docker_request") as docker:
                    result = handle_node_label(
                        state["joinToken"],
                        {
                            "nodeName": "m4-host",
                            "nodeId": "new-node-id",
                            "registeredName": "m4",
                            "region": "home",
                        },
                    )
                saved = load_state()
                docker.assert_not_called()
                self.assertEqual(saved["nodes"]["m4"]["nomadNodeId"], "new-node-id")
                self.assertEqual(saved["nodes"]["m4"]["nodeId"], "new-node-id")
                self.assertEqual(result["previousNodeId"], "old-node-id")
                self.assertNotIn("pinnedServicesUpdated", result)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_agent_join_updates_node_identity_and_keeps_agent_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.125:4647"
                state["nodes"] = {
                    "bot": {
                        "region": "global",
                        "status": "labeled",
                        "nodeId": "agent-node-id",
                        "labels": {"region": "global", "luma.node.name": "bot", "luma.node.id": "agent-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "bot", "nodeId": "agent-node-id"})
                agent_token = issued["agentToken"]
                handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "bot")
                    self.assertEqual(action, "join-nomad")
                    self.assertEqual(payload["serverAddr"], "100.64.0.125:4647")
                    self.assertNotIn("egressProxy", payload)
                    self.assertEqual(kwargs["required_capability"], "nomad-join")
                    return {
                        "taskId": "task-join",
                        "nodeName": "bot-host",
                        "nodeId": "nomad-node-id",
                        "tailscaleIP": "100.80.0.20",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task):
                    result = handle_node_nomad_join(state["deployToken"], {"nodeName": "bot"})

                saved = load_state()["nodes"]["bot"]
                self.assertEqual(result["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nodeId"], "nomad-node-id")
                self.assertEqual(saved["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nomadHostname"], "bot-host")
                self.assertEqual(saved["tailscaleIP"], "100.80.0.20")
                self.assertIn("agent-node-id", saved["agent"]["knownNodeIds"])
                self.assertEqual(saved["agent"]["nodeId"], "agent-node-id")
                lease = handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )
                self.assertIsNone(lease["task"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_nomad_join_task_lease_injects_tailscale_key_without_persisting_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["secrets"] = {"TAILSCALE_AUTHKEY": "ts-secret"}
                state["nodes"] = {
                    "bot": {
                        "region": "global",
                        "nodeId": "agent-node-id",
                        "labels": {"region": "global", "luma.node.name": "bot", "luma.node.id": "agent-node-id"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "bot", "nodeId": "agent-node-id"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-join"] = {
                    "id": "task-join",
                    "nodeName": "bot",
                    "action": "join-nomad",
                    "payload": {"nodeName": "bot", "region": "global", "serverAddr": "100.64.0.125:4647"},
                    "status": "queued",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    issued["agentToken"],
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )["task"]

                self.assertEqual(leased["payload"]["tailscaleAuthKey"], "ts-secret")
                saved_payload = load_state()["agentTasks"]["task-join"]["payload"]
                self.assertNotIn("tailscaleAuthKey", saved_payload)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_runs_ready_node_agents_and_reports_skipped_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": now, "os": "darwin", "capabilities": ["docker-volume", "luma-update"]},
                    },
                    "lab": {
                        "region": "home",
                        "agent": {"status": "offline", "lastSeen": now - 1000, "os": "linux", "capabilities": ["docker-volume"]},
                    },
                }
                save_state(state)

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(action, "update-luma")
                    self.assertEqual(payload["installRef"], "main")
                    self.assertEqual(kwargs["required_capability"], "luma-update")
                    return {"taskId": "task-1", "message": "Luma installer finished", "installRef": payload["installRef"]}

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task) as run:
                    result = handle_fleet_update(state["deployToken"], {"installRef": "main", "includeAll": True, "timeout": 120})

                run.assert_called_once()
                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["skipped"], 1)
                by_name = {item["nodeName"]: item for item in result["results"]}
                self.assertEqual(by_name["home-mac-mini"]["status"], "succeeded")
                self.assertEqual(by_name["lab"]["status"], "skipped")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_injects_node_scoped_egress_proxy_for_cn_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.106.154.3:4647"
                state["nodes"] = {
                    "cn-2": {
                        "region": "cn",
                        "labels": {"region": "cn", "luma.node.name": "cn-2"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update", "luma-update-proxy-v1"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "cn-2")
                    self.assertEqual(action, "update-luma")
                    self.assertEqual(payload["installRef"], "v0.1.235")
                    self.assertEqual(payload["proxy"], "http://100.106.154.3:7890")
                    self.assertEqual(kwargs["required_capability"], "luma-update")
                    return {
                        "taskId": "task-update",
                        "message": "Luma installer finished",
                        "installRef": payload["installRef"],
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.235", "nodeNames": ["cn-2"]},
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["failed"], 0)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_fleet_update_fails_fast_when_legacy_cn_agent_cannot_receive_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.106.154.3:4647"
                state["nodes"] = {
                    "cn-2": {
                        "region": "cn",
                        "labels": {"region": "cn", "luma.node.name": "cn-2"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.235", "nodeNames": ["cn-2"]},
                    )

                run.assert_not_called()
                self.assertEqual(result["failed"], 1)
                self.assertIn("one-time exact-ref bootstrap", result["results"][0]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_fleet_update_explicit_empty_target_list_updates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.173", "includeAll": True, "nodeNames": []},
                    )

                run.assert_not_called()
                self.assertEqual(result["total"], 0)
                self.assertEqual(result["results"], [])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_verifies_new_agent_heartbeat_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "version": "0.1.172",
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                def run_task(*_args, **_kwargs):
                    current = load_state()
                    current["nodes"]["lab"]["agent"]["lastSeen"] = int(time.time()) + 2
                    current["nodes"]["lab"]["agent"]["version"] = "0.1.173"
                    save_state(current)
                    return {"taskId": "task-1", "message": "installer finished", "installRef": "v0.1.173"}

                events = []
                with patch("luma.control.server._run_node_agent_task", side_effect=run_task), patch(
                    "luma.control.server.time.sleep", return_value=None
                ):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {
                            "installRef": "v0.1.173",
                            "includeAll": True,
                            "nodeNames": ["lab"],
                            "waitReadySeconds": 5,
                        },
                        progress=events.append,
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["agentVersionBefore"], "0.1.172")
                self.assertEqual(result["results"][0]["agentVersionAfter"], "0.1.173")
                self.assertIn("installing", [event["status"] for event in events])
                self.assertIn("verifying", [event["status"] for event in events])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_commit_update_verifies_reported_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "version": "0.1.221",
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                def run_task(*_args, **_kwargs):
                    current = load_state()
                    current["nodes"]["lab"]["agent"]["lastSeen"] = int(time.time()) + 2
                    current["nodes"]["lab"]["agent"]["version"] = "0.1.222"
                    save_state(current)
                    return {
                        "taskId": "task-1",
                        "message": "installer finished",
                        "installRef": "a" * 40,
                        "installedVersion": "0.1.222",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task), patch(
                    "luma.control.server.time.sleep", return_value=None
                ):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {
                            "installRef": "a" * 40,
                            "nodeNames": ["lab"],
                            "waitReadySeconds": 5,
                        },
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["installedVersion"], "0.1.222")
                self.assertEqual(result["results"][0]["agentVersionAfter"], "0.1.222")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_skips_manager_nodes_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "manager": {
                        "region": "cn",
                        "status": "manager",
                        "swarmRole": "manager",
                        "swarmManager": True,
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["luma-update"]},
                    },
                    "home-mac-mini": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": now, "os": "darwin", "capabilities": ["luma-update"]},
                    },
                }
                save_state(state)

                def run_task(_state, node_name, action, payload, **_kwargs):
                    self.assertEqual(node_name, "home-mac-mini")
                    self.assertEqual(action, "update-luma")
                    return {"message": f"updated {node_name}", "installRef": payload.get("installRef") or ""}

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task) as run:
                    result = handle_fleet_update(state["deployToken"], {"includeAll": True})

                run.assert_called_once()
                by_name = {item["nodeName"]: item for item in result["results"]}
                self.assertEqual(by_name["manager"]["status"], "skipped")
                self.assertIn("manager node is skipped", by_name["manager"]["message"])
                self.assertEqual(by_name["home-mac-mini"]["status"], "succeeded")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_can_include_manager_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager": {
                        "region": "cn",
                        "status": "manager",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["luma-update"]},
                    },
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task", return_value={"message": "updated manager"}) as run:
                    result = handle_fleet_update(state["deployToken"], {"includeManager": True})

                run.assert_called_once()
                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["nodeName"], "manager")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_skips_ready_agents_without_update_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "old-agent": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["docker-volume"]},
                    }
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(state["deployToken"], {"includeAll": True})
                run.assert_not_called()
                self.assertEqual(result["succeeded"], 0)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["results"][0]["status"], "skipped")
                self.assertIn("does not support fleet update", result["results"][0]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_register_rejects_unknown_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaises(Exception):
                    handle_node_register(state["joinToken"], {"nodeName": "b", "region": "mars"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_registered_only_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                handle_node_register(state["joinToken"], {"nodeName": "m3max", "region": "home"})
                result = handle_node_unregister(state["deployToken"], {"nodeName": "m3max"})
                saved = load_state()
                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertNotIn("m3max", saved.get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_registered_nomad_node_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mini": {
                        "region": "home",
                        "displayName": "home-mini",
                        "hostname": "orbstack",
                        "nodeId": "node-id-1",
                        "nomadNodeId": "node-id-1",
                        "labels": {"luma.node.name": "home-mini", "luma.node.id": "node-id-1", "region": "home"},
                    }
                }
                save_state(state)
                nomad = Mock()
                with patch("luma.control.server.docker_request") as docker, patch(
                    "luma.control.server.NomadApi", return_value=nomad
                ):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "home-mini"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                docker.assert_not_called()
                self.assertGreaterEqual(nomad.request.call_count, 2)
                self.assertEqual(
                    nomad.request.call_args_list[0].args,
                    (
                        "POST",
                        "/v1/node/node-id-1/drain",
                        {
                            "DrainSpec": {"Deadline": 0, "IgnoreSystemJobs": True},
                            "MarkEligible": False,
                            "Meta": {"message": "removed by Luma node remove: home-mini"},
                        },
                    ),
                )
                self.assertEqual(
                    nomad.request.call_args_list[1].args,
                    ("POST", "/v1/node/node-id-1/eligibility", {"Eligibility": "ineligible"}),
                )
                self.assertNotIn("home-mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_prefers_saved_luma_record_over_duplicate_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mini": {
                        "region": "home",
                        "status": "labeled",
                        "displayName": "home-mini",
                        "hostname": "orbstack",
                        "nodeId": "stale-node-id",
                        "nomadNodeId": "stale-node-id",
                        "labels": {"luma.node.name": "home-mini", "luma.node.id": "stale-node-id", "region": "home"},
                    },
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "active-node-id",
                        "nomadNodeId": "active-node-id",
                        "labels": {"luma.node.name": "home-mac-mini", "luma.node.id": "active-node-id", "region": "home"},
                    },
                }
                save_state(state)
                nomad = Mock()
                with patch("luma.control.server.docker_request") as docker, patch(
                    "luma.control.server.NomadApi", return_value=nomad
                ):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "home-mini"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                docker.assert_not_called()
                self.assertEqual(nomad.request.call_args_list[0].args[1], "/v1/node/stale-node-id/drain")
                saved_nodes = load_state().get("nodes", {})
                self.assertNotIn("home-mini", saved_nodes)
                self.assertIn("home-mac-mini", saved_nodes)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_stale_alias_never_drains_shared_manager_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                shared_id = "manager-node-id"
                state["nodes"] = {
                    "aly": {
                        "region": "cn",
                        "status": "labeled",
                        # Live Nomad reconciliation can copy server identity
                        # fields onto the stale alias because both records share
                        # one node ID. The authoritative sibling still makes
                        # alias-only removal safe.
                        "nomadRole": "server",
                        "nomadServer": True,
                        "nodeId": shared_id,
                        "labels": {
                            "luma.node.name": "aly",
                            "luma.node.id": shared_id,
                            "region": "cn",
                        },
                    },
                    "manager-hostname": {
                        "region": "cn",
                        "status": "manager",
                        "nomadRole": "server",
                        "nomadServer": True,
                        "nodeId": shared_id,
                        "nomadNodeId": shared_id,
                        "labels": {
                            "luma.node.name": "manager-hostname",
                            "luma.node.id": shared_id,
                            "role.nomad-manager": "true",
                            "region": "cn",
                        },
                    },
                }
                save_state(state)
                with patch("luma.control.server.NomadApi") as nomad:
                    result = handle_node_unregister(
                        state["deployToken"], {"nodeName": "aly"}
                    )

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertFalse(result["nomadDrained"])
                self.assertEqual(
                    result["nomadDrainSkipped"], "shared_manager_identity"
                )
                nomad.assert_not_called()
                saved_nodes = load_state().get("nodes", {})
                self.assertNotIn("aly", saved_nodes)
                self.assertIn("manager-hostname", saved_nodes)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_reports_missing_node_without_docker_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                with patch("luma.control.server.docker_request") as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "home-mini"})

                self.assertFalse(result["removed"])
                self.assertFalse(result["registeredRemoved"])
                docker.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_drains_nomad_node_left_after_prior_state_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = [
                    [{"ID": "node-id-cn-2", "Name": "VM-0-10-ubuntu"}],
                    {"Meta": {"luma_node_name": "cn-2"}},
                    {},
                    {},
                ]

                with patch("luma.control.server.NomadApi", return_value=nomad):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "cn-2"})

                self.assertTrue(result["removed"])
                self.assertFalse(result["registeredRemoved"])
                self.assertTrue(result["nomadDrained"])
                self.assertEqual(result["nomadNodeId"], "node-id-cn-2")
                self.assertEqual(nomad.request.call_args_list[2].args[1], "/v1/node/node-id-cn-2/drain")
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_refuses_to_remove_manager_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"manager": {"region": "cn", "status": "manager", "nomadRole": "server"}}
                save_state(state)
                with self.assertRaisesRegex(LumaError, "refusing to unregister Nomad manager"):
                    handle_node_unregister(state["deployToken"], {"nodeName": "manager"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_does_not_call_docker_when_removing_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-mini": {"region": "home", "displayName": "home-mini"}}
                save_state(state)
                with patch("luma.control.server.docker_request", side_effect=LumaError("Docker unavailable")) as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "home-mini"})

                self.assertTrue(result["removed"])
                docker.assert_not_called()
                self.assertNotIn("home-mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_deletes_luma_record_when_nomad_node_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "region": "home",
                        "status": "labeled",
                        "displayName": "home-2",
                        "nodeId": "stale-node-id",
                        "nomadNodeId": "stale-node-id",
                        "labels": {"luma.node.name": "home-2", "luma.node.id": "stale-node-id", "region": "home"},
                    }
                }
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = LumaError("Nomad API error 404: node not found")
                with patch("luma.control.server.NomadApi", return_value=nomad):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "home-2"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertFalse(result["nomadDrained"])
                self.assertEqual(result["nomadNodeId"], "stale-node-id")
                self.assertEqual(result["nomadDrainSkipped"], "nomad_node_not_found")
                self.assertEqual(result["message"], "Node removed: home-2")
                nomad.request.assert_called_once()
                self.assertEqual(
                    nomad.request.call_args.args[:2],
                    ("POST", "/v1/node/stale-node-id/drain"),
                )
                self.assertNotIn("home-2", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_skips_drain_when_nomad_reports_node_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "stale-home": {
                        "region": "home",
                        "displayName": "stale-home",
                        "nomadNodeId": "gone-id",
                    }
                }
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = LumaError("Nomad API error 400: node not found")
                with patch("luma.control.server.NomadApi", return_value=nomad):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "stale-home"})

                self.assertTrue(result["removed"])
                self.assertFalse(result["nomadDrained"])
                self.assertEqual(result["nomadDrainSkipped"], "nomad_node_not_found")
                self.assertNotIn("stale-home", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_still_fails_when_nomad_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "region": "home",
                        "displayName": "home-2",
                        "nomadNodeId": "stale-node-id",
                    }
                }
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = LumaError(
                    "Nomad API unavailable at http://nomad.example: Connection refused. "
                    "Check that the Nomad agent is running and nomadAddr is reachable "
                    "from the luma-control container."
                )
                with patch("luma.control.server.NomadApi", return_value=nomad):
                    with self.assertRaisesRegex(LumaError, "Nomad API unavailable"):
                        handle_node_unregister(state["deployToken"], {"nodeName": "home-2"})
                self.assertIn("home-2", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_still_fails_when_nomad_auth_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "region": "home",
                        "displayName": "home-2",
                        "nomadNodeId": "stale-node-id",
                    }
                }
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = LumaError("Nomad API error 403: Permission denied")
                with patch("luma.control.server.NomadApi", return_value=nomad):
                    with self.assertRaisesRegex(LumaError, "Nomad API error 403"):
                        handle_node_unregister(state["deployToken"], {"nodeName": "home-2"})
                self.assertIn("home-2", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_resolves_luma_node_name_to_nomad_meta_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "mac-mini-home": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "node-id-mini",
                        "nomadNodeId": "node-id-mini",
                        "labels": {
                            "region": "home",
                            "luma.node.name": "mac-mini-home",
                            "luma.node.id": "node-id-mini",
                        },
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "node": "mac-mini-home",
                        "exposure": "none",
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual(result["service"], "home-panel")
                stack = (root / "stacks" / "home" / "home-panel" / "home-panel.nomad.json").read_text(encoding="utf-8")
                self.assertIn('"LTarget": "${meta.luma_node_name}"', stack)
                self.assertIn('"RTarget": "mac-mini-home"', stack)
                self.assertNotIn("node.hostname", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_refreshes_nomad_cni_hostports_after_nomad_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [{"node": "home-2", "deleted": 1}], "skipped": []},
                    create=True,
                ) as refresh, patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["cniHostports"]["nodes"], ["home-2"])
                refresh.assert_called_once()
                self.assertEqual(refresh.call_args.args[2], "api")
                self.assertEqual(refresh.call_args.kwargs["fallback_nodes"], ["home-2"])
                self.assertEqual(refresh.call_args.kwargs["ports"], [18080])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_does_not_truncate_live_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = root / "routes" / "api.yml"
                original_write_text = Path.write_text

                def guarded_write_text(path, *args, **kwargs):
                    if Path(path) == route_target:
                        raise AssertionError("route file was overwritten directly")
                    return original_write_text(path, *args, **kwargs)

                with patch.object(Path, "write_text", autospec=True, side_effect=guarded_write_text), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["service"], "api")
                self.assertIn("http://100.64.0.3:18080", route_target.read_text(encoding="utf-8"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_stages_route_write_outside_watched_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = routes_root / "api.yml"
                replaced_sources: list[Path] = []
                real_replace = os.replace

                def record_replace(src, dst):
                    if Path(dst) == route_target:
                        replaced_sources.append(Path(src))
                    return real_replace(src, dst)

                with patch("luma.control.server.os.replace", side_effect=record_replace), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertTrue(replaced_sources)
                self.assertNotEqual(replaced_sources[0].parent, routes_root)
                self.assertNotIn(routes_root, replaced_sources[0].parents)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_route_write_falls_back_when_staging_crosses_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = routes_root / "api.yml"
                real_replace = os.replace
                exdev_raised = False
                fallback_sources: list[Path] = []

                def replace_with_cross_device_once(src, dst):
                    nonlocal exdev_raised
                    if Path(dst) == route_target and not exdev_raised:
                        exdev_raised = True
                        raise OSError(errno.EXDEV, "Invalid cross-device link")
                    if Path(dst) == route_target:
                        fallback_sources.append(Path(src))
                    return real_replace(src, dst)

                with patch("luma.control.server.os.replace", side_effect=replace_with_cross_device_once), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertTrue(exdev_raised)
                self.assertTrue(fallback_sources)
                self.assertEqual(fallback_sources[-1].parent, routes_root / ".luma-route-staging")
                self.assertEqual(fallback_sources[-1].suffix, ".tmp")
                self.assertFalse(list((routes_root / ".luma-route-staging").glob("*.tmp")))
                self.assertIn("http://100.64.0.3:18080", route_target.read_text(encoding="utf-8"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_validates_route_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                routes_root.mkdir()
                route_target = routes_root / "api.yml"
                previous_route = "http:\n  routers:\n    api:\n      rule: Host(`api.example.com`)\n"
                route_target.write_text(previous_route, encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [], "skipped": []},
                ), patch("luma.control.server.render_tailscale_route", return_value="not: a-traefik-route\n"):
                    with self.assertRaisesRegex(LumaError, "invalid route file"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(route_target.read_text(encoding="utf-8"), previous_route)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_waits_for_rollout_before_recreating_gateway_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "home-2",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [], "skipped": []},
                ), patch("luma.control.server.resolve_nomad_static_route_target", side_effect=lambda service, _state, **_kwargs: service), patch(
                    "luma.control.server._probe_public_route",
                    side_effect=[
                        LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 504"),
                        "Public route reachable: https://api.example.com/ -> HTTP 200",
                    ],
                ) as probe, patch(
                    "luma.control.server.handle_application_restart",
                    return_value={"mode": "recreate", "restarted": [{"allocId": "alloc-api", "task": "*", "mode": "recreate"}]},
                ) as restart:
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(probe.call_count, 2)
                restart.assert_not_called()
                self.assertIn("Public route settled after rollout", result["probe"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertNotIn("Recover application upstream", steps)
                self.assertIn("Probe public route=ok:Public route settled after rollout", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_reconciles_traefik_404_without_restarting_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "stackRoot": str(root / "stacks"),
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "api.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    api: {}\n  services:\n    api: {}\n",
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": [], "results": [], "skipped": []},
                ), patch(
                    "luma.control.server._probe_public_route",
                    side_effect=[
                        LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 404 (Traefik router not found)"),
                        "Public route reachable: https://api.example.com/ -> HTTP 200",
                    ],
                ) as probe, patch(
                    "luma.control.server.handle_application_restart",
                    return_value={"mode": "recreate", "restarted": [{"allocId": "alloc-api", "task": "*", "mode": "recreate"}]},
                ) as restart:
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(probe.call_count, 2)
                restart.assert_not_called()
                self.assertFalse(stale_route.exists())
                self.assertIn("Recovered public route after provider reconciliation", result["probe"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Remove stale file-provider route=ok:Removed stale file-provider route", steps)
                self.assertIn("Reconcile Traefik provider=ok:waiting for Nomad provider convergence", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_persistent_traefik_404_recreates_ingress_not_application(self):
        from luma.control.server import _probe_public_route_with_recovery

        service = ServiceSpec(
            source=Path("api.yaml"),
            name="api",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="api.example.com",
            port=80,
        )
        miss = LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 404 (Traefik router not found)")
        steps = []
        with patch("luma.control.server._probe_public_route", side_effect=miss), patch(
            "luma.control.server._wait_for_public_route",
            side_effect=[miss, "Public route reachable: https://api.example.com/ -> HTTP 200"],
        ) as wait, patch(
            "luma.control.server._recover_traefik_ingress",
            return_value="Traefik allocation recreated (new-traefik)",
        ) as recover_ingress, patch(
            "luma.control.server._recover_public_route_allocation"
        ) as recover_app:
            result = _probe_public_route_with_recovery(
                "deploy-token",
                service,
                stack="api",
                skip_orchestrator=False,
                steps=steps,
            )

        self.assertEqual(wait.call_count, 2)
        recover_ingress.assert_called_once_with()
        recover_app.assert_not_called()
        self.assertIn("Recovered public route after Traefik recreate", result)

    def test_persistent_gateway_failure_waits_then_recreates_application(self):
        from luma.control.server import _probe_public_route_with_recovery

        service = ServiceSpec(
            source=Path("api.yaml"),
            name="api",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="api.example.com",
            port=80,
        )
        gateway = LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 502")
        steps = []
        with patch("luma.control.server._probe_public_route", side_effect=gateway), patch(
            "luma.control.server._wait_for_public_route",
            side_effect=[gateway, "Public route reachable: https://api.example.com/ -> HTTP 200"],
        ) as wait, patch(
            "luma.control.server._recover_public_route_allocation",
            return_value="allocation recreate completed (1 replaced by 1 running allocation(s))",
        ) as recover_app, patch(
            "luma.control.server._recover_traefik_ingress"
        ) as recover_ingress:
            result = _probe_public_route_with_recovery(
                "deploy-token",
                service,
                stack="api",
                skip_orchestrator=False,
                steps=steps,
            )

        self.assertEqual(wait.call_count, 2)
        recover_app.assert_called_once_with("deploy-token", "api")
        recover_ingress.assert_not_called()
        self.assertIn("Recovered public route after allocation recreate", result)

    def test_nomad_node_pin_does_not_require_swarm_node_id(self):
        state = {"nodes": {"lab": {"name": "lab", "region": "home", "status": "ready"}}}
        service = ServiceSpec(
            source=Path("kato.yaml"),
            name="kato",
            image="ghcr.io/liutianjie/kato:latest",
            region="home",
            node="lab",
            exposure="none",
        )
        resolved = resolve_service_node_pin(service, state, engine="nomad")
        self.assertEqual(resolved.node, "lab")
        self.assertIsNone(resolved.node_id)

    def test_node_record_lookup_accepts_aliases(self):
        nodes = {
            "mini": {
                "displayName": "mini",
                "aliases": ["home-mac-mini", "Mac.lan"],
                "region": "home",
            }
        }

        self.assertIs(_node_record_for_name(nodes, "mini"), nodes["mini"])
        self.assertIs(_node_record_for_name(nodes, "home-mac-mini"), nodes["mini"])
        self.assertIs(_node_record_for_name(nodes, "Mac.lan"), nodes["mini"])

    def test_dashboard_nodes_accept_terminal_alias_connections(self):
        from luma.control.server import _dashboard_nodes, _registered_nodes_summary, _update_agent_heartbeat

        record = {}
        _update_agent_heartbeat(record, {"version": "0.1.173", "capabilities": ["terminal"], "os": "linux"})
        registered = _registered_nodes_summary({"bot": record})
        self.assertEqual(registered[0]["agentVersion"], "0.1.173")

        rows = _dashboard_nodes(
            [
                {
                    "name": "bot",
                    "displayName": "bot",
                    "hostname": "global-sg-1",
                    "aliases": ["global-sg-1"],
                    "agentStatus": "ready",
                    "agentVersion": "0.1.173",
                    "storageCapabilities": ["terminal"],
                }
            ],
            [],
            terminal_nodes={"global-sg-1"},
        )

        self.assertEqual(rows[0]["name"], "bot")
        self.assertTrue(rows[0]["terminalConnected"])
        self.assertEqual(rows[0]["terminalStatus"], "connected")
        self.assertEqual(rows[0]["agentVersion"], "0.1.173")

    def test_dashboard_nodes_merge_orchestrator_by_hostname(self):
        from luma.control.server import _dashboard_nodes

        rows = _dashboard_nodes(
            [
                {
                    "name": "manager",
                    "displayName": "manager",
                    "hostname": "iZmanager",
                    "region": "cn",
                    "agentStatus": "ready",
                }
            ],
            [
                {
                    "hostname": "iZmanager",
                    "state": "ready",
                    "availability": "eligible",
                    "leader": True,
                }
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "manager")
        self.assertEqual(rows[0]["state"], "ready")
        self.assertTrue(rows[0]["leader"])
        self.assertEqual(rows[0]["region"], "cn")

    def test_dashboard_nodes_ignore_unmatched_nomad_ids(self):
        from luma.control.server import _dashboard_nodes

        rows = _dashboard_nodes(
            [
                {
                    "name": "manager",
                    "displayName": "manager",
                    "hostname": "iZmanager",
                    "aliases": ["iZmanager"],
                    "region": "cn",
                    "agentStatus": "ready",
                }
            ],
            [
                {
                    "hostname": "iZmanager",
                    "state": "ready",
                    "leader": True,
                },
                {
                    "id": "24e56d1b-82b0-bec6-cca5-598fde372832",
                    "hostname": "24e56d1b-82b0-bec6-cca5-598fde372832",
                    "state": "ready",
                    "availability": "eligible",
                },
            ],
        )
        self.assertEqual([row["name"] for row in rows], ["manager"])

    def test_dashboard_nodes_merge_orchestrator_by_nomad_id(self):
        from luma.control.server import _dashboard_nodes

        rows = _dashboard_nodes(
            [
                {
                    "name": "ppt",
                    "displayName": "ppt",
                    "hostname": "ppt-host",
                    "nodeId": "24e56d1b-82b0-bec6-cca5-598fde372832",
                    "region": "cn",
                    "agentStatus": "ready",
                }
            ],
            [
                {
                    "id": "24e56d1b-82b0-bec6-cca5-598fde372832",
                    "hostname": "other-name",
                    "state": "ready",
                    "availability": "eligible",
                }
            ],
        )
        self.assertEqual([row["name"] for row in rows], ["ppt"])
        self.assertEqual(rows[0]["state"], "ready")
        self.assertEqual(rows[0]["availability"], "eligible")

    def test_state_nodes_expands_aliases_for_internal_resolution(self):
        state = {
            "nodes": {
                "aly": {
                    "displayName": "aly",
                    "aliases": ["iZ0jl8auywzycory05d9cuZ"],
                    "region": "cn",
                }
            }
        }

        nodes = _state_nodes(state)
        self.assertEqual(nodes["aly"]["region"], "cn")
        self.assertEqual(nodes["iZ0jl8auywzycory05d9cuZ"]["region"], "cn")

    def test_pinned_deployment_validates_image_for_target_node_platform_before_stack_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "platform": {"os": "linux", "arch": "aarch64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "pura",
                        "image": "ghcr.io/acme/pura:main",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "none",
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=LumaError("target node Docker pull failed; image does not provide a manifest for target platform linux/arm64"),
                ) as agent:
                    with self.assertRaisesRegex(LumaError, "target platform linux/arm64"):
                        handle_deployment(
                            state["deployToken"],
                            {"manifest": manifest, "sourceName": "pura.yaml", "skipDns": True, "skipOrchestrator": True},
                        )
                agent.assert_called_once()
                self.assertEqual(agent.call_args.args[1], "home-mac-mini")
                self.assertEqual(agent.call_args.args[2], "resolve-docker-image")
                self.assertEqual(agent.call_args.args[3]["platform"], "linux/arm64")
                self.assertFalse((root / "stacks" / "home" / "pura" / "pura.nomad.json").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_deployment_renders_target_platform_manifest_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "nomadAttributes": {"os.name": "linux", "cpu.arch": "aarch64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "pura",
                        "image": "ghcr.io/acme/pura:main",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "none",
                    }
                )
                digest = "ghcr.io/acme/pura@sha256:58307df1e4f8efcfec29a8f7a6653c65446d6afded7d03caa3329f2c0ac92719"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_registry_image_digest",
                    side_effect=AssertionError("Control must not resolve a pinned target image"),
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"deployed": digest, "digest": digest, "message": "Target node image pull ready"},
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "pura.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                agent.assert_called_once()
                self.assertEqual(agent.call_args.args[1], "home-mac-mini")
                self.assertEqual(agent.call_args.args[2], "resolve-docker-image")
                self.assertEqual(agent.call_args.args[3]["platform"], "linux/arm64")
                stack = (root / "stacks" / "home" / "pura" / "pura.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertEqual(result["image"]["platform"], "linux/arm64")
                self.assertEqual(result["image"]["resolvedBy"], "target-node")
                self.assertIn(f'"image": "{digest}"', stack)
                self.assertNotIn("ghcr.io/acme/pura:main", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_private_deployment_sends_registry_auth_to_target_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-lab",
                        "nomadNodeId": "node-id-lab",
                        "nomadAttributes": {"os.name": "linux", "cpu.arch": "amd64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "lab", "luma.node.id": "node-id-lab"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "gcode.gaojiua.com:3000", "username": "nick", "password": "secret"},
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "docs",
                        "image": "gcode.gaojiua.com:3000/acme/docs:latest",
                        "region": "home",
                        "node": "lab",
                        "exposure": "none",
                    }
                )
                digest = "gcode.gaojiua.com:3000/acme/docs@sha256:58307df1e4f8efcfec29a8f7a6653c65446d6afded7d03caa3329f2c0ac92719"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_registry_image_digest",
                    side_effect=AssertionError("Control must not resolve a pinned target's private mutable image"),
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"deployed": digest, "digest": digest, "message": "Target node image pull ready"},
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "docs.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                payload = agent.call_args.args[3]
                self.assertEqual(payload["image"], "gcode.gaojiua.com:3000/acme/docs:latest")
                self.assertTrue(payload["forcePull"])
                self.assertEqual(payload["platform"], "linux/amd64")
                self.assertEqual(payload["registryAuth"]["password"], "secret")
                self.assertTrue(result["image"]["registryAuth"])
                stack = (root / "stacks" / "home" / "docs" / "docs.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f'"image": "{digest}"', stack)
                self.assertIn('"server_address": "gcode.gaojiua.com:3000"', stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_refuses_pinned_down_nomad_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "nomadStatus": "down",
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                service = ServiceSpec(
                    source=Path("home-panel.yaml"),
                    name="home-panel",
                    image="ghcr.io/me/home-panel:1",
                    region="home",
                    node="home-mac-mini",
                    exposure="none",
                )
                with self.assertRaisesRegex(LumaError, "Nomad node is down"):
                    resolve_service_node_pin(service, state)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_refuses_pinned_nomad_node_when_scheduling_ineligible(self):
        state = {
            "nodes": {
                "home-mac-mini": {
                    "region": "home",
                    "nodeId": "home-node-id",
                    "nomadNodeId": "home-node-id",
                    "schedulingEligibility": "ineligible",
                    "labels": {"luma.node.name": "home-mac-mini", "luma.node.id": "home-node-id"},
                }
            }
        }
        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="ghcr.io/me/home-panel:1",
            region="home",
            node="home-mac-mini",
            exposure="none",
        )
        with self.assertRaisesRegex(LumaError, "scheduling eligibility is ineligible"):
            resolve_service_node_pin(service, state)

    def test_deployment_uses_control_state_and_portainer_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com"},
                                "portainer": {},
                            },
                            "defaults": {"stackRoot": str(root / "stacks")},
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                probe_error = urllib.error.HTTPError("https://api.example.com/", 404, "not found", {}, None)
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertIn(str(root / "stacks" / "cn" / "api" / "api.nomad.json"), result["written"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Parse manifest=ok:api -> cn/cn-edge", steps)
                self.assertIn("Sync DNS=ok:DNS updated", steps)
                self.assertIn("Deploy Nomad job=ok:Nomad job deployed", steps)
                self.assertIn("Probe public route=ok:Public route reachable: https://api.example.com/ -> HTTP 404", steps)
                deployment_state = load_state()
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["name"], "api")
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["manifest"], manifest)
                with self.assertRaises(Exception):
                    handle_deployment(state["joinToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_tailscale_relay_public_probe_checks_https_domain(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="nginx:alpine",
            region="home",
            exposure="tailscale-relay",
            domain="panel.example.com",
            port=8080,
        )
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch("luma.control.server.urllib.request.urlopen", return_value=response) as urlopen:
            result = _probe_public_route(service)

        self.assertEqual(result, "Public route reachable: https://panel.example.com/ -> HTTP 200")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://panel.example.com/")
        self.assertEqual(urlopen.call_args.args[0].get_method(), "HEAD")

    def test_public_probe_rejects_gateway_statuses(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="nginx:alpine",
            region="home",
            exposure="tailscale-relay",
            domain="panel.example.com",
            port=8080,
        )
        probe_error = urllib.error.HTTPError("https://panel.example.com/", 504, "gateway timeout", {}, None)
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            with self.assertRaisesRegex(LumaError, "Public route unhealthy: https://panel.example.com/ -> HTTP 504"):
                _probe_public_route(service)

    def test_public_probe_rejects_traefik_route_miss_404(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("panel.yaml"),
            name="panel",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="panel.example.com",
            port=80,
        )
        probe_error = urllib.error.HTTPError(
            "https://panel.example.com/",
            404,
            "not found",
            {"Server": "Traefik", "Content-Type": "text/plain; charset=utf-8"},
            io.BytesIO(b"404 page not found\n"),
        )
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            with self.assertRaisesRegex(LumaError, "Traefik router not found"):
                _probe_public_route(service)

    def test_public_probe_allows_app_default_404_body_without_traefik_header(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("panel.yaml"),
            name="panel",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="panel.example.com",
            port=80,
        )
        probe_error = urllib.error.HTTPError(
            "https://panel.example.com/",
            404,
            "not found",
            {"Content-Type": "text/plain; charset=utf-8"},
            io.BytesIO(b"404 page not found\n"),
        )
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            result = _probe_public_route(service)

        self.assertEqual(result, "Public route reachable: https://panel.example.com/ -> HTTP 404 (the app may not serve /)")

    def test_deployment_failure_after_manager_work_leaves_failed_partial_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch("luma.control.server._probe_public_route", side_effect=LumaError("route refused")):
                    with self.assertRaisesRegex(LumaError, "Probe public route failed"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                deployment = load_state()["deployments"]["services"]["api"]
                self.assertEqual(deployment["status"], "failed_partial")
                self.assertIn("Probe public route failed", deployment["lastError"])
                self.assertEqual(deployment["manifest"], manifest)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in deployment["steps"])
                self.assertIn("Sync DNS=ok:DNS updated", steps)
                self.assertIn("Deploy Nomad job=ok:Nomad job deployed", steps)
                self.assertIn("Probe public route=fail:route refused", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_deployment_preview_renders_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "edge": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "edge-node-id",
                        "labels": {"luma.node.id": "edge-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "node": "edge",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.sync_dns") as sync, patch("luma.control.server.deploy_to_nomad") as deploy:
                    result = handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["summary"]["exposure"], "cn-edge")
                self.assertEqual(result["artifacts"][0]["kind"], "job")
                self.assertIn('"LTarget": "${meta.luma_node_name}"', result["artifacts"][0]["content"])
                self.assertIn('"RTarget": "edge"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
                sync.assert_not_called()
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_preview_keeps_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"API_PASSWORD": "${API_PASSWORD}"},
                    }
                )
                result = handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["service"], "api")
                self.assertIn('"API_PASSWORD": "${API_PASSWORD}"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_deployment_rejects_existing_compose_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "api": {
                            "kind": "compose",
                            "name": "api",
                            "slug": "api",
                            "manifest": "name: api\nregion: cn\ncompose: docker-compose.yml\n",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                with patch("luma.control.server.deploy_to_nomad") as deploy:
                    with self.assertRaisesRegex(LumaError, "deployment name already exists"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_config_returns_saved_service_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api.yaml",
                            "updatedAt": 123,
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "api")
                self.assertEqual(result["kind"], "service")
                self.assertEqual(result["name"], "api")
                self.assertEqual(result["sourceName"], "console:api.yaml")
                self.assertEqual(result["updatedAt"], 123)
                self.assertEqual(result["manifest"], manifest)
                self.assertEqual(result["composeContent"], "")
                with self.assertRaisesRegex(LumaError, "unauthorized"):
                    handle_deployment_config(state["joinToken"], "api")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_config_returns_saved_compose_manifest_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:luma.compose.yml",
                            "updatedAt": 456,
                        }
                    },
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "app-stack")
                self.assertEqual(result["kind"], "compose")
                self.assertEqual(result["name"], "app-stack")
                self.assertEqual(result["manifest"], sidecar)
                self.assertEqual(result["composeContent"], compose)
                self.assertEqual(result["updatedAt"], 456)
                with self.assertRaisesRegex(LumaError, "deployment not found"):
                    handle_deployment_config(state["deployToken"], "missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_preview_rejects_invalid_region_exposure_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "global",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with self.assertRaisesRegex(LumaError, "exposure=cn-edge requires region=cn"):
                    handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_recreates_business_stack_allocations_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {"ID": "alloc-api", "ClientStatus": "running", "TaskStates": {"api": {}}},
                        {"ID": "alloc-worker", "ClientStatus": "running", "TaskStates": {"worker": {}}},
                        {"ID": "alloc-old", "ClientStatus": "complete", "TaskStates": {"old": {}}},
                    ],
                    {},
                    {},
                    [
                        {"ID": "alloc-api-new", "ClientStatus": "running", "TaskStates": {"api": {}}},
                        {"ID": "alloc-worker-new", "ClientStatus": "running", "TaskStates": {"worker": {}}},
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})
                self.assertEqual(result["stack"], "myapp")
                self.assertEqual(result["mode"], "recreate")
                self.assertEqual(len(result["restarted"]), 2)
                self.assertEqual(result["replacementAllocations"], ["alloc-api-new", "alloc-worker-new"])
                self.assertEqual(result["delivery"]["status"], "skipped")
                api.request.assert_any_call("GET", "/v1/job/myapp/allocations")
                api.request.assert_any_call("POST", "/v1/allocation/alloc-api/stop", None)
                api.request.assert_any_call("POST", "/v1/allocation/alloc-worker/stop", None)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_recreates_pending_allocation_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-pending",
                            "ClientStatus": "pending",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        },
                        {
                            "ID": "alloc-history",
                            "ClientStatus": "failed",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        },
                    ],
                    {},
                    {
                        "ID": "myapp",
                        "TaskGroups": [{"Name": "api", "Count": 1, "Tasks": [{"Name": "api"}]}],
                    },
                    {"EvalID": "eval-recovery"},
                    [
                        {
                            "ID": "alloc-new",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["mode"], "recreate")
                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                self.assertEqual(result["recovery"]["strategy"], "force-evaluate")
                api.request.assert_any_call("POST", "/v1/allocation/alloc-pending/stop", None)
                api.request.assert_any_call(
                    "POST",
                    "/v1/job/myapp/evaluate",
                    {"JobID": "myapp", "EvalOptions": {"ForceReschedule": True}},
                )
                self.assertNotIn(
                    call("POST", "/v1/allocation/alloc-history/stop", None),
                    api.request.call_args_list,
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_recovers_unknown_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-unknown",
                            "ClientStatus": "unknown",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                    {},
                    [
                        {
                            "ID": "alloc-new",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                api.request.assert_any_call("POST", "/v1/allocation/alloc-unknown/stop", None)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_forces_evaluation_when_allocations_are_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-recovery"},
                    [{"ID": "alloc-new", "ClientStatus": "running", "DesiredStatus": "run"}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                self.assertEqual(result["recovery"]["evaluationId"], "eval-recovery")
                api.request.assert_any_call(
                    "POST",
                    "/v1/job/myapp/evaluate",
                    {"JobID": "myapp", "EvalOptions": {"ForceReschedule": True}},
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_reports_blocked_pinned_node_instead_of_missing_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-blocked"},
                    [],
                    {
                        "ID": "eval-blocked",
                        "Status": "blocked",
                        "FailedTGAllocs": {
                            "api": {
                                "ConstraintFiltered": {
                                    "${meta.region} = home": 3,
                                    "${meta.luma_node_name} = home-2": 4,
                                }
                            }
                        },
                    },
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    with self.assertRaisesRegex(
                        LumaError,
                        "requested node home-2 is unavailable, down, or scheduling-ineligible",
                    ):
                        handle_application_restart(state["deployToken"], {"stack": "myapp"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_reports_placement_failure_from_completed_parent_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-parent"},
                    [],
                    {
                        "ID": "eval-parent",
                        "Status": "complete",
                        "BlockedEval": "eval-child",
                        "FailedTGAllocs": {
                            "api": {
                                "ConstraintFiltered": {
                                    "${meta.region} = home": 3,
                                    "${meta.luma_node_name} = home-2": 3,
                                }
                            }
                        },
                    },
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    with self.assertRaisesRegex(
                        LumaError,
                        "requested node home-2 is unavailable, down, or scheduling-ineligible",
                    ):
                        handle_application_restart(state["deployToken"], {"stack": "myapp"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_restores_gc_job_from_saved_deployment_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump(
                    {
                        "name": "myapp",
                        "image": "example/myapp:1",
                        "region": "home",
                        "exposure": "none",
                    }
                )
                state["deployments"] = {
                    "services": {
                        "myapp": {
                            "kind": "service",
                            "name": "myapp",
                            "slug": "myapp",
                            "manifest": manifest,
                            "sourceName": "deploy/myapp.luma.yml",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                api = Mock()
                api.request.side_effect = [
                    LumaError("Nomad API error 404: job not found"),
                    [{"ID": "alloc-restored", "ClientStatus": "running", "DesiredStatus": "run"}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.handle_deployment",
                    return_value={"service": "myapp", "probe": "ready"},
                ) as deploy:
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-restored"])
                self.assertEqual(result["recovery"], {"strategy": "stored-deployment", "kind": "service"})
                deploy.assert_called_once()
                self.assertEqual(deploy.call_args.args[0], state["deployToken"])
                self.assertEqual(deploy.call_args.args[1]["manifest"], manifest)
                self.assertEqual(deploy.call_args.args[1]["origin"], "application-restart-recovery")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_refreshes_nomad_cni_hostports_after_allocation_recreate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "nodeId": "node-home-2",
                        "region": "home",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-api",
                            "ClientStatus": "running",
                            "TaskStates": {"api": {}},
                            "NodeID": "node-home-2",
                            "NodeName": "ubuntu-general-1",
                            "AllocatedResources": {"Shared": {"Ports": [{"Label": "http", "Value": 14173, "To": 4173}]}},
                        },
                    ],
                    {},
                    [
                        {
                            "ID": "alloc-api-new",
                            "ClientStatus": "running",
                            "TaskStates": {"api": {}},
                            "NodeID": "node-home-2",
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server._queue_node_agent_task",
                    return_value="task-cni",
                ) as queue, patch("luma.control.server._wait_node_agent_task", return_value={"deleted": 1, "staleAllocIds": ["old"]}):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["cniHostports"]["nodes"], ["home-2"])
                queue.assert_called_once()
                self.assertEqual(queue.call_args.args[2], "repair-nomad-cni-hostports")
                self.assertEqual(queue.call_args.args[3], {"ports": [14173]})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_can_restart_single_task_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {"ID": "alloc-app", "ClientStatus": "running", "TaskStates": {"api": {}, "worker": {}}},
                    ],
                    {},
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp", "service": "api"})
                self.assertEqual(result["mode"], "task")
                self.assertEqual(result["restarted"], [{"allocId": "alloc-app", "task": "api", "mode": "task"}])
                api.request.assert_any_call("POST", "/v1/client/allocation/alloc-app/restart", {"TaskName": "api"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_removes_stale_http_file_route_for_nomad_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-2": {
                        "name": "cn-2",
                        "nodeId": "node-cn-2",
                        "tailscaleIP": "100.64.29.91",
                    }
                }
                manifest = yaml.safe_dump(
                    {
                        "name": "api-gateway",
                        "image": "example/gateway:1",
                        "region": "cn",
                        "public": True,
                        "exposure": "cn-edge",
                        "domain": "gateway.example.com",
                        "port": 8787,
                        "publishPort": 8787,
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api-gateway": {
                            "kind": "service",
                            "name": "api-gateway",
                            "slug": "api-gateway",
                            "manifest": manifest,
                            "sourceName": "gateway.yaml",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "engine": "nomad",
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "api-gateway.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    api-gateway: {}\n  services:\n    api-gateway: {}\n",
                    encoding="utf-8",
                )
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-old",
                            "ClientStatus": "running",
                            "TaskStates": {"gateway": {}},
                            "NodeID": "node-cn-2",
                        }
                    ],
                    {},
                    [{"ID": "alloc-new", "ClientStatus": "running", "TaskStates": {"gateway": {}}}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.sync_dns", return_value="DNS unchanged"
                ), patch(
                    "luma.control.server._wait_for_public_route", return_value="Public route reachable"
                ):
                    result = handle_application_restart(state["deployToken"], {"stack": "api-gateway"})

                self.assertFalse(stale_route.exists())
                self.assertEqual(result["delivery"]["status"], "ready")
                self.assertEqual(
                    result["delivery"]["routes"],
                    [f"Removed stale file-provider route: {stale_route}"],
                )
                self.assertEqual(result["delivery"]["probes"], ["Public route reachable"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_removes_all_stale_compose_edge_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-2": {"name": "cn-2", "nodeId": "node-cn-2", "tailscaleIP": "100.64.29.91"}
                }
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "web": {"image": "example/web:1"},
                            "admin": {"image": "example/admin:1"},
                        }
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "multi-http",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "services": {
                            "web": {
                                "node": "cn-2",
                                "exposure": "cn-edge",
                                "domain": "web.example.com",
                                "port": 3000,
                                "publishPort": 13000,
                            },
                            "admin": {
                                "node": "cn-2",
                                "exposure": "cn-edge",
                                "domain": "admin.example.com",
                                "port": 3001,
                                "publishPort": 13001,
                            },
                        },
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "multi-http": {
                            "kind": "compose",
                            "name": "multi-http",
                            "slug": "multi-http",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                web_route = routes / "multi-http-web.yml"
                admin_route = routes / "multi-http-admin.yml"
                for route in (web_route, admin_route):
                    route.write_text(
                        "http:\n  routers:\n    stale: {}\n  services:\n    stale: {}\n",
                        encoding="utf-8",
                    )
                api = Mock()
                api.request.side_effect = [
                    [{"ID": "alloc-old", "ClientStatus": "running", "TaskStates": {"web": {}, "admin": {}}}],
                    {},
                    [{"ID": "alloc-new", "ClientStatus": "running", "TaskStates": {"web": {}, "admin": {}}}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.sync_dns", return_value="DNS unchanged"
                ), patch(
                    "luma.control.server._wait_for_public_route", return_value="Public route reachable"
                ):
                    result = handle_application_restart(state["deployToken"], {"stack": "multi-http"})

                self.assertFalse(web_route.exists())
                self.assertFalse(admin_route.exists())
                self.assertEqual(len(result["delivery"]["routes"]), 2)
                self.assertEqual(len(result["delivery"]["probes"]), 2)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_rejects_system_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaisesRegex(LumaError, "system stack"):
                    handle_application_restart(state["deployToken"], {"stack": "traefik"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_certificate_retry_reloads_matching_http_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                route_file = routes / "tikhub.yml"
                route_text = yaml.safe_dump(
                    {
                        "http": {
                            "routers": {
                                "tikhub": {
                                    "rule": "Host(`tikhub.example.net`)",
                                    "entryPoints": ["websecure"],
                                    "tls": {"certResolver": "letsencrypt"},
                                    "service": "tikhub",
                                }
                            },
                            "services": {
                                "tikhub": {"loadBalancer": {"servers": [{"url": "http://100.64.0.10:8082"}]}}
                            },
                        }
                    }
                )
                route_file.write_text(route_text, encoding="utf-8")
                old_mtime = route_file.stat().st_mtime_ns
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with patch("luma.control.server.NomadApi") as nomad_api:
                    result = handle_certificate_retry(
                        state["deployToken"],
                        {"domain": "tikhub.example.net", "routeId": "tikhub"},
                    )
                self.assertEqual(result["mode"], "route-file-reload")
                self.assertEqual(result["routeId"], "tikhub")
                self.assertEqual(result["certResolver"], "letsencrypt")
                self.assertEqual(route_file.read_text(encoding="utf-8"), route_text)
                self.assertGreaterEqual(route_file.stat().st_mtime_ns, old_mtime)
                nomad_api.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_certificate_retry_does_not_revalidate_atypical_route_file(self):
        # Cert retry rewrites a file's own bytes to trigger a reload; it must NOT
        # re-validate the (unchanged, already-live) file, whose on-disk shape may
        # predate the current renderer (e.g. an inline provider backend with no
        # sibling `services` map). Re-validating would newly reject a working file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                route_file = routes / "legacy.yml"
                # HTTP router with a certResolver but no `services` map — would fail
                # _validate_route_file_text, which requires non-empty routers AND services.
                route_text = yaml.safe_dump(
                    {
                        "http": {
                            "routers": {
                                "legacy": {
                                    "rule": "Host(`legacy.example.net`)",
                                    "entryPoints": ["websecure"],
                                    "tls": {"certResolver": "letsencrypt"},
                                    "service": "legacy@file",
                                }
                            }
                        }
                    }
                )
                route_file.write_text(route_text, encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with patch("luma.control.server.NomadApi") as nomad_api:
                    result = handle_certificate_retry(
                        state["deployToken"],
                        {"domain": "legacy.example.net", "routeId": "legacy"},
                    )
                self.assertEqual(result["mode"], "route-file-reload")
                self.assertEqual(route_file.read_text(encoding="utf-8"), route_text)
                nomad_api.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_certificate_retry_rejects_tcp_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                (routes / "mysql.yml").write_text(
                    yaml.safe_dump(
                        {
                            "tcp": {
                                "routers": {"mysql": {"rule": "HostSNI(`*`)", "service": "mysql"}},
                                "services": {"mysql": {"loadBalancer": {"servers": [{"address": "100.64.0.10:3306"}]}}},
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(LumaError, "HTTP route file not found"):
                    handle_certificate_retry(state["deployToken"], {"domain": "mysql.example.net", "routeId": "mysql"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_cleans_dns_portainer_and_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                stack_dir = root / "stacks" / "home" / "home-panel"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                route_file = root / "routes" / "home-panel.yml"
                route_file.parent.mkdir(parents=True)
                route_file.write_text("http:\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                state["deployments"] = {
                    "services": {
                        "home-panel": {
                            "kind": "service",
                            "name": "home-panel",
                            "slug": "home-panel",
                            "manifest": manifest,
                            "sourceName": "console:home-panel",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns", return_value="DNS deleted: panel.example.com") as dns, patch(
                    "luma.control.server.remove_from_nomad", return_value="Nomad job removed: home-panel"
                ) as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "home-panel"})
                dns.assert_called_once()
                stack.assert_called_once()
                self.assertFalse(stack_dir.exists())
                self.assertFalse(route_file.exists())
                self.assertEqual(result["service"], "home-panel")
                self.assertIn(str(stack_dir), result["files"])
                self.assertIn(str(route_file), result["files"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Delete DNS=ok:DNS deleted: panel.example.com", steps)
                self.assertIn("Remove Nomad job=ok:Nomad job removed: home-panel", steps)
                self.assertIn("Delete generated files=ok:Generated files removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_service_history_handler_returns_slugged_nomad_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                with patch(
                    "luma.control.server.job_versions",
                    return_value=[{"version": 3, "stable": True, "image": "app:v3"}],
                ) as versions:
                    result = handle_service_history(state["deployToken"], {"name": "My App"})
                versions.assert_called_once()
                self.assertEqual(versions.call_args.kwargs["slug"], "my-app")
                self.assertEqual(result["service"], "My App")
                self.assertEqual(result["slug"], "my-app")
                self.assertEqual(result["versions"][0]["image"], "app:v3")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_rollback_handler_reverts_requested_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                with patch("luma.control.server.revert_job", return_value="Nomad job api reverted to v2") as revert:
                    result = handle_service_rollback(state["deployToken"], {"name": "api", "version": "2"})
                revert.assert_called_once()
                self.assertEqual(revert.call_args.kwargs["slug"], "api")
                self.assertEqual(revert.call_args.kwargs["version"], 2)
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["message"], "Nomad job api reverted to v2")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_dry_run_does_not_delete_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns") as dns, patch("luma.control.server.remove_from_nomad") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api", "dryRun": True})
                dns.assert_not_called()
                stack.assert_not_called()
                self.assertTrue(stack_dir.exists())
                self.assertTrue(result["dryRun"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Nomad job would be removed: api", steps)
                self.assertIn("Generated files would be removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_by_name_uses_registered_manifest_and_forgets_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api"})
                stack.assert_called_once()
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["sourceName"], "console:api")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_requires_registered_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {"services": {}, "compose": {}}
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.docker_request") as docker_request, patch("luma.control.server.remove_from_nomad") as remove:
                    with self.assertRaisesRegex(LumaError, "deployment not found: gitea"):
                        handle_service_remove(state["deployToken"], {"name": "gitea"})
                docker_request.assert_not_called()
                remove.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_by_name_removes_registered_compose_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:app-stack",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: app-stack") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "app-stack"})
                stack.assert_called_once()
                self.assertEqual(result["deployment"], "app-stack")
                self.assertEqual(result["sourceName"], "console:app-stack")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("app-stack", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_named_volumes_from_recorded_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "volumes": ["api-data:/data", "/host/data:/host-data", "./local-data:/local-data"],
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api"), patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api-data", "status": "removed local Docker volume"},
                ) as remove_volume:
                    result = handle_service_remove(
                        state["deployToken"],
                        {"name": "api", "skipDns": True, "deleteStorage": True},
                    )
                remove_volume.assert_called_once()
                # native render creates the volume under the RAW source name, so
                # cleanup must target "api-data" (not the slug-prefixed
                # "api_api-data", which was never created — that orphaned data).
                self.assertEqual(remove_volume.call_args.args[0], "api-data")
                self.assertEqual(remove_volume.call_args.args[1], [])
                self.assertIn("removed=1", result["storageCleanup"])
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "hostname": "home-node"}}
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/me/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "home" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep, patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api_api-data", "status": "removed local Docker volume"},
                ):
                    result = handle_service_remove(state["deployToken"], {"name": "api", "deleteStorage": True, "skipDns": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("Managed storage cleanup finished: removed=1", result["storageCleanup"])
                self.assertIn("Docker volume cleanup finished: removed=1", result["storageCleanup"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_single_service_deploy_prepares_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node", "agent": {"status": "ready"}}}
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "registry.local/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                with patch("luma.control.server.image_pull_requires_egress", return_value=False), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {"selected": service.image})
                ), patch("luma.control.server._storage_node_is_local", return_value=True), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep, patch("luma.control.server.deploy_to_nomad", return_value="Orchestrator deploy skipped"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "console:api", "skipDns": True, "skipOrchestrator": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("storagePreparation", result)
                self.assertIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_compose_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {
                            "nextcloud-data": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-data"},
                            "nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"},
                        },
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "nextcloud": {"image": "nextcloud:apache", "volumes": ["nextcloud-data:/var/www/html"]},
                            "postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]},
                        },
                        "volumes": {"nextcloud-data": {}, "nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "nextcloud"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: nextcloud"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True})
                self.assertEqual(host_prep.call_count, 2)
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/nextcloud/nextcloud-data", commands)
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", commands)
                self.assertIn("removed=2", result["storageCleanup"])
                self.assertNotIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_storage_cleanup_dry_run_does_not_delete_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-node", "path": "/srv/luma"}
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"}},
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {"postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]}},
                        "volumes": {"nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad") as remove, patch("luma.control.server._run_host_prep_command") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "dryRun": True})
                remove.assert_not_called()
                host_prep.assert_not_called()
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", result["storageCleanup"])
                self.assertIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_rejects_delete_storage_with_skip_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                with self.assertRaisesRegex(LumaError, "delete-storage"):
                    handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "skipOrchestrator": True})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_control_status_reports_dns_and_nomad_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "CLOUDFLARE_API_TOKEN", "value": "cf-token"})
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=False)
                state["nodes"] = {
                    "manager": {"region": "cn", "status": "manager", "labels": {"region": "cn", "role.nomad-manager": "true"}},
                    "home": {"displayName": "mini-mini", "region": "home", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "manager",
                        "path": "/srv/luma",
                        "regions": ["cn"],
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "zone-id"},
                            },
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                nomad_nodes = [
                    {"name": "home", "hostname": "home-host", "region": "home", "status": "ready"},
                    {"name": "manager", "hostname": "manager", "region": "cn", "status": "ready", "leader": True},
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "100.64.0.1:4647", "nodes": nomad_nodes},
                ), patch("luma.control.server.nomad_services_summary", return_value=[]):
                    result = handle_control_status(state["deployToken"])
                self.assertEqual(result["dns"]["provider"], "cloudflare")
                self.assertTrue(result["dns"]["tokenConfigured"])
                self.assertTrue(result["dns"]["zoneIdConfigured"])
                self.assertEqual(result["dns"]["target"], "203.0.113.10")
                self.assertEqual(result["dns"]["missing"], [])
                self.assertTrue(result["nomad"]["available"])
                self.assertEqual(result["nomad"]["nodes"][1]["leader"], True)
                self.assertEqual(result["nodes"]["registered"], 2)
                self.assertEqual(result["nodes"]["items"][0]["name"], "home")
                self.assertEqual(result["nodes"]["items"][0]["displayName"], "mini-mini")
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "cn-nfs")
                self.assertEqual(result["storage"]["storageClasses"][0]["path"], "/srv/luma")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_dashboard_nomad_readiness_uses_nomad_summary_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"bot": {"region": "global", "status": "registered"}}
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}, "providers": {"dns": {"type": "cloudflare"}}}),
                    encoding="utf-8",
                )
                nomad_summary = {
                    "available": False,
                    "error": "Nomad API unavailable",
                    "leader": "",
                    "nodes": [],
                }
                with patch("luma.control.server.nomad_status_summary", return_value=nomad_summary), patch(
                    "luma.control.server.nomad_services_summary", return_value=[]
                ):
                    result = handle_dashboard(state["deployToken"])

                self.assertFalse(result["readiness"]["nomad"]["available"])
                self.assertEqual(result["readiness"]["nomad"]["error"], "Nomad API unavailable")
                self.assertEqual(result["nodes"][0]["name"], "bot")
                self.assertEqual(result["nodes"][0]["state"], "missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_single_service_uses_task_desired_when_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "gitea",
                        "jobId": "gitea",
                        "status": "running",
                        "managedBy": "lae",
                        "running": 0,
                        "region": "home",
                        "compose": False,
                        "tasks": [
                            {
                                "name": "gitea",
                                "stack": "gitea",
                                "fullName": "gitea",
                                "status": "pending",
                                "region": "home",
                                "running": 0,
                                "desired": 1,
                                "pending": 1,
                                "nodes": ["lab"],
                            },
                            {
                                "name": "gitea-sidecar",
                                "stack": "gitea",
                                "fullName": "gitea-sidecar",
                                "status": "running",
                                "region": "home",
                                "running": 1,
                                "desired": 1,
                                "pending": 0,
                                "nodes": ["lab"],
                            }
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["fullName"], "gitea")
                self.assertEqual(service["managedBy"], "lae")
                self.assertEqual(service["running"], 0)
                self.assertEqual(service["desired"], 1)
                self.assertEqual(service["pending"], 1)
                self.assertEqual(service["nodes"], ["lab"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_keeps_configured_node_visible_without_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": yaml.safe_dump(
                                {
                                    "name": "api",
                                    "image": "example/api:1",
                                    "region": "home",
                                    "node": "home-2",
                                    "exposure": "tailscale-relay",
                                    "domain": "api.example.com",
                                    "port": 8080,
                                }
                            ),
                            "status": "failed_partial",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "api",
                        "jobId": "api",
                        "status": "pending",
                        "running": 0,
                        "desired": 1,
                        "pending": 1,
                        "region": "home",
                        "compose": False,
                        "tasks": [],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["node"], "home-2")
                self.assertEqual(service["nodes"], ["home-2"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_flags_failed_route_probe_even_when_nomad_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "status": "failed_partial",
                            "lastError": "Probe public route failed: Public route unhealthy: https://api.example.com/ -> HTTP 504",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "api",
                        "jobId": "api",
                        "status": "running",
                        "running": 1,
                        "region": "home",
                        "compose": False,
                        "tasks": [
                            {
                                "name": "api",
                                "stack": "api",
                                "fullName": "api",
                                "status": "running",
                                "region": "home",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["home-2"],
                            }
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["deploymentStatus"], "failed_partial")
                self.assertTrue(any("Public route unhealthy" in item for item in service["diagnostics"]))
                issue_messages = [issue["message"] for issue in result["issues"]]
                self.assertTrue(any("Public route unhealthy" in message for message in issue_messages))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_expands_compose_job_services_with_manifest_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump(
                    {
                        "name": "ledger",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {
                                "node": "lab",
                                "exposure": "tcp-relay",
                                "domain": "ledger-db.example.net",
                                "port": 3306,
                                "publishPort": 3306,
                            },
                            "ledger": {
                                "node": "lab",
                                "exposure": "tailscale-relay",
                                "domain": "api-ledger.example.net",
                                "port": 8888,
                                "publishPort": 8888,
                            },
                            "ledger-frontend": {
                                "node": "lab",
                                "exposure": "tailscale-relay",
                                "domain": "ledger.example.net",
                                "port": 80,
                                "publishPort": 8081,
                            },
                        },
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "ledger": {
                            "kind": "compose",
                            "name": "ledger",
                            "slug": "ledger",
                            "manifest": sidecar,
                            "composeContent": "services: {}\n",
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                routes = root / "routes"
                routes.mkdir()
                (routes / "ledger-mysql.yml").write_text(
                    yaml.safe_dump(
                        {
                            "tcp": {
                                "routers": {"ledger-mysql": {"rule": "HostSNI(`*`)", "service": "ledger-mysql"}},
                                "services": {
                                    "ledger-mysql": {"loadBalancer": {"servers": [{"address": "100.64.0.10:3306"}]}}
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (routes / "ledger-ledger-frontend.yml").write_text(
                    yaml.safe_dump(
                        {
                            "http": {
                                "routers": {"ledger-frontend": {"rule": "Host(`ledger.example.net`)", "service": "ledger-frontend"}},
                                "services": {
                                    "ledger-frontend": {"loadBalancer": {"servers": [{"url": "http://100.64.0.10:8081"}]}}
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "ledger",
                        "jobId": "ledger",
                        "status": "running",
                        "running": 1,
                        "region": "home",
                        "compose": True,
                        "tasks": [
                            {
                                "name": "mysql",
                                "stack": "ledger",
                                "fullName": "ledger_mysql",
                                "status": "running",
                                "region": "home",
                                "targetPort": "3306",
                                "publishPort": "3306",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                                "tasks": [{"id": "alloc-1", "node": "lab", "state": "running"}],
                            },
                            {
                                "name": "ledger",
                                "stack": "ledger",
                                "fullName": "ledger_ledger",
                                "status": "running",
                                "region": "home",
                                "targetPort": "8888",
                                "publishPort": "8888",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                            },
                            {
                                "name": "ledger-frontend",
                                "stack": "ledger",
                                "fullName": "ledger_ledger-frontend",
                                "status": "running",
                                "region": "home",
                                "targetPort": "80",
                                "publishPort": "8081",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                            },
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                services = {item["fullName"]: item for item in result["services"]}
                self.assertEqual(services["ledger_mysql"]["exposure"], "tcp-relay")
                self.assertEqual(services["ledger_mysql"]["domain"], "ledger-db.example.net")
                self.assertEqual(services["ledger_mysql"]["targetPort"], "3306")
                self.assertEqual(services["ledger_mysql"]["routeId"], "ledger-mysql")
                self.assertEqual(services["ledger_ledger"]["domain"], "api-ledger.example.net")
                self.assertEqual(services["ledger_ledger-frontend"]["domain"], "ledger.example.net")
                self.assertEqual(services["ledger_ledger-frontend"]["exposure"], "tailscale-relay")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_logs_resolve_compose_task_full_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "ledger": {
                            "kind": "compose",
                            "name": "ledger",
                            "slug": "ledger",
                            "manifest": yaml.safe_dump(
                                {
                                    "name": "ledger",
                                    "compose": "docker-compose.yml",
                                    "services": {"mysql": {"exposure": "tcp-relay", "domain": "ledger-db.example.net", "port": 3306}},
                                }
                            ),
                            "composeContent": "services: {}\n",
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")
                log_text = "mysql ready\n"
                calls: list[str] = []

                def request(_self, method, path, body=None):
                    calls.append(path)
                    if path == "/v1/job/ledger/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "running",
                                "CreateTime": 10,
                                "TaskGroup": "ledger",
                                "TaskStates": {"mysql": {"State": "running"}, "ledger": {"State": "running"}},
                            }
                        ]
                    if path.startswith("/v1/client/fs/ls/alloc-1?"):
                        return [{"Name": "mysql.stdout.0", "Size": len(log_text.encode()), "IsDir": False}]
                    raise AssertionError(path)

                def read_bytes(_reader, source, file, offset, limit):
                    self.assertEqual(source["task"], "mysql")
                    self.assertEqual(file, "alloc/logs/mysql.stdout.0")
                    return log_text.encode()[offset:offset + limit]

                with patch("luma.control.server.NomadApi.request", request), patch("luma.control.logs.LogReader._read_bytes", read_bytes):
                    result = handle_dashboard_logs(state["deployToken"], "ledger_mysql", tail=20)

                self.assertIn("/v1/job/ledger/allocations", calls)
                self.assertEqual(result["service"], "ledger_mysql")
                self.assertEqual(result["logs"], ["mysql ready"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_logs_tails_nomad_alloc_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")
                log_text = "2026-06-05T10:00:00Z api booted\n2026-06-05T10:00:01Z api ready\n"
                calls: list[str] = []

                def request(_self, method, path, body=None):
                    calls.append(path)
                    if path == "/v1/job/gitea/allocations":
                        return [{
                            "ID": "alloc-1",
                            "DesiredStatus": "run",
                            "ClientStatus": "running",
                            "CreateTime": 10,
                            "TaskGroup": "gitea",
                            "TaskStates": {"gitea": {"State": "running"}},
                        }]
                    if path.startswith("/v1/client/fs/ls/alloc-1?"):
                        return [{"Name": "gitea.stdout.0", "Size": len(log_text.encode()), "IsDir": False}]
                    raise AssertionError(path)

                def read_bytes(_reader, source, file, offset, limit):
                    self.assertEqual(source["task"], "gitea")
                    self.assertEqual(file, "alloc/logs/gitea.stdout.0")
                    return log_text.encode()[offset:offset + limit]

                with patch("luma.control.server.NomadApi.request", request), patch("luma.control.logs.LogReader._read_bytes", read_bytes):
                    result = handle_dashboard_logs(state["deployToken"], "gitea", tail=20)

                self.assertIn("/v1/job/gitea/allocations", calls)
                self.assertTrue(any("/v1/client/fs/ls/alloc-1?" in call for call in calls))
                self.assertEqual(result["service"], "gitea")
                self.assertEqual(result["logs"], [line for line in log_text.splitlines()])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_runtime_events_include_pending_allocation_pull_progress(self):
        from luma.control.server import handle_dashboard_runtime_events

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-2": {
                        "nodeId": "nomad-node-1",
                        "hostname": "VM-0-10-ubuntu",
                        "agent": {
                            "status": "ready",
                            "diagnostics": {
                                "recentImagePullErrors": [
                                    'Task event: alloc_id=alloc-1 task=app type=Driver msg="Docker image pull progress: Pulled 9/14 layers" failed=false'
                                ]
                            },
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/luxe-monitor":
                        return {
                            "ID": "luxe-monitor",
                            "Meta": {"luma.compose": "true"},
                            "TaskGroups": [
                                {
                                    "Name": "luxe-monitor",
                                    "Tasks": [
                                        {"Name": "app", "Config": {"image": "100.64.0.70:5000/liutianjie/luxe-monitor:a494e7f"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/luxe-monitor/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "TaskGroup": "luxe-monitor",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "NodeName": "VM-0-10-ubuntu",
                                "TaskStates": {
                                    "app": {
                                        "State": "pending",
                                        "Events": [
                                            {"Type": "Driver", "DisplayMessage": "Downloading image", "Message": "Docker image pull progress: Pulled 9/14 layers", "Time": 123}
                                        ],
                                    }
                                },
                            }
                        ]
                    if path == "/v1/allocation/alloc-1":
                        return request(_self, method, "/v1/job/luxe-monitor/allocations", body)[0]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request):
                    result = handle_dashboard_runtime_events(state["deployToken"], "luxe-monitor_app")

                self.assertEqual(result["service"], "luxe-monitor_app")
                self.assertEqual(result["job"], "luxe-monitor")
                self.assertEqual(result["task"], "app")
                self.assertEqual(result["allocId"], "alloc-1")
                self.assertEqual(result["node"], "cn-2")
                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["image"], "100.64.0.70:5000/liutianjie/luxe-monitor:a494e7f")
                self.assertTrue(any("Downloading image" in event["message"] for event in result["events"]))
                self.assertTrue(any("Pulled 9/14" in event["message"] for event in result["events"]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_pull_diagnostics_runs_on_latest_allocation_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "nodeId": "nomad-node-1",
                        "agent": {"status": "ready", "capabilities": ["docker-image"]},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/api":
                        return {
                            "ID": "api",
                            "TaskGroups": [
                                {
                                    "Name": "api",
                                    "Tasks": [
                                        {"Name": "api", "Config": {"image": "ghcr.io/acme/api@sha256:abc123"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/api/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "TaskStates": {"api": {"State": "pending"}},
                            }
                        ]
                    raise AssertionError(path)

                def queue_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "home-2")
                    self.assertEqual(action, "diagnose-docker-pull")
                    self.assertEqual(payload["image"], "ghcr.io/acme/api@sha256:abc123")
                    self.assertEqual(kwargs["required_capability"], "docker-image")
                    return "task-1"

                with patch("luma.control.server.NomadApi.request", request), patch(
                    "luma.control.server._queue_node_agent_task",
                    side_effect=queue_task,
                ), patch(
                    "luma.control.server._wait_node_agent_task",
                    return_value={"taskId": "task-1", "ok": True, "lines": ["Downloading"], "output": "Downloading\n"},
                ):
                    result = handle_service_pull_diagnostics(state["deployToken"], "api", timeout=30)

                self.assertEqual(result["service"], "api")
                self.assertEqual(result["node"], "home-2")
                self.assertEqual(result["image"], "ghcr.io/acme/api@sha256:abc123")
                self.assertEqual(result["lines"], ["Downloading"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_pull_diagnostics_falls_back_to_first_task_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "nodeId": "nomad-node-1",
                        "agent": {"status": "ready", "capabilities": ["docker-image"]},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/api":
                        return {
                            "ID": "api",
                            "TaskGroups": [
                                {
                                    "Name": "api",
                                    "Tasks": [
                                        {"Name": "web", "Config": {"image": "ghcr.io/acme/web@sha256:abc123"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/api/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "TaskStates": {"web": {"State": "pending"}},
                            }
                        ]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request), patch(
                    "luma.control.server._queue_node_agent_task",
                    return_value="task-1",
                ) as queue_task, patch(
                    "luma.control.server._wait_node_agent_task",
                    return_value={"taskId": "task-1", "ok": True, "lines": [], "output": ""},
                ):
                    result = handle_service_pull_diagnostics(state["deployToken"], "api", timeout=30)

                self.assertEqual(result["task"], "web")
                self.assertEqual(result["image"], "ghcr.io/acme/web@sha256:abc123")
                self.assertEqual(queue_task.call_args.args[3]["image"], "ghcr.io/acme/web@sha256:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_progress_appends_lines_to_running_task(self):
        from luma.control.server import _agent_task_progress_snapshot, handle_node_agent_progress

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-2": {"nodeId": "node-1", "agent": {"status": "ready"}}}
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "home-2", "nodeId": "node-1"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-pull"] = {
                    "nodeName": "home-2",
                    "action": "diagnose-docker-pull",
                    "status": "running",
                    "payload": {"image": "ghcr.io/acme/api:latest"},
                    "progress": [{"type": "output", "line": f"existing-{index}"} for index in range(300)],
                }
                save_state(current)

                initial, cursor, status, _, _ = _agent_task_progress_snapshot("task-pull", 0)
                self.assertEqual(len(initial), 300)
                self.assertEqual(cursor, 300)
                self.assertEqual(status, "running")

                result = handle_node_agent_progress(
                    issued["agentToken"],
                    {"nodeName": "home-2", "nodeId": "node-1", "taskId": "task-pull", "events": [{"type": "output", "line": "Downloading"}]},
                )

                self.assertEqual(result["taskId"], "task-pull")
                task = load_state()["agentTasks"]["task-pull"]
                self.assertEqual(len(task["progress"]), 300)
                self.assertEqual(task["progressOffset"], 1)
                self.assertEqual(task["progress"][-1]["line"], "Downloading")
                self.assertNotIn("registryAuth", json.dumps(task))

                appended, cursor, status, _, _ = _agent_task_progress_snapshot("task-pull", cursor)
                self.assertEqual([event["line"] for event in appended], ["Downloading"])
                self.assertEqual(cursor, 301)
                self.assertEqual(status, "running")
                repeated, repeated_cursor, _, _, _ = _agent_task_progress_snapshot("task-pull", cursor)
                self.assertEqual(repeated, [])
                self.assertEqual(repeated_cursor, cursor)

                handle_node_agent_progress(
                    issued["agentToken"],
                    {"nodeName": "home-2", "nodeId": "node-1", "taskId": "task-pull", "events": [{"type": "output", "line": "Extracting"}]},
                )
                continued, continued_cursor, _, _, _ = _agent_task_progress_snapshot("task-pull", cursor)
                self.assertEqual([event["line"] for event in continued], ["Extracting"])
                self.assertEqual(continued_cursor, 302)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)


    def test_asgi_app_serves_dashboard_health_and_rejects_missing_token(self):
        from starlette.testclient import TestClient
        from luma.control.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with TestClient(create_app()) as client:
                    response = client.get("/dashboard/")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("Luma · 控制台", response.text)
                    self.assertEqual(response.headers["cache-control"], "no-cache")
                    main_script = re.search(r'<script[^>]+src="(/dashboard/[^\"]+\.js)"', response.text)
                    self.assertIsNotNone(main_script)
                    main_asset = client.get(main_script.group(1), headers={"Accept-Encoding": "gzip"})
                    self.assertEqual(main_asset.status_code, 200)
                    self.assertEqual(main_asset.headers["content-encoding"], "gzip")
                    self.assertEqual(main_asset.headers["cache-control"], "public, max-age=31536000, immutable")
                    self.assertIn("/v1/dashboard", main_asset.text)
                    image_name = next(asset_path("dashboard").glob("*.png")).name
                    image = client.get(f"/dashboard/{image_name}")
                    self.assertEqual(image.status_code, 200)
                    self.assertEqual(image.headers["content-type"], "image/png")
                    self.assertGreater(len(image.content), 0)
                    # A deep client-side route falls back to index.html (SPA routing).
                    deep = client.get("/dashboard/apps/ledger/logs")
                    self.assertEqual(deep.status_code, 200)
                    self.assertIn("Luma · 控制台", deep.text)
                    self.assertEqual(deep.headers["content-type"], "text/html; charset=utf-8")
                    # An unknown asset path still 404s as JSON, not HTML.
                    missing_asset = client.get("/dashboard/does-not-exist.js")
                    self.assertEqual(missing_asset.status_code, 404)
                    self.assertNotIn("Luma · 控制台", missing_asset.text)
                    self.assertEqual(missing_asset.json()["error"], "not found")
                    health = client.get("/v1/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertIn("terminal", health.json()["capabilities"])
                    login = client.post("/v1/auth/login/verify", json={}, headers={"Authorization": f"Bearer {state['deployToken']}"})
                    self.assertEqual(login.status_code, 200)
                    self.assertEqual(login.json()["clusterId"], "luma-test")
                    rejected = client.get("/v1/dashboard")
                    self.assertEqual(rejected.status_code, 401)
                    self.assertEqual(rejected.json()["error"], "missing bearer token")
                    # The dashboard fallback never intercepts unknown /v1 GETs.
                    rejected_v1 = client.get("/v1/nope")
                    self.assertEqual(rejected_v1.status_code, 401)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_websocket_relays_between_browser_and_agent(self):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            original_broker = control_server.TERMINAL_BROKER
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                control_server.TERMINAL_BROKER = control_server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
                with TestClient(control_server.create_app()) as client:
                    with client.websocket_connect(
                        "/v1/terminal/agent?node=worker-storage&nodeId=worker-node-id"
                    ) as agent:
                        agent.send_json({"type": "auth", "token": agent_token})
                        self.assertEqual(agent.receive_json()["type"], "ready")
                        with client.websocket_connect(
                            "/v1/terminal/browser?node=worker-storage"
                        ) as browser:
                            browser.send_json({"type": "auth", "token": state["deployToken"]})
                            opened = browser.receive_json()
                            self.assertEqual(opened["type"], "open")
                            session_id = opened["sessionId"]
                            agent_open = agent.receive_json()
                            self.assertEqual(agent_open["type"], "open")
                            self.assertEqual(agent_open["sessionId"], session_id)
                            browser.send_json({"type": "input", "data": "pwd\n"})
                            agent_input = agent.receive_json()
                            self.assertEqual(agent_input["type"], "input")
                            self.assertEqual(agent_input["sessionId"], session_id)
                            self.assertEqual(agent_input["data"], "pwd\n")
                            agent.send_json({"type": "output", "sessionId": session_id, "data": "/root\r\n"})
                            self.assertEqual(browser.receive_json()["data"], "/root\r\n")
                            agent.send_json({"type": "exit", "sessionId": session_id, "exitCode": 0})
                            exit_event = browser.receive_json()
                            self.assertEqual(exit_event["type"], "exit")
                            self.assertEqual(exit_event["exitCode"], 0)
                            with self.assertRaises(WebSocketDisconnect):
                                browser.receive_json()
            finally:
                control_server.TERMINAL_BROKER = original_broker
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_websocket_canonicalizes_agent_alias(self):
        from starlette.testclient import TestClient
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            original_broker = control_server.TERMINAL_BROKER
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "mini": {
                        "displayName": "mini",
                        "hostname": "home-mac-mini",
                        "aliases": ["home-mac-mini"],
                        "region": "home",
                        "nodeId": "mini-node-id",
                        "labels": {"luma.node.name": "mini", "luma.node.id": "mini-node-id", "region": "home"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "mini", "nodeId": "mini-node-id"})
                agent_token = issued["agentToken"]
                control_server.TERMINAL_BROKER = control_server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
                with TestClient(control_server.create_app()) as client:
                    with client.websocket_connect(
                        "/v1/terminal/agent?node=home-mac-mini&nodeId=mini-node-id"
                    ) as agent:
                        agent.send_json({"type": "auth", "token": agent_token})
                        ready = agent.receive_json()
                        self.assertEqual(ready["type"], "ready")
                        self.assertEqual(ready["node"], "mini")
                        self.assertEqual(control_server.TERMINAL_BROKER.connected_nodes(), {"mini"})
                        with client.websocket_connect("/v1/terminal/browser?node=mini") as browser:
                            browser.send_json({"type": "auth", "token": state["deployToken"]})
                            opened = browser.receive_json()
                            self.assertEqual(opened["type"], "open")
                            self.assertEqual(opened["node"], "mini")
                            agent_open = agent.receive_json()
                            self.assertEqual(agent_open["type"], "open")
                            self.assertEqual(agent_open["node"], "mini")
            finally:
                control_server.TERMINAL_BROKER = original_broker
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_terminal_target_resolves_running_compose_service(self):
        from luma.control.server import _resolve_application_terminal_target

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-edge": {
                        "nodeId": "nomad-node-1",
                        "hostname": "iZ0jlep4ral3r2v0ajypnmZ",
                        "agent": {"status": "ready", "capabilities": ["terminal", "container-terminal"]},
                    }
                }
                state["deployments"] = {
                    "compose": {
                        "vibecheck": {
                            "name": "vibecheck",
                            "manifest": yaml.safe_dump({
                                "name": "vibecheck",
                                "services": {"api": {}, "web": {}, "postgres": {}},
                            }),
                        }
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/vibecheck":
                        return {
                            "ID": "vibecheck",
                            "Meta": {"luma.compose": "true"},
                            "TaskGroups": [{"Name": "vibecheck", "Tasks": [{"Name": "api", "Config": {"image": "registry.example/api:1"}}]}],
                        }
                    if path == "/v1/job/vibecheck/allocations":
                        return [
                            {
                                "ID": "alloc-running",
                                "DesiredStatus": "run",
                                "ClientStatus": "running",
                                "CreateTime": 20,
                                "NodeID": "nomad-node-1",
                                "NodeName": "iZ0jlep4ral3r2v0ajypnmZ",
                                "TaskStates": {"api": {"State": "running"}},
                            }
                        ]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request):
                    result = _resolve_application_terminal_target(state, "vibecheck_api")
                self.assertEqual(result["job"], "vibecheck")
                self.assertEqual(result["task"], "api")
                self.assertEqual(result["allocId"], "alloc-running")
                self.assertEqual(result["node"], "cn-edge")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_terminal_target_rejects_system_stack(self):
        from luma.control.server import _resolve_application_terminal_target
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaises(LumaError) as raised:
                    _resolve_application_terminal_target(state, "traefik")
                self.assertIn("system stack", str(raised.exception))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_websocket_opens_container_session_for_service(self):
        from starlette.testclient import TestClient
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            original_broker = control_server.TERMINAL_BROKER
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                        "agent": {"status": "ready", "capabilities": ["terminal", "container-terminal"]},
                    }
                }
                state["deployments"] = {
                    "compose": {
                        "vibecheck": {
                            "name": "vibecheck",
                            "manifest": yaml.safe_dump({"name": "vibecheck", "services": {"api": {}}}),
                        }
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                control_server.TERMINAL_BROKER = control_server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)

                def request(_self, method, path, body=None):
                    if path == "/v1/job/vibecheck":
                        return {
                            "ID": "vibecheck",
                            "TaskGroups": [{"Name": "vibecheck", "Tasks": [{"Name": "api", "Config": {"image": "registry.example/api:1"}}]}],
                        }
                    if path == "/v1/job/vibecheck/allocations":
                        return [
                            {
                                "ID": "alloc-running",
                                "DesiredStatus": "run",
                                "ClientStatus": "running",
                                "CreateTime": 20,
                                "NodeID": "worker-node-id",
                                "NodeName": "worker-storage",
                                "TaskStates": {"api": {"State": "running"}},
                            }
                        ]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request), TestClient(control_server.create_app()) as client:
                    with client.websocket_connect("/v1/terminal/agent?node=worker-storage&nodeId=worker-node-id") as agent:
                        agent.send_json({"type": "auth", "token": agent_token})
                        self.assertEqual(agent.receive_json()["type"], "ready")
                        with client.websocket_connect("/v1/terminal/browser?service=vibecheck_api") as browser:
                            browser.send_json({"type": "auth", "token": state["deployToken"]})
                            opened = browser.receive_json()
                            self.assertEqual(opened["type"], "open")
                            self.assertEqual(opened["target"], "container")
                            self.assertEqual(opened["task"], "api")
                            self.assertEqual(opened["allocId"], "alloc-running")
                            self.assertEqual(opened["node"], "worker-storage")
                            agent_open = agent.receive_json()
                            self.assertEqual(agent_open["type"], "open")
                            self.assertEqual(agent_open["target"], "container")
                            self.assertEqual(agent_open["allocId"], "alloc-running")
                            self.assertEqual(agent_open["task"], "api")
            finally:
                control_server.TERMINAL_BROKER = original_broker
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_terminal_open_argv_uses_docker_exec_for_container_target(self):
        from luma.agent import _terminal_open_argv

        with patch("luma.agent._docker_binary", return_value="/usr/bin/docker"), patch(
            "luma.agent._resolve_nomad_container_id", return_value="0123456789ab"
        ), patch("luma.agent._container_shell", return_value="/bin/sh"):
            argv = _terminal_open_argv({
                "target": "container",
                "allocId": "alloc-running",
                "task": "api",
            })
        self.assertEqual(argv, ["/usr/bin/docker", "exec", "-i", "-t", "-e", "TERM=xterm-256color", "0123456789ab", "/bin/sh"])

    def test_terminal_open_argv_rejects_invalid_container_identity(self):
        from luma.agent import _terminal_open_argv

        with self.assertRaises(ValueError):
            _terminal_open_argv({"target": "container", "allocId": "alloc; rm -rf /", "task": "api"})
        with self.assertRaises(ValueError):
            _terminal_open_argv({"target": "container", "allocId": "alloc-running", "task": "api && reboot"})
        with self.assertRaises(ValueError):
            _terminal_open_argv({"target": "host-escape", "allocId": "alloc-running", "task": "api"})

    def test_resolve_nomad_container_id_filters_by_alloc_and_task_labels(self):
        from luma.agent import _resolve_nomad_container_id

        result = Mock(returncode=0, stdout="0123456789abcdef\n", stderr="")
        with patch("luma.agent.subprocess.run", return_value=result) as run:
            container_id = _resolve_nomad_container_id("/usr/bin/docker", "alloc-running", "api")
        self.assertEqual(container_id, "0123456789abcdef")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/docker")
        self.assertIn("label=com.hashicorp.nomad.alloc_id=alloc-running", args)
        self.assertIn("label=com.hashicorp.nomad.task_name=api", args)

    def test_resolve_nomad_container_id_falls_back_to_driver_container_name_without_task_label(self):
        from luma.agent import _resolve_nomad_container_id

        strict = Mock(returncode=0, stdout="", stderr="")
        fallback = Mock(
            returncode=0,
            stdout=(
                "0123456789abcdef|api-alloc-running|\n"
                "fedcba9876543210|nomad_init_alloc-running|\n"
            ),
            stderr="",
        )
        with patch("luma.agent.subprocess.run", side_effect=[strict, fallback]) as run:
            container_id = _resolve_nomad_container_id("/usr/bin/docker", "alloc-running", "api")

        self.assertEqual(container_id, "0123456789abcdef")
        fallback_args = run.call_args_list[1].args[0]
        self.assertIn("label=com.hashicorp.nomad.alloc_id=alloc-running", fallback_args)
        self.assertNotIn("label=com.hashicorp.nomad.task_name=api", fallback_args)
        self.assertIn("{{.Names}}", fallback_args[-1])

    def test_resolve_nomad_container_id_fails_closed_for_ambiguous_unlabelled_allocation(self):
        from luma.agent import _resolve_nomad_container_id

        strict = Mock(returncode=0, stdout="", stderr="")
        fallback = Mock(
            returncode=0,
            stdout=(
                "0123456789abcdef|web-alloc-running|\n"
                "fedcba9876543210|worker-alloc-running|\n"
                "aaaaaaaaaaaaaaaa|nomad_init_alloc-running|\n"
            ),
            stderr="",
        )
        with patch("luma.agent.subprocess.run", side_effect=[strict, fallback]):
            with self.assertRaisesRegex(RuntimeError, "multiple application containers"):
                _resolve_nomad_container_id("/usr/bin/docker", "alloc-running", "api")

    def test_terminal_broker_closes_browser_and_only_cleans_old_agent_sessions(self):
        import asyncio
        from luma.control.server import TerminalBroker, _TerminalAgentConnection, _TerminalSession

        class FakeBrowser:
            def __init__(self):
                self.sent = []
                self.closed = False

            async def send_json(self, payload):
                self.sent.append(payload)

            async def close(self, code=1000):
                self.closed = code

        class FakeAgentSocket:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        async def scenario():
            broker = TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
            old_agent = _TerminalAgentConnection("worker-storage", FakeAgentSocket())
            new_agent = _TerminalAgentConnection("worker-storage", FakeAgentSocket())
            old_browser = FakeBrowser()
            new_browser = FakeBrowser()
            broker._sessions["old"] = _TerminalSession("old", "worker-storage", old_browser, old_agent)
            broker._sessions["new"] = _TerminalSession("new", "worker-storage", new_browser, new_agent)

            self.assertEqual(await broker._session_ids_for_agent("worker-storage", old_agent), ["old"])
            await broker.close_session("old", notify_agent=True, browser_message="terminal agent disconnected")

            self.assertTrue(old_browser.closed)
            self.assertEqual(old_browser.sent[0]["message"], "terminal agent disconnected")
            self.assertEqual(old_agent.websocket.sent[0], {"type": "close", "sessionId": "old"})
            self.assertIn("new", broker._sessions)
            self.assertFalse(new_browser.closed)

        asyncio.run(scenario())

    def test_terminal_broker_rejects_pending_auth_overflow(self):
        import asyncio
        from luma.control.server import TerminalBroker

        class FakeWebSocket:
            def __init__(self):
                self.closed = None

            async def close(self, code=1000):
                self.closed = code

        async def scenario():
            broker = TerminalBroker(per_node_limit=1, idle_timeout_seconds=60)
            broker._pending_auth = asyncio.Semaphore(0)
            websocket = FakeWebSocket()

            self.assertFalse(await broker._acquire_pending_auth(websocket))
            self.assertEqual(websocket.closed, 1013)

        asyncio.run(scenario())

    def test_terminal_websocket_rejects_wrong_agent_token(self):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        from luma.control.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                    }
                }
                save_state(state)
                with TestClient(create_app()) as client, self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/v1/terminal/agent?node=worker-storage&nodeId=worker-node-id") as agent:
                        agent.send_json({"type": "auth", "token": "wrong"})
                        agent.receive_json()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_asgi_log_stream_defers_docker_socket_to_background_reader(self):
        import asyncio
        import threading
        from unittest.mock import Mock
        from luma.control.server import _asgi_stream_service_logs

        event_loop_thread = threading.get_ident()
        io_threads = []
        reader = Mock(service="api_api", sources=[])
        reader.discover.side_effect = lambda: io_threads.append(threading.get_ident())
        reader.poll.side_effect = lambda: (
            io_threads.append(threading.get_ident()) or [{"line": "ready", "allocationId": "a1"}]
        )

        async def check():
            response = await _asgi_stream_service_logs("management-token", "api_api", "", 20)
            self.assertEqual(response.media_type, "application/x-ndjson")
            reader.poll.assert_not_called()
            self.assertEqual(json.loads(await anext(response.body_iterator))["status"], "start")
            reader.poll.assert_not_called()
            self.assertEqual(json.loads(await anext(response.body_iterator))["line"], "ready")
            await response.body_iterator.aclose()

        with patch("luma.control.server._dashboard_log_reader", return_value=reader), patch("luma.control.server.DockerSocketConnection") as docker_socket:
            asyncio.run(check())
        docker_socket.assert_not_called()
        reader.discover.assert_called_once()
        reader.poll.assert_called_once()
        self.assertEqual(len(io_threads), 2)
        self.assertTrue(all(ident != event_loop_thread for ident in io_threads))

    def test_docker_socket_connect_closes_socket_when_connect_fails(self):
        from luma.control.server import DockerSocketConnection

        class FailingSocket:
            def __init__(self):
                self.closed = False

            def connect(self, _path):
                raise OSError("socket unavailable")

            def close(self):
                self.closed = True

        failing = FailingSocket()
        with patch("luma.control.server.socket.socket", return_value=failing):
            with self.assertRaises(OSError):
                DockerSocketConnection("/missing/docker.sock").connect()
        self.assertTrue(failing.closed)

    def test_deployment_renders_referenced_secrets_into_nomad_job_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                response = MagicMock()
                response.__enter__.return_value.status = 200
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ) as deploy, patch("luma.control.server.urllib.request.urlopen", return_value=response):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["probe"], "Public route reachable: https://api.example.com/ -> HTTP 200")
                deploy.assert_called_once()
                job_text = deploy.call_args.args[1]
                self.assertIn('"DATABASE_URL": "postgres://secret"', job_text)
                self.assertIn('"DATABASE_URL": "postgres://secret"', (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_renders_storage_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["home"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "uptime-kuma": {
                                "image": "louislam/uptime-kuma:1",
                                "volumes": ["kuma-data:/app/data"],
                            }
                        },
                        "volumes": {"kuma-data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "uptime-kuma",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"kuma-data": {"storageClass": "home-nfs", "path": "uptime-kuma/kuma-data"}},
                        "services": {
                            "uptime-kuma": {
                                "exposure": "tailscale-relay",
                                "domain": "kuma.example.com",
                                "port": 3001,
                                "relay": {"host": "100.64.0.2"},
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad") as upsert:
                    result = handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(result["deployment"], "uptime-kuma")
                self.assertEqual(result["artifacts"][0]["kind"], "job")
                self.assertIn(
                    '"source": "luma-uptime-kuma-kuma-data-',
                    result["artifacts"][0]["content"],
                )
                self.assertIn('"volume_options"', result["artifacts"][0]["content"])
                self.assertIn('"device": ":/srv/luma/uptime-kuma/kuma-data"', result["artifacts"][0]["content"])
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                self.assertFalse((root / "stacks").exists())
                upsert.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_rejects_missing_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["data:/data"]}}, "volumes": {"data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"data": {"storageClass": "missing-nfs", "path": "app/data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "unknown storage class"):
                    handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_keeps_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "environment": {"APP_PASSWORD": "${APP_PASSWORD}"}}}})
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})

                result = handle_compose_deployment_preview(
                    state["deployToken"],
                    {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                )

                self.assertEqual(result["deployment"], "app-stack")
                self.assertIn('"APP_PASSWORD": "${APP_PASSWORD}"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_blocks_storage_backend_switch_without_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_storage_guard_uses_previous_runtime_mount_when_record_drifted(self):
        from luma.control.server import _guard_compose_storage_switch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "postgres": {
                                "image": "postgres:17",
                                "volumes": ["pg-data:/var/lib/postgresql/data"],
                            }
                        },
                        "volumes": {"pg-data": {}},
                    }
                ),
                encoding="utf-8",
            )
            sidecar_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {
                            "pg-data": {
                                "local": {"node": "cn-2", "path": "/opt/luma/state/app-stack/postgres"}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(sidecar_path)

            def job(mount_type: str, source: str) -> str:
                return json.dumps(
                    {
                        "Job": {
                            "TaskGroups": [
                                {
                                    "Tasks": [
                                        {
                                            "Name": "postgres",
                                            "Config": {
                                                "mount": [
                                                    {
                                                        "type": mount_type,
                                                        "source": source,
                                                        "target": "/var/lib/postgresql/data",
                                                        "readonly": False,
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )

            target = root / "app-stack.nomad.json"
            target.write_text(job("volume", "pg-data"), encoding="utf-8")
            previous_record = {
                "storageBackends": {
                    "pg-data": {
                        "kind": "local",
                        "node": "cn-2",
                        "path": "/opt/luma/state/app-stack/postgres",
                    }
                }
            }

            with self.assertRaisesRegex(LumaError, "runtime storage mount changed"):
                _guard_compose_storage_switch(
                    target,
                    job("bind", "/opt/luma/state/app-stack/postgres"),
                    deployment,
                    previous_record=previous_record,
                )

    def test_compose_storage_guard_allows_verified_runtime_mount_adoption(self):
        from luma.control.server import _guard_compose_storage_switch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                yaml.safe_dump(
                    {
                        "services": {"postgres": {"image": "postgres:17", "volumes": ["pg-data:/data"]}},
                        "volumes": {"pg-data": {}},
                    }
                ),
                encoding="utf-8",
            )
            sidecar = root / "luma.compose.yml"
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {
                            "pg-data": {
                                "local": {"node": "cn-2", "path": "/srv/app-stack/data"},
                                "adopted": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            deployment = load_compose_deployment(sidecar)
            target = root / "app-stack.nomad.json"
            target.write_text(
                json.dumps(
                    {
                        "Job": {
                            "TaskGroups": [
                                {
                                    "Tasks": [
                                        {
                                            "Name": "postgres",
                                            "Config": {"mount": [{"type": "volume", "source": "pg-data", "target": "/data"}]},
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            current = json.dumps(
                {
                    "Job": {
                        "TaskGroups": [
                            {
                                "Tasks": [
                                    {
                                        "Name": "postgres",
                                        "Config": {"mount": [{"type": "bind", "source": "/srv/app-stack/data", "target": "/data"}]},
                                    }
                                ]
                            }
                        ]
                    }
                }
            )

            result = _guard_compose_storage_switch(target, current, deployment)
            self.assertIn("unchanged", result)

    def test_compose_deployment_allows_storage_backend_switch_after_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data", "adopted": True}},
                    }
                )
                result = handle_compose_deployment(
                    state["deployToken"],
                    {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                )
                self.assertEqual(result["deployment"], "app-stack")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_rejected_storage_switch_does_not_poison_baseline_on_retry(self):
        # Regression: a rejected storage switch must NOT overwrite the stored
        # backend baseline. Otherwise retrying the same (rejected) switch sees
        # new-vs-new, slips past the guard, and orphans the old volume's data.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})

                def deploy(path: str):
                    sidecar = yaml.safe_dump(
                        {
                            "name": "app-stack",
                            "compose": "docker-compose.yml",
                            "region": "cn",
                            "volumes": {"pg-data": {"storageClass": "nfs", "path": path}},
                        }
                    )
                    return handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )

                # 1) first deploy establishes backend baseline at path=pg-data
                result = deploy("pg-data")
                self.assertEqual(result["deployment"], "app-stack")
                record = load_state()["deployments"]["compose"]["app-stack"]
                self.assertEqual(record["storageBackends"]["pg-data"]["path"], "pg-data")

                # 2) switch to a different path is rejected
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    deploy("pg-data-v2")
                # the rejected switch must NOT have poisoned the baseline
                record = load_state()["deployments"]["compose"]["app-stack"]
                self.assertEqual(record["status"], "failed_partial")
                self.assertEqual(record["storageBackends"]["pg-data"]["path"], "pg-data")

                # 3) retrying the same switch is STILL rejected (the bug: it passed)
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    deploy("pg-data-v2")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_sidecar_storage_classes_in_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {"nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "managed by Luma Control"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_unsafe_compose_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "../docker-compose.yml", "region": "cn"})
                with self.assertRaisesRegex(LumaError, "relative path without"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_class_is_managed_in_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "home",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                        "labels": {"luma.node.name": "home-nas", "luma.node.id": "home-node-id", "region": "home"},
                    },
                }
                save_state(state)
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {
                            "name": "home-nfs",
                            "provider": "nfs",
                            "node": "home-nas",
                            "path": "/srv/luma",
                            "regions": ["home", "cn"],
                        },
                    )
                self.assertTrue(result["saved"])
                listed = handle_storage_list(state["deployToken"])
                self.assertEqual(listed["storageClasses"][0]["name"], "home-nfs")
                self.assertEqual(listed["storageClasses"][0]["path"], "/srv/luma")
                self.assertNotIn("workloads", listed["storageClasses"][0])
                self.assertNotIn("exportRoot", listed["storageClasses"][0])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["provider"], "nfs")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["mode"], "managed")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["path"], "/srv/luma")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["mountOptions"], DEFAULT_NFS_MOUNT_OPTIONS)
                self.assertNotIn("workloads", persisted["storageClasses"]["home-nfs"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_validates_new_managed_and_external_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "home", "status": "labeled"},
                }
                save_state(state)
                with patch("luma.control.server.docker_request", return_value=[]):
                    with self.assertRaisesRegex(LumaError, "unknown Luma node"):
                        handle_storage_set(state["deployToken"], {"name": "bad-nfs", "provider": "nfs", "node": "missing", "path": "/srv/luma"})
                with self.assertRaisesRegex(LumaError, "cannot set endpoint"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "bad-nfs", "provider": "nfs", "node": "home-nas", "path": "/srv/luma", "endpoint": "home-nas:/srv/luma"},
                    )
                with self.assertRaisesRegex(LumaError, "requires at least one region"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "company-nfs", "provider": "nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma"},
                    )
                result = handle_storage_set(
                    state["deployToken"],
                    {"name": "company-nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma", "regions": ["cn"], "workloads": ["database"]},
                )
                self.assertTrue(result["saved"])
                saved = load_state()["storageClasses"]["company-nfs"]
                self.assertEqual(saved["provider"], "nfs")
                self.assertEqual(saved["mode"], "external")
                self.assertEqual(saved["regions"], ["cn"])
                self.assertNotIn("workloads", saved)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_rejects_unregistered_manager_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {"name": "home-mac-mini", "region": "home", "status": "labeled"},
                }
                save_state(state)
                with patch("luma.control.server.docker_request", side_effect=AssertionError("Docker discovery should not run")):
                    with self.assertRaisesRegex(LumaError, "unknown Luma node"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "cn-nfs", "provider": "nfs", "node": "iZ0jl8auywzycory05d9cuZ", "path": "/srv/luma"},
                        )
                self.assertNotIn("cn-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_uses_registered_manager_without_swarm_labeling(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "status": "manager",
                        "hostname": "manager-host",
                        "nodeId": "manager-node-id",
                        "labels": {"region": "cn", "ingress": "true"},
                    }
                }
                save_state(state)
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "ID": "manager-node-id"}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "manager-host", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["cn-nfs"]["path"], "/srv/luma")
                manager = persisted["nodes"]["manager-host"]
                self.assertEqual(manager["region"], "cn")
                self.assertEqual(manager["hostname"], "manager-host")
                self.assertEqual(manager["nodeId"], "manager-node-id")
                self.assertEqual(manager["status"], "manager")
                self.assertEqual(manager["labels"], {"region": "cn", "ingress": "true"})
                self.assertFalse(any(method == "POST" for method, _path, _body in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_keeps_registered_manager_labels_for_storage_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "status": "manager",
                        "hostname": "manager-host",
                        "nodeId": "manager-node-id",
                        "labels": {"region": "cn", "ingress": "true"},
                    }
                }
                save_state(state)
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "ID": "manager-node-id"}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                manager = load_state()["nodes"]["manager-host"]
                self.assertEqual(manager["labels"], {"region": "cn", "ingress": "true"})
                self.assertFalse(any(method == "POST" for method, _path, _body in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_creates_managed_path_for_local_storage_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                storage_path = root / "srv" / "luma"

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": str(storage_path)},
                    )
                self.assertTrue(result["saved"])
                command = host_prep.call_args.args[0]
                self.assertIn("nfs-kernel-server", command)
                self.assertIn(str(storage_path), command)
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_does_not_save_managed_class_when_host_prep_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", side_effect=LumaError("prep failed")
                ), patch("luma.control.server.remove_from_nomad") as remove:
                    with self.assertRaisesRegex(LumaError, "failed to prepare managed NFS storage"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "bad-nfs", "node": "manager-host", "path": "/srv/luma"},
                        )
                self.assertNotIn("bad-nfs", load_state().get("storageClasses", {}))
                remove.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_reuses_existing_managed_export_for_same_node_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "nodeId": "builder-node-id",
                        "labels": {
                            "luma.node.name": "builder",
                            "luma.node.id": "builder-node-id",
                            "region": "home",
                        },
                    }
                }
                state["storageClasses"] = {
                    "builder-registry-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                        "regions": ["home"],
                    }
                }
                save_state(state)

                with patch("luma.control.server._prepare_managed_nfs_host") as prepare:
                    result = handle_storage_set(
                        state["deployToken"],
                        {
                            "name": "staging-runtime-nfs",
                            "node": "builder",
                            "path": "/srv/luma",
                            "regions": ["cn"],
                            "nodes": ["manager", "cn-2"],
                        },
                    )

                prepare.assert_not_called()
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export reused")
                self.assertEqual(result["storageHost"]["reusedFrom"], "builder-registry-nfs")
                saved = load_state()["storageClasses"]["staging-runtime-nfs"]
                self.assertEqual(saved["exportName"], "builder-registry-nfs")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_remove_retains_shared_export_then_removes_owner_file_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "builder-registry-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                    },
                    "staging-runtime-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                        "exportName": "builder-registry-nfs",
                    },
                }
                save_state(state)

                with patch("luma.control.server._remove_local_nfs_export") as remove_export, patch(
                    "luma.control.server.remove_from_nomad", return_value="removed"
                ):
                    first = handle_storage_remove(
                        state["deployToken"], {"name": "builder-registry-nfs"}
                    )
                    remove_export.assert_not_called()
                    self.assertEqual(
                        first["storageHost"]["export"],
                        "retained: shared by staging-runtime-nfs",
                    )

                    second = handle_storage_remove(
                        state["deployToken"], {"name": "staging-runtime-nfs"}
                    )
                    self.assertEqual(remove_export.call_count, 1)
                    removed_spec = remove_export.call_args.args[0]
                    self.assertEqual(removed_spec.name, "builder-registry-nfs")
                    self.assertEqual(second["storageHost"]["export"], remove_export.return_value)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_rejects_remote_managed_storage_when_agent_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    with self.assertRaisesRegex(LumaError, "node agent is not ready"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                        )
                host_prep.assert_not_called()
                self.assertNotIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_uses_remote_node_agent_before_saving_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host", "managed-volume-path"],
                        "waitSeconds": 0,
                    },
                )

                errors = []

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                def agent_worker():
                    try:
                        for _ in range(10):
                            leased = handle_node_agent_lease(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "os": "linux",
                                    "capabilities": ["nfs-host", "managed-volume-path"],
                                    "waitSeconds": 1,
                                },
                            ).get("task")
                            if not leased:
                                continue
                            self.assertEqual(leased["action"], "prepare-managed-nfs-host")
                            self.assertEqual(leased["payload"]["path"], "/srv/luma")
                            handle_node_agent_complete(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "taskId": leased["id"],
                                    "status": "succeeded",
                                    "message": "prepared",
                                    "result": {"message": "host NFS export ready"},
                                },
                            )
                            return
                        errors.append("agent did not receive task")
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=agent_worker)
                thread.start()
                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                    )
                thread.join(timeout=5)
                self.assertFalse(errors)
                host_prep.assert_not_called()
                self.assertTrue(result["saved"])
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
                self.assertIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_join_token_cannot_issue_agent_token_by_node_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                with self.assertRaisesRegex(LumaError, "nodeId is required"):
                    handle_node_agent_token(state["joinToken"], {"nodeName": "worker-storage"})
                issued = handle_node_agent_token(state["joinToken"], {"nodeName": "anything", "nodeId": "worker-node-id"})
                self.assertEqual(issued["nodeName"], "worker-storage")
                self.assertTrue(issued["agentToken"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_token_issuance_reports_provisioned_until_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                    }
                }
                save_state(state)
                handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage"})
                status = handle_control_status(state["deployToken"])
                item = status["nodes"]["items"][0]
                self.assertEqual(item["agentStatus"], "provisioned")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_long_poll_does_not_drop_task_queued_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                leased: list[dict[str, object] | None] = []
                errors: list[str] = []

                def lease_worker():
                    try:
                        result = handle_node_agent_lease(
                            agent_token,
                            {
                                "nodeName": "worker-storage",
                                "nodeId": "worker-node-id",
                                "os": "linux",
                                "capabilities": ["nfs-host"],
                                "waitSeconds": 2,
                            },
                        )
                        leased.append(result.get("task"))
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=lease_worker)
                thread.start()
                time.sleep(0.2)
                current = load_state()
                current.setdefault("agentTasks", {})["task-race"] = {
                    "id": "task-race",
                    "nodeName": "worker-storage",
                    "action": "prepare-managed-nfs-host",
                    "payload": {"name": "race", "path": "/srv/luma"},
                    "status": "queued",
                }
                save_state(current)
                thread.join(timeout=5)
                self.assertFalse(errors)
                self.assertTrue(leased)
                self.assertIsNotNone(leased[0])
                self.assertEqual(leased[0]["id"], "task-race")
                self.assertEqual(load_state()["agentTasks"]["task-race"]["status"], "running")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_heartbeat_does_not_lease_queued_task(self):
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                current = load_state()
                current.setdefault("agentTasks", {})["task-waiting"] = {
                    "id": "task-waiting",
                    "nodeName": "worker-storage",
                    "action": "prepare-managed-nfs-host",
                    "payload": {"name": "waiting", "path": "/srv/luma"},
                    "status": "queued",
                }
                save_state(current)

                result = control_server.handle_node_agent_heartbeat(
                    agent_token,
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host"],
                    },
                )

                self.assertEqual(result["status"], "ready")
                saved = load_state()
                self.assertEqual(saved["agentTasks"]["task-waiting"]["status"], "queued")
                self.assertEqual(saved["nodes"]["worker-storage"]["agent"]["status"], "online")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_heartbeat_fails_only_orphaned_running_tasks(self):
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {
                            "luma.node.name": "worker-storage",
                            "luma.node.id": "worker-node-id",
                            "region": "cn",
                        },
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(
                    state["deployToken"],
                    {"nodeName": "worker-storage", "nodeId": "worker-node-id"},
                )
                current = load_state()
                current["agentTasks"] = {
                    "task-active": {
                        "id": "task-active",
                        "nodeName": "worker-storage",
                        "action": "prepare-managed-nfs-host",
                        "payload": {},
                        "status": "running",
                    },
                    "task-orphan": {
                        "id": "task-orphan",
                        "nodeName": "worker-storage",
                        "action": "prepare-managed-nfs-host",
                        "payload": {},
                        "status": "running",
                        "buildRunId": "run-orphan",
                    },
                    "task-waiting": {
                        "id": "task-waiting",
                        "nodeName": "worker-storage",
                        "action": "prepare-managed-nfs-host",
                        "payload": {},
                        "status": "queued",
                    },
                    "task-retired-node": {
                        "id": "task-retired-node",
                        "nodeName": "retired-node",
                        "action": "prepare-managed-nfs-host",
                        "payload": {},
                        "status": "running",
                    },
                }
                current["buildRuns"] = {
                    "run-orphan": {
                        "id": "run-orphan",
                        "status": "running",
                        "events": [],
                    }
                }
                save_state(current)

                result = control_server.handle_node_agent_heartbeat(
                    issued["agentToken"],
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host"],
                        "activeTaskId": "task-active",
                    },
                )

                self.assertEqual(result["activeTaskId"], "task-active")
                saved = load_state()["agentTasks"]
                self.assertEqual(saved["task-active"]["status"], "running")
                self.assertEqual(saved["task-waiting"]["status"], "queued")
                self.assertEqual(saved["task-orphan"]["status"], "failed")
                self.assertEqual(saved["task-retired-node"]["status"], "failed")
                self.assertEqual(
                    saved["task-orphan"]["message"],
                    "node agent restarted before task completion",
                )
                self.assertTrue(saved["task-orphan"]["completedAt"])
                recovered = load_state()
                self.assertEqual(recovered["buildRuns"]["run-orphan"]["status"], "failed")
                self.assertEqual(
                    recovered["buildRuns"]["run-orphan"]["message"],
                    "build interrupted by node agent restart",
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_control_restart_closes_only_imports_owned_by_previous_process(self):
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                now = int(time.time())
                state["buildRuns"] = {
                    "run-orphan": {
                        "id": "run-orphan",
                        "status": "running",
                        "controlProcessInstanceId": "control-previous",
                        "agentTaskId": "task-running",
                        "events": [],
                        "createdAt": now - 30,
                        "updatedAt": now - 20,
                    },
                    "run-canceling": {
                        "id": "run-canceling",
                        "status": "canceling",
                        "controlProcessInstanceId": "control-previous",
                        "agentTaskId": "task-queued",
                        "events": [],
                        "createdAt": now - 20,
                        "updatedAt": now - 10,
                    },
                    "run-current": {
                        "id": "run-current",
                        "status": "running",
                        "controlProcessInstanceId": control_server._CONTROL_PROCESS_INSTANCE_ID,
                        "events": [],
                        "createdAt": now,
                        "updatedAt": now,
                    },
                    "run-finished": {
                        "id": "run-finished",
                        "status": "succeeded",
                        "controlProcessInstanceId": "control-previous",
                        "events": [],
                        "createdAt": now - 40,
                        "updatedAt": now - 35,
                    },
                }
                state["agentTasks"] = {
                    "task-running": {
                        "id": "task-running",
                        "nodeName": "builder",
                        "action": "build-image",
                        "status": "running",
                        "buildRunId": "run-orphan",
                    },
                    "task-queued": {
                        "id": "task-queued",
                        "nodeName": "builder",
                        "action": "build-image",
                        "status": "queued",
                        "buildRunId": "run-canceling",
                    },
                }
                save_state(state)

                self.assertEqual(
                    control_server._reconcile_orphaned_build_runs_after_control_restart(),
                    2,
                )

                recovered = load_state()
                self.assertEqual(recovered["buildRuns"]["run-orphan"]["status"], "failed")
                self.assertEqual(
                    recovered["buildRuns"]["run-orphan"]["message"],
                    "build interrupted by Control restart",
                )
                self.assertTrue(recovered["agentTasks"]["task-running"]["cancelRequestedAt"])
                self.assertEqual(
                    recovered["buildRuns"]["run-canceling"]["status"],
                    "canceled",
                )
                self.assertEqual(recovered["agentTasks"]["task-queued"]["status"], "canceled")
                self.assertEqual(recovered["buildRuns"]["run-current"]["status"], "running")
                self.assertEqual(recovered["buildRuns"]["run-finished"]["status"], "succeeded")
                self.assertEqual(
                    control_server._reconcile_orphaned_build_runs_after_control_restart(),
                    0,
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_task_execution_timeout_starts_when_task_is_leased(self):
        from luma.control import server as control_server

        wait_started = 1000.0
        execution_timeout = 300.0
        queued_deadline = control_server._agent_task_wait_deadline(
            {"status": "queued"},
            wait_started,
            execution_timeout,
        )
        running_deadline = control_server._agent_task_wait_deadline(
            {"status": "running", "leasedAt": 1200},
            wait_started,
            execution_timeout,
        )

        self.assertEqual(
            queued_deadline,
            wait_started
            + max(execution_timeout, control_server.AGENT_TASK_QUEUE_TIMEOUT_SECONDS),
        )
        self.assertEqual(running_deadline, 1500.0)

    def test_node_agent_alias_can_lease_canonical_node_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "aly": {
                        "region": "cn",
                        "aliases": ["iZ0jl8auywzycory05d9cuZ"],
                        "swarmNodeId": "node-id-aly",
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "iZ0jl8auywzycory05d9cuZ", "nodeId": "node-id-aly"})
                agent_token = issued["agentToken"]
                current = load_state()
                current.setdefault("agentTasks", {})["task-image"] = {
                    "id": "task-image",
                    "nodeName": "aly",
                    "action": "resolve-docker-image",
                    "payload": {"image": "ghcr.io/acme/api:latest"},
                    "status": "queued",
                }
                current["agentTasks"]["task-orphan-alias"] = {
                    "id": "task-orphan-alias",
                    "nodeName": "iZ0jl8auywzycory05d9cuZ",
                    "action": "resolve-docker-image",
                    "payload": {"image": "ghcr.io/acme/orphan:latest"},
                    "status": "running",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "iZ0jl8auywzycory05d9cuZ",
                        "nodeId": "node-id-aly",
                        "os": "linux",
                        "capabilities": ["docker-image"],
                        "waitSeconds": 0,
                    },
                ).get("task")

                self.assertIsNotNone(leased)
                self.assertEqual(leased["id"], "task-image")
                self.assertEqual(
                    load_state()["agentTasks"]["task-orphan-alias"]["status"],
                    "failed",
                )
                handle_node_agent_complete(
                    agent_token,
                    {
                        "nodeName": "iZ0jl8auywzycory05d9cuZ",
                        "nodeId": "node-id-aly",
                        "taskId": "task-image",
                        "status": "succeeded",
                        "message": "resolved",
                        "result": {"deployed": "ghcr.io/acme/api@sha256:abc"},
                    },
                )
                self.assertEqual(load_state()["agentTasks"]["task-image"]["status"], "succeeded")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_host_prep_runs_privileged_chroot_container(self):
        docker_calls = []
        raw_calls = []

        def fake_docker_request(method, path, body=None):
            docker_calls.append((method, path, body))
            if method == "GET" and path == "/images/ubuntu%3A22.04/json":
                return {}
            if method == "POST" and path.startswith("/containers/create"):
                return {"Id": "container-id"}
            if method == "POST" and path == "/containers/container-id/start":
                return None
            if method == "POST" and path == "/containers/container-id/wait":
                return {"StatusCode": 0}
            if method == "DELETE" and path.startswith("/containers/container-id"):
                return None
            raise AssertionError(f"unexpected Docker request: {method} {path}")

        def fake_docker_request_raw(method, path, headers=None):
            raw_calls.append((method, path, headers))
            if method == "GET" and path.startswith("/containers/container-id/logs"):
                return 200, "prepared"
            raise AssertionError(f"unexpected raw Docker request: {method} {path}")

        with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_docker_request_raw
        ):
            result = _run_host_prep_container("echo ready")

        self.assertEqual(result, "prepared")
        create_body = next(body for method, path, body in docker_calls if method == "POST" and path.startswith("/containers/create"))
        self.assertEqual(create_body["Cmd"], ["chroot", "/host", "bash", "-lc", "echo ready"])
        self.assertTrue(create_body["HostConfig"]["Privileged"])
        self.assertEqual(create_body["HostConfig"]["PidMode"], "host")
        self.assertEqual(create_body["HostConfig"]["NetworkMode"], "host")
        self.assertIn("/:/host", create_body["HostConfig"]["Binds"])
        self.assertTrue(any(method == "DELETE" and path.startswith("/containers/container-id") for method, path, _ in docker_calls))

    def test_storage_remove_removes_managed_storage_nomad_job_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "cn-node",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: luma-storage-cn-nfs") as remove:
                    result = handle_storage_remove(state["deployToken"], {"name": "cn-nfs"})
                remove.assert_called_once()
                self.assertEqual(remove.call_args.kwargs["slug"], "luma-storage-cn-nfs")
                self.assertEqual(result["storageHost"]["removed"], "Nomad job removed: luma-storage-cn-nfs")
                self.assertNotIn("cn-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_resolves_storage_class_from_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "external",
                        "endpoint": "home-nas:/srv/luma",
                        "regions": ["cn"],
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data", "initialize": "empty"}},
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed") as upsert:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": False},
                    )
                stack_text = upsert.call_args.args[1]
                self.assertIn('"source": "luma-app-stack-pg-data-', stack_text)
                self.assertIn('"volume_options"', stack_text)
                self.assertIn('"device": ":/srv/luma/pg-data"', stack_text)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                self.assertEqual(result["storage"]["mounts"][0]["endpoint"], "home-nas:/srv/luma")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_nomad_compose_deployment_registers_job_and_resolves_node_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "name": "lab",
                        "region": "home",
                        "status": "ready",
                        "tailscaleIP": "100.69.154.50",
                    }
                }
                state["registries"] = {
                    "gcode.gaojiua.com:3000": {
                        "username": "deploy",
                        "password": "registry-token",
                        "serverAddress": "gcode.gaojiua.com:3000",
                    }
                }
                save_state(state)
                handle_secret_set(state["deployToken"], {"name": "GRANARY_MYSQL_ROOT_PASSWORD", "value": "mysql-secret"})
                handle_secret_set(state["deployToken"], {"name": "GRANARY_ADMIN_PASSWORD", "value": "admin-secret"})
                handle_secret_set(state["deployToken"], {"name": "GRANARY_JWT_SECRET", "value": "jwt-secret"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "mysql": {
                                "image": "mysql:8.4.9",
                                "environment": {
                                    "MYSQL_DATABASE": "ledger",
                                    "MYSQL_ROOT_PASSWORD": "${GRANARY_MYSQL_ROOT_PASSWORD}",
                                },
                                "volumes": ["ledger_mysql_data:/var/lib/mysql"],
                            },
                            "ledger": {
                                "image": "gcode.gaojiua.com:3000/gaojiuatech/ledger:latest",
                                "environment": {
                                    "GRANARY_ADMIN_PASSWORD": "${GRANARY_ADMIN_PASSWORD}",
                                    "GRANARY_JWT_SECRET": "${GRANARY_JWT_SECRET}",
                                    "GRANARY_MYSQL_DSN": "root:${GRANARY_MYSQL_ROOT_PASSWORD}@tcp(mysql:3306)/ledger",
                                },
                            },
                            "ledger-frontend": {
                                "image": "gcode.gaojiua.com:3000/gaojiuatech/ledger-frontend:latest",
                            },
                        },
                        "volumes": {"ledger_mysql_data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "ledger",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {"node": "lab", "exposure": "tcp-relay", "domain": "ledger-db.example.net", "port": 3306},
                            "ledger": {"node": "lab", "exposure": "tailscale-relay", "domain": "api-ledger.example.net", "port": 8888},
                            "ledger-frontend": {"node": "lab", "exposure": "tailscale-relay", "domain": "ledger.example.net", "port": 80, "publishPort": 8081},
                        },
                    }
                )
                with patch("luma.control.server._local_storage_previous_node", return_value=""), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job registered for ledger") as deploy, patch(
                    "luma.control.server.docker_request"
                ) as docker_request, patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server._probe_public_route", return_value="Public route probe skipped"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                deploy.assert_called_once()
                docker_request.assert_not_called()
                job_text = deploy.call_args.args[1]
                self.assertIn('"ID": "ledger"', job_text)
                self.assertIn('"source": "ledger_mysql_data"', job_text)
                self.assertIn('"server_address": "gcode.gaojiua.com:3000"', job_text)
                self.assertIn("mysql-secret", job_text)
                self.assertEqual(result["orchestrator"], "Nomad job registered for ledger")
                mysql_route = (root / "routes" / "ledger-mysql.yml").read_text(encoding="utf-8")
                api_route = (root / "routes" / "ledger-ledger.yml").read_text(encoding="utf-8")
                frontend_route = (root / "routes" / "ledger-ledger-frontend.yml").read_text(encoding="utf-8")
                self.assertIn("100.69.154.50:3306", mysql_route)
                self.assertIn("http://100.69.154.50:8888", api_route)
                self.assertIn("http://100.69.154.50:8081", frontend_route)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_refreshes_nomad_cni_hostports_after_nomad_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-2": {
                        "name": "home-2",
                        "region": "home",
                        "nodeId": "node-home-2",
                        "tailscaleIP": "100.84.163.118",
                        "labels": {"luma.node.id": "node-home-2", "luma.node.name": "home-2", "region": "home"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "web-stack",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "web": {
                                "node": "home-2",
                                "exposure": "tailscale-relay",
                                "domain": "web.example.com",
                                "port": 80,
                                "publishPort": 18081,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["home-2"], "results": [{"node": "home-2", "deleted": 1}], "skipped": []},
                    create=True,
                ) as refresh, patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                self.assertEqual(result["cniHostports"]["nodes"], ["home-2"])
                refresh.assert_called_once()
                self.assertEqual(refresh.call_args.args[2], "web-stack")
                self.assertEqual(refresh.call_args.kwargs["fallback_nodes"], ["home-2"])
                self.assertEqual(refresh.call_args.kwargs["ports"], [18081])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_prepares_managed_storage_paths_before_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                    }
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "app-stack/pg-data", "initialize": "empty"}},
                    }
                )

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/app-stack/pg-data" in command for command in commands))
                self.assertEqual(result["storagePreparation"][0]["prepared"], "host NFS export ready")
                self.assertEqual(result["storagePreparation"][1]["prepared"], "volume path ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_set_prepares_managed_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-node": {
                        "name": "cn-node",
                        "region": "cn",
                        "status": "labeled",
                        "swarmHostname": "cn-node",
                        "swarmNodeId": "cn-node-id",
                        "labels": {"luma.node.name": "cn-node", "luma.node.id": "cn-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "cn-node", "Swarm": {"NodeID": "cn-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "cn-node", "path": "/srv/luma"},
                    )
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_only_targets_storage_classes_referenced_by_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "cn", "status": "labeled", "swarmNodeId": "home-node-id", "labels": {"luma.node.name": "home-nas", "luma.node.id": "home-node-id"}},
                    "archive-nas": {"name": "archive-nas", "region": "cn", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    },
                    "archive-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "archive-nas",
                        "path": "/srv/archive",
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data"}},
                    }
                )
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(len(result["storage"]["storageClasses"]), 1)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/pg-data" in command for command in commands))
                self.assertFalse(any("/srv/archive" in command for command in commands))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_prepares_local_volume_on_owning_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager": {
                        "name": "manager",
                        "region": "cn",
                        "status": "manager",
                        "labels": {"luma.node.name": "manager", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {"postgres": {"image": "postgres:16", "volumes": ["pg-data:/var/lib/postgresql/data"]}},
                        "volumes": {"pg-data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {
                            "pg-data": {
                                "local": {"node": "manager", "path": "/srv/luma/staging/postgres/v1"}
                            }
                        },
                    }
                )
                with patch("luma.control.server._storage_node_is_local", return_value=False), patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"message": "volume path ready", "taskId": "task-1"},
                ) as run:
                    result = handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )

                run.assert_called_once_with(
                    unittest.mock.ANY,
                    "manager",
                    "prepare-managed-volume-path",
                    {"root": "/srv/luma/staging/postgres", "relative": "v1", "preserveExisting": True},
                )
                self.assertEqual(result["applied"][0]["path"], "/srv/luma/staging/postgres/v1")
                self.assertEqual(result["applied"][0]["node"], "manager")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_blocks_nfs_to_local_switch_before_path_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager": {
                        "name": "manager",
                        "region": "cn",
                        "status": "manager",
                        "labels": {"luma.node.name": "manager", "region": "cn"},
                    }
                }
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {"app": {"image": "nginx:alpine", "volumes": ["data:/data"]}},
                        "volumes": {"data": {}},
                    }
                )
                first_sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"data": {"storageClass": "nfs", "path": "app/data"}},
                    }
                )
                handle_compose_deployment(
                    state["deployToken"],
                    {
                        "manifest": first_sidecar,
                        "composeContent": compose,
                        "sourceName": "luma.compose.yml",
                        "skipDns": True,
                        "skipOrchestrator": True,
                    },
                )
                local_sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {
                            "data": {"local": {"node": "manager", "path": "/srv/luma/app/data"}}
                        },
                    }
                )
                with patch("luma.control.server._run_node_agent_task") as run:
                    with self.assertRaisesRegex(LumaError, "storage backend changed"):
                        handle_compose_deployment(
                            state["deployToken"],
                            {
                                "manifest": local_sidecar,
                                "composeContent": compose,
                                "sourceName": "luma.compose.yml",
                                "skipDns": True,
                                "skipOrchestrator": True,
                            },
                        )
                run.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_accepts_import_build_services_without_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "backup-node": {
                        "name": "backup-node",
                        "region": "cn",
                        "status": "labeled",
                        "labels": {"luma.node.name": "backup-node", "region": "cn"},
                    }
                }
                state["storageClasses"] = {
                    "backup-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "backup-node",
                        "path": "/srv/backups",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "backup": {
                                "build": {"context": ".", "dockerfile": "backup.Dockerfile"},
                                "volumes": ["backup-data:/backups"],
                            }
                        },
                        "volumes": {"backup-data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "backup-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {
                            "backup-data": {
                                "storageClass": "backup-nfs",
                                "path": "backup-data",
                            }
                        },
                    }
                )
                with patch(
                    "luma.control.server._prepare_compose_managed_storage",
                    return_value={"prepared": ["backup-data"]},
                ):
                    result = handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(result["deployment"], "backup-stack")
                self.assertEqual(result["applied"], {"prepared": ["backup-data"]})
                self.assertTrue(any("uses build" in warning for warning in result["storage"]["warnings"]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_rejects_unsafe_managed_volume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-nas": {"name": "home-nas", "region": "cn", "status": "labeled"}}
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "../escape"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "relative and cannot contain"):
                    handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_credentials_are_saved_without_returning_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                saved = handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                self.assertEqual(saved, {"host": "ghcr.io", "username": "octo", "saved": True})
                listed = handle_registry_list(state["deployToken"])
                serialized = json.dumps(listed)
                self.assertIn("ghcr.io", serialized)
                self.assertIn("octo", serialized)
                self.assertNotIn("ghp_secret", serialized)
                self.assertEqual(load_state()["registries"]["ghcr.io"]["password"], "ghp_secret")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_remove_cleans_luma_registry_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                result = handle_registry_remove(state["deployToken"], {"host": "ghcr.io"})
                self.assertTrue(result["removed"])
                self.assertNotIn("portainerRegistryRemoved", result)
                self.assertNotIn("ghcr.io", load_state().get("registries", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_uses_registry_auth_for_pull_and_nomad_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                )
                captured = {}

                def fake_resolve(_config, service, **kwargs):
                    captured["image_auth"] = kwargs.get("registry_auth")
                    return service, {"requested": service.image, "selected": service.image, "registryAuth": bool(kwargs.get("registry_auth"))}

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=fake_resolve
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ) as deploy:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": False},
                    )
                self.assertTrue(result["image"]["registryAuth"])
                self.assertEqual(captured["image_auth"]["username"], "octo")
                deploy.assert_called_once()
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn('"auth": {', stack)
                self.assertIn('"username": "octo"', stack)
                self.assertIn('"password": "ghp_secret"', stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_unpinned_fixed_tag_deployment_defers_image_pull_to_scheduled_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:1.2.3",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                def fail_manager_pull(*_args, **_kwargs):
                    raise AssertionError("manager Docker image pull should not be used for unpinned deployments")

                with patch("luma.control.server.docker_request_raw", side_effect=fail_manager_pull):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertTrue(result["image"]["deferred"])
                self.assertEqual(result["image"]["resolvedBy"], "scheduled-node")
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn("\"image\": \"ghcr.io/acme/api:1.2.3\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_image_resolve_lease_injects_registry_auth_without_persisting_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-1": {
                        "region": "cn",
                        "swarmHostname": "worker-1",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-1", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-1", "nodeId": "worker-node-id"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-image"] = {
                    "id": "task-image",
                    "nodeName": "worker-1",
                    "action": "resolve-docker-image",
                    "payload": {"image": "ghcr.io/acme/private-api:1", "forcePull": False, "platform": ""},
                    "status": "queued",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    issued["agentToken"],
                    {
                        "nodeName": "worker-1",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["docker-image"],
                        "waitSeconds": 0,
                    },
                )["task"]
                self.assertEqual(leased["payload"]["registryAuth"]["password"], "ghp_secret")
                persisted_payload = load_state()["agentTasks"]["task-image"]["payload"]
                self.assertNotIn("registryAuth", persisted_payload)
                self.assertNotIn("ghp_secret", json.dumps(load_state().get("agentTasks", {})))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_unpinned_docker_hub_deployment_keeps_original_image_without_manager_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "imageMirrors": ["mirror.local"]}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                def fail_manager_pull(*_args, **_kwargs):
                    raise AssertionError("manager Docker image pull should not be used for unpinned deployments")

                with patch("luma.control.server.docker_request_raw", side_effect=fail_manager_pull):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertTrue(result["image"]["deferred"])
                self.assertFalse(result["image"]["fallback"])
                self.assertEqual(result["image"]["deployed"], "nginx:alpine")
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn("\"image\": \"nginx:alpine\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_target_pull_network_failure_configures_target_proxy_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-1": {
                        "region": "cn",
                        "status": "manager",
                        "tailscaleIP": "100.64.0.1",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "manager-1", "role.egress": "true"},
                    },
                    "worker-1": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "worker-node-id",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:latest",
                        "region": "cn",
                        "node": "worker-1",
                        "exposure": "none",
                    }
                )
                docker_nodes = [
                    {
                        "ID": "worker-node-id",
                        "Description": {"Hostname": "worker-1", "Platform": {"OS": "linux", "Architecture": "x86_64"}},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.10"},
                    }
                ]
                digest = "ghcr.io/acme/api@sha256:abc123"
                with patch("luma.control.server.docker_request", return_value=docker_nodes), patch(
                    "luma.control.server._running_egress_gateway_node_name", return_value="manager-1"
                ), patch(
                    "luma.control.server.resolve_registry_image_digest",
                    side_effect=AssertionError("Control must not resolve a pinned target's mutable image"),
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=[
                        LumaError("target node Docker pull failed for ghcr.io/acme/api:latest: network is unreachable"),
                        {"message": "Docker daemon egress proxy configured"},
                        {"deployed": digest, "digest": digest},
                    ],
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual([call.args[2] for call in agent.call_args_list], ["resolve-docker-image", "configure-docker-egress-proxy", "resolve-docker-image"])
                self.assertEqual(agent.call_args_list[1].args[3]["proxy"], "http://100.64.0.1:7890")
                self.assertEqual(agent.call_args_list[0].args[3]["image"], "ghcr.io/acme/api:latest")
                self.assertTrue(agent.call_args_list[0].args[3]["forcePull"])
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertEqual(result["image"]["resolvedBy"], "target-node")
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_docker_hub_image_falls_back_to_mirror_after_proxy_retry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-1": {
                        "region": "cn",
                        "status": "manager",
                        "tailscaleIP": "100.64.0.1",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "manager-1", "role.egress": "true"},
                    },
                    "worker-1": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "worker-node-id",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "imageMirrors": ["mirror.local"]}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "node": "worker-1",
                        "exposure": "none",
                    }
                )
                docker_nodes = [
                    {
                        "ID": "worker-node-id",
                        "Description": {"Hostname": "worker-1", "Platform": {"OS": "linux", "Architecture": "x86_64"}},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.10"},
                    }
                ]
                mirror_digest = "mirror.local/nginx@sha256:def456"
                with patch("luma.control.server.docker_request", return_value=docker_nodes), patch(
                    "luma.control.server._running_egress_gateway_node_name", return_value="manager-1"
                ) as running_egress, patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=[
                        LumaError("target node Docker pull failed for nginx:alpine: failed to do request: EOF"),
                        {"message": "Docker daemon egress proxy configured"},
                        LumaError("target node Docker pull failed for nginx:alpine: failed to do request: EOF"),
                        {"deployed": mirror_digest, "digest": mirror_digest},
                    ],
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual(
                    [call.args[2] for call in agent.call_args_list],
                    ["resolve-docker-image", "configure-docker-egress-proxy", "resolve-docker-image", "resolve-docker-image"],
                )
                self.assertEqual(agent.call_args_list[1].args[3]["proxy"], "http://100.64.0.1:7890")
                self.assertEqual(
                    [call.args[3]["image"] for call in agent.call_args_list if call.args[2] == "resolve-docker-image"],
                    ["nginx:alpine", "nginx:alpine", "mirror.local/nginx:alpine"],
                )
                self.assertGreaterEqual(running_egress.call_count, 1)
                self.assertTrue(result["image"]["fallback"])
                self.assertEqual(result["image"]["selected"], "mirror.local/nginx:alpine")
                self.assertEqual(result["image"]["deployed"], mirror_digest)
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f"\"image\": \"{mirror_digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_tailscale_relay_uses_pinned_nomad_node_tailscale_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "m3max": {"name": "m3max", "region": "home", "status": "ready", "tailscaleIP": "100.64.0.3"},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "m3max",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch(
                    "luma.control.server.deploy_to_nomad",
                    return_value="Nomad job deployed",
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True},
                    )
                route = (root / "routes" / "home-panel.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.3:8080", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_tailscale_relay_uses_publish_port_for_pinned_nomad_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "name": "home-mac-mini",
                        "region": "home",
                        "status": "ready",
                        "tailscaleIP": "100.64.0.2",
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "code-server",
                        "image": "lscr.io/linuxserver/code-server:latest",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "tailscale-relay",
                        "domain": "code.example.com",
                        "port": 8443,
                        "publishPort": 1997,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch(
                    "luma.control.server.deploy_to_nomad",
                    return_value="Nomad job deployed",
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "code-server.yaml", "skipDns": True},
                )
                route = (root / "routes" / "code-server.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.2:1997", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_cn_edge_uses_registered_node_address_for_nomad_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-2": {
                        "name": "cn-2",
                        "region": "cn",
                        "status": "ready",
                        "nodeId": "node-cn-2",
                        "tailscaleIP": "100.64.29.91",
                        "labels": {"luma.node.name": "cn-2", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "stackRoot": str(root / "stacks"),
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "price-app.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    price-app: {}\n  services:\n    price-app: {}\n",
                    encoding="utf-8",
                )
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "price",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "services": {
                            "app": {
                                "node": "cn-2",
                                "exposure": "cn-edge",
                                "domain": "price.example.com",
                                "port": 8000,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed") as deploy, patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                stack_text = deploy.call_args.args[1]
                self.assertIn('"Address": "100.64.29.91"', stack_text)
                self.assertNotIn('"AddressMode": "host"', stack_text)
                self.assertFalse(stale_route.exists())
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Remove stale file-provider route app=ok:Removed stale file-provider route", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_tailscale_relay_uses_pinned_nomad_node_tailscale_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {"name": "home-mac-mini", "region": "home", "status": "ready", "tailscaleIP": "100.64.0.2"},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump({"services": {"nextcloud": {"image": "nextcloud:apache"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "nextcloud": {
                                "region": "home",
                                "node": "home-mac-mini",
                                "exposure": "tailscale-relay",
                                "domain": "next.example.com",
                                "port": 80,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server.sync_dns", return_value="DNS synced"
                ), patch("luma.control.server._probe_public_route", return_value="Public route probe skipped"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                route = (root / "routes" / "nextcloud-nextcloud.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.2:80", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay nextcloud=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_fails_when_referenced_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = os.environ.get("DATABASE_URL")
            try:
                os.environ.pop("DATABASE_URL", None)
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), self.assertRaises(LumaError):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_secret_list_hides_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                result = handle_secret_list(state["deployToken"])
                self.assertEqual(result, {"secrets": ["DATABASE_URL"]})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_secret_remove_cleans_global_and_scoped_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://global"})
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://api", "scope": "api"})
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://global-scope", "scope": "global"})

                global_scope = handle_secret_remove(state["deployToken"], {"name": "DATABASE_URL", "scope": "global"})
                self.assertEqual(global_scope, {"name": "DATABASE_URL", "scope": "global", "removed": True})
                self.assertIn("DATABASE_URL", load_state().get("secrets", {}))
                scoped = handle_secret_remove(state["deployToken"], {"name": "DATABASE_URL", "scope": "api"})
                global_secret = handle_secret_remove(state["deployToken"], {"name": "DATABASE_URL"})
                missing = handle_secret_remove(state["deployToken"], {"name": "DATABASE_URL", "scope": "api"})

                self.assertEqual(scoped, {"name": "DATABASE_URL", "scope": "api", "removed": True})
                self.assertEqual(global_secret, {"name": "DATABASE_URL", "scope": "", "removed": True})
                self.assertEqual(missing, {"name": "DATABASE_URL", "scope": "api", "removed": False})
                self.assertEqual(handle_secret_list(state["deployToken"]), {"secrets": []})
                persisted = load_state()
                self.assertNotIn("DATABASE_URL", persisted.get("secrets", {}))
                self.assertNotIn("api", persisted.get("scopedSecrets", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_secret_remove_is_exposed_by_control_api(self):
        from starlette.testclient import TestClient
        from luma.control.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "OPENAI_API_KEY", "value": "secret", "scope": "api"})
                with TestClient(create_app()) as client:
                    response = client.post(
                        "/v1/secrets/remove",
                        json={"name": "OPENAI_API_KEY", "scope": "api"},
                        headers={"Authorization": f"Bearer {state['deployToken']}"},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"name": "OPENAI_API_KEY", "scope": "api", "removed": True})
                self.assertEqual(handle_secret_list(state["deployToken"]), {"secrets": []})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_scoped_env_secrets_are_imported_from_deploy_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = _set_env("DATABASE_URL", "postgres://stale-global")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://legacy-global"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"):
                    result = handle_deployment(
                        state["deployToken"],
                        {
                            "manifest": manifest,
                            "sourceName": "api.yaml",
                            "skipDns": True,
                            "skipOrchestrator": True,
                            "envSecrets": {"DATABASE_URL": "postgres://scoped-api", "UNUSED_TOKEN": "do-not-store"},
                        },
                    )
                persisted = load_state()
                self.assertEqual(persisted["scopedSecrets"]["api"]["DATABASE_URL"], "postgres://scoped-api")
                self.assertNotIn("UNUSED_TOKEN", persisted["scopedSecrets"]["api"])
                self.assertIn("api/DATABASE_URL", handle_secret_list(state["deployToken"])["secrets"])
                stack_text = Path(result["written"][0]).read_text(encoding="utf-8")
                self.assertIn("postgres://scoped-api", stack_text)
                self.assertNotIn("postgres://legacy-global", stack_text)
                self.assertNotIn("postgres://stale-global", stack_text)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Load scoped env=ok:api: imported 1 of 1 referenced secret(s)", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_existing_scoped_secret_blocks_global_fallback_for_missing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_openai = _set_env("OPENAI_API_KEY", "global-openai")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://scoped-api", "scope": "api"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                    }
                )
                with self.assertRaisesRegex(LumaError, "missing scoped deployment secrets for api: OPENAI_API_KEY"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("OPENAI_API_KEY", old_openai)

    def test_scoped_secret_env_does_not_leak_to_unscoped_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = _set_env("DATABASE_URL", "")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                api_manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                worker_manifest = yaml.safe_dump(
                    {
                        "name": "worker",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"):
                    handle_deployment(
                        state["deployToken"],
                        {
                            "manifest": api_manifest,
                            "sourceName": "api.yaml",
                            "skipDns": True,
                            "skipOrchestrator": True,
                            "envSecrets": {"DATABASE_URL": "postgres://api-only"},
                        },
                    )
                    # The scoped secret is persisted under the api scope only.
                    self.assertEqual(load_state()["scopedSecrets"]["api"]["DATABASE_URL"], "postgres://api-only")
                    # worker has no scope and no global secret, so its ${DATABASE_URL}
                    # cannot resolve — the api-scoped value must NOT bleed into it.
                    with self.assertRaisesRegex(LumaError, "missing deployment secret: DATABASE_URL"):
                        handle_deployment(
                            state["deployToken"],
                            {
                                "manifest": worker_manifest,
                                "sourceName": "worker.yaml",
                                "skipDns": True,
                                "skipOrchestrator": True,
                            },
                        )
                # Render is pure: no deploy ever mutates process-global env.
                self.assertEqual(os.environ.get("DATABASE_URL"), "")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_deployment_skip_flags_are_honored_by_control_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns"
                ) as sync, patch("luma.control.server.deploy_to_nomad") as deploy:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                sync.assert_not_called()
                deploy.assert_not_called()
                self.assertEqual(result["dns"], "DNS skipped: --skip-dns")
                self.assertEqual(result["orchestrator"], "Orchestrator deploy skipped")
                self.assertEqual(result["probe"], "Public route probe skipped: orchestrator deploy skipped")
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Sync DNS=ok:DNS skipped: --skip-dns", steps)
                self.assertIn("Deploy Nomad job=ok:Orchestrator deploy skipped", steps)
                self.assertIn("Probe public route=ok:Public route probe skipped: orchestrator deploy skipped", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_unpinned_deployment_resolves_latest_to_digest_for_scheduled_node_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                digest = "ghcr.io/acme/api@sha256:abc123"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.docker_request_raw", side_effect=AssertionError("manager Docker pull should not resolve latest tags")
                ), patch(
                    "luma.control.server.resolve_registry_image_digest",
                    return_value=digest,
                    create=True,
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["requested"], "ghcr.io/acme/api:latest")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertFalse(result["image"].get("deferred", False))
                self.assertEqual(result["image"]["resolvedBy"], "registry")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_latest_deployment_resolves_digest_when_target_agent_lacks_docker_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "region": "home",
                        "status": "labeled",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": ["terminal"]},
                        "labels": {"region": "home", "luma.node.name": "lab", "luma.node.id": "node-lab"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "gitea",
                        "image": "ghcr.io/liutianjie/gitea-review-agent:latest",
                        "region": "home",
                        "node": "lab",
                        "exposure": "none",
                    }
                )
                digest = "ghcr.io/liutianjie/gitea-review-agent@sha256:def456"

                with patch("luma.control.server.resolve_registry_image_digest", return_value=digest, create=True), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=AssertionError("node agent image pull should not be required"),
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "gitea.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                stack = (root / "stacks" / "home" / "gitea" / "gitea.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["requested"], "ghcr.io/liutianjie/gitea-review-agent:latest")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertEqual(result["image"]["resolvedBy"], "registry")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_registry_digest_resolver_handles_bearer_auth_challenge(self):
        digest = "sha256:" + "a" * 64
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:acme/api:pull"'
        unauthorized = urllib.error.HTTPError(
            "https://ghcr.io/v2/acme/api/manifests/latest",
            401,
            "Unauthorized",
            {"WWW-Authenticate": challenge},
            io.BytesIO(b""),
        )
        token_response = MagicMock()
        token_response.__enter__.return_value.read.return_value = b'{"token":"registry-token"}'
        manifest_response = MagicMock()
        manifest_response.__enter__.return_value.headers = {"Docker-Content-Digest": digest}

        with patch("luma.control.server.urllib.request.urlopen", side_effect=[unauthorized, token_response, manifest_response]) as urlopen:
            resolved = resolve_registry_image_digest("ghcr.io/acme/api:latest")

        self.assertEqual(resolved, f"ghcr.io/acme/api@{digest}")
        manifest_retry = urlopen.call_args_list[2].args[0]
        self.assertEqual(manifest_retry.headers["Authorization"], "Bearer registry-token")
        self.assertEqual(manifest_retry.headers["Accept"].split(",", 1)[0], "application/vnd.oci.image.index.v1+json")
        token_request = urlopen.call_args_list[1].args[0]
        self.assertIn("scope=repository%3Aacme%2Fapi%3Apull", token_request.full_url)

    def test_service_image_falls_back_to_domestic_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "traefik/whoami:latest",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"defaults": {"imageMirrors": ["mirror.local"]}}, None)
            with patch("luma.control.server.ensure_image_present") as ensure:
                ensure.side_effect = [LumaError("upstream failed"), "mirror.local/traefik/whoami@sha256:abc123"]
                selected, result = resolve_service_image(config, service)
            self.assertEqual(selected.image, "mirror.local/traefik/whoami@sha256:abc123")
            self.assertEqual(result["selected"], "mirror.local/traefik/whoami:latest")
            self.assertEqual(result["deployed"], "mirror.local/traefik/whoami@sha256:abc123")
            self.assertTrue(result["fallback"])

    def test_empty_image_mirrors_disables_default_mirror_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "traefik/whoami:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"defaults": {"imageMirrors": []}}, None)
            with patch("luma.control.server.ensure_image_present", side_effect=LumaError("upstream failed")) as ensure:
                with self.assertRaisesRegex(
                    LumaError,
                    "unable to pull service image; tried traefik/whoami:latest",
                ):
                    resolve_service_image(config, service)
            self.assertEqual(ensure.call_count, 1)

    def test_service_image_pull_sends_registry_auth_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            calls = []

            def fake_raw(method, path, *, headers=None):
                calls.append((method, path, headers or {}))
                if method == "GET":
                    return 404, ""
                return 200, "{}"

            with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
                selected, result = resolve_service_image(
                    config,
                    service,
                    registry_auth={"username": "octo", "password": "ghp_secret1", "serveraddress": "ghcr.io"},
                )
            self.assertEqual(selected.image, "ghcr.io/acme/private-api:1")
            self.assertTrue(result["registryAuth"])
            pull_headers = calls[-1][2]
            self.assertIn("X-Registry-Auth", pull_headers)
            self.assertTrue(pull_headers["X-Registry-Auth"].endswith("="))
            decoded = json.loads(base64.b64decode(pull_headers["X-Registry-Auth"], validate=True).decode("utf-8"))
            self.assertEqual(decoded["username"], "octo")
            self.assertEqual(decoded["password"], "ghp_secret1")
            self.assertEqual(decoded["serveraddress"], "ghcr.io")

    def test_image_pull_egress_registry_whitelist(self):
        self.assertTrue(image_pull_requires_egress("ghcr.io/acme/api:latest"))
        self.assertTrue(image_pull_requires_egress("nginx:alpine"))
        self.assertFalse(image_pull_requires_egress("docker.1panel.live/library/nginx:alpine"))

    def test_target_image_pull_proxy_uses_running_egress_allocation_node(self):
        from luma.control.server import _target_image_pull_proxy_url

        with tempfile.TemporaryDirectory() as tmp:
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(Path(tmp) / "luma.yaml"))
            try:
                Path(tmp, "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = {
                    "nodes": {
                        "aaa-stale-egress": {
                            "tailscaleIP": "100.64.0.99",
                            "labels": {"role.egress": "true"},
                        },
                        "manager-1": {
                            "nodeId": "egress-node-id",
                            "tailscaleIP": "100.64.0.1",
                        },
                        "worker-1": {
                            "nodeId": "worker-node-id",
                            "tailscaleIP": "100.64.0.10",
                        },
                    }
                }

                def request(_client, method, path, body=None):
                    self.assertEqual((method, path), ("GET", "/v1/job/egress/allocations"))
                    return [
                        {
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "NodeID": "egress-node-id",
                            "NodeName": "nomad-egress-host",
                        }
                    ]

                with patch("luma.control.server.NomadApi.request", request):
                    self.assertEqual(_target_image_pull_proxy_url(state, "worker-1"), "http://100.64.0.1:7890")
                    self.assertEqual(_target_image_pull_proxy_url(state, "manager-1"), "http://127.0.0.1:7890")
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_image_pull_egress_configures_daemon_proxy_through_node_agent(self):
        state = {
            "nodes": {
                "manager-1": {
                    "nodeId": "node-1",
                    "hostname": "manager-host",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": ["docker-egress-proxy"],
                    },
                }
            }
        }
        docker_calls = []

        def fake_docker(method, path, body=None):
            docker_calls.append((method, path))
            if path == "/info":
                if len([call for call in docker_calls if call[1] == "/info"]) == 1:
                    return {"Name": "manager-host", "ID": "node-1"}
                return {"Name": "manager-host", "ID": "node-1", "HTTPProxy": "http://127.0.0.1:7890"}
            raise AssertionError(path)

        with patch("luma.control.server.docker_request", side_effect=fake_docker), patch(
            "luma.control.server._require_egress_gateway_running"
        ) as require_egress, patch(
            "luma.control.server._run_node_agent_task",
            return_value={"message": "Docker daemon egress proxy configured"},
        ) as agent:
            result = ensure_image_pull_egress_proxy(state, "ghcr.io/acme/api:latest")
        require_egress.assert_called_once()
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[2], "configure-docker-egress-proxy")
        self.assertEqual(agent.call_args.kwargs["required_capability"], "docker-egress-proxy")
        self.assertEqual(result, "Docker daemon egress proxy configured")

    def test_private_direct_registry_bypasses_existing_daemon_egress_proxy(self):
        state = {
            "registries": {
                "gcode.gaojiua.com:3000": {
                    "serverAddress": "gcode.gaojiua.com:3000",
                    "username": "Nickname4th",
                    "password": "secret",
                }
            },
            "nodes": {
                "manager-1": {
                    "nodeId": "node-1",
                    "hostname": "manager-host",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": ["docker-egress-proxy"],
                    },
                }
            },
        }
        docker_calls = []

        def fake_docker(method, path, body=None):
            docker_calls.append((method, path))
            if path == "/info":
                if len([call for call in docker_calls if call[1] == "/info"]) == 1:
                    return {
                        "Name": "manager-host",
                        "ID": "node-1",
                        "HTTPProxy": "http://127.0.0.1:7890",
                        "NoProxy": "localhost,127.0.0.1",
                    }
                return {
                    "Name": "manager-host",
                    "ID": "node-1",
                    "HTTPProxy": "http://127.0.0.1:7890",
                    "NoProxy": "localhost,127.0.0.1,gcode.gaojiua.com:3000,gcode.gaojiua.com",
                }
            raise AssertionError(path)

        with patch("luma.control.server.docker_request", side_effect=fake_docker), patch(
            "luma.control.server._run_node_agent_task",
            return_value={"message": "Docker daemon proxy bypass configured"},
        ) as agent:
            result = ensure_image_pull_network(
                state,
                "gcode.gaojiua.com:3000/gaojiuatech/docs-site-journey-preview:latest",
            )
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[2], "configure-docker-egress-proxy")
        payload = agent.call_args.args[3]
        self.assertIn("gcode.gaojiua.com:3000", payload["noProxy"])
        self.assertIn("gcode.gaojiua.com", payload["noProxy"])
        self.assertEqual(result, "Docker daemon proxy bypass configured")

    def test_latest_service_image_uses_local_digest_cache_when_registry_matches(self):
        calls = []
        digest = "ghcr.io/acme/api@sha256:abc123"

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            if method == "GET":
                return 200, json.dumps({"RepoDigests": [digest]})
            return 200, "{}"

        with patch("luma.control.server.resolve_registry_image_digest", return_value=digest), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ):
            resolved = ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)
        self.assertEqual(resolved, digest)
        self.assertEqual([method for method, _path, _headers in calls], ["GET"])
        self.assertEqual(calls[0][1], "/images/ghcr.io%2Facme%2Fapi%40sha256%3Aabc123/json")

    def test_latest_service_image_pulls_when_registry_digest_drifted(self):
        calls = []
        remote_digest = "ghcr.io/acme/api@sha256:def456"
        local_digest = "ghcr.io/acme/api@sha256:abc123"

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            if method == "GET" and "%40sha256%3Adef456" in path:
                return 404, ""
            if method == "GET":
                return 200, json.dumps({"RepoDigests": [local_digest]})
            return 200, "Digest: sha256:def456\n"

        with patch("luma.control.server.resolve_registry_image_digest", return_value=remote_digest), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ):
            resolved = ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)

        self.assertEqual(resolved, remote_digest)
        self.assertEqual([method for method, _path, _headers in calls], ["GET", "GET", "POST"])

    def test_pinned_service_image_uses_local_cache_when_present(self):
        calls = []

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            return 200, "{}"

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
            ensure_image_present("ghcr.io/acme/api:1.0.0")
        self.assertEqual([method for method, _path, _headers in calls], ["GET"])

    def test_service_image_pull_error_points_to_daemon_proxy_for_registry_network_failures(self):
        def fake_raw(method, path, *, headers=None):
            if method == "GET":
                return 404, ""
            return 500, '{"message":"failed to do request: Head \\"https://ghcr.io/v2/acme/api/manifests/latest\\": EOF"}'

        with patch("luma.control.server.resolve_registry_image_digest", return_value="ghcr.io/acme/api@sha256:def456"), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ), self.assertRaisesRegex(
            LumaError,
            "Docker daemon could not reach the registry",
        ):
            ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)

    def test_private_service_image_pull_error_points_to_proxy_bypass_not_egress(self):
        def fake_raw(method, path, *, headers=None):
            if method == "GET":
                return 404, ""
            return 500, '{"message":"failed to do request: Head \\"https://registry.example.com/v2/acme/api/manifests/latest\\": EOF"}'

        registry_auth = {"username": "octo", "password": "secret", "serveraddress": "registry.example.com"}
        with patch("luma.control.server.resolve_registry_image_digest", return_value="registry.example.com/acme/api@sha256:def456"), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ), self.assertRaisesRegex(
            LumaError,
            "private registry.*proxy bypass",
        ):
            ensure_image_present("registry.example.com/acme/api:latest", registry_auth=registry_auth, force_pull=True)

    def test_service_image_stream_error_points_to_target_platform_manifest(self):
        def fake_raw(method, path, *, headers=None):
            self.assertEqual(method, "POST")
            self.assertIn("platform=linux/arm64", path)
            return 200, '{"error":"no matching manifest for linux/arm64/v8 in the manifest list entries"}'

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw), self.assertRaisesRegex(
            LumaError,
            "target platform linux/arm64",
        ):
            ensure_image_present("ghcr.io/acme/api:1.0.0", platform="linux/arm64")

    def test_digest_image_without_registry_uses_docker_hub_registry(self):
        image = "mysql:8.4.9@sha256:c36050afdca850f23cef85703f84c7531a5ae155a11b5ee1c60acb09937c4084"
        self.assertEqual(registry_host_from_image(image), DEFAULT_DOCKER_REGISTRY)



if __name__ == "__main__":
    unittest.main()
