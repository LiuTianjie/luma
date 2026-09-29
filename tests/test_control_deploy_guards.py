"""Deployment guards: secret isolation, locking, publish ports and terminal states."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from luma.config import LumaConfig
from luma.compose import load_compose_deployment
from luma.control.server import _DEPLOY_LOCK, _ensure_compose_exposure_supported_on_nodes, _tcp_relay_ports_needing_ingress_refresh, handle_deployment, resolve_service_node_pin
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.service import ServiceSpec, load_service
from tests.support import _restore_env, _set_env


class RenderSecretsIsolationTests(unittest.TestCase):
    """Two concurrent-style deploys referencing the same secret name with
    different scoped values must each render their OWN value. Guards the
    regression where secrets flowed through process-global os.environ, letting
    one deploy clobber another's value mid-render."""

    def _config(self):
        return LumaConfig(
            {"defaults": {"engine": "nomad", "entrypoint": "websecure", "certResolver": "letsencrypt"}},
            None,
        )

    def _service(self, name: str):
        manifest = f"""
name: {name}
image: ghcr.io/acme/{name}:latest
region: cn
exposure: none
env:
  DB_PW: ${{DB_PW}}
"""
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(manifest)
            tmp.close()
            return load_service(Path(tmp.name)), manifest
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_same_secret_name_resolves_per_scope(self):
        from luma.control.server import _render_secrets
        from luma.nomad_render import render_nomad_job

        # body is empty -> no incoming envSecrets, so _render_secrets does NOT
        # touch the filesystem; it just reads the stored scoped values.
        state = {
            "secrets": {},
            "scopedSecrets": {
                "svc-a": {"DB_PW": "secret-A"},
                "svc-b": {"DB_PW": "secret-B"},
            },
        }
        config = self._config()

        service_a, manifest_a = self._service("svc-a")
        service_b, manifest_b = self._service("svc-b")

        secrets_a, _ = _render_secrets(state, scope="svc-a", body={}, texts=[manifest_a])
        secrets_b, _ = _render_secrets(state, scope="svc-b", body={}, texts=[manifest_b])

        self.assertEqual(secrets_a["DB_PW"], "secret-A")
        self.assertEqual(secrets_b["DB_PW"], "secret-B")

        job_a = render_nomad_job(config, service_a, as_json=False, secrets=secrets_a)["Job"]
        job_b = render_nomad_job(config, service_b, as_json=False, secrets=secrets_b)["Job"]

        self.assertEqual(job_a["TaskGroups"][0]["Tasks"][0]["Env"]["DB_PW"], "secret-A")
        self.assertEqual(job_b["TaskGroups"][0]["Tasks"][0]["Env"]["DB_PW"], "secret-B")

    def test_global_secret_used_when_no_scope_override(self):
        from luma.control.server import _render_secrets

        state = {"secrets": {"DB_PW": "global-pw"}, "scopedSecrets": {}}
        _, manifest = self._service("svc-a")
        secrets, result = _render_secrets(state, scope="svc-a", body={}, texts=[manifest])
        self.assertEqual(secrets["DB_PW"], "global-pw")
        self.assertFalse(result["scoped"])

    def test_extra_referenced_token_resolves_from_scope(self):
        # cloudflared tunnel.tokenEnv is a plain field, not a ${...} reference,
        # so it must be passed as extra_referenced or the scoped/--env paths
        # silently drop it. Scoped-only token must resolve.
        from luma.control.server import _render_secrets

        state = {
            "secrets": {},
            "scopedSecrets": {"home-tool": {"CLOUDFLARE_TUNNEL_TOKEN": "scoped-tok"}},
        }
        secrets, result = _render_secrets(
            state,
            scope="home-tool",
            body={},
            texts=["name: tool\n"],  # no ${...} in the manifest text
            extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
        )
        self.assertEqual(secrets["CLOUDFLARE_TUNNEL_TOKEN"], "scoped-tok")
        self.assertTrue(result["scoped"])

    def test_extra_referenced_token_imported_from_env(self):
        # --env path: the token arrives via envSecrets and must NOT be filtered
        # out as "unreferenced" just because it isn't a ${...} placeholder.
        from luma.control.server import _render_secrets

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(Path(tmp) / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["secrets"] = {}
                state["scopedSecrets"] = {}
                save_state(state)
                secrets, result = _render_secrets(
                    state,
                    scope="home-tool",
                    body={"envSecrets": {"CLOUDFLARE_TUNNEL_TOKEN": "env-tok"}},
                    texts=["name: tool\n"],
                    extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
                )
                self.assertEqual(secrets["CLOUDFLARE_TUNNEL_TOKEN"], "env-tok")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_extra_referenced_token_missing_raises(self):
        # No global, no scope, no --env -> must raise a clear scoped-secret error
        # rather than render later failing with an opaque missing-secret.
        from luma.control.server import _render_secrets

        state = {"secrets": {}, "scopedSecrets": {"home-tool": {"OTHER": "x"}}}
        with self.assertRaises(LumaError):
            _render_secrets(
                state,
                scope="home-tool",
                body={},
                texts=["name: tool\n"],
                extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
            )


class DeployConcurrencyTests(unittest.TestCase):
    """The state-touching deploy handlers must be serialized by _DEPLOY_LOCK so
    two concurrent deploys cannot interleave their read-modify-write of
    control.json (the slug-availability / scopedSecrets TOCTOU)."""

    def _assert_serialized(self, handler_name: str):
        from luma.control import server as srv

        active = 0
        max_active = 0
        track_lock = threading.Lock()

        def fake_load_state(*a, **k):
            nonlocal active, max_active
            with track_lock:
                active += 1
                max_active = max(max_active, active)
            # Widen the critical-section window so an unlocked handler would
            # show overlapping entries (max_active > 1).
            time.sleep(0.05)
            with track_lock:
                active -= 1
            # load_state runs as the first line inside the lock; abort the rest
            # of the handler — we only care that entry is serialized.
            raise LumaError("stop after entry")

        handler = getattr(srv, handler_name)
        errors: list[Exception] = []

        def run():
            try:
                handler("tok", {"manifest": "x", "composeContent": "x"})
            except LumaError:
                pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with patch.object(srv, "load_state", side_effect=fake_load_state):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)

    def test_handle_deployment_is_serialized(self):
        self._assert_serialized("handle_deployment")

    def test_handle_compose_deployment_is_serialized(self):
        self._assert_serialized("handle_compose_deployment")


