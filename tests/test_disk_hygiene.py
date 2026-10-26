import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from luma import __version__, disk_hygiene
from luma.agent import _DiskHygieneScheduler, execute_agent_task, node_agent_capabilities
from luma.bootstrap import configure_journald_limit
from luma.control.server import _dashboard_disk_hygiene, handle_node_disk_cleanup
from luma.control.state import init_state, save_state
from luma.disk_hygiene import (
    DISK_HYGIENE_ACTION,
    DISK_HYGIENE_CAPABILITY,
    ImageRecord,
    image_from_inspect,
    image_ref_keys,
    is_due,
    journald_limit_script,
    nomad_allocation_image_refs,
    parse_docker_time,
    prune_images,
    resolve_settings,
    select_images,
)
from luma.errors import LumaError
from tests.support import _restore_env, _set_env

DAY = 86400
NOW = 1_800_000_000.0


def _id(n: int) -> str:
    return f"sha256:{n:064x}"


def _image(n: int, *, tags=(), digests=(), created_days=30.0, tagged_days=None, size=100) -> ImageRecord:
    return ImageRecord(
        id=_id(n),
        repo_tags=tuple(tags),
        repo_digests=tuple(digests),
        created=NOW - created_days * DAY,
        last_tag_time=0.0 if tagged_days is None else NOW - tagged_days * DAY,
        size=size,
    )


def _select(images, **kwargs):
    options = {
        "container_refs": (),
        "nomad_refs": (),
        "now": NOW,
        "min_age_seconds": 7 * DAY,
    }
    options.update(kwargs)
    return select_images(images, **options)


def _completed(args, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


class ImageReferenceTests(unittest.TestCase):
    def test_docker_hub_short_and_long_names_share_a_key(self):
        self.assertEqual(image_ref_keys("nginx"), {"docker.io/library/nginx:latest"})
        self.assertEqual(image_ref_keys("docker.io/library/nginx:latest"), {"docker.io/library/nginx:latest"})
        self.assertEqual(image_ref_keys("index.docker.io/acme/api:1"), {"docker.io/acme/api:1"})

    def test_private_registry_with_port_keeps_its_host(self):
        self.assertEqual(image_ref_keys("10.0.0.5:5000/app/web:v2"), {"10.0.0.5:5000/app/web:v2"})
        self.assertEqual(image_ref_keys("localhost/app"), {"localhost/app:latest"})

    def test_tag_and_digest_yield_both_keys(self):
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            image_ref_keys(f"ghcr.io/acme/api:v1@{digest}"),
            {"ghcr.io/acme/api:v1", f"ghcr.io/acme/api@{digest}"},
        )
        self.assertEqual(image_ref_keys(f"ghcr.io/acme/api@{digest}"), {f"ghcr.io/acme/api@{digest}"})

    def test_ids_and_unresolvable_refs(self):
        self.assertEqual(image_ref_keys(_id(1)), {_id(1)})
        self.assertEqual(image_ref_keys("${NOMAD_META_image}"), set())
        self.assertEqual(image_ref_keys(""), set())

    def test_inspect_parsing_drops_none_tags_and_zero_tag_time(self):
        image = image_from_inspect(
            {
                "Id": _id(3),
                "RepoTags": ["<none>:<none>"],
                "RepoDigests": ["<none>@sha256:" + "b" * 64],
                "Created": "2026-10-01T10:00:00.123456789Z",
                "Size": 42,
                "Metadata": {"LastTagTime": "0001-01-01T00:00:00Z"},
            }
        )
        self.assertEqual(image.repo_tags, ())
        self.assertEqual(image.repo_digests, ())
        self.assertEqual(image.last_tag_time, 0.0)
        self.assertAlmostEqual(image.created, parse_docker_time("2026-10-01T10:00:00Z") + 0.123456789, places=5)

    def test_docker_time_offsets(self):
        self.assertEqual(parse_docker_time("2026-10-01T18:00:00+08:00"), parse_docker_time("2026-10-01T10:00:00Z"))
        self.assertEqual(parse_docker_time("garbage"), 0.0)


