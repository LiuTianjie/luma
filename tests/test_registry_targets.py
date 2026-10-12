import copy
import json
import unittest

from luma.control.registry_targets import registry_candidate_nodes, registry_task_groups
from luma.errors import LumaError


HOST = "100.64.0.70:5000"
OTHER_HOST = "registry.internal:5050"


def constraint(left, right="", operand="="):
    return {"LTarget": left, "RTarget": right, "Operand": operand}


def task(name="app", driver="docker", image=HOST + "/app:latest", constraints=None):
    return {"Name": name, "Driver": driver, "Config": {"image": image}, "Constraints": constraints or []}


def job(tasks=None, constraints=None, datacenters=None, **overrides):
    return {
        "Name": "app",
        "Datacenters": datacenters or ["dc1"],
        "Constraints": constraints or [constraint("${meta.region}", "cn")],
        "TaskGroups": [{"Name": "app", "Count": 1, "Tasks": tasks or [task()]}],
        **overrides,
    }


def node(name="mac-a", **overrides):
    return {
        "ID": name + "-id", "Name": "nomad-" + name, "Datacenter": "dc1",
        "Status": "ready", "SchedulingEligibility": "eligible", "Drain": False,
        "NodePool": "default", "NodeClass": "render",
        "Meta": {"region": "cn", "luma_node_name": name, "luma_tailscale_ip": "100.64.0.11"},
        "Attributes": {"kernel.name": "darwin", "cpu.arch": "arm64"},
        "Drivers": {"docker": {"Detected": True, "Healthy": True}},
        **overrides,
    }


def groups(value, hosts=None):
    return registry_task_groups(json.dumps({"Job": value}), [HOST] if hosts is None else hosts)


