# Milestone 4 Feature 7: openSUSE MicroOS Self-Healing & macOS Darwin Alignment — Investigation & Handoff Report

## Executive Summary
This report presents the findings of the adversarial investigation into **Milestone 4 (M4) Feature 7: openSUSE MicroOS Self-Healing & macOS Darwin Alignment** across:
1. `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`
2. `luma/paas/migration/migrator.py` (`evaluate_node_self_heal` & `NodeSelfHealReport`)
3. Darwin proxy and networking alignments (NO_PROXY, `<local>`, Tailscale mTLS routing)

The investigation identified specific architectural disconnects and missing guards in both the Kubernetes DaemonSet manifest and the Python self-healing engine. Concrete, drop-in remediations and verification procedures are detailed below.

---

## 1. Observation

### 1.1 `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`
Inspection of `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` (lines 1–95) reveals:

```yaml
17:     spec:
18:       hostPID: true
19:       hostNetwork: true
20:       tolerations:
21:         - operator: Exists
...
66:               run_fstrim_and_status() {
67:                 nsenter -t 1 -m -u -i -n fstrim -v / >/host/var/run/luma/last-fstrim.log 2>&1 || true
68:                 date -u +"%Y-%m-%dT%H:%M:%SZ" >/host/var/run/luma/self-heal-heartbeat
69:               }
...
83:           volumeMounts:
84:             - name: host-etc
85:               mountPath: /host/etc
86:             - name: host-run
87:               mountPath: /host/var/run
88:       volumes:
89:         - name: host-etc
90:           hostPath:
91:             path: /etc
92:         - name: host-run
93:           hostPath:
94:             path: /var/run
```

**Direct Observations**:
1. **Missing Node OS Selector**: There is no `nodeSelector: kubernetes.io/os: linux`. In a hybrid cluster with Darwin (macOS Apple Silicon) nodes, Kubernetes will attempt to schedule this Alpine Linux container onto Darwin nodes, failing with execution errors.
2. **Missing Host `/var` Volume Mount**: Only `/etc` and `/var/run` are mounted. Host `/var` is completely unmounted. Consequently, the container cannot directly inspect or create the writable subvolumes `/var/lib/rancher/k3s` and `/var/lib/buildkit`.
3. **No Transactional Read-Only Rootfs Handling**: MicroOS mounts `/` read-only. The script does not detect MicroOS (`/host/etc/os-release`), nor does it verify that the designated subvolumes (`/var/lib/rancher/k3s`, `/etc/rancher/k3s`, `/var/lib/buildkit`) are writable.
4. **Missing Btrfs Scrub**: The script does not run `btrfs scrub start -B /var`. MicroOS relies on Btrfs for integrity, and thin-pool scrub is completely omitted.
5. **Fstrim Target Disconnect**: Line 67 executes `fstrim -v /`. On MicroOS, `/` is a read-only snapshot. The active writable subvolume on NVMe/SSD thin-pools is `/var`. Thus, `fstrim -v /var` is required.
6. **Missing SELinux Context Restoration**: MicroOS runs SELinux in enforcing mode. When files like `/host/etc/rancher/k3s/registries.yaml` are created or directories are created, the container does not invoke `restorecon -Rv` on `/etc/rancher/k3s`, `/var/lib/rancher/k3s`, or `/var/lib/buildkit`, which can cause K3s daemons to be denied access by SELinux.

### 1.2 `luma/paas/migration/migrator.py` (`evaluate_node_self_heal`)
Inspection of `luma/paas/migration/migrator.py` (lines 78–186) reveals:

```python
124: def evaluate_node_self_heal(
125:     node: Mapping[str, Any],
126:     *,
127:     auto_fix: bool = False,
128:     required_no_proxy_hosts: Iterable[str] = ("gcode.gaojiua.com", "100.66.177.70:5000", "<local>"),
129: ) -> NodeSelfHealReport:
...
132:     os_name = str(node.get("osName") or node.get("agentOs") or "linux").lower()
...
137:     no_proxy_ok = all(host in no_proxy_raw for host in required_no_proxy_hosts if host != "<local>" or os_name == "darwin")
...
149:             repaired.append("converged /etc/rancher/k3s/registries.yaml for Zot mirror")
150:         if not no_proxy_ok:
151:             no_proxy_ok = True
152:             repaired.append("aligned NO_PROXY with internal Gitea, Zot Registry, and <local>")
153:         if not zot_ok:
154:             zot_ok = True
155:             repaired.append("Configured direct Tailscale route to Zot Registry")
...
159:         if disk_pct >= 75.0 or (os_name == "linux" and last_fstrim == 0):
160:             last_fstrim = int(time.time())
161:             disk_pct = min(disk_pct, 68.0)
162:             repaired.append("executed containerd image GC and fstrim -v / for PVE thin pool")
```

**Direct Observations**:
1. **MicroOS Btrfs Maintenance Failure**: Line 159 checks `os_name == "linux" and last_fstrim == 0`. When `osName: "opensuse-microos"` is passed with `diskUsedPercent: 84.0`, line 162 executes `repaired.append("executed containerd image GC and fstrim -v / for PVE thin pool")`. This action string does NOT mention `btrfs`. `TEST_INFRA.md` line 720 tests:
   ```python
   actions = " ".join(report.repaired_actions)
   self.assertIn("btrfs", actions.lower())
   self.assertIn("registries.yaml", actions)
   ```
   This assertion will FAIL on the current implementation.
2. **Missing SELinux `restorecon` in Registries Repair**: Line 149 only appends `"converged /etc/rancher/k3s/registries.yaml for Zot mirror"`. TC-F7-03 states: *"Verify repair actions execute restorecon on /etc/rancher/k3s/registries.yaml."* Currently, `restorecon` is not mentioned.
3. **Darwin NO_PROXY Repair String Disconnect**: TC-F7-04 states: *"Verify repair actions append <local> and localhost to NO_PROXY."* Current line 152 only appends `"aligned NO_PROXY with internal Gitea, Zot Registry, and <local>"`, omitting `localhost`.
4. **Missing NO_PROXY Multi-Separator Normalizer**: TC-B-F7-05 states: *"NO_PROXY Multiple Separators: Provide NO_PROXY with mixed commas, spaces, and duplicate localhost entries. Verify normalizer cleans string into comma-separated canonical form."* Currently, `no_proxy_raw` is not normalized into a canonical form.
5. **`NodeSelfHealReport` Access Flexibility**: `NodeSelfHealReport` is a frozen dataclass without dict-subscription support (`__getitem__`, `get`), and lacks `no_proxy_canonical` and `read_only_rootfs` fields.

---

## 2. Logic Chain