class ImageSelectionTests(unittest.TestCase):
    def test_images_used_by_any_container_are_kept_by_id_or_name(self):
        by_id = _image(1, tags=["acme/api:1"])
        by_name = _image(2, tags=["nginx:1.25"])
        unused = _image(3, tags=["acme/old:1"])
        selection = _select(
            [by_id, by_name, unused],
            container_refs=[_id(1), "docker.io/library/nginx:1.25"],
        )
        self.assertEqual([item.id for item in selection.kept["container"]], [_id(1), _id(2)])
        self.assertEqual(selection.remove, [unused])

    def test_images_referenced_by_nomad_allocations_are_kept(self):
        digest = "sha256:" + "c" * 64
        pinned = _image(1, tags=["registry.internal:5000/app/api:7"], digests=[f"registry.internal:5000/app/api@{digest}"])
        selection = _select([pinned], nomad_refs=[f"registry.internal:5000/app/api@{digest}"])
        self.assertEqual(selection.kept["nomad"], [pinned])
        self.assertEqual(selection.remove, [])

    def test_current_control_image_is_kept_from_any_registry(self):
        current = _image(1, tags=["registry.internal:5000/luma/luma-control:v9.9.9"])
        older = _image(2, tags=["ghcr.io/liutianjie/luma-control:v9.9.8"])
        selection = _select([current, older], control_tags=["v9.9.9"])
        self.assertEqual(selection.kept["control"], [current])
        self.assertEqual(selection.remove, [older])

    def test_explicitly_protected_images_are_kept(self):
        image = _image(1, tags=["acme/backup:keep"])
        selection = _select([image], protected_refs=["docker.io/acme/backup:keep"])
        self.assertEqual(selection.kept["protected"], [image])

    def test_runtime_helper_images_are_kept_without_any_reference(self):
        # Nomad pulls the pause image for every bridge allocation; nodes that
        # cannot reach registry.k8s.io must keep their copy.
        pause = _image(1, tags=["registry.k8s.io/pause-amd64:3.3"], created_days=2000)
        pause_arm = _image(2, tags=["registry.k8s.io/pause-arm64:3.3"], created_days=2000)
        host_prep = _image(3, tags=["ubuntu:22.04"], created_days=200)
        registry = _image(4, tags=["registry:2"], created_days=200)
        lookalike = _image(5, tags=["registry.k8s.io/pauser:1"], created_days=200)
        selection = _select([pause, pause_arm, host_prep, registry, lookalike])
        self.assertEqual(selection.kept["protected"], [pause, pause_arm, host_prep, registry])
        self.assertEqual(selection.remove, [lookalike])

    def test_recent_pull_of_an_old_build_is_kept(self):
        # Created months ago, but tagged here (pulled) yesterday.
        image = _image(1, tags=["acme/api:2"], created_days=90, tagged_days=1)
        selection = _select([image])
        self.assertEqual(selection.kept["recent"], [image])

    def test_containerd_store_uses_first_seen_time(self):
        image = _image(1, tags=["acme/api:2"], created_days=90)
        self.assertEqual(_select([image]).remove, [image])
        selection = _select([image], first_seen={_id(1): NOW - 2 * DAY})
        self.assertEqual(selection.kept["recent"], [image])
        old_seen = _select([image], first_seen={_id(1): NOW - 8 * DAY})
        self.assertEqual(old_seen.remove, [image])

    def test_unknown_age_is_kept(self):
        image = ImageRecord(id=_id(1), repo_tags=("acme/x:1",))
        self.assertEqual(_select([image]).kept["unknown-age"], [image])

    def test_age_threshold_is_respected(self):
        young = _image(1, tags=["a:1"], created_days=6.9)
        old = _image(2, tags=["a:2"], created_days=7.1)
        selection = _select([young, old])
        self.assertEqual(selection.kept["recent"], [young])
        self.assertEqual(selection.remove, [old])

    def test_dangling_images_are_removed_by_id_and_newest_first(self):
        parent = _image(1, tags=["base:1"], created_days=40)
        child = _image(2, created_days=20)
        selection = _select([parent, child])
        self.assertEqual(selection.remove, [child, parent])
        self.assertEqual(child.removal_refs(), [_id(2)])
        self.assertEqual(parent.removal_refs(), ["base:1"])

    def test_container_reason_wins_over_other_reasons(self):
        image = _image(1, tags=["ghcr.io/liutianjie/luma-control:v1.0.0"], created_days=1)
        selection = _select([image], container_refs=[_id(1)], nomad_refs=[_id(1)], control_tags=["v1.0.0"])
        self.assertEqual(selection.kept_counts(), {"container": 1})


