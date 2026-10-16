# Investigation & Handoff Report: Feature 6 — Compose Splitting, PVC Volume Mounting & 6 Exposure Modes

**Author**: Explorer M3-2  
**Target File**: `luma/paas/release/helm_compiler.py`  
**Milestone**: M3 (Helm Compiler & Workload Placement)  
**Scope**: Feature 6 (Compose Deployment Splitting vs Sidecar Co-Location, Volume and PVC Synthesis, All 6 Exposure Modes)  
**Parent**: Project Orchestrator (`1387c386-51c1-4a2c-b02a-a266794155bd`)

---

## 1. Observation

Direct code examination of `luma/paas/release/helm_compiler.py` (832 lines), `luma/service.py` (394 lines), `luma/compose.py` (815 lines), and `TEST_INFRA.md` revealed 10 concrete architectural disconnects and bugs in `helm_compiler.py`:

### Observation 1.1: Complete Omission of PVC & Volume Mounting in `compile_compose_release`
In `luma/paas/release/helm_compiler.py`:
- At lines 614–638, PersistentVolumeClaim manifests are generated and added to `pvcs`:
```python
    volumes_cfg = sidecar_cfg.get("volumes") if isinstance(sidecar_cfg.get("volumes"), dict) else {}
    for vol_name, vol_spec in sorted(volumes_cfg.items()):
        if not isinstance(vol_spec, dict):
            continue
        pvc_name = f"{project}-{slugify(vol_name)}"
        pvcs.append({ ... })
```
- **However**, at line 668:
```python
        main_container: Dict[str, Any] = {
            "name": slugify(svc_name),
            "image": image_ref,
            "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
            "volumeMounts": list(secret_mounts),
        }
```
- At line 691 (sidecar container):
```python
            child_container: Dict[str, Any] = {
                "name": slugify(child_name),
                "image": child_img,
                "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
                "volumeMounts": list(secret_mounts),
            }
```
- And at line 701:
```python
        pod_spec: Dict[str, Any] = {
            "containers": containers,
            "volumes": list(secret_vols),
            "securityContext": {"fsGroup": int(luma_svc.get("fsGroup") or 1000)},
        }
```
**Direct Observation**:
The PVCs created at line 619 are **NEVER** mounted to container `volumeMounts` and **NEVER** declared in `pod_spec["volumes"]`. Containers only mount `secret_mounts` and Pods only mount `secret_vols`. The PVCs are completely orphaned, causing any Compose stack with persistent storage (e.g. `test_compose_pvc_volume_mounting_and_sidecar_command`) to run with ephemeral container storage, failing `TC-F6-01`.

### Observation 1.2: `sidecar_cfg.get("storage")` Ignored in Compose Compilation
In `luma/paas/release/helm_compiler.py` line 614:
```python
volumes_cfg = sidecar_cfg.get("volumes") if isinstance(sidecar_cfg.get("volumes"), dict) else {}
```
In standard Luma sidecars and `TEST_INFRA.md` line 644, persistent volumes are defined under `storage:`:
```yaml
storage:
  app-data:
    storageClass: local-path
    size: 20Gi
```
Because line 614 only inspects `sidecar_cfg.get("volumes")` and ignores `sidecar_cfg.get("storage")`, `volumes_cfg` evaluates to `{}`. No PVCs are created at all when `storage:` is used.

### Observation 1.3: Sidecar Container Custom Command Dropped in `compile_compose_release`
In `luma/paas/release/helm_compiler.py` lines 672–696:
- For the main container (line 672–674):
```python
        if svc_body.get("command"):
            cmd = svc_body["command"]
            main_container["args"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]
```
- For the child sidecar container (lines 678–696):
```python
        for child_name, parent_name in sorted(sidecar_pairs.items()):
            if parent_name != svc_name:
                continue
            child_body = compose_services[child_name]
            ...
            child_container: Dict[str, Any] = {
                "name": slugify(child_name),
                "image": child_img,
                "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
                "volumeMounts": list(secret_mounts),
            }
            if proxy_env:
                child_container["env"] = proxy_env
            containers.append(child_container)
```
**Direct Observation**:
`child_body.get("command")` is completely ignored! The sidecar child container in the compiled Pod spec never receives a `command` or `args`. This directly violates `TC-F6-02`, where `agent_c.get("command") == ["/bin/agent", "--foreground"]` is asserted. Furthermore, main container sets `args` instead of `command`.