### 2.1 openSUSE MicroOS Self-Healing Chain
1. *Observation 1.1.3 & 1.1.5*: openSUSE MicroOS mounts `/` read-only via transactional Btrfs subvolumes, while `/var` and `/etc` subvolumes are writable.
2. *Deduction*: Writing to `/host` or trimming `/` fails or misses the primary write volume. Node self-healing must trim `/var` (`fstrim -v /var`), execute Btrfs integrity scrubs (`btrfs scrub start -B /var`), and verify writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`).
3. *Observation 1.1.6 & 1.2.2*: MicroOS enforces SELinux. Containers creating `/etc/rancher/k3s/registries.yaml` or `/var/lib` paths leave labels in container contexts (`container_file_t`) unless repaired.
4. *Deduction*: Both the daemonset and `evaluate_node_self_heal` must execute and report `restorecon` (specifically `restorecon on /etc/rancher/k3s/registries.yaml` and `restorecon -Rv /etc/rancher/k3s /var/lib/rancher/k3s`).

### 2.2 macOS Darwin Alignment Chain
1. *Observation 1.2.3 & 1.2.4*: macOS URL loading APIs and tools require `<local>` and `localhost` in `NO_PROXY` so traffic to `.local` mDNS domains, internal loopback, and local services bypass proxies.
2. *Observation 1.2.5*: Darwin nodes connect as remote ARM64 BuildKit builders over Tailscale CGNAT subnet (`100.64.0.0/10`) to Zot (`100.66.177.70:5000`).
3. *Deduction*: When evaluating Darwin nodes, missing `<local>`, `localhost`, or Tailscale routes must trigger explicit repairs:
   - Appending `<local>` and `localhost` to `NO_PROXY`.
   - Configuring Tailscale mTLS routing to the Zot registry.
   - Normalizing multi-separator proxy strings into canonical comma-separated form.

---

## 3. Concrete Implementation Proposals & Code Diffs

### 3.1 Remediating `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`

#### Target File
`/Users/turning4th/infra-stacks/deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`

#### Proposed Changes
1. Add `nodeSelector: kubernetes.io/os: linux` to prevent scheduling onto Darwin nodes.
2. Add volume and volumeMount for `host-var` (`path: /var` -> `mountPath: /host/var`).
3. Enhance shell script to:
   - Detect openSUSE MicroOS from `/host/etc/os-release`.
   - Reconcile writable directories (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`, `/var/run/luma`).
   - Run `restorecon -Rv` after updating `registries.yaml`.
   - Run `btrfs scrub start -B /var` on Btrfs/MicroOS filesystems.
   - Run `fstrim -v /var` (with fallback to `/`).

#### Proposed Diff
```diff
--- a/deploy/paas-stack/k8s/node-self-heal-daemonset.yaml
+++ b/deploy/paas-stack/k8s/node-self-heal-daemonset.yaml
@@ -17,6 +17,8 @@ spec:
     spec:
+      nodeSelector:
+        kubernetes.io/os: linux
       hostPID: true
       hostNetwork: true
       tolerations:
@@ -40,7 +42,21 @@ spec:
             - |
               set -eu
-              mkdir -p /host/etc/rancher/k3s /host/var/run/luma
+
+              IS_MICROOS=false
+              if [ -f /host/etc/os-release ] && grep -qi "microos" /host/etc/os-release; then
+                IS_MICROOS=true
+              fi
+
+              reconcile_writable_dirs() {
+                for dir in /host/etc/rancher/k3s /host/var/lib/rancher/k3s /host/var/lib/buildkit /host/var/run/luma; do
+                  mkdir -p "$dir" 2>/dev/null || nsenter -t 1 -m -u -i -n mkdir -p "${dir#/host}"
+                done
+              }
+
+              reconcile_selinux() {
+                if nsenter -t 1 -m -u -i -n command -v restorecon >/dev/null 2>&1; then
+                  nsenter -t 1 -m -u -i -n restorecon -Rv /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/buildkit >/host/var/run/luma/last-restorecon.log 2>&1 || true
+                fi
+              }
 
               reconcile_registries() {
                 desired="/host/etc/rancher/k3s/registries.yaml.luma-tmp"
@@ -59,18 +75,27 @@ spec:
                 if [ ! -f /host/etc/rancher/k3s/registries.yaml ] || ! cmp -s "$desired" /host/etc/rancher/k3s/registries.yaml; then
                   mv "$desired" /host/etc/rancher/k3s/registries.yaml
+                  reconcile_selinux
                 else
                   rm -f "$desired"
                 fi
               }
 
-              run_fstrim_and_status() {
-                nsenter -t 1 -m -u -i -n fstrim -v / >/host/var/run/luma/last-fstrim.log 2>&1 || true
+              run_maintenance_and_status() {
+                if nsenter -t 1 -m -u -i -n command -v btrfs >/dev/null 2>&1; then
+                  if [ "$IS_MICROOS" = "true" ] || nsenter -t 1 -m -u -i -n stat -f -c %T /var 2>/dev/null | grep -qi "btrfs"; then
+                    nsenter -t 1 -m -u -i -n btrfs scrub start -B /var >/host/var/run/luma/last-btrfs-scrub.log 2>&1 || true
+                  fi
+                fi
+                nsenter -t 1 -m -u -i -n fstrim -v /var >/host/var/run/luma/last-fstrim.log 2>&1 || \
+                nsenter -t 1 -m -u -i -n fstrim -v / >>/host/var/run/luma/last-fstrim.log 2>&1 || true
                 date -u +"%Y-%m-%dT%H:%M:%SZ" >/host/var/run/luma/self-heal-heartbeat
               }
 
               while true; do
+                reconcile_writable_dirs
                 reconcile_registries
-                run_fstrim_and_status
+                run_maintenance_and_status
                 sleep "${LUMA_FSTRIM_INTERVAL_SECONDS}"
               done
@@ -85,6 +110,9 @@ spec:
             - name: host-etc
               mountPath: /host/etc
+            - name: host-var
+              mountPath: /host/var
             - name: host-run
               mountPath: /host/var/run
       volumes:
@@ -91,6 +119,9 @@ spec:
         - name: host-etc
           hostPath:
             path: /etc
+        - name: host-var
+          hostPath:
+            path: /var
         - name: host-run
           hostPath:
             path: /var/run
```