class NomadReferenceTests(unittest.TestCase):
    def test_only_live_allocations_and_their_group_count(self):
        job = {
            "TaskGroups": [
                {"Name": "web", "Tasks": [{"Driver": "docker", "Config": {"image": "acme/web:1"}}]},
                {"Name": "worker", "Tasks": [
                    {"Driver": "docker", "Config": {"image": "acme/worker:1"}},
                    {"Driver": "exec", "Config": {"command": "/bin/true"}},
                ]},
            ]
        }
        allocations = [
            {"ClientStatus": "running", "DesiredStatus": "run", "TaskGroup": "web", "Job": job},
            {"ClientStatus": "pending", "DesiredStatus": "run", "TaskGroup": "worker", "Job": job},
            {"ClientStatus": "complete", "DesiredStatus": "stop", "TaskGroup": "web", "Job": {
                "TaskGroups": [{"Name": "web", "Tasks": [{"Driver": "docker", "Config": {"image": "acme/web:0"}}]}]
            }},
        ]
        self.assertEqual(nomad_allocation_image_refs(allocations), {"acme/web:1", "acme/worker:1"})

    def test_failed_allocation_that_should_still_run_is_counted(self):
        allocations = [{"ClientStatus": "failed", "DesiredStatus": "run", "TaskGroup": "g", "Job": {
            "TaskGroups": [{"Name": "g", "Tasks": [{"Driver": "docker", "Config": {"image": "acme/retry:1"}}]}]
        }}]
        self.assertEqual(nomad_allocation_image_refs(allocations), {"acme/retry:1"})


class FakeDocker:
    """Minimal docker CLI double for prune_images."""

    def __init__(self, images, containers=(), rm_failures=()):
        self.images = {item["Id"]: item for item in images}
        self.containers = list(containers)
        self.rm_failures = set(rm_failures)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        command = args[1:]
        if command[:2] == ["image", "ls"]:
            # Docker 29 lists dangling images only with the dangling filter.
            dangling = "dangling=true" in command
            return _completed(args, "\n".join(i for i, row in self.images.items() if bool(row["RepoTags"]) != dangling))
        if command[:2] == ["image", "inspect"]:
            return _completed(args, json.dumps([self.images[i] for i in command[2:] if i in self.images]))
        if command[:1] == ["ps"]:
            return _completed(args, "\n".join(f"c{n}" for n, _ in enumerate(self.containers)))
        if command[:2] == ["container", "inspect"]:
            return _completed(args, "\n".join(f"{image_id}\t{ref}" for image_id, ref in self.containers))
        if command[:2] == ["system", "df"]:
            return _completed(args, f"Images\t{len(self.images)}GB\n")
        if command[:2] == ["image", "rm"]:
            refs = command[2:]
            target = next((i for i, row in self.images.items() if set(refs) & set(row["RepoTags"] or [i])), None)
            if target in self.rm_failures:
                return _completed(args, returncode=1, stderr="conflict: image is being used by stopped container")
            self.images.pop(target, None)
            return _completed(args, "Deleted: x\n")
        raise AssertionError(f"unexpected docker call: {args}")


def _inspect(n, tags=(), created="2026-01-01T00:00:00Z"):
    return {"Id": _id(n), "RepoTags": list(tags), "RepoDigests": [], "Created": created, "Size": 10, "Metadata": {"LastTagTime": "0001-01-01T00:00:00Z"}}


