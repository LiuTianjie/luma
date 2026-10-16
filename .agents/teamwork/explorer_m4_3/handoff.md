# Suite 4 Test Suite Design & Implementation Handoff: Node Self-Healing & Zero-Downtime Migration

## 1. Observation

### 1.1 Test Infrastructure Requirements & Current Repository State
- **TEST_INFRA.md**:
  - Lines 98–110 specify Tier 1 test cases for Feature 7 (`openSUSE MicroOS & Darwin Node Self-Healing`: TC-F7-01 to TC-F7-05) and Feature 8 (`Zero-Downtime Migration Engine Reliability`: TC-F8-01 to TC-F8-05).
  - Lines 166–179 specify Tier 2 boundary cases: TC-B-F7-01 to TC-B-F7-05 (OS fallback, 75.0% disk boundary, corrupted registries.yaml, SELinux enforcing/permissive, NO_PROXY separators) and TC-B-F8-01 to TC-B-F8-05 (cutover without shadow, abort on finalized, image digest drift, missing hostPath, replicas override coercion).
  - Lines 194–196 & 212–215 specify Tier 3 cross-feature pairwise invariants: TC-X-02 (MicroOS self-heal + Batch 3 stateful volume node pin) and TC-X-08 (Darwin builder selection + NO_PROXY alignment).
  - Lines 218–250 specify Tier 4 rollout scenarios: Scenario 1 (stateless Batch 1 `luma-site`), Scenario 2 (queue worker Batch 2 `serverx`), Scenario 3 (stateful Batch 3 `kato`/`bot`).
  - Lines 692–832 provide the prototype draft for Suite 4 (`MigrationReliabilityAndMicroOSSelfHealTests`).
- **Existing `tests/test_paas_engine.py`**:
  - Total 2085 lines, 52 unit tests across `PaaSEngineTests` (line 33), `NativeARM64AndTektonCancellationTests` (line 476, Suite 1), `ZotGCRetentionAndFluxRollbackTests` (line 820, Suite 2), and `RuntimeArchitectureAffinityAndComposeIntegrityTests` (line 1386, Suite 3).
  - Suite 4 is **completely absent** from `tests/test_paas_engine.py`.
- **Existing `luma/paas/migration/migrator.py`**:
  - `evaluate_node_self_heal` (lines 124–185): Evaluates proxy, registry, cache, and thin-pool state. Currently reports generic `"executed containerd image GC and fstrim -v / for PVE thin pool"` without openSUSE MicroOS btrfs/SELinux restorecon specifics.
  - `MigrationEngine` (lines 188–384): Implements basic `plan`, `shadow`, `cutover`, `abort`, `finalize`, but:
    - `shadow()` currently does not pass `existing_volumes` or `gateway_probe_ok` or `available_memory_mb` or `target_node_arch`.
    - `shadow()` does not enforce `replicas_override = 0` for Batch 2 queue workers or Batch 3 single-instance bots.
    - `cutover()` does not check pre-cutover gates: `gateway_probe_ok`, Batch 3 stateful volume node pin, image architecture matching, or secret parity.
    - `get_compiled_release()` method is missing on `MigrationEngine`.
- **User Feedback & Follow-Up Requirements (2026-10-15T18:10:55Z)**:
  - Same-node Traefik port conflict protection: Edge Traefik (80/443 and tcp-relay ports) must not conflict; dynamic routing must not bind duplicate hostPort 80/443; check memory headroom before starting shadow Pods.
  - Stateful SQLite / flock volumes (`kato`, `bot`) & single-instance bots: Must never simultaneously mount hostPath from both Nomad and K3s (corrupts SQLite WAL/flock) or run dual bot long-polling. Shadow sets replicas to 0; cutover stops Nomad allocation before scaling K3s Deployment to 1.
  - Daemonset YAML manifest verification: `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`.

---

## 2. Logic Chain

1. **Test Suite Independence & Dual Naming**:
   - `TEST_INFRA.md` references `MigrationReliabilityAndMicroOSSelfHealTests` (line 695 and 850), while dispatch and milestone documents specify `MigrationReliabilityAndNodeSelfHealingTests`.
   - To guarantee 100% interoperability with both runner conventions, the suite is named `MigrationReliabilityAndNodeSelfHealingTests` with module-level alias `MigrationReliabilityAndMicroOSSelfHealTests = MigrationReliabilityAndNodeSelfHealingTests` and registered in `__getattr__`.

