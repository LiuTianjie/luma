from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from luma.compose import ComposeDeploymentSpec, ComposeServiceSpec
from luma.config import LumaConfig
from luma.control import server as control_server
from luma.lae_placement import (
    PlacementFailure,
    REASON_NO_CAPACITY,
    REASON_UNAVAILABLE,
    REASON_VOLUME_INCOMPATIBLE,
    PlacementDecision,
    plan_lae_placement,
    safe_placement_json,
    validate_nomad_plan,
)
from luma.lae_runtime import (
    RuntimeBinding,
    canonical_hash,
    validate_deploy_body,
)


def _manifest(*, region: str = "cn", stateful: bool = False) -> dict:
    return {
        "region": region,
        "services": [
            {
                "key": "web",
                "resources": {"cpu": "0.25", "memoryMiB": 256},
            },
            {
                "key": "worker",
                "resources": {"cpu": "0.50", "memoryMiB": 512},
            },
        ],
        "volumes": ([{"key": "data"}] if stateful else []),
    }


def _registered(
    name: str,
    node_id: str,
    *,
    region: str = "cn",
    status: str = "ready",
    roles: list[str] | None = None,
) -> tuple[str, dict]:
    return name, {
        "nodeId": node_id,
        "region": region,
        "hostname": name + ".internal",
        "roles": list(roles or []),
        "agent": {
            "status": status,
            "os": "linux",
            "arch": "x86_64",
            "capabilities": ["docker-image"],
        },
    }


def _nomad(
    name: str,
    node_id: str,
    *,
    region: str = "cn",
    status: str = "ready",
    eligibility: str = "eligible",
    drain: bool = False,
    failure_domain: str = "",
) -> dict:
    meta = {"region": region, "luma_node_name": name}
    if failure_domain:
        meta["luma_failure_domain"] = failure_domain
    return {
        "ID": node_id,
        "Name": name + ".internal",
        "Status": status,
        "SchedulingEligibility": eligibility,
        "Drain": drain,
        "Meta": meta,
        "Drivers": {"docker": {"Detected": True, "Healthy": True}},
    }


def _storage(*, nodes: list[str] | None = None) -> dict:
    return {
        "provider": "nfs",
        "mode": "managed",
        "node": "storage",
        "path": "/srv/luma",
        "regions": ["cn"],
        "nodes": list(nodes or []),
    }


