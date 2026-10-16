from __future__ import annotations

import copy
import unittest
import uuid
from unittest.mock import patch

from luma.compose_network_runtime import DockerError, LABEL_OWNER, cleanup, network_spec, prepare, watch


class FakeDocker:
    def __init__(self):
        self.container = None
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET":
            if self.container is None:
                raise DockerError(404, "not found")
            return copy.deepcopy(self.container)
        if path.startswith("/containers/create"):
            self.container = {
                "Config": {"Labels": body["Labels"]},
                "HostConfig": body["HostConfig"],
                "State": {"Running": False},
            }
        elif path.endswith("/start"):
            self.container["State"]["Running"] = True
        elif method == "DELETE":
            self.container = None
        else:
            raise AssertionError((method, path))
        return {}


class ComposeNetworkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.allocation = str(uuid.uuid4())
        self.job = "video-test"
        self.image = "registry.local/python@sha256:" + "a" * 64

    def prepare(self, ports=None):
        return prepare(self.docker, self.allocation, self.job, self.image, ["api", "database"], ports or [{"port": 8080, "published": 18080}])

    def test_only_declared_port_published_and_siblings_keep_private_aliases(self):
        info = self.prepare()
        config = info["HostConfig"]
        self.assertEqual(config["NetworkMode"], "bridge")
        self.assertEqual(config["PortBindings"], {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]})
        self.assertEqual(config["ExtraHosts"], ["api:127.0.0.1", "database:127.0.0.1"])
        self.assertTrue(info["State"]["Running"])
        self.assertEqual(config["RestartPolicy"]["Name"], "unless-stopped")

    def test_prepare_retry_is_idempotent_and_cannot_change_existing_ports(self):
        self.prepare()
        self.prepare()
        self.assertEqual(sum(path.startswith("/containers/create") for _, path, _ in self.docker.calls), 1)
        with self.assertRaisesRegex(RuntimeError, "different specification"):
            self.prepare([{"port": 8080, "published": 18081}])
        self.assertEqual(self.docker.container["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostPort"], "18080")

    def test_internal_network_has_no_host_bindings(self):
        info = prepare(self.docker, self.allocation, self.job, self.image, ["worker", "database"], [])
        self.assertEqual(info["HostConfig"]["PortBindings"], {})

    def test_cleanup_is_idempotent_and_refuses_foreign_container(self):
        self.prepare()
        self.docker.container["Config"]["Labels"][LABEL_OWNER] = "other-owner"
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            cleanup(self.docker, self.allocation, self.job)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.docker.calls))
        self.docker.container = None
        cleanup(self.docker, self.allocation, self.job)
        self.prepare()
        cleanup(self.docker, self.allocation, self.job)
        cleanup(self.docker, self.allocation, self.job)
        self.assertIsNone(self.docker.container)

    def test_invalid_and_duplicate_port_bindings_rejected(self):
        for ports in ([{"port": 0, "published": 8080}], [{"port": 80, "published": True}],
                      [{"port": 80, "published": 65536}], [{"port": 80, "published": "8080"}],
                      [{"port": 80, "published": 8080}, {"port": 81, "published": 8080}]):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                network_spec(self.image, ["api"], ports)

    def test_namespace_loss_fails_watch_so_nomad_can_reschedule_allocation(self):
        self.prepare()
        def stop(_):
            self.docker.container["State"]["Running"] = False
        with patch("luma.compose_network_runtime.time.sleep", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "network stopped"):
                watch(self.docker, self.allocation, self.job)