### Observation 1.4: Silent Fallthrough and Missing Guards on `network_mode: service:*`
In `luma/paas/release/helm_compiler.py` lines 601–605:
```python
        if net_mode.startswith("service:"):
            target_svc = net_mode.split(":", 1)[1].strip()
            if target_svc in compose_services:
                sidecar_pairs[str(svc_name)] = target_svc
```
**Direct Observation**:
1. If `target_svc` does not exist in `compose_services` (e.g. `network_mode: service:nonexistent`), line 603 evaluates to `False`. The invalid sidecar is silently ignored, not added to `sidecar_pairs`, and falls through to line 640 where it is compiled as an independent `Deployment`! This directly violates `TC-B-F5-02`.
2. If `target_svc == svc_name` (self-referential loop, e.g. `web` specifies `network_mode: service:web`), line 603 adds `sidecar_pairs["web"] = "web"`. Then at line 641: `if svc_name in sidecar_pairs: continue` skips `web`, so no Deployment is created at all and no error is raised! This directly violates `TC-B-F5-03`.
3. If sidecars are chained (e.g. `A` points to `B` and `B` points to `C`), both `A` and `B` are in `sidecar_pairs`. When compiling `C`, only `B` is attached as a sidecar; `A` is completely dropped and never scheduled (`TC-B-F5-01`).

### Observation 1.5: `exposure: none` Actively Overridden to `cn-edge` / `external-edge` by Domain
In `luma/paas/release/helm_compiler.py` lines 493–512:
```python
    if exposure != "none" or not domain:
        ingresses, probe_url = _compile_exposure_ingress(
            name=project,
            namespace=namespace,
            region=region,
            exposure=exposure,
            domain=domain,
            port=port,
            publish_port=publish_port,
        )
    else:
        ingresses, probe_url = _compile_exposure_ingress(
            name=project,
            namespace=namespace,
            region=region or "cn",
            exposure="cn-edge" if (region or "cn") == "cn" else "external-edge",
            domain=domain,
            port=port,
            publish_port=publish_port,
        )
```
**Direct Observation**:
If a service defines `exposure: none` AND specifies a `domain` (e.g. for internal routing documentation or DNS records):
`exposure != "none"` is `False`.
`not domain` is `False`.
The conditional jumps to the `else:` branch, where `exposure` is forcibly changed to `"cn-edge"` (or `"external-edge"`), generating an Ingress resource!
This breaks `TC-F6-03 (Exposure Mode None Suppression)`: when `exposure: none`, zero Ingress resources must be emitted (`compiled.ingresses == []`).

### Observation 1.6: Raw TCP Streaming (`tcp-relay`) Uses HTTP Port Name in Service
In `luma/paas/release/helm_compiler.py` line 489 and line 768:
- In `compile_service_release` line 489–490:
```python
        "ports": [{"name": "http", "port": port, "targetPort": port}],
```
- In `compile_compose_release` line 768:
```python
        "ports": [{"name": "http", "port": svc_port, "targetPort": svc_port}],
```
**Direct Observation**:
When `exposure == "tcp-relay"`, the Kubernetes `Service` port is hardcoded with `"name": "http"`. In Kubernetes ingress controllers (Traefik, Cilium, Envoy), port name `"http"` instructs the proxy layer to treat traffic as HTTP/1.1 or HTTP/2. Raw TCP connections (e.g. Redis, database protocols, TCP sockets) sent to an HTTP-named port fail with HTTP 400 Bad Request protocol mismatch. The port name must be `"tcp"`.

### Observation 1.7: Missing Container Ports in Compose Deployment Manifests
In `luma/paas/release/helm_compiler.py` line 664–676:
```python
        main_container: Dict[str, Any] = {
            "name": slugify(svc_name),
            "image": image_ref,
            "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
            "volumeMounts": list(secret_mounts),
        }
```
**Direct Observation**:
In `compile_compose_release`, `main_container` has no `ports` definition at all. When `port` or `publishPort` is declared, the container spec must define `ports: [{"name": "tcp" if exposure == "tcp-relay" else "http", "containerPort": container_port}]`.