class PruneImagesTests(unittest.TestCase):
    def test_nomad_failure_fails_closed(self):
        docker = FakeDocker([_inspect(1, ["old:1"])])

        def broken():
            raise RuntimeError("connection refused")

        report = prune_images("docker", min_age_days=7, dry_run=False, nomad_refs=broken, now=NOW, runner=docker)
        self.assertEqual(report["status"], "skipped")
        self.assertIn("connection refused", report["reason"])
        self.assertFalse(any(call[1:3] == ["image", "rm"] for call in docker.calls))

    def test_dry_run_never_removes(self):
        docker = FakeDocker([_inspect(1, ["old:1"]), _inspect(2, ["used:1"])], containers=[(_id(2), "used:1")])
        report = prune_images("docker", min_age_days=7, dry_run=True, nomad_refs=set, now=NOW, runner=docker)
        self.assertEqual(report["candidateCount"], 1)
        self.assertEqual(report["candidates"][0]["refs"], ["old:1"])
        self.assertEqual(report["kept"], {"container": 1})
        self.assertFalse(any(call[1:3] == ["image", "rm"] for call in docker.calls))

    def test_removal_never_forces_and_reports_failures(self):
        docker = FakeDocker([_inspect(1, ["old:1"]), _inspect(2, ["raced:1"])], rm_failures={_id(2)})
        report = prune_images("docker", min_age_days=7, dry_run=False, nomad_refs=set, now=NOW, runner=docker)
        rm_calls = [call for call in docker.calls if call[1:3] == ["image", "rm"]]
        self.assertTrue(rm_calls)
        self.assertTrue(all("-f" not in call and "--force" not in call for call in rm_calls))
        self.assertEqual(report["removed"], [_id(1)])
        self.assertEqual(report["failed"][0]["id"], _id(2))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["reclaimedBytes"], 1_000_000_000)

    def test_untagged_images_hidden_from_plain_ls_are_considered(self):
        docker = FakeDocker([_inspect(1, ["old:1"]), _inspect(2)])
        report = prune_images("docker", min_age_days=7, dry_run=True, nomad_refs=set, now=NOW, runner=docker)
        self.assertEqual({item["id"] for item in report["candidates"]}, {_id(1), _id(2)})

    def test_dry_run_build_cache_uses_system_df(self):
        def runner(args, **kwargs):
            self.assertEqual(args[1:3], ["system", "df"])
            return _completed(args, "Images\t1GB (10%)\nBuild Cache\t2.5GB\n")

        report = disk_hygiene.prune_build_cache("docker", min_age_days=7, dry_run=True, runner=runner)
        self.assertEqual(report["builders"], [{"name": "default", "reclaimableBytes": 2_500_000_000}])

    def test_build_cache_prune_filters_by_age(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return _completed(args, "Total:\t1.5GB\n")

        report = disk_hygiene.prune_build_cache("docker", min_age_days=7, dry_run=False, runner=runner)
        self.assertEqual(calls, [["docker", "builder", "prune", "-f", "--filter", "until=168h"]])
        self.assertEqual(report["reclaimedBytes"], 1_500_000_000)

    def test_container_scan_failure_raises(self):
        def runner(args, **kwargs):
            if args[1] == "ps":
                return _completed(args, returncode=1, stderr="permission denied")
            return FakeDocker([_inspect(1, ["old:1"])])(args, **kwargs)

        with self.assertRaises(RuntimeError):
            prune_images("docker", min_age_days=7, dry_run=False, nomad_refs=set, now=NOW, runner=runner)


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = resolve_settings({}, {})
        self.assertTrue(settings.enabled)
        self.assertEqual((settings.interval_hours, settings.min_age_days, settings.journald_max_use), (24, 7, "500M"))

    def test_agent_config_and_environment_overrides(self):
        config = {"diskHygiene": {"enabled": True, "minAgeDays": 14, "intervalHours": 12, "journaldMaxUse": "1g"}}
        settings = resolve_settings(config, {"LUMA_DISK_HYGIENE": "off", "LUMA_DISK_HYGIENE_MIN_AGE_DAYS": "0"})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.min_age_days, 1)
        self.assertEqual(settings.interval_hours, 12)
        self.assertEqual(settings.journald_max_use, "1G")

    def test_invalid_journald_size_falls_back_and_off_disables(self):
        self.assertEqual(resolve_settings({}, {"LUMA_JOURNALD_MAX_USE": "lots"}).journald_max_use, "500M")
        self.assertEqual(resolve_settings({}, {"LUMA_JOURNALD_MAX_USE": "off"}).journald_max_use, "")

    def test_due_and_attempt_backoff(self):
        settings = resolve_settings({}, {})
        self.assertTrue(is_due({}, settings, now=NOW))
        self.assertFalse(is_due({"lastRunAt": NOW - 3600}, settings, now=NOW))
        self.assertFalse(is_due({"lastRunAt": NOW - 2 * DAY, "lastAttemptAt": NOW - 60}, settings, now=NOW))
        self.assertTrue(is_due({"lastRunAt": NOW - 2 * DAY}, settings, now=NOW))
        self.assertFalse(is_due({}, resolve_settings({"diskHygiene": {"enabled": False}}, {}), now=NOW))

    def test_first_seen_keeps_existing_and_forgets_removed(self):
        status = {"imagesFirstSeen": {_id(1): 100.0, _id(3): 0.0, _id(9): 50.0}}
        self.assertEqual(
            disk_hygiene.update_first_seen(status, [_id(1), _id(2), _id(3)], now=500),
            {_id(1): 100.0, _id(2): 500.0, _id(3): 0.0},
        )

    def test_first_scan_records_existing_images_as_baseline(self):
        baseline = disk_hygiene.update_first_seen({}, [_id(1)], now=500)
        self.assertEqual(baseline, {_id(1): 0.0})
        old_build = _image(1, tags=["acme/api:1"], created_days=90)
        # Present before tracking: judged by build time, so cleanup is not postponed.
        self.assertEqual(_select([old_build], first_seen=baseline).remove, [old_build])
        later = disk_hygiene.update_first_seen({"imagesFirstSeen": baseline}, [_id(1), _id(2)], now=NOW)
        new_pull = _image(2, tags=["acme/api:2"], created_days=90)
        self.assertEqual(_select([new_pull], first_seen=later).kept["recent"], [new_pull])


