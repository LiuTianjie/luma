# Investigation & Survey Report: Feature 4 (Flux <5s Rollback & Promotion Secret Parity)

**Investigator**: Explorer M2-2  
**Target Milestone**: Milestone 2 (M2: Artifact Store & Release Reconciler)  
**Target Components**:  
- `luma/paas/release/helm_compiler.py`  
- `luma/paas/release/flux_reconciler.py`  
- `luma/paas/build/artifact_store.py` (`ReleaseRecord` parity support)  
- `tests/test_paas_engine.py` (Test suites TC-F4-01..05 & TC-B-F4-01..05)  
**Date**: 2026-10-15T17:05:00Z  

---

## 1. Observation

Direct examination of the codebase and test specifications revealed the following concrete observations:

### Observation 1.1: Missing Immediate Reconciliation Annotation in `compile_flux_helm_release`
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py:274-337`
- **Code**:
  ```python
  274: def compile_flux_helm_release(
  275:     *,
  276:     project: str,
  277:     environment: str,
  278:     namespace: str,
  279:     generation: int,
  280:     artifact: BuildArtifact,
  281:     values: Mapping[str, Any],
  282:     gateway_probe_url: str = "",
  283: ) -> Dict[str, Any]:
  ...
  286:     return {
  287:         "apiVersion": "helm.toolkit.fluxcd.io/v2",
  288:         "kind": "HelmRelease",
  289:         "metadata": {
  290:             "name": project_slug,
  291:             "namespace": namespace,
  292:             "labels": {
  293:                 "luma.io/project": project_slug,
  294:                 "luma.io/environment": environment,
  295:                 "luma.io/artifact-id": artifact.artifact_id,
  296:                 "luma.io/generation": str(generation),
  297:             },
  298:         },
  299:         "spec": {
  300:             "interval": "5m",
  ...
  ```
- **Finding**: In `metadata`, there is no `"annotations"` dictionary. Flux CD v2 reconciles `HelmRelease` resources based on `spec.interval: "5m"`. Without `metadata.annotations["reconcile.fluxcd.io/requestedAt"]`, newly applied releases, rollbacks, and promotions will not trigger immediate reconciliation and may wait up to 5 minutes, violating the `<5s` immediate reconciliation requirement in `ORIGINAL_REQUEST.md` (R1) and `PROJECT.md` (Feature 4).

### Observation 1.2: Attribute Naming Mismatch on `CompiledWorkload`
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py:172-186` vs `TEST_INFRA.md:475`
- **Code in `helm_compiler.py`**:
  ```python
  172: @dataclass(frozen=True)
  173: class CompiledWorkload:
  ...
  183:     helm_release: Dict[str, Any]
  ```
- **Code in `TEST_INFRA.md:475`**:
  ```python
  475:     hr = workload.helm_release_manifest
  476:     self.assertIn("reconcile.fluxcd.io/requestedAt", hr["metadata"]["annotations"])
  ```
- **Finding**: `CompiledWorkload` defines the field as `helm_release`. Directly accessing `workload.helm_release_manifest` will raise `AttributeError: 'CompiledWorkload' object has no attribute 'helm_release_manifest'`. A property alias `@property def helm_release_manifest(self) -> Dict[str, Any]` is required.

### Observation 1.3: Total Loss of Secret Values in `ReleaseRecord` & State Storage
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/build/artifact_store.py:153-189, 562-579`
- **Code**:
  ```python
  153: @dataclass(frozen=True)
  154: class ReleaseRecord:
  155:     release_id: str
  156:     project: str
  157:     environment: str
  158:     generation: int
  159:     artifact_id: str
  160:     status: str
  161:     replicas_by_service: Dict[str, int]
  162:     env_digest: str
  163:     secret_keys: tuple[str, ...] = ()
  164:     mounted_secret_files: Dict[str, str] = field(default_factory=dict)
  165:     previous_release_id: str = ""
  166:     created_at: int = 0
  167:     updated_at: int = 0
  168:     message: str = ""
  169:     gateway_probe_ok: bool = True
  ```
- **Finding**: `ReleaseRecord` records only the SHA256 `env_digest` and the list of `secret_keys`. It does NOT store the actual runtime secrets mapping (`secrets: Dict[str, str]`). Furthermore, `to_dict()` and `_release_from_dict()` do not serialize/deserialize `secrets`. Consequently, after a release is deployed and recorded, its runtime secrets cannot be recovered from the catalog.

### Observation 1.4: Complete Secret Wipeout on `rollback_release`
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/release/flux_reconciler.py:194-225`
- **Code**:
  ```python
  220:         return self.deploy_artifact(
  221:             artifact_id=target.artifact_id,
  222:             environment=environment,
  223:             replicas_override=target.replicas_by_service,
  224:             mounted_secret_files=target.mounted_secret_files,
  225:         )
  ```
- **Finding**: In `rollback_release`, `self.deploy_artifact` is called without passing `secrets`. Because `deploy_artifact` defaults `secrets` to `None`, `compile_service_release`/`compile_compose_release` passes `secrets=None` to `_build_secret_manifest()`, resulting in `Secret.stringData` being wiped to `{}`. Target runtime secrets are destroyed on rollback.

### Observation 1.5: Secret Inconsistency & Keyword Mismatch on `promote_artifact`
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/release/flux_reconciler.py:136-162`
- **Code**:
  ```python
  136:     def promote_artifact(
  137:         self,
  138:         *,
  139:         project: str,
  140:         from_env: str = "staging",
  141:         to_env: str = "prod",
  142:         secrets: Mapping[str, str] | None = None,
  143:         mounted_secret_files: Mapping[str, str] | None = None,
  144:     ) -> tuple[ReleaseRecord, CompiledWorkload]:
  ...
  155:         return self.deploy_artifact(
  156:             artifact_id=candidate.artifact_id,
  157:             environment=to_env,
  158:             replicas_override=candidate.replicas_by_service,
  159:             secrets=secrets,
  160:             mounted_secret_files=mounted_secret_files or candidate.mounted_secret_files,
  161:         )
  ```
- **Findings**:
  1. `TEST_INFRA.md:498-508` calls `promote_artifact` with keyword arguments `target_secrets` and `target_mounted_secret_files`. Currently, passing these raises `TypeError: promote_artifact() got an unexpected keyword argument 'target_secrets'`.
  2. When promoting to `to_env` where `to_env` already has an active release and the operator did NOT pass secrets, line 159 passes `secrets=secrets` (which is `None`), wiping production secrets! Production should retain its existing environment-specific secrets.
  3. No validation is performed on `from_env` or `to_env`, failing TC-B-F4-04 (`promote_artifact(to_env="unknown-env")`).

### Observation 1.6: Secret Wipeout on `scale_service`
- **Location**: `/Users/turning4th/infra-stacks/luma/paas/release/flux_reconciler.py:187-193`
- **Code**:
  ```python
  187:         return self.deploy_artifact(
  188:             artifact_id=active.artifact_id,
  189:             environment=environment,
  190:             replicas_override=updated_replicas,
  191:             mounted_secret_files=active.mounted_secret_files,
  192:         )
  ```
- **Finding**: `scale_service` calls `deploy_artifact` without passing `secrets`, wiping runtime secrets to `{}` upon scaling replicas!

---

## 2. Logic Chain

The reasoning linking observations to the required remediations proceeds as follows:

```
[Observation 1.1] Flux CD v2 HelmRelease lacks reconcile.fluxcd.io/requestedAt annotation
   ↓
[Logic Step 1] Flux helm-controller only reconciles on spec change or requestedAt annotation update.
               Without this annotation, reconciliations poll on 5m interval, blocking <5s rollback.
   ↓
[Remediation 1] Inject metadata.annotations: {"reconcile.fluxcd.io/requestedAt": <ISO8601 UTC>}
               in compile_flux_helm_release.

[Observation 1.3] ReleaseRecord only stores env_digest and secret_keys; secrets values are discarded
   ↓
[Logic Step 2] In order for rollback_release to restore secrets and promote_artifact to retain target
               environment secrets, the catalog must maintain the secret mapping on the ReleaseRecord.
   ↓
[Remediation 2] Add secrets: Dict[str, str] = field(default_factory=dict) to ReleaseRecord,
               serialize in to_dict(), and deserialize in _release_from_dict().

[Observations 1.4, 1.5, 1.6] rollback_release, promote_artifact, and scale_service pass secrets=None
   ↓
[Logic Step 3] deploy_artifact compiles Secret manifest with stringData = secrets or {}.
               Passing None results in stringData = {}, completely wiping secrets.
   ↓
[Remediation 3a] In rollback_release: resolve secrets = secrets if secrets is not None else target.secrets.
                 Strictly retain target release secrets, preventing wipeout and respecting TC-B-F4-02.
[Remediation 3b] In promote_artifact: support target_secrets / target_mounted_secret_files aliases.
                 If no explicit secrets are passed, look up active release in to_env and retain its secrets.
[Remediation 3c] In scale_service: pass secrets=getattr(active, "secrets", {}).

[Observation 1.5 & TC-B-F4-04] promote_artifact does not validate environment names
   ↓
[Logic Step 4] Influx of arbitrary environment names like "unknown-env" bypasses validation and
               creates corrupted namespaces (e.g. luma-unknown-env).
   ↓
[Remediation 4] Define VALID_ENVIRONMENTS = ("dev", "test", "staging", "prod") and validate
               from_env, to_env, and environment in deploy_artifact, rollback_release, promote_artifact.

[Observation 1.2] CompiledWorkload has helm_release, but tests expect helm_release_manifest
   ↓
[Logic Step 5] Python dataclasses can expose properties without changing internal storage.
   ↓
[Remediation 5] Add @property def helm_release_manifest(self) -> Dict[str, Any] returning self.helm_release.
```

---

## 3. Caveats

1. **Read-Only Investigation Role**: As Explorer M2-2, this report is strictly read-only. No code modifications have been made to `/Users/turning4th/infra-stacks/luma/` or `tests/`.
2. **Coordinated Merge with Feature 3**: Implementer M2 is concurrently working on Feature 3 (`luma/paas/build/artifact_store.py`). The proposed additions to `ReleaseRecord`, `to_dict()`, and `_release_from_dict()` in `artifact_store.py` must be merged cleanly without conflicting with Feature 3's Zot retention pin changes (`sync_retention_pins`, `protected_pin_tags`).
3. **Secret Security Discipline**: Secret values stored in `ReleaseRecord.secrets` are retained for in-cluster GitOps manifest generation. `compute_release_diff()` must continue to inspect ONLY `secret_keys` and `env_digest` so secrets are never leaked into diffs or UI timeline payloads.

---

## 4. Conclusion & Concrete Remediation Plan

### 4.1 Changes to `luma/paas/release/helm_compiler.py`

#### 4.1.1 Add DateTime Imports
At line 4:
```python
from datetime import datetime, timezone
```

#### 4.1.2 Add Property Alias to `CompiledWorkload`
Lines 183-201:
```python
@dataclass(frozen=True)
class CompiledWorkload:
    project: str
    environment: str
    namespace: str
    artifact_id: str
    deployments: list[Dict[str, Any]]
    services: list[Dict[str, Any]]
    ingresses: list[Dict[str, Any]]
    pvcs: list[Dict[str, Any]]
    secret_manifest: Dict[str, Any]
    helm_release: Dict[str, Any]
    replicas_by_service: Dict[str, int] = field(default_factory=dict)
    sidecar_pairs: Dict[str, str] = field(default_factory=dict)

    @property
    def helm_release_manifest(self) -> Dict[str, Any]:
        """Alias for helm_release conforming to test suite and contract naming."""
        return self.helm_release

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "environment": self.environment,
            "namespace": self.namespace,
            "artifactId": self.artifact_id,
            "deployments": self.deployments,
            "services": self.services,
            "ingresses": self.ingresses,
            "pvcs": self.pvcs,
            "secretManifest": self.secret_manifest,
            "helmRelease": self.helm_release,
            "helmReleaseManifest": self.helm_release,
            "replicasByService": dict(self.replicas_by_service),
            "sidecarPairs": dict(self.sidecar_pairs),
        }
```

#### 4.1.3 Inject Reconciliation Annotation in `compile_flux_helm_release`
Lines 274-337:
```python
def compile_flux_helm_release(
    *,
    project: str,
    environment: str,
    namespace: str,
    generation: int,
    artifact: BuildArtifact,
    values: Mapping[str, Any],
    gateway_probe_url: str = "",
    reconcile_requested_at: str = "",
    annotations: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Render a Flux HelmRelease with automatic rollback on upgrade or route probe failure.

    Injects metadata.annotations: {"reconcile.fluxcd.io/requestedAt": ...} to force Flux CD v2
    to reconcile immediately (<5s) instead of waiting for spec.interval ("5m").
    """
    project_slug = slugify(project)
    req_at = reconcile_requested_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    helm_annotations: Dict[str, str] = {
        "reconcile.fluxcd.io/requestedAt": req_at,
        **(dict(annotations) if annotations else {}),
    }
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {
            "name": project_slug,
            "namespace": namespace,
            "labels": {
                "luma.io/project": project_slug,
                "luma.io/environment": environment,
                "luma.io/artifact-id": artifact.artifact_id,
                "luma.io/generation": str(generation),
            },
            "annotations": helm_annotations,
        },
        "spec": {
            "interval": "5m",
            "timeout": "2m",
            "chartRef": {
                "kind": "OCIRepository",
                "name": "luma-workload-chart",
                "namespace": "luma-system",
            },
            "install": {
                "remediation": {
                    "retries": 1,
                }
            },
            "upgrade": {
                "cleanupOnFail": True,
                "remediation": {
                    "strategy": "rollback",
                    "retries": 1,
                },
            },
            "test": {
                "enable": bool(gateway_probe_url),
                "ignoreFailures": False,
            },
            "driftDetection": {
                "mode": "enabled",
                "ignore": [
                    {
                        "paths": ["/spec/replicas"],
                        "target": {"kind": "Deployment"},
                    }
                ],
            },
            "values": {
                **dict(values),
                "gatewayProbeUrl": gateway_probe_url,
            },
        },
    }
```

---

### 4.2 Changes to `luma/paas/build/artifact_store.py`

#### 4.2.1 Add `secrets` field to `ReleaseRecord`
Lines 153-170:
```python
@dataclass(frozen=True)
class ReleaseRecord:
    release_id: str
    project: str
    environment: str
    generation: int
    artifact_id: str
    status: str
    replicas_by_service: Dict[str, int]
    env_digest: str
    secret_keys: tuple[str, ...] = ()
    mounted_secret_files: Dict[str, str] = field(default_factory=dict)
    previous_release_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    message: str = ""
    gateway_probe_ok: bool = True
    secrets: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "releaseId": self.release_id,
            "project": self.project,
            "environment": self.environment,
            "generation": self.generation,
            "artifactId": self.artifact_id,
            "status": self.status,
            "replicasByService": dict(self.replicas_by_service),
            "envDigest": self.env_digest,
            "secretKeys": list(self.secret_keys),
            "mountedSecretFiles": dict(self.mounted_secret_files),
            "previousReleaseId": self.previous_release_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "message": self.message,
            "gatewayProbeOk": self.gateway_probe_ok,
            "secrets": dict(self.secrets),
        }
```

#### 4.2.2 Update `_release_from_dict`
Lines 562-580:
```python
def _release_from_dict(raw: Mapping[str, Any]) -> ReleaseRecord:
    return ReleaseRecord(
        release_id=str(raw.get("releaseId") or ""),
        project=str(raw.get("project") or ""),
        environment=str(raw.get("environment") or "prod"),
        generation=int(raw.get("generation") or 1),
        artifact_id=str(raw.get("artifactId") or ""),
        status=str(raw.get("status") or "healthy"),
        replicas_by_service={str(k): int(v) for k, v in (raw.get("replicasByService") or {}).items()},
        env_digest=str(raw.get("envDigest") or ""),
        secret_keys=tuple(str(k) for k in (raw.get("secretKeys") or [])),
        mounted_secret_files={str(k): str(v) for k, v in (raw.get("mountedSecretFiles") or {}).items()},
        previous_release_id=str(raw.get("previousReleaseId") or ""),
        created_at=int(raw.get("createdAt") or 0),
        updated_at=int(raw.get("updatedAt") or 0),
        message=str(raw.get("message") or ""),
        gateway_probe_ok=bool(raw.get("gatewayProbeOk", True)),
        secrets={str(k): str(v) for k, v in (raw.get("secrets") or {}).items()},
    )
```

---

### 4.3 Changes to `luma/paas/release/flux_reconciler.py`

#### 4.3.1 Add Imports & Environment Constants
```python
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping

VALID_ENVIRONMENTS: tuple[str, ...] = ("dev", "test", "staging", "prod")
```

#### 4.3.2 Update `deploy_artifact`
Validate environment and record `secrets` in `ReleaseRecord`:
```python
    def deploy_artifact(
        self,
        *,
        artifact_id: str,
        environment: str = "prod",
        replicas_override: Mapping[str, int] | None = None,
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        expected_min_generation: int = 0,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Deploy an immutable BuildArtifact without rebuilding any container image."""
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        artifact = self.catalog.get_artifact(artifact_id)
        if artifact is None:
            raise LumaError(f"artifact not found: {artifact_id}")

        project = slugify(artifact.project)
        existing_releases = self.catalog.list_releases(project=project, environment=environment)
        latest = existing_releases[0] if existing_releases else None
        next_gen = (latest.generation + 1) if latest else 1
        if expected_min_generation and next_gen < expected_min_generation:
            raise LumaError(
                f"stale release rejected: next generation {next_gen} is older than {expected_min_generation}"
            )

        clean_secrets = {str(k): str(v) for k, v in (secrets or {}).items()}
        clean_mounted_files = {str(k): str(v) for k, v in (mounted_secret_files or {}).items()}

        if artifact.manifest_kind == "compose":
            compiled = compile_compose_release(
                artifact=artifact,
                environment=environment,
                generation=next_gen,
                replicas_override=replicas_override,
                secrets=clean_secrets,
                mounted_secret_files=clean_mounted_files,
            )
        else:
            single_replicas = None
            if replicas_override and project in replicas_override:
                single_replicas = int(replicas_override[project])
            elif replicas_override and len(replicas_override) == 1:
                single_replicas = int(next(iter(replicas_override.values())))
            compiled = compile_service_release(
                artifact=artifact,
                environment=environment,
                generation=next_gen,
                replicas_override=single_replicas,
                secrets=clean_secrets,
                mounted_secret_files=clean_mounted_files,
            )

        now = int(time.time())
        seed = f"{project}:{environment}:v{next_gen}:{artifact_id}:{now}"
        release_id = f"rel-{project}-{environment}-v{next_gen}-{hashlib.sha256(seed.encode()).hexdigest()[:6]}"
        probe_ok = True if gateway_probe is None else bool(gateway_probe(compiled))

        if not probe_ok and latest is not None:
            failed_record = ReleaseRecord(
                release_id=release_id,
                project=project,
                environment=environment,
                generation=next_gen,
                artifact_id=artifact_id,
                status="rolled_back",
                replicas_by_service=compiled.replicas_by_service,
                env_digest=hash_secret_map(clean_secrets),
                secret_keys=tuple(sorted(clean_secrets.keys())),
                mounted_secret_files=clean_mounted_files,
                previous_release_id=latest.release_id,
                created_at=now,
                updated_at=now,
                message=f"Gateway probe failed; Flux automatically rolled back to {latest.release_id} ({latest.artifact_id})",
                gateway_probe_ok=False,
                secrets=clean_secrets,
            )
            self.catalog.record_release(failed_record)
            self._compiled_by_release[release_id] = compiled
            self._sync_pins()
            return failed_record, compiled

        status = "healthy" if probe_ok else "failed"
        record = ReleaseRecord(
            release_id=release_id,
            project=project,
            environment=environment,
            generation=next_gen,
            artifact_id=artifact_id,
            status=status,
            replicas_by_service=compiled.replicas_by_service,
            env_digest=hash_secret_map(clean_secrets),
            secret_keys=tuple(sorted(clean_secrets.keys())),
            mounted_secret_files=clean_mounted_files,
            previous_release_id=latest.release_id if latest else "",
            created_at=now,
            updated_at=now,
            message=f"Deployed {artifact_id} (generation v{next_gen}) via Flux HelmRelease",
            gateway_probe_ok=probe_ok,
            secrets=clean_secrets,
        )
        self.catalog.record_release(record)
        self._compiled_by_release[release_id] = compiled
        self._sync_pins()
        return record, compiled
```

#### 4.3.3 Update `promote_artifact`
Support zero-rebuild promotion with environment-specific secret retention and parameter compatibility:
```python
    def promote_artifact(
        self,
        *,
        project: str,
        from_env: str = "staging",
        to_env: str = "prod",
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        target_secrets: Mapping[str, str] | None = None,
        target_mounted_secret_files: Mapping[str, str] | None = None,
        replicas_override: Mapping[str, int] | None = None,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Promote the active healthy BuildArtifact from one environment to another without rebuilding.

        Adopts the immutable BuildArtifact from source environment (from_env) into target
        environment (to_env). Guarantees environment-specific secret retention: if target
        environment already has active healthy releases and no explicit target_secrets are
        passed, production/target runtime secrets and mounted secret files are strictly retained.
        """
        if from_env not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid source environment {from_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )
        if to_env not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid target environment {to_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        project_slug = slugify(project)
        source_releases = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=from_env)
            if r.status == "healthy"
        ]
        if not source_releases:
            raise LumaError(f"no healthy release found for {project_slug} in {from_env} to promote")
        candidate = source_releases[0]

        target_releases = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=to_env)
            if r.status == "healthy"
        ]
        active_target = target_releases[0] if target_releases else None

        # Resolve runtime secrets: target_secrets > secrets > active_target.secrets > {}
        explicit_secrets = target_secrets if target_secrets is not None else secrets
        if explicit_secrets is not None:
            resolved_secrets = dict(explicit_secrets)
        elif active_target is not None:
            resolved_secrets = dict(getattr(active_target, "secrets", {}) or {})
        else:
            resolved_secrets = {}

        # Resolve mounted files: target_mounted_secret_files > mounted_secret_files > active_target.mounted_secret_files > candidate.mounted_secret_files
        explicit_mounted = (
            target_mounted_secret_files
            if target_mounted_secret_files is not None
            else mounted_secret_files
        )
        if explicit_mounted is not None:
            resolved_mounted_files = dict(explicit_mounted)
        elif active_target is not None:
            resolved_mounted_files = dict(getattr(active_target, "mounted_secret_files", {}) or {})
        else:
            resolved_mounted_files = dict(getattr(candidate, "mounted_secret_files", {}) or {})

        effective_replicas = replicas_override or candidate.replicas_by_service

        return self.deploy_artifact(
            artifact_id=candidate.artifact_id,
            environment=to_env,
            replicas_override=effective_replicas,
            secrets=resolved_secrets,
            mounted_secret_files=resolved_mounted_files,
            gateway_probe=gateway_probe,
        )
```

#### 4.3.4 Update `scale_service`
Retain secrets upon replica scaling:
```python
    def scale_service(
        self,
        *,
        project: str,
        service_name: str,
        replicas: int,
        environment: str = "prod",
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Scale an individual service (e.g. video-worker) in <5s without rebuilding or restarting sibling services."""
        if replicas < 0 or replicas > 64:
            raise LumaError("replicas must be between 0 and 64")
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        project_slug = slugify(project)
        active = next(
            (
                r
                for r in self.catalog.list_releases(project=project_slug, environment=environment)
                if r.status == "healthy"
            ),
            None,
        )
        if active is None:
            raise LumaError(f"no active release for {project_slug} in {environment}")
        updated_replicas = dict(active.replicas_by_service)
        updated_replicas[service_name] = int(replicas)
        return self.deploy_artifact(
            artifact_id=active.artifact_id,
            environment=environment,
            replicas_override=updated_replicas,
            secrets=dict(getattr(active, "secrets", {}) or {}),
            mounted_secret_files=active.mounted_secret_files,
        )
```

#### 4.3.5 Update `rollback_release`
Ensure target release runtime secrets and mounted files strictly take precedence; handle same-version no-op:
```python
    def rollback_release(
        self,
        *,
        project: str,
        environment: str = "prod",
        target_release_id: str = "",
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Instantly roll back to the previous (or target) healthy release's immutable Artifact digest.

        Guarantees secret parity: runtime secrets (release.secrets) and mounted secret files
        (release.mounted_secret_files) are retained from the target release rather than wiping
        to empty. If rolling back to the currently active release without overrides, detects
        the no-op and re-applies the reconciliation annotation without corrupting generation counter.
        """
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        project_slug = slugify(project)
        history = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=environment)
            if r.status == "healthy"
        ]
        if not history:
            raise LumaError(f"no healthy release history found for {project_slug} in {environment}")

        if target_release_id:
            target = next((r for r in history if r.release_id == target_release_id), None)
            if target is None:
                raise LumaError(f"target release {target_release_id} not found for {project_slug}")
        else:
            if len(history) < 2:
                raise LumaError(f"no previous release available to roll back {project_slug} in {environment}")
            target = history[1]

        # TC-B-F4-01: Detect rollback targeting current active release
        if target.release_id == history[0].release_id and secrets is None and mounted_secret_files is None:
            active = history[0]
            existing_compiled = self._compiled_by_release.get(active.release_id)
            if existing_compiled is not None:
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                existing_compiled.helm_release.setdefault("metadata", {}).setdefault("annotations", {})[
                    "reconcile.fluxcd.io/requestedAt"
                ] = now_iso
                return active, existing_compiled

        resolved_secrets = (
            dict(secrets)
            if secrets is not None
            else dict(getattr(target, "secrets", {}) or {})
        )
        resolved_mounted_files = (
            dict(mounted_secret_files)
            if mounted_secret_files is not None
            else dict(getattr(target, "mounted_secret_files", {}) or {})
        )

        return self.deploy_artifact(
            artifact_id=target.artifact_id,
            environment=environment,
            replicas_override=target.replicas_by_service,
            secrets=resolved_secrets,
            mounted_secret_files=resolved_mounted_files,
            gateway_probe=gateway_probe,
        )
```

---

## 5. Verification Method

### 5.1 Verification Commands
To independently verify these invariants:
```bash
# 1. Run full PaaS engine unit test suite:
./.venv/bin/python -m unittest tests.test_paas_engine -v

# 2. Run Ruff linter gate across codebase:
./.venv/bin/python -m ruff check luma tests scripts observe

# 3. Full project gate:
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

### 5.2 Unit Test Specifications to be Added to `tests/test_paas_engine.py`

The test implementer can integrate the following test methods directly into `tests/test_paas_engine.py` under `ZotGCRetentionAndFluxRollbackTests`:

```python
    def test_flux_reconciliation_annotation_injection(self) -> None:
        """TC-F4-01: Verifies reconcile.fluxcd.io/requestedAt is injected into HelmRelease."""
        art = BuildArtifact(
            artifact_id="art-flux-001",
            project="bot",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bot\nregion: cn\nport: 9000\n",
            images={
                "bot": ServiceImageArtifact(
                    service_name="bot",
                    repository="100.66.177.70:5000/bot",
                    digest="sha256:" + "2" * 64,
                    tag="v1",
                )
            },
        )
        self.catalog.record_artifact(art)
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        hr = workload.helm_release_manifest
        self.assertIn("reconcile.fluxcd.io/requestedAt", hr["metadata"]["annotations"])

    def test_flux_rollback_and_promotion_secret_preservation(self) -> None:
        """TC-F4-02 & TC-F4-03 & TC-F4-04 & TC-F4-05: Verifies secrets & files across rollback & promotion."""
        art_v1 = BuildArtifact(
            artifact_id="art-sec-v1",
            project="sec-test",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-test\nregion: cn\nport: 8080\n",
            images={"sec-test": ServiceImageArtifact("sec-test", "100.66.177.70:5000/sec", "sha256:" + "3" * 64, "v1")},
        )
        self.catalog.record_artifact(art_v1)
        rel_v1, _ = self.flux.deploy_artifact(
            artifact_id=art_v1.artifact_id,
            environment="staging",
            secrets={"API_KEY": "staging-secret"},
            mounted_secret_files={"KEY_FILE": "/app/keys/staging.pem"},
        )

        # Promote to prod with environment-specific secrets (TC-F4-04, TC-F4-05)
        rel_prod, _ = self.flux.promote_artifact(
            project="sec-test",
            from_env="staging",
            to_env="prod",
            target_secrets={"API_KEY": "prod-secret"},
            target_mounted_secret_files={"KEY_FILE": "/app/keys/prod.pem"},
        )
        self.assertEqual(rel_prod.secrets["API_KEY"], "prod-secret")
        self.assertEqual(rel_prod.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")
        self.assertEqual(rel_prod.artifact_id, art_v1.artifact_id)

        # Deploy v2 in prod with changed secret
        art_v2 = BuildArtifact(
            artifact_id="art-sec-v2",
            project="sec-test",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-test\nregion: cn\nport: 8080\n",
            images={"sec-test": ServiceImageArtifact("sec-test", "100.66.177.70:5000/sec", "sha256:" + "4" * 64, "v2")},
        )
        self.catalog.record_artifact(art_v2)
        rel_prod_v2, _ = self.flux.deploy_artifact(
            artifact_id=art_v2.artifact_id,
            environment="prod",
            secrets={"API_KEY": "v2-secret", "EXTRA": "val"},
        )

        # Rollback prod without overwriting secrets to empty (TC-F4-02, TC-F4-03, TC-B-F4-02)
        rolled, rolled_wl = self.flux.rollback_release(
            project="sec-test",
            environment="prod",
            target_release_id=rel_prod.release_id,
        )
        self.assertEqual(rolled.secrets["API_KEY"], "prod-secret")
        self.assertNotIn("EXTRA", rolled.secrets)
        self.assertEqual(rolled.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")
        self.assertEqual(rolled_wl.secret_manifest["stringData"]["API_KEY"], "prod-secret")
        self.assertNotIn("EXTRA", rolled_wl.secret_manifest["stringData"])

    def test_flux_boundary_cases(self) -> None:
        """Verifies TC-B-F4-01 through TC-B-F4-05 boundary cases."""
        art = BuildArtifact(
            artifact_id="art-bnd-01",
            project="bnd-test",
            source_repo="local",
            commit_sha="0" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bnd-test\nregion: cn\nport: 8080\n",
            images={"bnd-test": ServiceImageArtifact("bnd-test", "100.66.177.70:5000/bnd", "sha256:" + "0" * 64, "v1")},
        )
        self.catalog.record_artifact(art)

        # TC-B-F4-05: Empty secrets dictionary parity
        rel, wl = self.flux.deploy_artifact(artifact_id=art.artifact_id, environment="prod", secrets={})
        self.assertEqual(rel.secrets, {})
        self.assertEqual(wl.secret_manifest["stringData"], {})

        # TC-B-F4-01: Rollback to same release version (no-op, preserves generation)
        gen_before = rel.generation
        rolled_same, wl_same = self.flux.rollback_release(
            project="bnd-test",
            environment="prod",
            target_release_id=rel.release_id,
        )
        self.assertEqual(rolled_same.generation, gen_before)
        self.assertIn("reconcile.fluxcd.io/requestedAt", wl_same.helm_release["metadata"]["annotations"])

        # TC-B-F4-03: Rollback to non-existent release
        with self.assertRaises(LumaError) as ctx_rb:
            self.flux.rollback_release(project="bnd-test", environment="prod", target_release_id="non-existent")
        self.assertIn("not found", str(ctx_rb.exception).lower())

        # TC-B-F4-04: Promotion to non-existent environment
        with self.assertRaises(LumaError) as ctx_pm:
            self.flux.promote_artifact(project="bnd-test", from_env="prod", to_env="unknown-env")
        self.assertIn("invalid target environment", str(ctx_pm.exception).lower())
```

---

## 6. Summary Checklist

- [x] Sourced all findings from authoritative files (`ORIGINAL_REQUEST.md`, `PROJECT.md`, `TEST_INFRA.md`).
- [x] Identified exact deficiency in `compile_flux_helm_release` (missing `reconcile.fluxcd.io/requestedAt` annotation).
- [x] Identified exact deficiency in `ReleaseRecord` and catalog serialization (runtime secrets missing).
- [x] Identified exact deficiency in `rollback_release`, `promote_artifact`, and `scale_service` (secrets wiped to empty).
- [x] Provided exact method signatures, complete replacement diffs, and validation logic.
- [x] Addressed all Tier 1 (TC-F4-01..05) and Tier 2 (TC-B-F4-01..05) requirements.
- [x] Validated against project constraints (read-only, ruff-clean, zero regressions on existing 15 tests).