### Observation 1.8: No TCP Relay Port Collision Detection Across Compose Services
In `luma/paas/release/helm_compiler.py` lines 759–788:
The compiler iterates through `compose_services`. If two independent services in the same stack specify `exposure: tcp-relay` and request the same `publishPort` (e.g. `publishPort: 16379`), the compiler generates two conflicting `IngressRouteTCP` manifests with identical entrypoint `tcp-16379` without raising an error. This violates `TC-B-F6-03`.

### Observation 1.9: Read-Only Volume Flags (`:ro`) and Mount Paths Corrupted
In `luma/paas/release/helm_compiler.py` lines 397–424:
```python
    for vol_entry in parsed.get("volumes") or []:
        if not isinstance(vol_entry, str) or ":" not in vol_entry:
            continue
        vol_name, mount_path = [part.strip() for part in vol_entry.split(":", 1)]
```
**Direct Observation**:
If a volume entry is `data:/app/data:ro`:
`vol_entry.split(":", 1)` produces `mount_path = "/app/data:ro"`.
The mount path is corrupted with the `:ro` suffix, and `readOnly: True` is never set on the container `volumeMounts`. This violates `TC-B-F6-05`.

### Observation 1.10: Unvalidated Volume Storage Size Quantities (Bare Numbers Allowed)
In `luma/paas/release/helm_compiler.py` line 419 and line 635:
```python
"resources": {"requests": {"storage": str(vol_spec.get("size") or "10Gi")}}
```
**Direct Observation**:
If `storage.media.size: 50` is provided as an integer or string without units (`Gi`, `Mi`), the compiler passes `"50"` directly into the PVC manifest. Kubernetes rejects bare integers for storage quantities. `TC-B-F6-04` mandates that bare numbers without units must be rejected with `LumaError`.

---

## 2. Logic Chain

1. **Volume Data Loss in Compose Deployments**:
   - *Premise*: In `compile_compose_release`, lines 668, 691, and 701 only populate `secret_mounts` and `secret_vols`.
   - *Inference*: When a user deploys a multi-service Compose stack with database or application storage (e.g. `serverx`, `granary`, `bot`), the compiled Pod has no PVC volumes attached. Any file written to `/var/lib/...` is stored in the container's ephemeral rootfs.
   - *Impact*: Container restarts or pod rescheduling result in immediate data loss. Fixing this requires resolving volume references and injecting them into `pod_spec["volumes"]` and `container["volumeMounts"]`.

2. **Sidecar Container Command Truncation**:
   - *Premise*: `child_body.get("command")` is ignored at lines 678–696.
   - *Inference*: Sidecars like metric collectors, log forwarders, or sync agents often require explicit CLI arguments (e.g. `command: ["/bin/agent", "--foreground"]`).
   - *Impact*: Without this field, K8s falls back to the image's default `CMD`, which may launch an interactive shell or exit immediately, crashing the sidecar.

3. **Silent Sidecar Topology Errors**:
   - *Premise*: Line 603 checks `if target_svc in compose_services:`, dropping missing targets without error.
   - *Inference*: A typo in `network_mode: service:target_typod` results in the sidecar running as an isolated `Deployment` with its own localhost network, unable to communicate with the target primary container via `127.0.0.1`.
   - *Impact*: Silent broken deployments instead of immediate compile-time rejection.

4. **Exposure None Leakage**:
   - *Premise*: Line 493 branches on `if exposure != "none" or not domain: ... else: ...`.
   - *Inference*: If `exposure: none` is set and `domain` is present, the compiler forces `exposure: cn-edge`, attaching an Ingress and exposing private internal endpoints to the edge router.
   - *Impact*: Security violation; private internal services become reachable from the public internet.

5. **TCP Relay Protocol Failure**:
   - *Premise*: Service port name is hardcoded as `"http"`.
   - *Inference*: Modern Kubernetes ingress controllers use port name prefixes (`http-`, `tcp-`) to select the L7 protocol parser.
   - *Impact*: Traefik parses incoming raw TCP byte streams as HTTP, logs `bad HTTP request`, and terminates the connection.