class LaePlacementTests(unittest.TestCase):
    def test_public_runtime_wire_accepts_only_region_and_rejects_node_fields(self) -> None:
        binding = RuntimeBinding(
            "tenant-wire", "app-wire", "op-wire", "rev-wire", "dep-wire"
        )
        services = [
            {
                "key": "web",
                "role": "http",
                "required": True,
                "exposure": "none",
                "image": {
                    "builderTaskRef": "build-wire",
                    "buildKey": "web",
                    "imageDigest": "sha256:" + "a" * 64,
                },
                "command": None,
                "dependencies": [],
                "resources": {"cpu": "0.25", "memoryMiB": 256},
                "environmentNames": [],
                "port": 8080,
            }
        ]
        routes = [
            {
                "serviceKey": "web",
                "kind": "http",
                "hostname": "b" * 32 + ".itool.tech",
                "containerPort": 8080,
                "exposure": "cn-edge",
                "healthPath": "/healthz",
            }
        ]
        digest = canonical_hash(
            {
                "schemaVersion": "lae.runtime-manifest/v1",
                "applicationId": binding.application_ref,
                "revisionId": binding.revision_ref,
                "name": "wire-app",
                "kind": "service",
                "region": "cn",
                "services": services,
                "routes": routes,
                "volumes": [],
                "environment": [],
            }
        )
        body = {
            "schemaVersion": "luma.lae-runtime/v1",
            "manifest": {
                "schemaVersion": "luma.lae-runtime/v1",
                "name": "wire-app",
                "kind": "service",
                "region": "cn",
                "services": services,
                "routes": routes,
                "volumes": [],
                "secretRefs": [],
                "manifestDigest": digest,
            },
        }
        self.assertEqual(validate_deploy_body(body, binding)["region"], "cn")
        for forbidden_field in (
            "node",
            "nodeId",
            "ip",
            "pool",
            "failureDomain",
            "constraints",
        ):
            invalid = json.loads(json.dumps(body))
            invalid["manifest"][forbidden_field] = "caller-controlled"
            with self.assertRaises(Exception):
                validate_deploy_body(invalid, binding)
        invalid_region = json.loads(json.dumps(body))
        invalid_region["manifest"]["region"] = "home"
        with self.assertRaises(Exception):
            validate_deploy_body(invalid_region, binding)

    def test_filters_builder_not_ready_and_wrong_region_before_nomad(self) -> None:
        registered = dict(
            [
                _registered("runtime", "node-runtime"),
                _registered("builder", "node-builder", roles=["builder"]),
                _registered("offline", "node-offline", status="offline"),
                _registered("global", "node-global", region="global"),
            ]
        )
        nodes = [
            _nomad("runtime", "node-runtime"),
            _nomad("builder", "node-builder"),
            _nomad("offline", "node-offline"),
            _nomad("global", "node-global", region="global"),
        ]
        decision = plan_lae_placement(
            manifest=_manifest(),
            registered_nodes=registered,
            nomad_nodes=nodes,
            declared_builder_nodes=["builder"],
            allowed_runtime_nodes=["runtime"],
        )
        self.assertEqual(decision.candidate_node_ids, ("node-runtime",))
        self.assertEqual(decision.requested_cpu_mhz, 750)
        self.assertEqual(decision.requested_memory_mib, 768)
        job = {
            "Job": {
                "Constraints": [
                    {
                        "LTarget": "${meta.region}",
                        "RTarget": "cn",
                        "Operand": "=",
                    }
                ]
            }
        }
        decision.apply_to_job(job)
        self.assertEqual(job["Job"]["Constraints"][-1]["LTarget"], "${node.unique.id}")
        self.assertNotIn("builder", json.dumps(job))

    def test_live_shape_prefers_unique_alias_over_historical_duplicate_id(self) -> None:
        # Mirrors the current cluster's important shape: aly and a stale
        # manager registration may share historical identity data, tecent is a
        # second cn runtime candidate, and builder is a home-only build node.
        aly_name, aly = _registered("aly", "historical-duplicate")
        manager_name, manager = _registered(
            "manager",
            "historical-duplicate",
            roles=["nomad-manager", "edge"],
        )
        manager["status"] = "manager"
        tecent_name, tecent = _registered("tecent", "node-tecent")
        builder_name, builder = _registered(
            "builder",
            "node-builder",
            region="home",
            roles=["builder"],
        )
        registered = {
            aly_name: aly,
            manager_name: manager,
            tecent_name: tecent,
            builder_name: builder,
        }
        decision = plan_lae_placement(
            manifest=_manifest(),
            registered_nodes=registered,
            nomad_nodes=[
                _nomad("aly", "historical-duplicate"),
                _nomad("manager", "node-manager"),
                _nomad("tecent", "node-tecent"),
                _nomad("builder", "node-builder", region="home"),
            ],
            declared_builder_nodes=["builder"],
            allowed_runtime_nodes=["tecent"],
        )
        self.assertEqual(
            decision.candidate_node_ids,
            ("node-tecent",),
        )

    def test_manager_or_edge_requires_explicit_runtime_role(self) -> None:
        manager_name, manager = _registered(
            "manager", "node-manager", roles=["nomad-manager", "edge"]
        )
        runtime_manager_name, runtime_manager = _registered(
            "mixed",
            "node-mixed",
            roles=["nomad-manager", "edge", "lae-runtime"],
        )
        decision = plan_lae_placement(
            manifest=_manifest(),
            registered_nodes={
                manager_name: manager,
                runtime_manager_name: runtime_manager,
            },
            nomad_nodes=[
                _nomad("manager", "node-manager"),
                _nomad("mixed", "node-mixed"),
            ],
            allowed_runtime_nodes=["mixed"],
        )
        self.assertEqual(decision.candidate_node_ids, ("node-mixed",))

    def test_hostname_manager_uses_stable_alias_after_explicit_runtime_opt_in(self) -> None:
        canonical_name, manager = _registered(
            "iZ0jlep4ral3r2v0ajypnmZ",
            "node-manager",
            roles=["nomad-manager", "edge", "lae-runtime"],
        )
        manager.update(
            {
                "status": "manager",
                "nomadRole": "server",
                "nomadServer": True,
            }
        )
        decision = plan_lae_placement(
            manifest=_manifest(),
            registered_nodes={canonical_name: manager},
            nomad_nodes=[_nomad(canonical_name, "node-manager")],
            allowed_runtime_nodes=["manager"],
        )
        self.assertEqual(decision.candidate_node_ids, ("node-manager",))

    def test_no_ready_runtime_capacity_is_stable_and_redacted(self) -> None:
        registered = dict([_registered("builder-secret", "node-secret", roles=["builder"])])
        with self.assertRaises(PlacementFailure) as caught:
            plan_lae_placement(
                manifest=_manifest(),
                registered_nodes=registered,
                nomad_nodes=[_nomad("builder-secret", "node-secret")],
                declared_builder_nodes=["builder-secret"],
                allowed_runtime_nodes=["builder-secret"],
            )
        self.assertEqual(caught.exception.reason, REASON_NO_CAPACITY)
        self.assertNotIn("builder-secret", str(caught.exception))
        self.assertNotIn("node-secret", str(caught.exception))

    def test_conflicting_region_metadata_fails_closed(self) -> None:
        registered = dict([_registered("runtime", "node-runtime")])
        node = _nomad("runtime", "node-runtime", region="global")
        with self.assertRaises(PlacementFailure) as caught:
            plan_lae_placement(
                manifest=_manifest(region="cn"),
                registered_nodes=registered,
                nomad_nodes=[node],
                allowed_runtime_nodes=["runtime"],
            )
        self.assertEqual(caught.exception.reason, REASON_NO_CAPACITY)

    def test_non_linux_amd64_runtime_is_not_eligible(self) -> None:
        name, record = _registered("mac", "node-mac")
        record["agent"]["os"] = "darwin"
        record["agent"]["arch"] = "arm64"
        with self.assertRaises(PlacementFailure) as caught:
            plan_lae_placement(
                manifest=_manifest(),
                registered_nodes={name: record},
                nomad_nodes=[_nomad("mac", "node-mac")],
                allowed_runtime_nodes=["mac"],
            )
        self.assertEqual(caught.exception.reason, REASON_NO_CAPACITY)

    def test_managed_volume_node_allowlist_mismatch_is_distinct(self) -> None:
        registered = dict(
            [
                _registered("runtime", "node-runtime"),
                _registered("storage", "node-storage"),
            ]
        )
        with self.assertRaises(PlacementFailure) as caught:
            plan_lae_placement(
                manifest=_manifest(stateful=True),
                registered_nodes=registered,
                nomad_nodes=[_nomad("runtime", "node-runtime")],
                storage_class=_storage(nodes=["another-runtime"]),
                allowed_runtime_nodes=["runtime"],
            )
        self.assertEqual(caught.exception.reason, REASON_VOLUME_INCOMPATIBLE)

    def test_prior_node_and_failure_domain_are_soft_affinities(self) -> None:
        registered = dict(
            [
                _registered("runtime-a", "node-a"),
                _registered("runtime-b", "node-b"),
            ]
        )
        decision = plan_lae_placement(
            manifest=_manifest(),
            registered_nodes=registered,
            nomad_nodes=[
                _nomad("runtime-a", "node-a", failure_domain="zone-a"),
                _nomad("runtime-b", "node-b", failure_domain="zone-a"),
            ],
            prior_node_id="node-a",
            allowed_runtime_nodes=["runtime-a", "runtime-b"],
        )
        job = {"Job": {}}
        decision.apply_to_job(job)
        affinities = job["Job"]["Affinities"]
        self.assertEqual(affinities[0]["RTarget"], "node-a")
        self.assertEqual(affinities[1]["LTarget"], "${meta.luma_failure_domain}")
        self.assertEqual(decision.continuity, "preferred")

    def test_failed_prior_node_reschedules_to_remaining_compatible_node(self) -> None:
        registered = dict(
            [
                _registered("runtime-a", "node-a"),
                _registered("runtime-b", "node-b"),
                _registered("storage", "node-storage"),
            ]
        )
        decision = plan_lae_placement(
            manifest=_manifest(stateful=True),
            registered_nodes=registered,
            nomad_nodes=[
                _nomad("runtime-a", "node-a", status="down"),
                _nomad("runtime-b", "node-b"),
            ],
            storage_class=_storage(),
            prior_node_id="node-a",
            allowed_runtime_nodes=["runtime-a", "runtime-b"],
        )
        self.assertEqual(decision.candidate_node_ids, ("node-b",))
        self.assertEqual(decision.preferred_node_id, "")
        self.assertEqual(decision.continuity, "rescheduled")

    def test_runtime_positive_admission_is_required_and_config_is_closed(self) -> None:
        registered = dict([_registered("runtime", "node-runtime")])
        with self.assertRaises(PlacementFailure) as caught:
            plan_lae_placement(
                manifest=_manifest(),
                registered_nodes=registered,
                nomad_nodes=[_nomad("runtime", "node-runtime")],
            )
        self.assertEqual(caught.exception.reason, REASON_UNAVAILABLE)

        for raw in (
            "",
            "null",
            "[]",
            '["runtime","runtime"]',
            '["runtime-b","runtime-a"]',
            '["runtime secret"]',
        ):
            with self.subTest(raw=raw), patch.dict(
                control_server.os.environ,
                {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": raw},
                clear=False,
            ), self.assertRaises(PlacementFailure) as configured:
                control_server._lae_runtime_node_allowlist()
            self.assertEqual(configured.exception.reason, REASON_UNAVAILABLE)

        with patch.dict(
            control_server.os.environ,
            {"LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": '["runtime-a","runtime-b"]'},
            clear=False,
        ):
            self.assertEqual(
                control_server._lae_runtime_node_allowlist(),
                ("runtime-a", "runtime-b"),
            )

    def test_nomad_plan_capacity_failure_does_not_surface_dimensions(self) -> None:
        secret_dimension = "secret-node-class"
        with self.assertRaises(PlacementFailure) as caught:
            validate_nomad_plan(
                {
                    "FailedTGAllocs": {
                        "app": {
                            "NodesExhausted": 1,
                            "DimensionExhausted": {secret_dimension: 1},
                        }
                    }
                }
            )
        self.assertEqual(caught.exception.reason, REASON_NO_CAPACITY)
        self.assertNotIn(secret_dimension, str(caught.exception))
        validate_nomad_plan({"FailedTGAllocs": {}})
        validate_nomad_plan({"EvalID": "eval-ok"})

    def test_control_maps_nomad_plan_to_stable_capacity_error(self) -> None:
        client = Mock()
        client.request.return_value = {
            "FailedTGAllocs": {
                "app": {
                    "ConstraintFiltered": {"secret-pool": 1},
                    "NodesExhausted": 1,
                }
            }
        }
        with self.assertRaises(control_server.LumaRuntimeError) as caught:
            control_server._lae_runtime_validate_placement_plan(
                client,
                job_slug="lae-secret-job",
                stack_text=json.dumps({"Job": {"ID": "lae-secret-job"}}),
            )
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "capacity_unavailable")
        self.assertNotIn("secret-pool", str(caught.exception))
        client.request.assert_called_once_with(
            "POST",
            "/v1/job/lae-secret-job/plan",
            {
                "Job": {"ID": "lae-secret-job"},
                "Diff": False,
                "PolicyOverride": False,
            },
        )

    def test_runtime_renderer_receives_internal_constraint_and_affinity(self) -> None:
        decision = PlacementDecision(
            region="cn",
            requested_cpu_mhz=250,
            requested_memory_mib=256,
            stateful=False,
            candidate_node_ids=("node-a", "node-b"),
            preferred_node_id="node-a",
        )
        manifest = {
            "region": "cn",
            "services": [
                {
                    "key": "web",
                    "resources": {"cpu": "0.25", "memoryMiB": 256},
                }
            ],
            "routes": [],
            "volumes": [],
            "secretRefs": [],
            "manifestDigest": "sha256:" + "a" * 64,
        }
        spec = ComposeDeploymentSpec(
            source=Path("lae-runtime"),
            compose_path=Path("lae-runtime/docker-compose.yml"),
            compose={
                "services": {
                    "web": {
                        "image": "registry.invalid/web@sha256:" + "b" * 64,
                        "deploy": {
                            "resources": {
                                "limits": {"cpus": "0.25", "memory": "256M"},
                                "reservations": {
                                    "cpus": "0.25",
                                    "memory": "256M",
                                },
                            }
                        },
                    }
                }
            },
            name="lae-runtime-test",
            region="cn",
            storage_classes={},
            volumes={},
            services={
                "web": ComposeServiceSpec(
                    name="web", region="cn", exposure="none"
                )
            },
        )
        rendered, variables, paths = control_server._lae_runtime_render_job(
            {},
            LumaConfig({"defaults": {}}, None),
            RuntimeBinding("tenant", "app", "operation", "revision", "deployment"),
            manifest,
            spec,
            {},
            runtime_deployment_ref="lae-run-render",
            placement=decision,
        )
        job = json.loads(rendered)["Job"]
        self.assertEqual(job["Constraints"][0]["LTarget"], "${meta.region}")
        self.assertEqual(job["Constraints"][1]["LTarget"], "${node.unique.id}")
        self.assertRegex("node-a", job["Constraints"][1]["RTarget"])
        self.assertEqual(job["Affinities"][0]["RTarget"], "node-a")
        self.assertEqual(variables, {})
        self.assertEqual(paths, [])

    def test_safe_summary_and_audit_json_never_include_concrete_topology(self) -> None:
        decision = PlacementDecision(
            region="cn",
            requested_cpu_mhz=250,
            requested_memory_mib=256,
            stateful=True,
            candidate_node_ids=("node-canary-10.0.0.8",),
            preferred_node_id="node-canary-10.0.0.8",
            preferred_failure_domain_key="failure_domain",
            preferred_failure_domain="secret-zone",
        )
        safe = safe_placement_json(decision)
        for forbidden in (
            "node-canary",
            "10.0.0.8",
            "secret-zone",
            "failure_domain",
            "candidateNodeIds",
            "preferredNodeId",
        ):
            self.assertNotIn(forbidden, safe)
        internal = decision.internal_state()
        self.assertIn("candidateNodeIds", internal)
        self.assertNotIn("candidateNodeIds", internal["summary"])

    def test_public_runtime_projection_redacts_internal_placement(self) -> None:
        record = {
            "runtimeDeploymentRef": "lae-run-safe",
            "status": "deploying",
            "manifestDigest": "sha256:" + "a" * 64,
            "serviceStatuses": {"web": "starting"},
            "routeStatuses": {},
            "volumeBindings": [],
            "placement": {
                "candidateNodeIds": ["node-secret-10.0.0.8"],
                "preferredFailureDomain": {
                    "metaKey": "failure_domain",
                    "value": "secret-zone",
                },
            },
        }
        public = control_server._lae_runtime_deployment_public(record)
        serialized = json.dumps(public)
        self.assertNotIn("placement", public)
        for forbidden in (
            "node-secret",
            "10.0.0.8",
            "failure_domain",
            "secret-zone",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