---

### 3.2 Remediating `luma/paas/migration/migrator.py`

#### Target File
`/Users/turning4th/infra-stacks/luma/paas/migration/migrator.py`

#### Proposed Changes
1. Add helper `canonicalize_no_proxy(raw: str) -> str` to handle mixed separators (commas, semicolons, spaces) and deduplication.
2. Update `NodeSelfHealReport`:
   - Add fields `no_proxy_canonical: str = ""` and `read_only_rootfs: bool = False`.
   - Add `__getitem__` and `get` methods for dictionary compatibility.
   - Include new fields in `to_dict()`.
3. Update `evaluate_node_self_heal`:
   - Detect `opensuse-microos` (`"microos"` in OS string), `darwin`, and generic OS fallbacks.
   - On `opensuse-microos`:
     - Set `read_only_rootfs = True`.
     - Validate writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`).
     - In `registries.yaml` convergence: append `"converged /etc/rancher/k3s/registries.yaml for Zot mirror and executed restorecon on /etc/rancher/k3s/registries.yaml"`.
     - In disk maintenance (when `disk_pct >= 75.0` or `last_fstrim == 0`): append `"executed btrfs scrub start -B /var and fstrim -v /var maintenance for MicroOS transactional rootfs"`.
     - Support corrupted `registriesYamlContent` detection and repair with restorecon (TC-B-F7-03).
   - On `darwin`:
     - Ensure required hosts include `<local>`, `localhost`, `127.0.0.1`.
     - In NO_PROXY repair: append `"aligned NO_PROXY with internal Gitea, Zot Registry, and appended <local> and localhost for Darwin"`.
     - In Tailscale reachability repair: append `"configured direct Tailscale mTLS route to Zot Registry (100.66.177.70:5000)"`.
     - Avoid executing `fstrim` on Darwin (APFS); prune image/cache instead.
   - On standard `linux` / fallback:
     - Preserve existing behavior and PVE thin pool fstrim message.

#### Proposed Code Snippet
```python
def canonicalize_no_proxy(raw: str) -> str:
    """Parse comma/space/semicolon delimited NO_PROXY string into clean, deduplicated comma-separated string."""
    tokens: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\s]+", raw or ""):
        item = chunk.strip().strip("'\"")
        if item and item not in seen:
            seen.add(item)
            tokens.append(item)
    return ",".join(tokens)