class RegistryTaskGroupsTests(unittest.TestCase):
    def test_no_managed_hosts_needs_no_job_parse(self):
        self.assertEqual(registry_task_groups("not-json", []), [])

    def test_extracts_only_exact_managed_docker_image_hosts(self):
        rendered = job(tasks=[
            task(image=HOST.upper() + "/app@sha256:abcd"),
            task("worker", image=OTHER_HOST.upper() + "/worker:v1"),
            task("sidecar", image="ghcr.io/org/sidecar:v1"),
            task("lookalike", image=HOST + ".evil.example/app:latest"),
            task("https", image="secure.internal:5050/app:v1"),
            task("private", image="10.0.0.99:5000/app:v1"),
            task("exec", driver="exec", image="registry.internal:5050/not-a-docker-image"),
        ])
        self.assertEqual(groups(rendered, [HOST, OTHER_HOST])[0]["registries"], [HOST, OTHER_HOST])

    def test_exec_task_image_and_unmanaged_docker_do_not_trigger_preflight(self):
        rendered = job(tasks=[task(driver="exec"), task("sidecar", image="ghcr.io/org/app:latest")])
        self.assertEqual(groups(rendered), [])

    def test_image_with_https_scheme_does_not_select_an_http_host(self):
        self.assertEqual(groups(job(tasks=[task(image="https://" + HOST + "/app")])), [])

    def test_default_docker_registry_is_not_aliased_into_an_http_allowlist(self):
        self.assertEqual(groups(job(tasks=[task(image="library/alpine:latest")]), ["docker.io"]), [])

    def test_zero_count_group_does_not_need_a_transport_or_supported_constraint(self):
        rendered = job()
        rendered["TaskGroups"][0].update(Count=0, Constraints=[constraint("${meta.foo}", ".*", "regexp")])
        self.assertEqual(groups(rendered), [])

    def test_collects_job_group_and_all_colocated_task_constraints(self):
        job_constraint = constraint("${meta.region}", "cn")
        group_constraint = constraint("${meta.luma_node_name}", "mac-a")
        app_constraint = constraint("${attr.cpu.arch}", "arm64")
        sidecar_constraint = constraint("${meta.gpu}", operand="is_not_set")
        rendered = job(tasks=[task(constraints=[app_constraint]), task("sidecar", "exec", constraints=[sidecar_constraint])])
        rendered["TaskGroups"][0]["Constraints"] = [group_constraint]
        selected = groups(rendered)[0]
        self.assertEqual(selected, {
            "name": "app", "registries": [HOST], "drivers": ["docker", "exec"],
            "datacenters": ["dc1"], "nodePool": "default",
            "constraints": [job_constraint, group_constraint, app_constraint, sidecar_constraint],
        })

    def test_each_group_has_its_own_hosts_and_constraints(self):
        rendered = job()
        rendered["TaskGroups"].append({"Name": "second", "Count": 2,
            "Tasks": [task(image=OTHER_HOST + "/app:latest")],
            "Constraints": [constraint("${meta.luma_node_name}", "mac-b")]})
        first, second = groups(rendered, [HOST, OTHER_HOST])
        self.assertEqual(first["registries"], [HOST])
        self.assertEqual(second["registries"], [OTHER_HOST])
        self.assertEqual(len(first["constraints"]), 1)
        self.assertEqual(len(second["constraints"]), 2)

    def test_unwrapped_job_and_nomad_defaults(self):
        rendered = job()
        del rendered["Datacenters"]
        del rendered["TaskGroups"][0]["Count"]
        selected = registry_task_groups(json.dumps(rendered), [HOST])[0]
        self.assertEqual(selected["datacenters"], ["*"])
        self.assertEqual(selected["nodePool"], "default")

    def test_invalid_job_and_group_count_fail_explicitly(self):
        for value in ("not-json", "[]", '{"Job":null}'):
            with self.subTest(value=value), self.assertRaises(LumaError):
                registry_task_groups(value, [HOST])
        for count in ("1", -1, True):
            rendered = job()
            rendered["TaskGroups"][0]["Count"] = count
            with self.subTest(count=count), self.assertRaisesRegex(LumaError, "Count"):
                groups(rendered)

    def test_unsupported_constraints_do_not_broaden_candidates(self):
        for value in (
            constraint("${meta.region}", "cn.*", "regexp"),
            constraint("${node.unknown}", "x"),
            constraint("${meta.region}", "prefix-${meta.region}"),
            constraint("${attr.}", "x"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(LumaError, "cannot evaluate"):
                groups(job(constraints=[value]))


class RegistryCandidateNodesTests(unittest.TestCase):
    def test_all_same_region_darwin_docker_nodes_are_candidates_including_manager(self):
        manager = node("manager", role="manager")
        other_region = node("us", Meta={"region": "us", "luma_node_name": "us"})
        candidates = registry_candidate_nodes(groups(job())[0], [node(), node("mac-b"), manager, other_region])
        self.assertEqual([n["ID"] for n in candidates], ["mac-a-id", "mac-b-id", "manager-id"])
        self.assertIs(candidates[2], manager)

    def test_pin_uses_actual_nomad_meta_not_nomad_name_or_role(self):
        rendered = job(constraints=[constraint("${meta.region}", "cn"), constraint("${meta.luma_node_name}", "mac-b")])
        candidates = registry_candidate_nodes(groups(rendered)[0], [node(), node("mac-b", Name="unrelated-nomad-name")])
        self.assertEqual([n["ID"] for n in candidates], ["mac-b-id"])

    def test_group_and_every_task_constraints_limit_candidates_together(self):
        rendered = job(tasks=[
            task(constraints=[constraint("${attr.cpu.arch}", "arm64")]),
            task("sidecar", image="ghcr.io/other/sidecar", constraints=[constraint("${meta.disabled}", "true", "!=")]),
        ])
        rendered["TaskGroups"][0]["Constraints"] = [constraint("${node.class}", "render")]
        candidates = [node(), node("wrong-class", NodeClass="web"),
            node("wrong-arch", Attributes={"cpu.arch": "amd64"}),
            node("disabled", Meta={"region": "cn", "disabled": "true"})]
        self.assertEqual([n["ID"] for n in registry_candidate_nodes(groups(rendered)[0], candidates)], ["mac-a-id"])

    def test_ready_eligible_and_not_draining_are_required(self):
        candidates = [node(), node("down", Status="down"), node("ineligible", SchedulingEligibility="ineligible"),
            node("drain", Drain=True), node("missing-eligibility", SchedulingEligibility=None), node("no-id", ID="")]
        self.assertEqual([n["ID"] for n in registry_candidate_nodes(groups(job())[0], candidates)], ["mac-a-id"])

    def test_missing_unhealthy_or_undetected_docker_driver_is_excluded(self):
        candidates = [node()]
        for name, driver in (("missing", {}), ("unhealthy", {"Detected": True, "Healthy": False}),
                             ("undetected", {"Detected": False, "Healthy": True}),
                             ("unknown", {"Detected": True}), ("string", {"Detected": "true", "Healthy": "true"})):
            candidates.append(node(name, Drivers={"docker": driver}))
        candidates.append(node("exec-only", Drivers={"exec": {"Detected": True, "Healthy": True}}))
        self.assertEqual([n["ID"] for n in registry_candidate_nodes(groups(job())[0], candidates)], ["mac-a-id"])

    def test_colocated_unmanaged_sidecar_driver_must_also_be_healthy(self):
        rendered = job(tasks=[task(), task("script", "exec")])
        all_drivers = node("both", Drivers={"docker": {"Detected": True, "Healthy": True}, "exec": {"Detected": True, "Healthy": True}})
        candidates = registry_candidate_nodes(groups(rendered)[0], [node(), all_drivers])
        self.assertEqual([n["ID"] for n in candidates], ["both-id"])

    def test_datacenter_patterns_and_node_pool_both_apply(self):
        rendered = job(datacenters=["edge-*", "dc1"], NodePool="renderers")
        candidates = [node("match", Datacenter="edge-hk", NodePool="renderers"),
            node("match-exact", NodePool="renderers"), node("wrong-dc", Datacenter="core-hk", NodePool="renderers"),
            node("wrong-pool", Datacenter="edge-hk")]
        self.assertEqual([n["ID"] for n in registry_candidate_nodes(groups(rendered)[0], candidates)], ["match-id", "match-exact-id"])

    def test_node_builtin_targets_and_rhs_interpolation(self):
        cases = [constraint("${node.unique.id}", "mac-a-id"), constraint("${node.unique.name}", "nomad-mac-a"),
            constraint("${node.datacenter}", "dc1"), constraint("${node.class}", "render"),
            constraint("${node.pool}", "default"), constraint("${meta.region}", "${attr.region}")]
        actual = node(Attributes={"region": "cn"})
        self.assertEqual(registry_candidate_nodes(groups(job(constraints=cases))[0], [actual]), [actual])

    def test_is_set_checks_presence_not_nonempty_value(self):
        rendered = job(constraints=[constraint("${meta.luma_tailscale_ip}", operand="is_set")])
        empty = node("empty", Meta={"luma_tailscale_ip": ""})
        missing = node("missing", Meta={})
        self.assertEqual(registry_candidate_nodes(groups(rendered)[0], [empty, missing]), [empty])

    def test_is_not_set_requires_missing_key(self):
        rendered = job(constraints=[constraint("${attr.optional}", operand="is_not_set")])
        empty = node("empty", Attributes={"optional": ""})
        missing = node("missing", Attributes={})
        self.assertEqual(registry_candidate_nodes(groups(rendered)[0], [empty, missing]), [missing])

    def test_missing_attribute_is_not_equal_to_empty_string(self):
        rendered = job(constraints=[constraint("${meta.optional}", "")])
        empty = node("empty", Meta={"optional": ""})
        missing = node("missing", Meta={})
        self.assertEqual(registry_candidate_nodes(groups(rendered)[0], [empty, missing]), [empty])

    def test_nomad_inequality_treats_missing_attribute_as_empty_value(self):
        missing = node("missing", Meta={})
        for value, matches in (("excluded", [missing]), ("", [])):
            with self.subTest(value=value):
                rendered = job(constraints=[constraint("${meta.optional}", value, "!=")])
                self.assertEqual(registry_candidate_nodes(groups(rendered)[0], [missing]), matches)

    def test_unsupported_constraint_errors_even_when_no_nodes_exist(self):
        group = groups(job())[0]
        group["constraints"].append(constraint("${meta.region}", "cn", "regexp"))
        with self.assertRaisesRegex(LumaError, "cannot evaluate"):
            registry_candidate_nodes(group, [])

    def test_selection_does_not_mutate_job_or_nodes(self):
        rendered = job()
        actual = node()
        original = copy.deepcopy((rendered, actual))
        registry_candidate_nodes(groups(rendered)[0], [actual])
        self.assertEqual((rendered, actual), original)