---

## 3. Caveats

1. **Coordination with Explorer M3-1 (Feature 5)**:
   - Explorer M3-1 is investigating `nodeSelector` architecture affinity (`kubernetes.io/arch`) and sidecar architecture mismatch rejection.
   - Both Feature 5 and Feature 6 modify `compile_service_release` and `compile_compose_release` in `luma/paas/release/helm_compiler.py`.
   - The remediation code below is structured modularly: helper functions `_validate_storage_size`, `_parse_volume_entry`, `_resolve_service_volumes` and updated compilation blocks can be cleanly integrated alongside Feature 5's architecture checks without collision.

2. **Compose Volume Syntax Compatibility**:
   - In Docker Compose, volumes may be declared in `compose.services.<svc>.volumes` as well as Luma's `luma.yaml` `services.<svc>.volumes` and top-level `storage:` / `volumes:`.
   - The remediation resolver examines both sources to ensure backward and forward compatibility.

3. **StorageClass Provisioners**:
   - PersistentVolumeClaims generated default to `local-path` storageClass if not explicitly specified. HostPath binds (e.g. `/var/log:/app/log`) synthesize Kubernetes `hostPath` volumes directly without creating an unnecessary PVC.

---

## 4. Conclusion & Concrete Remediation Plan

### Required Code Additions & Fixes in `luma/paas/release/helm_compiler.py`:

#### 1. Add Kubernetes Resource Quantity Unit Validation (`TC-B-F6-04`)
```python
QUANTITY_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?(?:[eE][-+]?\d+)?([kKMGTPE]i?|[m])$")

def _validate_storage_size(size: Any, vol_name: str) -> str:
    """Validate Kubernetes resource quantity units (e.g. 10Gi, 500Mi) per TC-B-F6-04."""
    if size is None or size == "":
        return "10Gi"
    if isinstance(size, (int, float)):
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {size}"
        )
    raw = str(size).strip()
    if raw.isdigit():
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {raw}"
        )
    if not QUANTITY_UNIT_RE.match(raw):
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {raw}"
        )
    return raw
```

#### 2. Add Volume Entry Parser Supporting `:ro` and HostPaths (`TC-B-F6-05`)
```python
def _parse_volume_entry(entry: Any) -> tuple[str, str, bool]:
    """Parse volume entry (string or mapping) into (source, target_mount_path, read_only).
    
    Handles:
      - 'app-data:/var/lib/app' -> ('app-data', '/var/lib/app', False)
      - 'app-data:/var/lib/app:ro' -> ('app-data', '/var/lib/app', True)
      - 'app-data:/var/lib/app:rw' -> ('app-data', '/var/lib/app', False)
      - {'source': 'data', 'target': '/var/data', 'read_only': True}
    """
    if isinstance(entry, str):
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) < 2:
            raise LumaError(f"volume entry '{entry}' does not include a target mount path")
        source = parts[0]
        target = parts[1]
        read_only = False
        if len(parts) >= 3:
            mode = parts[2].lower()
            if mode == "ro":
                read_only = True
            elif mode == "rw":
                read_only = False
            else:
                raise LumaError(f"invalid volume access mode flag '{parts[2]}' in '{entry}'")
        return source, target, read_only
    if isinstance(entry, dict):
        source = str(entry.get("source") or "").strip()
        target = str(entry.get("target") or "").strip()
        if not target:
            raise LumaError(f"volume mapping requires 'target': {entry}")
        read_only = bool(entry.get("read_only", False))
        return source, target, read_only
    raise LumaError(f"unsupported volume entry: {entry}")
```