@dataclass(frozen=True)
class NodeSelfHealReport:
    node_name: str
    os_name: str
    arch: str
    role: str
    registries_yaml_converged: bool
    no_proxy_aligned: bool
    zot_direct_reachable: bool
    buildkit_cache_bytes: int
    buildkit_cache_limit_bytes: int
    last_fstrim_at: int
    disk_used_percent: float
    healthy: bool
    repaired_actions: tuple[str, ...] = ()
    no_proxy_canonical: str = ""
    read_only_rootfs: bool = False

    def __getitem__(self, key: str) -> Any:
        d = self.to_dict()
        if key in d:
            return d[key]
        snake_map = {
            "node_name": self.node_name,
            "os_name": self.os_name,
            "arch": self.arch,
            "role": self.role,
            "registries_yaml_converged": self.registries_yaml_converged,
            "no_proxy_aligned": self.no_proxy_aligned,
            "no_proxy_canonical": self.no_proxy_canonical,
            "zot_direct_reachable": self.zot_direct_reachable,
            "buildkit_cache_bytes": self.buildkit_cache_bytes,
            "buildkit_cache_limit_bytes": self.buildkit_cache_limit_bytes,
            "last_fstrim_at": self.last_fstrim_at,
            "disk_used_percent": self.disk_used_percent,
            "read_only_rootfs": self.read_only_rootfs,
            "healthy": self.healthy,
            "repaired_actions": self.repaired_actions,
        }
        if key in snake_map:
            return snake_map[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodeName": self.node_name,
            "osName": self.os_name,
            "arch": self.arch,
            "role": self.role,
            "registriesYamlConverged": self.registries_yaml_converged,
            "noProxyAligned": self.no_proxy_aligned,
            "noProxyCanonical": self.no_proxy_canonical,
            "zotDirectReachable": self.zot_direct_reachable,
            "buildkitCacheBytes": self.buildkit_cache_bytes,
            "buildkitCacheLimitBytes": self.buildkit_cache_limit_bytes,
            "lastFstrimAt": self.last_fstrim_at,
            "diskUsedPercent": self.disk_used_percent,
            "readOnlyRootfs": self.read_only_rootfs,
            "healthy": self.healthy,
            "repairedActions": list(self.repaired_actions),
        }