class MacPublishPortGuardTests(unittest.TestCase):
    """Mac/OrbStack nodes can't use Nomad bridge port mapping (publishPort binds
    a Mac host NIC IP absent inside the OrbStack VM -> silent 502). Deploy must
    fail fast when a service pins to a darwin node with publishPort set, but only
    then — Linux pins and unpinned (Nomad-scheduled) services stay unaffected."""

    def _spec(self, yaml: str) -> ServiceSpec:
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            f.write(yaml)
            f.close()
            return load_service(Path(f.name))
        finally:
            Path(f.name).unlink(missing_ok=True)

    def _mac_state(self):
        return {"nodes": {"macmini": {"name": "macmini", "region": "home", "platform": "darwin/arm64"}}}

    def _linux_state(self):
        return {"nodes": {"lab": {"name": "lab", "region": "home", "platform": "linux/amd64"}}}

    def test_mac_node_with_publish_port_raises(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        with self.assertRaises(LumaError) as ctx:
            resolve_service_node_pin(spec, self._mac_state())
        self.assertIn("publishPort", str(ctx.exception))

    def test_mac_node_without_publish_port_passes(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\n"
        )
        resolved = resolve_service_node_pin(spec, self._mac_state())
        self.assertEqual(resolved.node_platform, "darwin/arm64")

    def test_linux_node_with_publish_port_passes(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: lab\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        resolved = resolve_service_node_pin(spec, self._linux_state())
        self.assertEqual(resolved.node_platform, "linux/amd64")

    def _mac_state_real_shape(self):
        # The record shape node join + agent heartbeat actually produces: NO
        # top-level platform/os/arch, os+arch nested under "agent". Before the
        # fix the guard read only top-level keys and so never fired in
        # production even though the unit fixtures (with top-level platform)
        # passed. This locks the guard to the real data shape.
        return {"nodes": {"macmini": {"name": "macmini", "region": "home", "agent": {"os": "darwin", "arch": "arm64"}}}}

    def test_mac_node_real_record_shape_with_publish_port_raises(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        with self.assertRaises(LumaError) as ctx:
            resolve_service_node_pin(spec, self._mac_state_real_shape())
        self.assertIn("publishPort", str(ctx.exception))

    def test_compose_mac_node_real_record_shape_bridge_exposure_raises(self):
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(
                self._mac_state_real_shape(), self._compose_dep("macmini")
            )
        self.assertIn("macOS", str(ctx.exception))

    def _compose_dep(self, node: str, exposure: str = "tailscale-relay"):
        d = tempfile.mkdtemp()
        (Path(d) / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:latest\n")
        sidecar = (
            "name: tool\ncompose: docker-compose.yml\nregion: home\nservices:\n"
            f"  app:\n    node: {node}\n    exposure: {exposure}\n"
        )
        if exposure != "none":
            sidecar += "    domain: a.example.com\n    port: 8080\n"
        sc = Path(d) / "luma.compose.yml"
        sc.write_text(sidecar)
        return load_compose_deployment(sc)

    def test_compose_mac_node_bridge_exposure_raises(self):
        # compose render has no host-mode path; a bridge exposure on a Mac node
        # would silently 502. Must fail fast like the native path does.
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(self._mac_state(), self._compose_dep("macmini"))
        self.assertIn("macOS", str(ctx.exception))

    def test_compose_linux_node_bridge_exposure_passes(self):
        _ensure_compose_exposure_supported_on_nodes(self._linux_state(), self._compose_dep("lab"))

    def test_compose_mac_node_none_exposure_passes(self):
        _ensure_compose_exposure_supported_on_nodes(self._mac_state(), self._compose_dep("macmini", exposure="none"))

    def _compose_dep_sibling_pin(self, pin_node: str):
        # Exposed service carries NO node of its own; a sibling (db) pins the
        # whole group to pin_node. Since a compose group runs on one node (the
        # union of all pins), the exposed service still lands on pin_node and
        # renders a bridge port there. The guard must catch this, not skip it
        # because the exposed service's own .node is None.
        d = tempfile.mkdtemp()
        (Path(d) / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:latest\n  db:\n    image: postgres:16\n"
        )
        sidecar = (
            "name: tool\ncompose: docker-compose.yml\nregion: home\nservices:\n"
            "  app:\n    exposure: tailscale-relay\n    domain: a.example.com\n    port: 8080\n"
            f"  db:\n    node: {pin_node}\n    exposure: none\n"
        )
        sc = Path(d) / "luma.compose.yml"
        sc.write_text(sidecar)
        return load_compose_deployment(sc)

    def test_compose_mac_group_pin_via_sibling_raises(self):
        # Regression: the exposed service has no node pin, but its db sibling
        # pins the group to a Mac node -> the exposed bridge port lands on Mac
        # and silently 502s. The guard must fire on the group's resolved node.
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(
                self._mac_state(), self._compose_dep_sibling_pin("macmini")
            )
        self.assertIn("macOS", str(ctx.exception))

    def test_compose_linux_group_pin_via_sibling_passes(self):
        _ensure_compose_exposure_supported_on_nodes(
            self._linux_state(), self._compose_dep_sibling_pin("lab")
        )


class DeployTerminalStateTests(unittest.TestCase):
    """A deploy that fails partway must drive the record to a terminal state even
    when the failure is NOT a LumaError (e.g. OSError from a full/read-only disk,
    raw socket errors from the Nomad/DNS calls). Leaving it at "pending" strands a
    ghost deploy that also blocks later deploys (pending counts as occupying
    tcp-relay ports)."""

    def test_non_luma_error_marks_failed_partial_not_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "api", "image": "registry.local/api:1", "region": "cn", "exposure": "none"})
                with patch(
                    "luma.control.server.resolve_service_node_pin",
                    side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(OSError):
                        handle_deployment(
                            state["deployToken"],
                            {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                        )
                record = load_state()["deployments"]["services"]["api"]
                self.assertEqual(record["status"], "failed_partial")
                self.assertNotEqual(record["status"], "pending")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)


class TcpIngressRefreshAdvisoryTests(unittest.TestCase):
    """A deploy that introduces a tcp-relay port Traefik has no static entrypoint
    for must surface an advisory (the job runs but the port is unreachable until
    `luma update manager` rebuilds entrypoints). Ports already served by an active
    deployment — including this slug's own active record on redeploy — must NOT
    warn, or the advisory becomes noise that erodes trust."""

    def _state(self, deployments):
        return {"deployments": {"services": deployments.get("services", {}), "compose": deployments.get("compose", {})}}

    def test_brand_new_port_warns(self):
        state = self._state({})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [3306])

    def test_port_served_by_active_deployment_does_not_warn(self):
        state = self._state({"services": {"other": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [])

    def test_port_only_in_failed_deployment_still_warns(self):
        # A failed deploy never made it into Traefik's entrypoint set.
        state = self._state({"services": {"other": {"status": "failed_partial", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [3306])

    def test_redeploy_same_port_does_not_warn(self):
        state = self._state({"services": {"me": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [])

    def test_mixed_new_and_existing_warns_only_for_new(self):
        state = self._state({"services": {"other": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306, 5432]), [5432])

    def test_no_tcp_ports_no_warning(self):
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(self._state({}), []), [])


class AgentCallbackNoDeployLockTests(unittest.TestCase):
    """A deploy holds _DEPLOY_LOCK while it dispatches image-pull tasks to a node
    agent and waits (up to AGENT_TASK_TIMEOUT_SECONDS) for the agent to call back
    via handle_node_agent_lease / handle_node_agent_complete. If those callback
    handlers ever grabbed _DEPLOY_LOCK too (e.g. someone adds @_serialize_deploy),
    every agent-dispatched deploy would self-deadlock until the 300s timeout. Lock
    this invariant: the callbacks must NOT block on the deploy lock."""

    def _assert_not_blocked_by_deploy_lock(self, handler):
        result: dict = {}

        def run():
            try:
                handler("tok", {})  # empty body -> handler validates and raises fast
            except LumaError as exc:
                result["raised"] = str(exc)
            except Exception as exc:  # pragma: no cover - unexpected
                result["error"] = repr(exc)

        with _DEPLOY_LOCK:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=5)
            blocked = thread.is_alive()
        # The handler must have run to completion (raised a validation error)
        # while the deploy lock was held by this thread.
        self.assertFalse(blocked, "agent callback blocked on _DEPLOY_LOCK — would deadlock agent-dispatched deploys")
        self.assertIn("raised", result)

    def test_agent_lease_does_not_block_on_deploy_lock(self):
        from luma.control.server import handle_node_agent_lease

        self._assert_not_blocked_by_deploy_lock(handle_node_agent_lease)

    def test_agent_complete_does_not_block_on_deploy_lock(self):
        from luma.control.server import handle_node_agent_complete

        self._assert_not_blocked_by_deploy_lock(handle_node_agent_complete)



if __name__ == "__main__":
    unittest.main()