#### 3. Add Comprehensive Volume and PVC Resolver (`TC-F6-01`, `TC-B-F6-01`)
```python
def _resolve_service_volumes(
    *,
    project: str,
    container_name: str,
    raw_volume_entries: list[Any],
    storage_configs: Mapping[str, Any],
    existing_pod_volumes: dict[str, Dict[str, Any]],
    pvcs_by_name: dict[str, Dict[str, Any]],
    namespace: str,
    root_region: str,
    root_node: str,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Resolve volume mounts for a container, validating duplicate paths and synthesizing PVCs/hostPaths."""
    container_mounts: list[Dict[str, Any]] = []
    seen_mount_paths: set[str] = set()

    for entry in raw_volume_entries:
        source, target, read_only = _parse_volume_entry(entry)
        if target in seen_mount_paths:
            raise LumaError(
                f"duplicate mount path in container '{container_name}': {target}"
            )
        seen_mount_paths.add(target)

        # Distinguish named volume vs hostPath bind
        is_named = not (source.startswith("/") or source.startswith(".") or source.startswith("~"))
        if is_named:
            vol_name = slugify(source)
            pvc_name = f"{project}-{vol_name}"
            if pvc_name not in pvcs_by_name:
                vol_spec = storage_configs.get(source) or storage_configs.get(vol_name) or {}
                if not isinstance(vol_spec, dict):
                    vol_spec = {}
                size_str = _validate_storage_size(vol_spec.get("size"), source)
                pvcs_by_name[pvc_name] = {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": pvc_name,
                        "namespace": namespace,
                        "annotations": {
                            "luma.io/reclaim-policy": "Retain",
                            "luma.io/region": str(vol_spec.get("region") or root_region or "cn"),
                            "luma.io/node-affinity": str(vol_spec.get("node") or root_node),
                        },
                    },
                    "spec": {
                        "accessModes": [str(vol_spec.get("accessMode") or "ReadWriteOnce")],
                        "storageClassName": str(vol_spec.get("storageClass") or "local-path"),
                        "resources": {"requests": {"storage": size_str}},
                    },
                }
            if vol_name not in existing_pod_volumes:
                existing_pod_volumes[vol_name] = {
                    "name": vol_name,
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            mount: Dict[str, Any] = {"name": vol_name, "mountPath": target}
            if read_only:
                mount["readOnly"] = True
            container_mounts.append(mount)
        else:
            vol_name = f"hostpath-{slugify(source.lstrip('./~').replace('/', '-'))}"
            if vol_name not in existing_pod_volumes:
                existing_pod_volumes[vol_name] = {
                    "name": vol_name,
                    "hostPath": {"path": source, "type": "DirectoryOrCreate"},
                }
            mount = {"name": vol_name, "mountPath": target}
            if read_only:
                mount["readOnly"] = True
            container_mounts.append(mount)

    return container_mounts, list(existing_pod_volumes.values())
```

#### 4. Fix `exposure: none` Suppression & Port Naming in `compile_service_release`
- Replace lines 484–513:
```python
    svc_port = port or int(publish_port or 0)
    port_name = "tcp" if exposure == "tcp-relay" else "http"
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": project, "namespace": namespace},
        "spec": {
            "selector": {"app.kubernetes.io/name": project},
            "ports": [{"name": port_name, "port": svc_port, "targetPort": port or svc_port}],
        },
    }

    ingresses, probe_url = _compile_exposure_ingress(
        name=project,
        namespace=namespace,
        region=region,
        exposure=exposure,
        domain=domain,
        port=port,
        publish_port=publish_port,
    )
```

#### 5. Harden `compile_compose_release` for Sidecar Commands, Sidecar Validation, Volume Mounting & TCP Port Collisions
1. **Sidecar Network Mode Validation (`TC-B-F5-01`, `TC-B-F5-02`, `TC-B-F5-03`)**:
```python
    sidecar_pairs: Dict[str, str] = {}
    for svc_name, svc_body in compose_services.items():
        if not isinstance(svc_body, dict):
            continue
        net_mode = str(svc_body.get("network_mode") or "").strip()
        if net_mode.startswith("service:"):
            target_svc = net_mode.split(":", 1)[1].strip()
            if not target_svc or target_svc not in compose_services:
                raise LumaError(
                    f"service '{svc_name}' specifies network_mode 'service:{target_svc}', "
                    f"but target service '{target_svc}' does not exist in compose services"
                )
            if target_svc == svc_name:
                raise LumaError(
                    f"service '{svc_name}' cannot specify network_mode 'service:{svc_name}' referencing itself"
                )
            sidecar_pairs[str(svc_name)] = target_svc

    # Validate no compound/chained sidecars (e.g. A -> B and B -> C)
    for child_svc, parent_svc in sidecar_pairs.items():
        if parent_svc in sidecar_pairs:
            raise LumaError(
                f"chained sidecars not supported: service '{child_svc}' targets '{parent_svc}' "
                f"which is itself a sidecar of '{sidecar_pairs[parent_svc]}'"
            )
```