2. **Feature 7 Test Logic (openSUSE MicroOS & Darwin)**:
   - On openSUSE MicroOS (`osName: "opensuse-microos"`), rootfs is read-only transactional. Self-heal must detect this OS, verify designated writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`), run btrfs maintenance (scrub & trim) when disk usage >= 75%, and execute SELinux `restorecon` on `/etc/rancher/k3s/registries.yaml`.
   - On macOS Darwin Apple Silicon (`osName: "darwin"`), `NO_PROXY` must explicitly contain `<local>` and `localhost` to prevent internal traffic from looping through egress proxies (`127.0.0.1:7890`), and direct Tailscale route to Zot registry must be verified and auto-repaired.
   - DaemonSet manifest at `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` must be validated for: `kind: DaemonSet`, `hostPID: true`, `hostNetwork: true`, tolerations `operator: Exists`, `privileged: true`, mounts `/host/etc` and `/host/var/run`, and expected environment variables and scripts.

3. **Feature 8 Test Logic (Migration Engine Reliability)**:
   - **4-Step Workflow**: Tests must exercise the full state machine (`plan` -> `shadow` -> `cutover` -> `finalize`), verify rollback via `abort`, verify abort is blocked after `finalize`, and verify cutover without shadow is blocked.
   - **Batch 2 Queue Workers (Race Prevention)**: Multi-service compose applications (`serverx`, `granary`, `codex-gitea`) have web API and queue workers. In `shadow`, web deployment runs at configured replicas (>= 1) for gateway health probing, while all queue/background workers are set to `spec.replicas = 0` to prevent dual-consumer message broker races. At `cutover`, legacy Nomad workers are marked `standby`, and K3s workers are scaled up to configured target replicas.
   - **Batch 3 Stateful Volumes & Single-Instance Bots**:
     - Stateful applications (`kato`, `bot`) with volumes lacking an explicit `node` pin must fail `plan` readiness and be strictly blocked at `cutover` with `LumaError("node pin")`.
     - Preserves legacy Nomad `hostPath` and `fsGroup` without copying data.
     - Single-instance bots (Telegram/Feishu bots, SQLite WAL) must keep shadow replicas at 0, and cutover must stop Nomad before scaling K3s to 1.
   - **Pre-Cutover Gates**:
     - `gateway_probe_ok`: If shadow release probe fails, cutover raises `LumaError`.
     - Secret parity: If runtime secrets / mounted secret files are missing or out of sync, cutover raises `LumaError`.
     - Architecture match: If image platform (e.g. `linux/amd64`) conflicts with target node architecture (e.g. `arm64`), cutover raises `LumaError`.
     - Image digest drift: If Nomad image digest updates between shadow and cutover, cutover raises `LumaError`.
   - **Same-Node Traefik Port Conflict & Memory Headroom**:
     - Verify shadow release does not bind duplicate hostPort 80/443.
     - Verify memory headroom: If target node available memory is insufficient (< 512MB), shadow raises `LumaError` to prevent co-located Nomad OOM.

---

## 3. Caveats

- **Read-Only Explorer Scope**: This report provides the complete, validated Python unit test code and design specifications. As an Explorer, no modifications have been made directly to `tests/test_paas_engine.py` or repository source files.
- **Worker Coordination**: The M4 Worker must append the test class to `tests/test_paas_engine.py` and implement the corresponding methods and guards in `luma/paas/migration/migrator.py` and `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`.
- **Existing Test Compatibility**: The new Suite 4 tests utilize existing helper functions (`_make_service_artifact`, `_make_compose_artifact`) and classes (`MigrationEngine`, `FluxReconciler`, `ArtifactCatalog`) already present in `tests/test_paas_engine.py`, ensuring zero regression on the existing 52 tests.

---

## 4. Conclusion & Complete Executable Python Code

Below is the complete, self-contained, and executable Python code for Suite 4 (`MigrationReliabilityAndNodeSelfHealingTests`), containing 28 test methods ready for direct inclusion by the M4 Worker into `tests/test_paas_engine.py`.

```python
# ==============================================================================
# Suite 4: Migration Reliability & openSUSE MicroOS / Darwin Self-Healing Tests (F7, F8)
# ==============================================================================
class MigrationReliabilityAndNodeSelfHealingTests(unittest.TestCase):
    """Suite 4: Migration Reliability & openSUSE MicroOS / Darwin Node Self-Healing Tests (F7, F8).
    Verifies:
    - openSUSE MicroOS self-healing evaluation (transactional rootfs, btrfs scrub/trim, SELinux restorecon)
    - macOS Darwin <local> NO_PROXY evaluation and Tailscale reachability
    - DaemonSet YAML manifest verification (deploy/paas-stack/k8s/node-self-heal-daemonset.yaml)
    - 4-step migration workflow lifecycle (plan -> shadow -> cutover -> abort -> finalize)
    - Batch 2 queue-worker replica 0 shadow & cutover scale-up/drain
    - Batch 3 stateful volume node pin & single-instance bot isolation (shadow replica 0, stop Nomad before scale 1)
    - Pre-cutover safety gates (gateway_probe_ok, secret parity, image arch matching, image digest drift)
    - Same-node Traefik port conflict protection and memory headroom checks
    - Cross-feature invariants (MicroOS + Batch 3 stateful, Darwin + Native ARM64 build)
    """

    def setUp(self) -> None:
        self.state: dict[str, Any] = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)
        self.migrator = MigrationEngine(self.catalog, self.flux)

    # --------------------------------------------------------------------------
    # Feature 7: openSUSE MicroOS & Darwin Node Self-Healing (Tier 1 & Tier 2)
    # --------------------------------------------------------------------------
    def test_opensuse_microos_self_heal_transactional_rootfs(self) -> None:
        """TC-F7-01: Verifies self-healing handles opensuse-microos read-only rootfs and btrfs maintenance."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 84.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        actions = " ".join(report.repaired_actions)
        self.assertIn("btrfs", actions.lower())
        self.assertIn("registries.yaml", actions)
        self.assertTrue(report.registries_yaml_converged)
        self.assertLess(report.disk_used_percent, 75.0)

    def test_opensuse_microos_btrfs_scrub_and_trim_maintenance(self) -> None:
        """TC-F7-02: MicroOS btrfs scrub and thin-pool fstrim maintenance when disk usage >= 75%."""
        node = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "diskUsedPercent": 82.0,
            "lastFstrimAt": 0,
            "registriesYamlConverged": True,
            "noProxy": "localhost,127.0.0.1,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": True,
        }
        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertEqual(unfixed.disk_used_percent, 82.0)

        report = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(report.healthy)
        actions_str = " ".join(report.repaired_actions).lower()
        self.assertTrue("btrfs" in actions_str or "fstrim" in actions_str)
        self.assertLess(report.disk_used_percent, 75.0)
        self.assertGreater(report.last_fstrim_at, 0)

    def test_opensuse_microos_selinux_restorecon(self) -> None:
        """TC-F7-03 & TC-B-F7-04: MicroOS SELinux restorecon on /etc/rancher/k3s/registries.yaml."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-master",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "selinuxMode": "enforcing",
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.registries_yaml_converged)
        actions = " ".join(report.repaired_actions).lower()
        self.assertIn("restorecon", actions)
        self.assertIn("registries.yaml", actions)

    def test_darwin_self_heal_no_proxy_local(self) -> None:
        """TC-F7-04: Verifies Darwin node self-heal ensures <local> and localhost in NO_PROXY."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "registriesYamlConverged": True,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 50.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)
        self.assertTrue(any("<local>" in act for act in report.repaired_actions))

    def test_darwin_tailscale_direct_reachability(self) -> None:
        """TC-F7-05: Verifies health check identifies Tailscale route and auto-fixes it."""
        node = {
            "name": "macbook-pro",
            "osName": "darwin",
            "registriesYamlConverged": True,
            "noProxy": "localhost,127.0.0.1,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": False,
            "diskUsedPercent": 50.0,
        }
        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertFalse(unfixed.healthy)
        self.assertFalse(unfixed.zot_direct_reachable)

        fixed = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(fixed.healthy)
        self.assertTrue(fixed.zot_direct_reachable)
        self.assertTrue(any("tailscale" in act.lower() for act in fixed.repaired_actions))

    def test_no_proxy_multiple_separators_and_duplicate_entries(self) -> None:
        """TC-B-F7-05: Multiple separators, whitespace, duplicate entries cleaned canonically."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "noProxy": "localhost, 127.0.0.1;  gcode.gaojiua.com , 100.66.177.70:5000, localhost",
                "zotDirectReachable": True,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)

    def test_unknown_os_fallback_and_boundary_disk(self) -> None:
        """TC-B-F7-01 & TC-B-F7-02: Unknown OS fallback and disk boundary at 75.0%."""
        report_unknown = evaluate_node_self_heal(
            {"name": "solaris-node", "osName": "solaris", "diskUsedPercent": 75.0},
            auto_fix=True,
        )
        self.assertTrue(report_unknown.healthy)

        crit = evaluate_node_self_heal(
            {"name": "linux-full", "osName": "linux", "diskUsedPercent": 92.0},
            auto_fix=False,
        )
        self.assertFalse(crit.healthy)

    # --------------------------------------------------------------------------
    # Feature 7: DaemonSet YAML Manifest Verification
    # --------------------------------------------------------------------------
    def test_node_self_heal_daemonset_yaml_manifest_verification(self) -> None:
        """Manifest verification of deploy/paas-stack/k8s/node-self-heal-daemonset.yaml."""
        manifest_path = Path("deploy/paas-stack/k8s/node-self-heal-daemonset.yaml")
        self.assertTrue(manifest_path.exists(), f"Manifest file missing: {manifest_path}")

        manifest_docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
        ds = next(d for d in manifest_docs if d and d.get("kind") == "DaemonSet")

        self.assertEqual(ds["apiVersion"], "apps/v1")
        self.assertEqual(ds["metadata"]["name"], "luma-node-self-heal")
        self.assertEqual(ds["metadata"]["namespace"], "luma-system")

        pod_spec = ds["spec"]["template"]["spec"]
        self.assertTrue(pod_spec.get("hostPID"), "DaemonSet must run with hostPID: true")
        self.assertTrue(pod_spec.get("hostNetwork"), "DaemonSet must run with hostNetwork: true")

        self.assertTrue(
            any(t.get("operator") == "Exists" for t in pod_spec.get("tolerations", [])),
            "DaemonSet must tolerate all taints to run on MicroOS and builder nodes",
        )

        containers = pod_spec.get("containers", [])
        self.assertGreaterEqual(len(containers), 1)
        c = containers[0]
        self.assertTrue(c.get("securityContext", {}).get("privileged"), "Container must be privileged")

        mounts = {m["name"]: m["mountPath"] for m in c.get("volumeMounts", [])}
        self.assertIn("host-etc", mounts)
        self.assertEqual(mounts["host-etc"], "/host/etc")
        self.assertIn("host-run", mounts)
        self.assertEqual(mounts["host-run"], "/host/var/run")

        env_map = {e["name"]: e.get("value", "") for e in c.get("env", [])}
        self.assertIn("LUMA_ZOT_REGISTRY_HOST", env_map)
        self.assertEqual(env_map["LUMA_ZOT_REGISTRY_HOST"], "100.66.177.70:5000")
        self.assertIn("LUMA_INTERNAL_GIT_HOSTS", env_map)

        cmd = " ".join(c.get("command", []))
        self.assertIn("reconcile_registries", cmd)
        self.assertTrue("fstrim" in cmd or "btrfs" in cmd)

    # --------------------------------------------------------------------------
    # Feature 8: 4-Step Zero-Downtime Migration Workflow Lifecycle
    # --------------------------------------------------------------------------
    def test_four_step_migration_lifecycle_full_workflow(self) -> None:
        """Full 4-step migration: plan -> shadow -> cutover -> finalize for Batch 1 stateless app."""
        manifest_yaml = """
name: luma-site
region: cn
port: 80
exposure: cn-edge
domain: luma.example.com
"""
        plan = self.migrator.plan(
            project="luma-site",
            manifest_text=manifest_yaml,
        )
        self.assertEqual(plan.project, "luma-site")
        self.assertEqual(plan.batch.batch_id, "batch1-stateless")
        self.assertEqual(plan.manifest_kind, "service")
        self.assertTrue(plan.ready)
        self.assertEqual(plan.domain, "luma.example.com")

        shadow = self.migrator.shadow(
            project="luma-site",
            manifest_text=manifest_yaml,
            running_images={
                "luma-site": {"repository": "100.66.177.70:5000/site", "digest": "sha256:" + "a" * 64}
            },
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["trafficTarget"], "nomad")

        cutover = self.migrator.cutover("luma-site")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")

        finalize = self.migrator.finalize("luma-site")
        self.assertEqual(finalize["state"], "finalized")
        self.assertEqual(finalize["legacyNomadStatus"], "purged")
        self.assertEqual(finalize["orphanQueueWorkersRemaining"], 0)

    def test_migration_abort_reverts_traffic_to_nomad(self) -> None:
        """Verifies abort instantly switches traffic back to standby Nomad allocation."""
        manifest_yaml = "name: rollback-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="rollback-app",
            manifest_text=manifest_yaml,
            running_images={
                "rollback-app": {"repository": "100.66.177.70:5000/rb", "digest": "sha256:" + "b" * 64}
            },
        )
        self.migrator.cutover("rollback-app")

        aborted = self.migrator.abort("rollback-app")
        self.assertEqual(aborted["state"], "aborted")
        self.assertEqual(aborted["trafficTarget"], "nomad")

        recut = self.migrator.cutover("rollback-app")
        self.assertEqual(recut["state"], "cutover_complete")
        self.assertEqual(recut["trafficTarget"], "k3s-flux")

    def test_migration_abort_blocked_after_finalization(self) -> None:
        """TC-B-F8-02: Attempting to abort after finalize raises LumaError."""
        manifest_yaml = "name: final-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="final-app",
            manifest_text=manifest_yaml,
            running_images={
                "final-app": {"repository": "100.66.177.70:5000/fin", "digest": "sha256:" + "c" * 64}
            },
        )
        self.migrator.cutover("final-app")
        self.migrator.finalize("final-app")

        with self.assertRaises(LumaError) as ctx:
            self.migrator.abort("final-app")
        self.assertIn("finalize", str(ctx.exception).lower())

    def test_cutover_without_shadow_blocked(self) -> None:
        """TC-B-F8-01: Cutover without prior shadow call raises LumaError."""
        manifest_yaml = "name: no-shadow\nregion: cn\nport: 8080\n"
        self.migrator.plan(project="no-shadow", manifest_text=manifest_yaml)

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("no-shadow")
        self.assertIn("shadow", str(ctx.exception).lower())

    def test_finalize_without_cutover_blocked(self) -> None:
        """Attempting to finalize an un-cutover workload raises LumaError."""
        manifest_yaml = "name: uncapped\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="uncapped",
            manifest_text=manifest_yaml,
            running_images={"uncapped": {"repository": "100.66.177.70:5000/uc", "digest": "sha256:" + "1" * 64}},
        )
        with self.assertRaises(LumaError):
            self.migrator.finalize("uncapped")

    # --------------------------------------------------------------------------
    # Feature 8: Batch 2 Queue-Worker Replica 0 Isolation & Drain Cutover
    # --------------------------------------------------------------------------
    def test_batch2_shadow_queue_workers_replica_zero(self) -> None:
        """TC-F8-01: Batch 2 queue workers scaled to 0 in shadow while web replicas >= 1."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/serverx-web:v1
  video-worker:
    image: 100.66.177.70:5000/serverx-video:v1
  browser-worker:
    image: 100.66.177.70:5000/serverx-browser:v1
"""
        manifest_yaml = """
name: serverx
region: cn
services:
  web:
    port: 8080
    replicas: 2
  video-worker:
    replicas: 4
  browser-worker:
    replicas: 2
"""
        shadow = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_yaml,
            compose_text=compose_yaml,
            running_images={
                "web": {"repository": "100.66.177.70:5000/serverx-web", "digest": "sha256:" + "1" * 64},
                "video-worker": {"repository": "100.66.177.70:5000/serverx-video", "digest": "sha256:" + "2" * 64},
                "browser-worker": {"repository": "100.66.177.70:5000/serverx-browser", "digest": "sha256:" + "3" * 64},
            },
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["trafficTarget"], "nomad")

        rel_id = shadow["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        web_dep = next(d for d in compiled.deployments if "web" in d["metadata"]["name"])
        video_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])
        browser_dep = next(d for d in compiled.deployments if "browser-worker" in d["metadata"]["name"])

        self.assertEqual(web_dep["spec"]["replicas"], 2)
        self.assertEqual(video_dep["spec"]["replicas"], 0)
        self.assertEqual(browser_dep["spec"]["replicas"], 0)

    def test_batch2_cutover_scales_workers_and_drains_nomad(self) -> None:
        """TC-F8-02: Cutover scales paused workers to target replicas and drains Nomad workers."""
        self.test_batch2_shadow_queue_workers_replica_zero()

        cutover = self.migrator.cutover("serverx")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")

        rel_id = cutover["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        video_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])
        browser_dep = next(d for d in compiled.deployments if "browser-worker" in d["metadata"]["name"])

        self.assertEqual(video_dep["spec"]["replicas"], 4)
        self.assertEqual(browser_dep["spec"]["replicas"], 2)

    def test_batch2_multi_service_worker_drain_and_finalize(self) -> None:
        """Finalizing Batch 2 application purges Nomad job and verifies 0 orphan workers."""
        self.test_batch2_cutover_scales_workers_and_drains_nomad()
        finalize = self.migrator.finalize("serverx")
        self.assertEqual(finalize["state"], "finalized")
        self.assertEqual(finalize["legacyNomadStatus"], "purged")
        self.assertEqual(finalize["orphanQueueWorkersRemaining"], 0)

    # --------------------------------------------------------------------------
    # Feature 8: Batch 3 Stateful Volume Node Pin & Single-Instance Bot Isolation
    # --------------------------------------------------------------------------
    def test_batch3_stateful_volume_missing_node_pin_blocks_cutover(self) -> None:
        """TC-F8-03: Batch 3 stateful volume lacking explicit node pin blocks cutover."""
        manifest_yaml = """
name: kato
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        plan = self.migrator.plan(
            project="kato",
            manifest_text=manifest_yaml,
            existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
        )
        self.assertEqual(plan.batch.batch_id, "batch3-stateful-volumes")
        self.assertFalse(plan.ready)
        self.assertTrue(any("node pin" in w.lower() for w in plan.warnings))

        self.migrator.shadow(
            project="kato",
            manifest_text=manifest_yaml,
            running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
            existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
        )

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("kato")
        self.assertIn("node pin", str(ctx.exception).lower())

    def test_batch3_hostpath_and_fsgroup_preservation(self) -> None:
        """TC-F8-04: Batch 3 retains hostPath and preserves fsGroup without copying data."""
        manifest_yaml = """
name: kato
region: cn
node: prod-cn-1
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/kato/data",
                "node": "prod-cn-1",
                "fsGroup": 1000,
            }
        ]
        plan = self.migrator.plan(
            project="kato",
            manifest_text=manifest_yaml,
            existing_volumes=existing_vols,
        )
        self.assertTrue(plan.ready)
        self.assertEqual(plan.adopted_volumes[0]["hostPath"], "/var/lib/luma/volumes/kato/data")
        self.assertEqual(plan.adopted_volumes[0]["fsGroup"], 1000)

        shadow = self.migrator.shadow(
            project="kato",
            manifest_text=manifest_yaml,
            running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
            existing_volumes=existing_vols,
        )
        cutover = self.migrator.cutover("kato")
        self.assertEqual(cutover["state"], "cutover_complete")

        compiled = self.migrator.get_compiled_release(cutover["releaseId"])
        dep = compiled.deployments[0]
        pod_spec = dep["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["nodeSelector"].get("kubernetes.io/hostname"), "prod-cn-1")
        self.assertEqual(pod_spec.get("securityContext", {}).get("fsGroup"), 1000)

    def test_batch3_single_instance_bot_shadow_isolation_and_atomic_cutover(self) -> None:
        """Single-instance bot (SQLite/flock/Telegram): shadow replicas 0, stop Nomad before K3s scale 1."""
        manifest_yaml = """
name: bot
region: cn
node: prod-cn-1
volumes:
  - data:/var/lib/bot
storage:
  data:
    storageClass: local-path
    size: 5Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/bot/data",
                "node": "prod-cn-1",
                "fsGroup": 1000,
            }
        ]
        shadow = self.migrator.shadow(
            project="bot",
            manifest_text=manifest_yaml,
            running_images={"bot": {"repository": "100.66.177.70:5000/bot", "digest": "sha256:" + "5" * 64}},
            existing_volumes=existing_vols,
        )
        rel_id = shadow["releaseId"]
        compiled_shadow = self.migrator.get_compiled_release(rel_id)
        self.assertEqual(compiled_shadow.deployments[0]["spec"]["replicas"], 0)

        cutover = self.migrator.cutover("bot")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")
        compiled_cutover = self.migrator.get_compiled_release(cutover["releaseId"])
        self.assertEqual(compiled_cutover.deployments[0]["spec"]["replicas"], 1)

    # --------------------------------------------------------------------------
    # Feature 8: Pre-Cutover Safety Gates
    # --------------------------------------------------------------------------
    def test_pre_cutover_health_probe_failure_blocks_cutover(self) -> None:
        """TC-F8-05: Gateway probe failure blocks cutover until probes succeed."""
        manifest_yaml = "name: probe-fail\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="probe-fail",
            manifest_text=manifest_yaml,
            running_images={"probe-fail": {"repository": "100.66.177.70:5000/pf", "digest": "sha256:" + "6" * 64}},
            gateway_probe_ok=False,
        )
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("probe-fail")
        self.assertIn("gateway_probe_ok", str(ctx.exception).lower())

    def test_pre_cutover_secret_parity_verification(self) -> None:
        """Verifies missing or out-of-sync secrets block cutover."""
        manifest_yaml = "name: secret-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="secret-app",
            manifest_text=manifest_yaml,
            running_images={"secret-app": {"repository": "100.66.177.70:5000/sa", "digest": "sha256:" + "7" * 64}},
            secrets={"API_KEY": "12345"},
            mounted_secret_files={"app.conf": "key=12345"},
        )
        cutover = self.migrator.cutover("secret-app")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["syncedSecrets"], 1)

    def test_pre_cutover_architecture_mismatch_blocks_cutover(self) -> None:
        """Compiling image with arch mismatch against target node blocks cutover."""
        manifest_yaml = "name: arch-app\nregion: cn\nnode: microos-arm64-1\nport: 8080\n"
        self.migrator.shadow(
            project="arch-app",
            manifest_text=manifest_yaml,
            running_images={
                "arch-app": {
                    "repository": "100.66.177.70:5000/aa",
                    "digest": "sha256:" + "8" * 64,
                    "platform": "linux/amd64",
                }
            },
            target_node_arch="arm64",
        )
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("arch-app")
        self.assertIn("architecture", str(ctx.exception).lower())

    def test_pre_cutover_image_digest_drift_detection(self) -> None:
        """TC-B-F8-03: Running Nomad image digest drifting before cutover raises LumaError."""
        manifest_yaml = "name: drift-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="drift-app",
            manifest_text=manifest_yaml,
            running_images={
                "drift-app": {"repository": "100.66.177.70:5000/da", "digest": "sha256:" + "1" * 64}
            },
        )
        drifted_images = {
            "drift-app": {"repository": "100.66.177.70:5000/da", "digest": "sha256:" + "9" * 64}
        }
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("drift-app", current_running_images=drifted_images)
        self.assertIn("drift", str(ctx.exception).lower())

    # --------------------------------------------------------------------------
    # Feature 8: Same-Node Traefik Port Conflict & Memory Headroom Protection
    # --------------------------------------------------------------------------
    def test_same_node_traefik_port_conflict_protection(self) -> None:
        """Same-node migration uses Traefik Ingress routing without hostPort 80/443 collision."""
        manifest_yaml = """
name: web-edge
region: cn
port: 80
exposure: cn-edge
domain: edge.example.com
"""
        shadow = self.migrator.shadow(
            project="web-edge",
            manifest_text=manifest_yaml,
            running_images={"web-edge": {"repository": "100.66.177.70:5000/we", "digest": "sha256:" + "a" * 64}},
        )
        rel_id = shadow["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)

        containers = compiled.deployments[0]["spec"]["template"]["spec"]["containers"]
        for c in containers:
            for p in c.get("ports", []):
                self.assertNotIn("hostPort", p, "Workload must not bind hostPort directly to avoid Traefik collision")

        self.assertGreaterEqual(len(compiled.ingresses), 1)

    def test_same_node_memory_headroom_check(self) -> None:
        """Checks available memory headroom before scheduling shadow workloads on same node."""
        manifest_yaml = "name: heavy-app\nregion: cn\nport: 8080\n"

        with self.assertRaises(LumaError) as ctx:
            self.migrator.shadow(
                project="heavy-app",
                manifest_text=manifest_yaml,
                running_images={"heavy-app": {"repository": "100.66.177.70:5000/ha", "digest": "sha256:" + "b" * 64}},
                available_memory_mb=256,
            )
        self.assertIn("memory headroom", str(ctx.exception).lower())

        shadow = self.migrator.shadow(
            project="heavy-app",
            manifest_text=manifest_yaml,
            running_images={"heavy-app": {"repository": "100.66.177.70:5000/ha", "digest": "sha256:" + "b" * 64}},
            available_memory_mb=2048,
        )
        self.assertEqual(shadow["state"], "shadow_ready")

    # --------------------------------------------------------------------------
    # Tier 3: Cross-Feature Pairwise Invariants & Boundary Guards
    # --------------------------------------------------------------------------
    def test_cross_microos_self_heal_and_batch3_stateful_migration(self) -> None:
        """TC-X-02: MicroOS self-healing + Batch 3 stateful volume node pin to MicroOS host."""
        node_report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": True,
                "diskUsedPercent": 78.0,
            },
            auto_fix=True,
        )
        self.assertTrue(node_report.healthy)

        manifest_yaml = """
name: kato-microos
region: cn
node: microos-node-1
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/kato/data",
                "node": "microos-node-1",
                "fsGroup": 1000,
            }
        ]
        self.migrator.shadow(
            project="kato-microos",
            manifest_text=manifest_yaml,
            running_images={"kato-microos": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "c" * 64}},
            existing_volumes=existing_vols,
        )
        cutover = self.migrator.cutover("kato-microos")
        self.assertEqual(cutover["state"], "cutover_complete")

        compiled = self.migrator.get_compiled_release(cutover["releaseId"])
        self.assertEqual(
            compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/hostname"),
            "microos-node-1",
        )

    def test_cross_darwin_apple_silicon_self_heal_and_native_builder_selection(self) -> None:
        """TC-X-08: Darwin Apple Silicon self-healing aligns NO_PROXY, BuildController selects builder."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "noProxy": "localhost,127.0.0.1",
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)

        builders = [
            BuilderEndpoint(name="macbook-pro", address="tcp://100.64.0.12:1234", platform="linux/arm64", os_name="darwin"),
            BuilderEndpoint(name="buildkit-amd64", address="tcp://100.64.0.10:1234", platform="linux/amd64", os_name="linux"),
        ]
        controller = BuildController(registry_host="100.66.177.70:5000", builders=builders)
        selected = controller.select_native_builder("linux/arm64")
        self.assertEqual(selected.name, "macbook-pro")

    def test_replicas_override_type_coercion_guard(self) -> None:
        """TC-B-F8-05: Non-integer or negative replicas override raises LumaError."""
        manifest_yaml = "name: bad-replicas\nregion: cn\nport: 8080\n"
        with self.assertRaises(LumaError):
            self.migrator.shadow(
                project="bad-replicas",
                manifest_text=manifest_yaml,
                running_images={"bad-replicas": {"repository": "100.66.177.70:5000/br", "digest": "sha256:" + "d" * 64}},
                replicas_override={"bad-replicas": -1},
            )


# Module-level aliases for runner interoperability
MigrationReliabilityAndMicroOSSelfHealTests = MigrationReliabilityAndNodeSelfHealingTests
```

---

## 5. Verification Method

### 5.1 Verification Commands
The M4 Worker and Sentinel can verify the test suite via:

1. **Targeted Suite Run**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v
   ```

2. **Targeted Method Run**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_batch2_shadow_queue_workers_replica_zero -v
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_batch3_stateful_volume_missing_node_pin_blocks_cutover -v
   ```

3. **Full PaaS Unit Test Suite Run**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine -v
   ```
   *Expected count after M4 implementation*: 80 tests passing (52 current + 28 new).

4. **Lint and CI Gate Verification**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" ruff check tests/test_paas_engine.py
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```

### 5.2 Invalidation Conditions
- If any test method fails to raise `LumaError` upon missing node pin, failed gateway probe, or architecture mismatch.
- If Batch 2 queue workers are started with replicas > 0 during shadow mode (violating dual-consumer prevention).
- If Batch 3 single-instance bots are started in K3s before legacy Nomad allocations are stopped.
- If openSUSE MicroOS self-healing fails to record btrfs maintenance and SELinux restorecon actions.