def evaluate_node_self_heal(
    node: Mapping[str, Any],
    *,
    auto_fix: bool = False,
    required_no_proxy_hosts: Iterable[str] = ("gcode.gaojiua.com", "100.66.177.70:5000", "<local>"),
) -> NodeSelfHealReport:
    """Evaluate and optionally auto-reconcile a cluster node's proxy, registry, cache, and thin-pool state.

    Explicitly handles openSUSE MicroOS (transactional read-only rootfs, btrfs scrub & trim,
    writable /var/lib paths, SELinux restorecon) as well as macOS Darwin (<local> NO_PROXY
    and Tailscale routing) and standard Linux nodes.
    """
    name = str(node.get("name") or "unknown")
    os_name_raw = str(node.get("osName") or node.get("agentOs") or node.get("os") or "linux").strip().lower()
    is_microos = "microos" in os_name_raw or "opensuse-microos" in os_name_raw or "suse-microos" in os_name_raw
    is_darwin = os_name_raw in {"darwin", "macos", "mac os", "mac os x"} or "darwin" in os_name_raw

    if is_microos:
        os_name = "opensuse-microos"
    elif is_darwin:
        os_name = "darwin"
    elif os_name_raw in {"linux", "ubuntu", "debian", "centos", "rocky", "alpine"}:
        os_name = "linux"
    else:
        # TC-B-F7-01: Unknown OS fallback (e.g. "solaris")
        os_name = os_name_raw

    arch = str(node.get("arch") or "amd64")
    role = "remote-arm64-builder" if os_name == "darwin" else str(node.get("role") or "k3s-worker")
    read_only_rootfs = is_microos or bool(node.get("readOnlyRootfs", False))

    no_proxy_raw = str(node.get("noProxy") or "")
    no_proxy_canonical = canonicalize_no_proxy(no_proxy_raw)
    existing_tokens = set(no_proxy_canonical.split(",")) if no_proxy_canonical else set()

    req_hosts = list(required_no_proxy_hosts)
    if os_name == "darwin":
        for darwin_req in ("<local>", "localhost", "127.0.0.1"):
            if darwin_req not in req_hosts:
                req_hosts.append(darwin_req)
    else:
        req_hosts = [h for h in req_hosts if h != "<local>"]

    missing_no_proxy = [h for h in req_hosts if not (h in no_proxy_raw or h in existing_tokens)]
    no_proxy_ok = len(missing_no_proxy) == 0

    registries_ok = bool(node.get("registriesYamlConverged", True))
    corrupted_registries = False
    if "registriesYamlContent" in node:
        raw_yaml = str(node.get("registriesYamlContent") or "")
        if raw_yaml:
            try:
                parsed_yaml = yaml.safe_load(raw_yaml)
                if not isinstance(parsed_yaml, dict) or ("mirrors" not in parsed_yaml and "configs" not in parsed_yaml):
                    corrupted_registries = True
                    registries_ok = False
            except Exception:
                corrupted_registries = True
                registries_ok = False

    zot_ok = bool(node.get("zotDirectReachable", True))
    cache_bytes = int(node.get("buildkitCacheBytes") or 0)
    cache_limit = int(node.get("buildkitCacheLimitBytes") or 30 * 1024 ** 3)
    disk_pct = float(node.get("diskUsedPercent") or 0.0)
    last_fstrim = int(node.get("lastFstrimAt") or 0)

    repaired: list[str] = []
    if auto_fix:
        if not registries_ok or corrupted_registries:
            registries_ok = True
            if corrupted_registries:
                if is_microos:
                    repaired.append("detected syntax failure in registries.yaml; restored canonical config and executed restorecon on /etc/rancher/k3s/registries.yaml")
                else:
                    repaired.append("detected syntax failure in registries.yaml; restored canonical config")
            elif is_microos:
                repaired.append("converged /etc/rancher/k3s/registries.yaml for Zot mirror and executed restorecon on /etc/rancher/k3s/registries.yaml")
            else:
                repaired.append("converged /etc/rancher/k3s/registries.yaml for Zot mirror")

        if is_microos:
            repaired.append("validated transactional read-only rootfs writable subvolumes (/etc/rancher/k3s, /var/lib/rancher/k3s, /var/lib/buildkit)")

        if not no_proxy_ok:
            no_proxy_ok = True
            canonical_tokens = list(existing_tokens)
            for m in req_hosts:
                if m not in canonical_tokens:
                    canonical_tokens.append(m)
            no_proxy_canonical = ",".join(canonical_tokens)
            if os_name == "darwin":
                repaired.append("aligned NO_PROXY with internal Gitea, Zot Registry, and appended <local> and localhost for Darwin")
            else:
                repaired.append("aligned NO_PROXY with internal Gitea and Zot Registry")

        if not zot_ok:
            zot_ok = True
            if os_name == "darwin":
                repaired.append("configured direct Tailscale mTLS route to Zot Registry (100.66.177.70:5000)")
            else:
                repaired.append("Configured direct Tailscale route to Zot Registry")

        if cache_bytes > cache_limit:
            cache_bytes = int(cache_limit * 0.6)
            repaired.append("pruned BuildKit cache below 30 GiB watermark")

        if disk_pct >= 75.0 or (os_name in {"linux", "opensuse-microos"} and last_fstrim == 0):
            last_fstrim = int(time.time())
            disk_pct = min(disk_pct, 68.0)
            if is_microos:
                repaired.append("executed btrfs scrub start -B /var and fstrim -v /var maintenance for MicroOS transactional rootfs")
            elif os_name == "darwin":
                repaired.append("pruned Docker/OrbStack builder image layers and BuildKit cache")
            else:
                repaired.append("executed containerd image GC and fstrim -v / for PVE thin pool")

    healthy = (
        registries_ok
        and no_proxy_ok
        and zot_ok
        and cache_bytes <= cache_limit
        and disk_pct < 85.0
    )
    return NodeSelfHealReport(
        node_name=name,
        os_name=os_name,
        arch=arch,
        role=role,
        registries_yaml_converged=registries_ok,
        no_proxy_aligned=no_proxy_ok,
        zot_direct_reachable=zot_ok,
        buildkit_cache_bytes=cache_bytes,
        buildkit_cache_limit_bytes=cache_limit,
        last_fstrim_at=last_fstrim,
        disk_used_percent=disk_pct,
        healthy=healthy,
        repaired_actions=tuple(repaired),
        no_proxy_canonical=no_proxy_canonical,
        read_only_rootfs=read_only_rootfs,
    )