2. **Combined Storage Config & Initial Size Validation (`TC-B-F6-04`)**:
```python
    combined_storage: Dict[str, Any] = {}
    for source_dict in [sidecar_cfg.get("storage"), sidecar_cfg.get("volumes"), compose_cfg.get("volumes")]:
        if isinstance(source_dict, dict):
            for k, v in source_dict.items():
                if isinstance(v, dict):
                    combined_storage[str(k)] = v
                else:
                    combined_storage[str(k)] = {}

    for vol_k, vol_v in combined_storage.items():
        if "size" in vol_v:
            _validate_storage_size(vol_v["size"], vol_k)
```

3. **Sidecar Command Retention (`TC-F6-02`)**:
```python
        # Main container
        if svc_body.get("command"):
            cmd = svc_body["command"]
            main_container["command"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]

        # Sidecar container
        if child_body.get("command"):
            child_cmd = child_body["command"]
            child_container["command"] = child_cmd if isinstance(child_cmd, list) else ["sh", "-c", str(child_cmd)]
```

4. **TCP Port Collision Detection (`TC-B-F6-03`)**:
```python
    seen_tcp_ports: set[int] = set()
    ...
    if exposure == "tcp-relay":
        relay_port = int(publish_port or port or 0)
        if relay_port in seen_tcp_ports:
            raise LumaError(
                f"TCP relay port collision: publishPort {relay_port} is already used by another service in this release"
            )
        seen_tcp_ports.add(relay_port)
```

5. **Container Volume Mounts and Pod Volumes Attachment (`TC-F6-01`, `TC-B-F6-05`)**:
```python
        pod_volumes_by_name: dict[str, Dict[str, Any]] = {
            v["name"]: v for v in secret_vols
        }
        pvcs_by_name: dict[str, Dict[str, Any]] = {}

        # Resolve volumes for main container
        main_vol_entries = list(luma_svc.get("volumes") or []) + list(svc_body.get("volumes") or [])
        main_mounts, _ = _resolve_service_volumes(
            project=project,
            container_name=svc_name,
            raw_volume_entries=main_vol_entries,
            storage_configs=combined_storage,
            existing_pod_volumes=pod_volumes_by_name,
            pvcs_by_name=pvcs_by_name,
            namespace=namespace,
            root_region=svc_region or root_region or "cn",
            root_node=node_pin or root_node,
        )
        main_container["volumeMounts"] = list(secret_mounts) + main_mounts
```

---

## 5. Verification Method

### Test Execution Commands
The worker implementing Milestone 3 must verify these fixes using the following commands:
```bash
# 1. Run all PaaS engine tests including existing suite
PATH="$PWD/.venv/bin:$PATH" ./.venv/bin/python -m unittest tests.test_paas_engine -v

# 2. Run full repository checks (linter, types, site checks)
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

### Invalidation Conditions
The fix shall be considered **invalidated** if:
1. `spec.template.spec.containers[0].volumeMounts` in `compile_compose_release` fails to contain the named volume mount path (e.g. `/var/lib/app`).
2. Sidecar container `spec.template.spec.containers[1]` fails to preserve custom `command: ["/bin/agent", "--foreground"]`.
3. `exposure: none` with a `domain` generates any Ingress or Route resource (`len(compiled.ingresses) > 0`).
4. `exposure: tcp-relay` generates a Kubernetes Service with port name `"http"` instead of `"tcp"`.
5. Defining two services in Compose with identical `publishPort: 16379` succeeds instead of raising `LumaError`.
6. Defining `storage.media.size: 50` succeeds instead of raising `LumaError` for missing quantity units.
7. Defining `data:/data:ro` sets `readOnly: False` or leaves `:ro` in the `mountPath`.
8. Defining `network_mode: service:nonexistent` or `network_mode: service:self` succeeds instead of raising `LumaError`.