class JournaldTests(unittest.TestCase):
    def test_script_is_idempotent_and_check_only_does_not_write(self):
        script = journald_limit_script("500M")
        self.assertIn("SystemMaxUse=500M", script)
        self.assertIn("journald-limit: unchanged", script)
        self.assertIn("systemctl restart systemd-journald", script)
        self.assertIn("journalctl --vacuum-size=500M", script)
        check = journald_limit_script("500M", check_only=True)
        self.assertNotIn("mv -f", check)
        self.assertNotIn("systemctl", check)
        with self.assertRaises(ValueError):
            journald_limit_script("500M; rm -rf /")

    def test_script_writes_once_then_reports_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "journald.conf.d" / "luma.conf"
            fakebin = Path(tmp) / "bin"
            fakebin.mkdir()
            log = Path(tmp) / "calls.log"
            for name in ("systemctl", "journalctl"):
                tool = fakebin / name
                tool.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {log}\n")
                tool.chmod(0o755)
            script = (
                journald_limit_script("500M")
                .replace(disk_hygiene.JOURNALD_DROPIN_PATH, str(target))
                .replace("/etc/systemd/journald.conf.d", str(target.parent))
                .replace("[ ! -d /run/systemd/system ] || ", "")
            )
            env = {"PATH": f"{fakebin}:/usr/bin:/bin"}
            first = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, env=env, check=True)
            second = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, env=env, check=True)
            self.assertEqual(disk_hygiene.parse_journald_limit_state(first.stdout), "changed")
            self.assertEqual(disk_hygiene.parse_journald_limit_state(second.stdout), "unchanged")
            self.assertIn("SystemMaxUse=500M", target.read_text())
            self.assertEqual(log.read_text().count("systemctl restart systemd-journald"), 1)

    def test_bootstrap_step_skips_macos_and_never_blocks(self):
        remote = Mock()
        with patch("luma.bootstrap._is_darwin", return_value=True):
            self.assertIn("skipped on macOS", configure_journald_limit(remote))
        remote.sudo_result.assert_not_called()
        with patch("luma.bootstrap._is_darwin", return_value=False):
            remote.sudo_result.return_value = Mock(code=0, output="journald-limit: changed\n")
            self.assertIn("limited to 500M", configure_journald_limit(remote))
            remote.sudo_result.return_value = Mock(code=1, output="sudo: a password is required\n")
            self.assertIn("not applied", configure_journald_limit(remote))
            with patch.dict("os.environ", {"LUMA_JOURNALD_MAX_USE": "huge"}):
                with self.assertRaises(LumaError):
                    configure_journald_limit(remote)