```

---

## 4. Caveats
1. **Host-Level Tool Availability**: In `node-self-heal-daemonset.yaml`, `nsenter -t 1` relies on the host's `/sbin` and `/usr/sbin` containing `btrfs`, `fstrim`, and `restorecon`. On openSUSE MicroOS, these are installed as standard core packages. If an experimental or minimal custom rootfs lacks `restorecon`, the script guards with `command -v restorecon` checks.
2. **Read-Only Investigation Mode**: Per instructions, Explorer M4-1 has not mutated `/Users/turning4th/infra-stacks/deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` or `/Users/turning4th/infra-stacks/luma/paas/migration/migrator.py`. Implementation is ready for the Milestone 4 Implementer.

---

## 5. Conclusion
1. `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` requires:
   - `nodeSelector: kubernetes.io/os: linux`
   - Volume mount `host-var` (`/var` -> `/host/var`)
   - MicroOS OS detection (`/host/etc/os-release`)
   - `reconcile_writable_dirs` for `/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`
   - `btrfs scrub start -B /var` and `fstrim -v /var`
   - `restorecon -Rv` for SELinux context repair
2. `luma/paas/migration/migrator.py` requires:
   - `canonicalize_no_proxy` helper
   - `NodeSelfHealReport` dataclass enhancement with dictionary mapping support, `no_proxy_canonical`, and `read_only_rootfs`
   - `evaluate_node_self_heal` alignment with MicroOS Btrfs scrub, `restorecon` repair, Darwin `<local>` and `localhost` appending, and Tailscale mTLS routing.

All test assertions in `TEST_INFRA.md` Suite 4 (TC-F7-01 to TC-F7-05 and TC-B-F7-01 to TC-B-F7-05) are completely supported by this design.

---

## 6. Verification Method

### 6.1 Unit Test Executions
Run the test harness using the project Python virtual environment:

```bash
# 1. Execute PaaS engine unit tests
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine -v

# 2. Targeted verification of Migration & MicroOS self-heal tests once integrated
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v
```

### 6.2 Key Assertions to Verify
```python
# Verify openSUSE MicroOS self-healing contains "btrfs" and "registries.yaml"
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
assert report.healthy is True
assert report.read_only_rootfs is True
actions = " ".join(report.repaired_actions)
assert "btrfs" in actions.lower()
assert "registries.yaml" in actions
assert "restorecon on /etc/rancher/k3s/registries.yaml" in actions

# Verify Darwin self-healing appends <local> and localhost
report_darwin = evaluate_node_self_heal(
    {
        "name": "macbook-pro",
        "osName": "darwin",
        "registriesYamlConverged": True,
        "noProxy": "127.0.0.1",
        "zotDirectReachable": False,
        "diskUsedPercent": 50.0,
    },
    auto_fix=True,
)
assert report_darwin.healthy is True
assert any("<local>" in act for act in report_darwin.repaired_actions)
assert any("localhost" in act for act in report_darwin.repaired_actions)
assert any("Tailscale" in act for act in report_darwin.repaired_actions)
```

### 6.3 Invalidation Conditions
- If `report.repaired_actions` on `opensuse-microos` fails to include `"btrfs"`, TC-F7-01 and TC-F7-02 fail.
- If `node-self-heal-daemonset.yaml` is scheduled onto a Darwin node due to a missing `nodeSelector`, container startup will fail with an OS exec format error.
- If `/host/var` is not mounted, the reconciler container cannot touch host `/var/lib/rancher/k3s` or `/var/lib/buildkit`.
- If `restorecon` is omitted, K3s may fail to read `/etc/rancher/k3s/registries.yaml` under enforcing SELinux.
