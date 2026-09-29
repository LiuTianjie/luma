"""Manager bootstrap layers: Docker, Nomad, firewall, Tailscale and Control."""
import base64
import json
import unittest
from unittest.mock import Mock, patch

import yaml

from luma.config import LumaConfig
from luma.bootstrap import (
    _ensure_control_image,
    _ensure_control_image_pull_egress,
    _ensure_control_registry_direct_route,
    _resolve_control_image,
    deploy_control_stack,
    install_control_config,
    _merge_control_config,
    install_docker,
    refresh_manager_control_local,
    sync_nomad_tailscale_service_metadata,
    verify_local_nomad_node,
)
from luma.errors import LumaError


class NomadBootstrapTests(unittest.TestCase):
    def test_manager_config_merge_preserves_operator_providers_and_nested_defaults(self):
        existing = {
            "providers": {
                "dns": {
                    "type": "cloudflare",
                    "zone": "example.net",
                    "zoneId": "zone-id",
                    "apiTokenEnv": "CLOUDFLARE_API_TOKEN",
                }
            },
            "defaults": {"engine": "nomad", "routesRoot": "/opt/luma/routes"},
            "nodes": {"manager": {"host": "localhost", "roles": ["edge"]}},
        }
        incoming = {
            "defaults": {"engine": "nomad", "images": {"lumaControl": "example/control:new"}},
            "nodes": {"manager": {"host": "localhost"}},
        }

        merged = _merge_control_config(existing, incoming)

        self.assertEqual(merged["providers"], existing["providers"])
        self.assertEqual(merged["defaults"]["routesRoot"], "/opt/luma/routes")
        self.assertEqual(merged["defaults"]["images"]["lumaControl"], "example/control:new")
        self.assertEqual(merged["nodes"]["manager"]["roles"], ["edge"])

    def test_install_control_config_generates_config_without_local_path(self):
        config = LumaConfig({}, None)
        node = config.default_manager()
        if node is None:
            from luma.config import NodeConfig

            node = NodeConfig(
                name="manager",
                host="localhost",
                public_ip="127.0.0.1",
                region="cn",
                roles=["nomad-manager", "edge"],
            )
        remote = Mock()
        remote.write_secret.return_value = "Secret written: /opt/luma/luma.yaml"

        result = install_control_config(remote, config, node)

        self.assertEqual(result, "Secret written: /opt/luma/luma.yaml")
        content = remote.write_secret.call_args.args[0]
        data = yaml.safe_load(content)
        self.assertEqual(data["project"], "luma")
        self.assertEqual(data["nodes"]["manager"]["host"], "localhost")
        self.assertEqual(data["defaults"]["publicNetwork"], "public")

    def test_control_image_pull_failure_is_fatal(self):
        remote = Mock()
        remote.sudo.side_effect = Exception("pull failed")

        with self.assertRaisesRegex(LumaError, "failed to pull Luma Control image"):
            _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        remote.upload.assert_not_called()
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker image inspect" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_image_pull_retries_transient_registry_ingress_failure(self):
        remote = Mock()
        remote.sudo.side_effect = [
            Exception('Head "https://registry.example/v2/control/manifests/v1": EOF'),
            Exception("unexpected status code 502 Bad Gateway"),
            "",
        ]

        with patch("luma.bootstrap.time.sleep") as sleep:
            result = _ensure_control_image(remote, "registry.example/control:v1")

        self.assertEqual(result, "Control image pulled: registry.example/control:v1")
        self.assertEqual(remote.sudo.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 4])

    def test_control_image_prefetch_routes_internal_registry_direct_in_egress(self):
        remote = Mock()
        installed = yaml.safe_dump({"mixed-port": 7890, "rules": ["MATCH,EGRESS"]})
        remote.run_result.return_value = Mock(code=0, output="401")
        remote.sudo.return_value = base64.b64encode(installed.encode()).decode()
        remote.run.return_value = ""

        with patch("luma.bootstrap._docker_daemon_uses_egress_proxy", return_value=True), patch(
            "luma.bootstrap._wait_nomad_job", return_value="egress ready"
        ):
            result = _ensure_control_registry_direct_route(
                remote,
                "registry.example.net/luma-control:v1",
            )

        self.assertEqual(result, "Internal registry now bypasses external egress: registry.example.net")
        written = yaml.safe_load(remote.write_secret.call_args.args[0])
        self.assertEqual(
            written["rules"],
            ["DOMAIN,registry.example.net,DIRECT", "MATCH,EGRESS"],
        )
        remote.run.assert_called_once_with(
            "nomad job restart -yes -on-error=fail egress",
            timeout=180,
        )

    def test_control_image_pulls_published_image_during_bootstrap(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(remote.upload.call_count, 0)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_image_pull_uses_ephemeral_registry_auth_and_deletes_it(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(
            remote,
            "registry.example.net/luma-control:v1",
            registry_auth={
                "serverAddress": "registry.example.net",
                "username": "luma-pull",
                "password": "secret-value",
            },
        )

        self.assertEqual(result, "Control image pulled: registry.example.net/luma-control:v1")
        config = json.loads(remote.write_secret.call_args.args[0])
        encoded = config["auths"]["registry.example.net"]["auth"]
        self.assertEqual(base64.b64decode(encoded).decode(), "luma-pull:secret-value")
        self.assertNotIn("secret-value", " ".join(str(call.args) for call in remote.sudo.call_args_list))
        pull_command = remote.sudo.call_args_list[0].args[0]
        self.assertIn("DOCKER_CONFIG=/run/luma/control-image-auth-", pull_command)
        self.assertIn("docker pull registry.example.net/luma-control:v1", pull_command)
        self.assertIn("rm -rf /run/luma/control-image-auth-", remote.sudo.call_args_list[-1].args[0])
        self.assertFalse(remote.sudo.call_args_list[-1].kwargs["check"])

    def test_control_latest_image_resolves_to_pulled_repo_digest(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")

        def sudo(command):
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "docker pull" in command:
                return ""
            if "docker image inspect --format" in command:
                return '["ghcr.io/liutianjie/luma-control@sha256:abc123","mirror.local/luma-control@sha256:def456"]\n'
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control:latest", result)
        self.assertIn("resolved digest: ghcr.io/liutianjie/luma-control@sha256:abc123", result)

    def test_control_pinned_image_does_not_require_repo_digest_lookup(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")

        def sudo(command):
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control@sha256:abc123")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pull egress ready for ghcr.io", result)
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control@sha256:abc123", result)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertFalse(any("docker image inspect --format" in cmd for cmd in docker_commands))

    def test_control_image_pull_configures_docker_daemon_egress_proxy(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        info_calls = 0

        def sudo(command):
            nonlocal info_calls
            if "docker info --format" in command:
                info_calls += 1
                if info_calls == 1:
                    return "HTTPProxy= HTTPSProxy=\n"
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "systemctl restart docker" in command:
                return ""
            return ""

        remote.sudo.side_effect = sudo

        result = _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pull egress configured for ghcr.io: Docker daemon proxy http://127.0.0.1:7890")
        self.assertTrue(any("nomad job status -short egress" in call.args[0] for call in remote.run_result.call_args_list))
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("HTTP_PROXY=http://127.0.0.1:7890" in cmd for cmd in docker_commands))
        self.assertTrue(any("NO_PROXY=localhost,127.0.0.1" in cmd for cmd in docker_commands))

    def test_control_image_pull_requires_running_egress_gateway(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=1, output="")

        with self.assertRaisesRegex(LumaError, "control image pull egress requires a running Nomad egress job"):
            _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertIn("nomad job status -short egress", remote.run_result.call_args.args[0])
        remote.sudo.assert_not_called()

    def test_control_stack_deploy_uses_resolved_digest_image(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        remote.sudo.return_value = ""
        submitted = {}
        progress = []

        def capture_job(_remote, job_json, job_id):
            submitted["job"] = job_json
            submitted["jobId"] = job_id
            return f"Nomad job deployed: {job_id}"

        config = LumaConfig({}, None)

        with patch(
            "luma.bootstrap._ensure_control_image_pull_egress",
            return_value="Control image pull egress ready for ghcr.io: Docker daemon proxy http://127.0.0.1:7890",
        ), patch("luma.bootstrap._ensure_control_image", return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest"), patch(
            "luma.bootstrap._control_image_repo_digest", return_value=digest_image
        ), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status", return_value="Nomad tmpfs compatibility ok"
        ), patch(
            "luma.bootstrap._deploy_nomad_job", side_effect=capture_job
        ), patch(
            "luma.bootstrap._wait_nomad_job", return_value="Nomad job running: luma-control"
        ):
            result = deploy_control_stack(remote, config, "luma.example.com", emit=progress.append)

        self.assertIn("Control image pull egress ready for ghcr.io", result[0])
        self.assertEqual(result[1], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(result[2], f"Control image digest resolved: {digest_image}")
        self.assertEqual(result[3], "Nomad tmpfs compatibility ok")
        self.assertEqual(result[4], "Nomad job deployed: luma-control")
        self.assertEqual(result[5], "Nomad job running: luma-control")
        progress_text = "\n".join(progress)
        self.assertIn("[start] Ensure control image pull egress", progress_text)
        self.assertIn("[start] Pull Luma control image", progress_text)
        self.assertIn("[start] Resolve Luma control image digest", progress_text)
        self.assertIn("[start] Check Nomad tmpfs compatibility", progress_text)
        self.assertEqual(submitted["jobId"], "luma-control")
        self.assertIn(f'"image": "{digest_image}"', submitted["job"])

    def test_control_stack_deploy_can_skip_pull_egress_precheck(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        remote.sudo.return_value = ""
        submitted = {}
        progress = []

        def capture_job(_remote, job_json, job_id):
            submitted["job"] = job_json
            return f"Nomad job deployed: {job_id}"

        config = LumaConfig({}, None)

        with patch("luma.bootstrap._ensure_control_image_pull_egress") as ensure_egress, patch(
            "luma.bootstrap._ensure_control_image",
            return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest",
        ), patch("luma.bootstrap._control_image_repo_digest", return_value=digest_image), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status", return_value="Nomad tmpfs compatibility ok"
        ), patch(
            "luma.bootstrap._deploy_nomad_job", side_effect=capture_job
        ), patch(
            "luma.bootstrap._wait_nomad_job", return_value="Nomad job running: luma-control"
        ):
            result = deploy_control_stack(
                remote,
                config,
                "luma.example.com",
                emit=progress.append,
                require_pull_egress=False,
            )

        ensure_egress.assert_not_called()
        self.assertEqual(result[0], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(result[2], "Nomad tmpfs compatibility ok")
        self.assertNotIn("[start] Ensure control image pull egress", "\n".join(progress))
        self.assertIn(f'"image": "{digest_image}"', submitted["job"])


    def test_manager_control_refresh_updates_ingress_without_recreating_other_core_stacks(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["nomad-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "deployments": {
                "services": {},
                "compose": {
                    "ledger": {
                        "status": "active",
                        "tcpRelayPorts": [3306],
                    }
                },
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="manager-host"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("manager-host", "nomad-node-id")
        ), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ) as install_config, patch("luma.bootstrap.install_control_state", return_value="state") as install_state, patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ) as prefetch, patch(
            "luma.bootstrap.configure_firewall", return_value="firewall"
        ) as configure_fw, patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ) as deploy_nomad, patch(
            "luma.bootstrap._wait_nomad_job", return_value="traefik ready"
        ), patch(
            "luma.bootstrap.install_docker"
        ) as docker, patch("luma.bootstrap.setup_egress") as egress, patch(
            "luma.bootstrap._refresh_core_services"
        ) as refresh_core, patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ) as sync_metadata, patch("luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog") as watchdog:
            result = refresh_manager_control_local(config, node, "luma.example.com", state)

        self.assertIn("firewall", result)
        self.assertIn("traefik ready", result)
        self.assertIn("metadata", result)
        self.assertIn("watchdog", result)
        self.assertIn("control image prefetched", result)
        self.assertIn("config", result)
        self.assertIn("state", result)
        self.assertIn("control", result)
        self.assertEqual(state["domain"], "luma.example.com")
        self.assertEqual(state["nodes"]["manager"]["nomadNodeId"], "nomad-node-id")
        self.assertNotIn("managerAddr", state)
        install_config.assert_called_once()
        install_state.assert_called_once()
        deploy_control.assert_called_once()
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        prefetch.assert_called_once()
        configure_fw.assert_called_once()
        self.assertEqual(configure_fw.call_args.kwargs["tcp_ports"], [3306])
        deploy_nomad.assert_called_once()
        self.assertIn("--entrypoints.tcp-3306.address=:3306", deploy_nomad.call_args.args[1])
        watchdog.assert_called_once()
        sync_metadata.assert_called_once()
        docker.assert_not_called()
        egress.assert_not_called()
        refresh_core.assert_not_called()

    def test_nomad_manager_control_refresh_uses_luma_node_name(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "aly": {
                        "host": "iZ0jl8auywzycory05d9cuZ",
                        "publicIp": "127.0.0.1",
                        "region": "cn",
                        "roles": ["nomad-manager", "edge"],
                    }
                },
            },
            None,
        )
        node = config.get_node("aly")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "nodes": {
                "iZ0jl8auywzycory05d9cuZ": {
                    "region": "cn",
                    "aliases": ["old-manager"],
                }
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="iZ0jl8auywzycory05d9cuZ"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("iZ0jl8auywzycory05d9cuZ", "node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.64.0.125"), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ), patch("luma.bootstrap.install_control_state", return_value="state"), patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ), patch("luma.bootstrap.configure_firewall", return_value="firewall"), patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ), patch("luma.bootstrap._wait_nomad_job", return_value="traefik ready"), patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ), patch(
            "luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog"
        ):
            refresh_manager_control_local(config, node, "luma.example.com", state)

        deploy_control.assert_called_once()
        self.assertEqual(deploy_control.call_args.kwargs["node_name"], "aly")
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        self.assertIn("aly", state["nodes"])
        self.assertNotIn("iZ0jl8auywzycory05d9cuZ", state["nodes"])
        self.assertEqual(state["nodes"]["aly"]["displayName"], "aly")
        self.assertIn("iZ0jl8auywzycory05d9cuZ", state["nodes"]["aly"]["aliases"])
        self.assertIn("old-manager", state["nodes"]["aly"]["aliases"])
        self.assertNotIn("managerAddr", state)

    def test_nomad_manager_control_refresh_prefers_existing_nomad_meta_over_hostname_config(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "iZ0jl8auywzycory05d9cuZ": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "region": "cn",
                        "roles": ["nomad-manager", "edge"],
                    }
                },
            },
            None,
        )
        node = config.get_node("iZ0jl8auywzycory05d9cuZ")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "nodes": {
                "aly": {
                    "region": "cn",
                    "nodeId": "node-id",
                    "labels": {"luma.node.name": "aly", "luma.node.id": "node-id"},
                }
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="iZ0jl8auywzycory05d9cuZ"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("aly", "node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.64.0.125"), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ), patch("luma.bootstrap.install_control_state", return_value="state"), patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ), patch("luma.bootstrap.configure_firewall", return_value="firewall"), patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ), patch("luma.bootstrap._wait_nomad_job", return_value="traefik ready"), patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ), patch(
            "luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog"
        ):
            refresh_manager_control_local(config, node, "luma.example.com", state)

        deploy_control.assert_called_once()
        self.assertEqual(deploy_control.call_args.kwargs["node_name"], "aly")
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        self.assertIn("aly", state["nodes"])
        self.assertNotIn("iZ0jl8auywzycory05d9cuZ", state["nodes"])
        self.assertEqual(state["nodes"]["aly"]["displayName"], "aly")
        self.assertIn("iZ0jl8auywzycory05d9cuZ", state["nodes"]["aly"]["aliases"])
        self.assertNotIn("managerAddr", state)

    def test_manager_syncs_ready_nomad_nodes_with_tailscale_service_metadata(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(
                code=0,
                output=json.dumps(
                    [
                        {
                            "ID": "node-manager",
                            "Status": "ready",
                            "Address": "100.106.154.3",
                        },
                        {
                            "ID": "node-cn-2",
                            "Status": "ready",
                            "Address": "100.64.29.91",
                        },
                        {
                            "ID": "node-down",
                            "Status": "down",
                            "Address": "100.64.0.9",
                        },
                    ]
                ),
            ),
            Mock(code=0, output="Metadata updated"),
            Mock(code=0, output="Metadata updated"),
        ]

        result = sync_nomad_tailscale_service_metadata(remote)

        self.assertEqual(result, "Nomad Tailscale service metadata applied to 2 ready node(s)")
        commands = [call.args[0] for call in remote.run_result.call_args_list[1:]]
        self.assertIn(
            "nomad node meta apply -node-id node-manager luma_tailscale_ip=100.106.154.3",
            commands,
        )
        self.assertIn(
            "nomad node meta apply -node-id node-cn-2 luma_tailscale_ip=100.64.29.91",
            commands,
        )

    def test_manager_metadata_sync_defers_unreachable_nodes_during_update(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(
                code=0,
                output=json.dumps(
                    [
                        {
                            "ID": "node-lab",
                            "Status": "ready",
                            "Address": "100.69.154.50",
                        }
                    ]
                ),
            ),
            Mock(code=1, output="Unexpected response code: 404 (No path to node)"),
        ]

        result = sync_nomad_tailscale_service_metadata(remote, strict=False)

        self.assertIn("1 unreachable node(s) deferred", result)

    def test_verify_nomad_node_accepts_tailscale_http_address(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="ready\n")

        result = verify_local_nomad_node(remote, http_addrs=["100.69.154.50"])

        self.assertEqual(result, "Nomad agent ready")
        command = remote.run_result.call_args.args[0]
        self.assertIn("http://127.0.0.1:4646/v1/agent/self", command)
        self.assertIn("http://100.69.154.50:4646/v1/agent/self", command)

    def test_install_docker_repairs_known_bad_apt_mirror(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo_result.return_value = Mock(code=1, output="")
        remote.sudo.return_value = ""

        result = install_docker(remote)

        self.assertEqual(result, "Docker installed")
        command = remote.sudo.call_args.args[0]
        self.assertIn("command -v apt-get", command)
        self.assertIn("mirrors.ivolces.com/ubuntu", command)
        self.assertIn("mirrors.aliyun.com/ubuntu", command)
        self.assertIn("apt-get install -y docker.io docker-compose-v2", command)
        self.assertNotIn("apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml || true", command)

    def test_install_docker_on_macos_requires_docker_cli(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Install Docker Desktop"):
            install_docker(remote)

    def test_install_docker_on_macos_requires_running_daemon(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=0, output=""),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Start Docker Desktop"):
            install_docker(remote)



if __name__ == "__main__":
    unittest.main()