class AgentIntegrationTests(unittest.TestCase):
    def test_capability_is_advertised_on_linux_and_macos(self):
        self.assertIn(DISK_HYGIENE_CAPABILITY, node_agent_capabilities("linux"))
        self.assertIn(DISK_HYGIENE_CAPABILITY, node_agent_capabilities("darwin"))

    def test_manual_task_defaults_to_dry_run_and_protects_control_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"diskHygiene": {"minAgeDays": 10}}))
            with patch("luma.disk_hygiene.run_disk_hygiene", return_value={"status": "ok", "message": "m"}) as run, patch(
                "luma.agent._docker_binary", return_value=None
            ):
                result = execute_agent_task({"action": DISK_HYGIENE_ACTION, "payload": {"controlTag": "v8.0.0"}}, config_path=config)
            self.assertEqual(result["message"], "m")
            kwargs = run.call_args.kwargs
            self.assertTrue(kwargs["dry_run"])
            self.assertEqual(kwargs["min_age_days"], 10)
            self.assertEqual(kwargs["control_tags"], [f"v{__version__}", "v8.0.0"])
            # Dry runs never count as the periodic run.
            self.assertNotIn("lastRunAt", disk_hygiene.load_status(Path(tmp) / disk_hygiene.STATUS_FILE_NAME))

    def test_scheduler_runs_when_due_and_records_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text("{}")
            scheduler = _DiskHygieneScheduler(config)
            fake = {"status": "ok", "message": "Removed 2", "finishedAt": int(NOW), "reclaimedBytes": 5, "images": {"removedCount": 2}}
            with patch("luma.disk_hygiene.run_disk_hygiene", return_value=fake) as run, patch(
                "luma.agent._docker_binary", return_value=None
            ):
                scheduler.tick(now=NOW)
                self.assertFalse(run.call_args.kwargs["dry_run"])
                status = disk_hygiene.load_status(Path(tmp) / disk_hygiene.STATUS_FILE_NAME)
                self.assertEqual(status["lastRemovedImages"], 2)
                self.assertEqual(status["lastTrigger"], "schedule")
                run.reset_mock()
                scheduler.tick(now=NOW + 3600)
                run.assert_not_called()

    def test_scheduler_respects_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"diskHygiene": {"enabled": False}}))
            with patch("luma.disk_hygiene.run_disk_hygiene") as run:
                _DiskHygieneScheduler(config).tick(now=NOW)
            run.assert_not_called()


class ControlEndpointTests(unittest.TestCase):
    def _state(self, capabilities):
        state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
        state["nodes"] = {
            "manager": {
                "name": "manager",
                "nodeId": "node-manager",
                "region": "cn",
                "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": capabilities},
            }
        }
        save_state(state)
        return state

    def test_queues_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = self._state([DISK_HYGIENE_CAPABILITY])
                with patch("luma.control.server._queue_node_agent_task", return_value="task-1") as queue, patch(
                    "luma.control.server._wait_node_agent_task", return_value={"message": "Would remove 0"}
                ):
                    result = handle_node_disk_cleanup(state["deployToken"], {"node": "manager"})
                self.assertEqual(result["node"], "manager")
                self.assertEqual(queue.call_args.args[2], DISK_HYGIENE_ACTION)
                self.assertEqual(queue.call_args.args[3], {"dryRun": True, "controlTag": f"v{__version__}"})
                self.assertEqual(queue.call_args.kwargs["required_capability"], DISK_HYGIENE_CAPABILITY)
                with patch("luma.control.server._queue_node_agent_task", return_value="task-2") as queue, patch(
                    "luma.control.server._wait_node_agent_task", return_value={}
                ):
                    handle_node_disk_cleanup(state["deployToken"], {"node": "manager", "dryRun": False, "minAgeDays": 3})
                self.assertEqual(queue.call_args.args[3]["dryRun"], False)
                self.assertEqual(queue.call_args.args[3]["minAgeDays"], 3)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_rejects_old_agents_bad_input_and_bad_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = self._state(["docker-image"])
                with self.assertRaisesRegex(LumaError, "too old"):
                    handle_node_disk_cleanup(state["deployToken"], {"node": "manager"})
                with self.assertRaisesRegex(LumaError, "not registered"):
                    handle_node_disk_cleanup(state["deployToken"], {"node": "ghost"})
                with self.assertRaisesRegex(LumaError, "between 1 and 365"):
                    handle_node_disk_cleanup(state["deployToken"], {"node": "manager", "minAgeDays": 0})
                with self.assertRaises(LumaError):
                    handle_node_disk_cleanup("wrong-token", {"node": "manager"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_dashboard_summary_keeps_only_known_fields(self):
        summary = _dashboard_disk_hygiene(
            {"diskHygiene": {"enabled": True, "lastRunAt": 5, "lastReclaimedBytes": -1, "lastMessage": "ok", "extra": {"x": 1}}}
        )
        self.assertEqual(summary, {"enabled": True, "lastRunAt": 5, "lastMessage": "ok"})
        self.assertEqual(_dashboard_disk_hygiene({}), {})


if __name__ == "__main__":
    unittest.main()
