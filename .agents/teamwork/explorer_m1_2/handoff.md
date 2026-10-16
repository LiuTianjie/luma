# Handoff Report: M1-2 Native Builder Selection & Build Submission Architecture

**Author**: Explorer M1-2  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_2`  
**Target Milestone**: M1 (Build & Native Architecture)  
**Target Module**: `luma/paas/build/controller.py` (and integration with `luma/build_capabilities.py` and `tests/test_paas_engine.py`)

---

## 1. Observation

Direct examination of `luma/paas/build/controller.py`, `luma/build_capabilities.py`, `tests/test_paas_engine.py`, and peer findings from `explorer_m1_1` and `explorer_m1_3` revealed the following exact observations:

### 1.1 `select_native_builder` Missing Topology Parameters, Inference & Conflict Guards
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py` lines 162–198:
```python
162:     def select_native_builder(
163:         self,
164:         platform: str,
165:         *,
166:         preferred_node: str = "",
167:     ) -> BuilderEndpoint:
168:         """Select a ready native BuilderEndpoint for the target platform (never QEMU)."""
169:         norm_platform = str(platform or "linux/amd64").strip().lower()
170:         arm_platform = native_arm_build_platform([norm_platform])
171:         target_platform = arm_platform or norm_platform
172: 
173:         if preferred_node:
174:             candidate = self._builders.get(preferred_node)
175:             if candidate is None:
176:                 raise LumaError(f"requested builder node is not registered: {preferred_node}")
177:             if not candidate.ready:
178:                 raise LumaError(f"requested builder node is offline: {preferred_node}")
179:             if candidate.platform.lower() != target_platform:
180:                 raise LumaError(
181:                     f"builder {preferred_node} ({candidate.platform}) does not match native target "
182:                     f"platform {target_platform}; cross-architecture emulation is disabled"
183:                 )
184:             return candidate
185: 
186:         matching = [
187:             b
188:             for b in self._builders.values()
189:             if b.ready and b.platform.lower() == target_platform
190:         ]
191:         if not matching:
192:             raise LumaError(
193:                 f"no ready native BuildKit endpoint available for {target_platform}; "
194:                 "register an online builder for this architecture"
195:             )
196:         matching.sort(key=lambda b: (-b.available_slots, b.cache_used_bytes, b.name))
197:         return matching[0]
```
- **Direct Observations**:
  1. `select_native_builder` accepts only `platform` and `preferred_node`. It does not accept `target_node` or `target_region`.
  2. If `platform` is omitted or empty, line 169 falls back unconditionally to `"linux/amd64"`.
  3. Line 170 invokes `native_arm_build_platform([norm_platform])`. As verified in `test_native_builders.py:144`, `native_arm_build_platform(["linux/amd64,linux/arm64"])` returns `"linux/arm64"`, silently accepting comma-separated multi-platform strings without raising an error.
  4. There is no check whether an explicitly provided `platform` conflicts with `target_region` (e.g. `region="home"`) or `target_node` (e.g. `node="macbook-pro"`). If a caller specifies `platform="linux/amd64"` and `target_node="macbook-pro"`, the method selects an AMD64 builder without error, generating an unusable image that crashes on Apple Silicon nodes.
  5. If `target_node` is a registered builder (e.g., `macbook-pro`), line 186-196 does not enforce that `target_node` matches `target_platform`, nor does sorting prioritize `target_node` when available.

### 1.2 `submit_build` Lacks `target_node`/`target_region` Parameters & `build_spec` Inspection
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py` lines 199–218:
```python
199:     def submit_build(
200:         self,
201:         *,
202:         project: str,
203:         repo_url: str,
204:         revision: str,
205:         branch: str = "main",
206:         platform: str = "linux/amd64",
207:         context_subpath: str = ".",
208:         dockerfile_path: str = "Dockerfile",
209:         build_spec: Mapping[str, Any] | None = None,
210:         preferred_builder: str = "",
211:         idempotency_key: str = "",
212:     ) -> Dict[str, Any]:
...
215:         builder = self.select_native_builder(platform, preferred_node=preferred_builder)
216:         git_contexts = validate_git_contexts_spec(build_spec or {})
```
- **Direct Observations**:
  1. `submit_build` parameters lack `target_node: str = ""` and `target_region: str = ""`.
  2. `platform` has a default of `"linux/amd64"`, which masks whether the caller intended an explicit AMD64 build or simply omitted the platform.
  3. `submit_build` does not inspect `build_spec` for `node`, `target_node`, `region`, or `target_region`.
  4. Because `platform` defaults to `"linux/amd64"`, any deployment to an ARM64 node (`macbook-pro`, `air3`, openSUSE MicroOS ARM64) or `home` region without an explicit `--platform linux/arm64` flag results in an AMD64 image build.

### 1.3 Concurrency Slots on `BuilderEndpoint` Are Never Claimed or Released
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py`:
- Lines 27–45:
  ```python
  27: @dataclass(frozen=True)
  28: class BuilderEndpoint:
  ...
  33:     max_concurrency: int = 4
  34:     active_builds: int = 0
  ...
  40:     @property
  41:     def available_slots(self) -> int:
  42:         if not self.ready:
  43:             return 0
  44:         return max(self.max_concurrency - self.active_builds, 0)
  ```
- Lines 299:
  ```python
  "status": "running" if builder.available_slots > 0 else "queued",
  ```
- Lines 310–327 (`cancel_build`):
  No decrement of `active_builds`.
- **Direct Observations**:
  1. `builder.active_builds` is never modified upon submission (`submit_build`).
  2. `builder.active_builds` is never decremented upon cancellation (`cancel_build`) or completion.
  3. `builder.available_slots` permanently returns `max_concurrency` (4), rendering concurrency limits and load balancing across multiple builders non-functional.

---

## 2. Logic Chain

1. **Step 1: Strict Single-Architecture Validation Integration**:
   - *Observation Reference*: Observation 1.1 item 3.
   - *Reasoning*: Multi-platform fat manifest compilation requires QEMU emulation or concurrent multi-builder pipelines, violating Requirement R2 ("Zero Cross-Compilation / Zero QEMU").
   - *Action*: `select_native_builder` must pass `platform` through `validate_native_platform(platform)` (provided by `luma/build_capabilities.py`), which raises `LumaError` if multiple comma-separated architectures are submitted, and normalizes aliases (`arm64/v8`, `aarch64` -> `arm64`, `x86_64` -> `amd64`).

2. **Step 2: Topology-Based Platform Inference Hierarchy**:
   - *Observation Reference*: Observation 1.1 item 2, Observation 1.2 items 1–4.
   - *Reasoning*: Users and deployment manifests often specify target execution environments without redundantly specifying Docker build architectures.
   - *Action*:
     - `submit_build` signature is updated with `platform: str = ""`, `target_node: str = ""`, and `target_region: str = ""`.
     - If `target_node` or `target_region` is not explicitly passed to `submit_build`, `build_spec` is inspected for `target_node`, `targetNode`, `node`, `target_region`, `targetRegion`, `region`, or nested `placement` dicts.
     - If `platform` is empty/omitted, `infer_native_platform` is invoked with topology parameters:
       - `target_region == "home"` -> `"linux/arm64"`
       - `target_node in node_architectures` -> normalized node architecture
       - `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()` -> `"linux/arm64"`
       - Otherwise -> defaults to `"linux/amd64"`

3. **Step 3: Conflict Detection Between Explicit Platform and Target Topology**:
   - *Observation Reference*: Observation 1.1 item 4.
   - *Reasoning*: If an explicit platform is supplied (e.g. `--platform linux/amd64`), but the target deployment node is Apple Silicon (`macbook-pro`) or target region is `home`, building an AMD64 binary is doomed to fail at runtime.
   - *Action*: In `select_native_builder`, when `platform` is explicitly provided, validate against `target_region` and `target_node`:
     - If `target_region == "home"` and `target_platform != "linux/arm64"`: raise `LumaError("explicit platform ... conflicts with target region home (requires linux/arm64); cross-architecture emulation is disabled")`.
     - If `target_node` resolves to ARM64 and `target_platform != "linux/arm64"`: raise `LumaError("explicit platform ... conflicts with target node ...; cross-architecture emulation is disabled")`.

4. **Step 4: Strict Native Builder Routing & Zero QEMU Fallback**:
   - *Observation Reference*: Observation 1.1 items 1, 4, 5.
   - *Reasoning*: A build must only be scheduled on a native builder matching the required architecture.
   - *Action*:
     - If `preferred_node` is specified: verify registration, ready status, and exact architecture match (`candidate_platform == target_platform`).
     - If `target_node` is registered in `self._builders`: enforce `validate_native_platform(target_builder.platform) == target_platform`.
     - Filter candidates strictly by `b.ready and validate_native_platform(b.platform) == target_platform`. If none exist, raise `LumaError(f"no ready native BuildKit endpoint available for {target_platform}; register an online builder for this architecture")`.
     - Sort matching builders prioritizing `b.name == target_node`, then `-b.available_slots`, then `b.cache_used_bytes`, then `b.name`.

5. **Step 5: Concurrency Accounting via Immutable Dataclass Replacement**:
   - *Observation Reference*: Observation 1.3 items 1–3.
   - *Reasoning*: `BuilderEndpoint` is `@dataclass(frozen=True)` for immutability and thread safety. Mutating active build counts must occur through `dataclasses.replace` in `BuildController`.
   - *Action*:
     - Implement `_claim_slot(builder_name: str)`: updates `self._builders[name] = replace(builder, active_builds=builder.active_builds + 1)`.
     - Implement `_release_slot(builder_name: str)`: updates `self._builders[name] = replace(builder, active_builds=max(0, builder.active_builds - 1))`.
     - In `submit_build`: when initial status is `"running"` (`builder.available_slots > 0`), call `_claim_slot(builder.name)`.
     - In `cancel_build` and `complete_build`: if previous status was `"running"`, call `_release_slot(builder_node)`.
     - In `submit_build` supersession loop: superseded running builds are cancelled, immediately releasing their concurrency slot so the replacement commit can claim it.

---

## 3. Caveats

1. **Mac Host Platform vs Linux Container Image**:
   - Apple Silicon macOS endpoints (such as `macbook-pro`, `air3`) report host OS `darwin/arm64`. However, container images built on them target `linux/arm64` (e.g. BuildKit running inside a Linux VM or container engine). `infer_native_platform` normalizes Darwin host targets to `linux/arm64` for container build outputs.
2. **Queued Build Auto-Promotion**:
   - When a concurrency slot is released during `cancel_build` or `complete_build`, `_release_slot` frees the slot. Active queue polling/promotion can also promote any queued build on that builder endpoint if an event loop is attached.
3. **Read-Only Scope Compliance**:
   - Per explorer constraints, no production files were modified. The specifications and diff patches below are ready for the Milestone 1 implementer.

---

## 4. Conclusion & Proposed Implementation Blueprint

### 4.1 Target File: `luma/paas/build/controller.py`

#### A. Imports & Constants
```python
from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, Mapping

from ...build_capabilities import (
    infer_native_platform,
    native_arm_build_platform,
    validate_native_platform,
)
from ...build_sources import validate_git_contexts_spec
from ...errors import LumaError
from ...service import slugify
from .artifact_store import BuildStepTiming

TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "canceled"}
```

#### B. Helper Methods on `BuildController`
```python
    def _claim_slot(self, builder_name: str) -> None:
        builder = self._builders.get(builder_name)
        if builder:
            self._builders[builder_name] = replace(
                builder,
                active_builds=builder.active_builds + 1,
            )

    def _release_slot(self, builder_name: str) -> None:
        builder = self._builders.get(builder_name)
        if builder and builder.active_builds > 0:
            self._builders[builder_name] = replace(
                builder,
                active_builds=builder.active_builds - 1,
            )
```

#### C. `select_native_builder` Implementation
```python
    def select_native_builder(
        self,
        platform: str = "",
        *,
        preferred_node: str = "",
        target_node: str = "",
        target_region: str = "",
    ) -> BuilderEndpoint:
        """Select a ready native BuilderEndpoint for the target platform (never QEMU)."""
        node_archs = {b.name: b.platform for b in self._builders.values()}

        # 1. Resolve and validate platform
        if platform and str(platform).strip():
            target_platform = validate_native_platform(platform)

            # Check explicit platform conflicts with target_region
            region = str(target_region or "").strip().lower()
            if region == "home" and target_platform != "linux/arm64":
                raise LumaError(
                    f"explicit platform {target_platform} conflicts with target region "
                    f"{target_region} (requires linux/arm64); cross-architecture emulation is disabled"
                )

            # Check explicit platform conflicts with target_node
            node = str(target_node or "").strip()
            if node:
                expected_arch = None
                if node in node_archs:
                    expected_arch = validate_native_platform(node_archs[node])
                elif node.lower() in {"macbook-pro", "air3"} or "arm64" in node.lower() or "aarch64" in node.lower():
                    expected_arch = "linux/arm64"

                if expected_arch and target_platform != expected_arch:
                    raise LumaError(
                        f"explicit platform {target_platform} conflicts with target node "
                        f"{target_node} ({expected_arch}); cross-architecture emulation is disabled"
                    )
        else:
            target_platform = infer_native_platform(
                platform="",
                target_node=target_node or preferred_node,
                target_region=target_region,
                node_architectures=node_archs,
            )

        # 2. Preferred node validation
        if preferred_node:
            candidate = self._builders.get(preferred_node)
            if candidate is None:
                raise LumaError(f"requested builder node is not registered: {preferred_node}")
            if not candidate.ready:
                raise LumaError(f"requested builder node is offline: {preferred_node}")
            candidate_platform = validate_native_platform(candidate.platform)
            if candidate_platform != target_platform:
                raise LumaError(
                    f"builder {preferred_node} ({candidate.platform}) does not match native target "
                    f"platform {target_platform}; cross-architecture emulation is disabled"
                )
            return candidate

        # 3. Target node validation if target_node is a registered builder
        if target_node and target_node in self._builders:
            target_b = self._builders[target_node]
            target_b_platform = validate_native_platform(target_b.platform)
            if target_b_platform != target_platform:
                raise LumaError(
                    f"target node {target_node} ({target_b.platform}) does not match native target "
                    f"platform {target_platform}; cross-architecture emulation is disabled"
                )

        # 4. Filter ready native builders matching target_platform (zero QEMU fallback)
        matching = [
            b
            for b in self._builders.values()
            if b.ready and validate_native_platform(b.platform) == target_platform
        ]
        if not matching:
            raise LumaError(
                f"no ready native BuildKit endpoint available for {target_platform}; "
                "register an online builder for this architecture"
            )

        # Prefer target_node if it is a matching builder, then max available slots, min cache used, name
        matching.sort(
            key=lambda b: (
                b.name != target_node,
                -b.available_slots,
                b.cache_used_bytes,
                b.name,
            )
        )
        return matching[0]
```

#### D. `submit_build` Implementation
```python
    def submit_build(
        self,
        *,
        project: str,
        repo_url: str,
        revision: str,
        branch: str = "main",
        platform: str = "",
        target_node: str = "",
        target_region: str = "",
        context_subpath: str = ".",
        dockerfile_path: str = "Dockerfile",
        build_spec: Mapping[str, Any] | None = None,
        preferred_builder: str = "",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """Compile and submit a Tekton PipelineRun and automatically supersede older branch runs."""
        project_slug = slugify(project)

        # Resolve target node and region from parameters or build_spec
        resolved_node = str(target_node or "").strip()
        resolved_region = str(target_region or "").strip()
        if isinstance(build_spec, Mapping):
            if not resolved_node:
                resolved_node = str(
                    build_spec.get("target_node")
                    or build_spec.get("targetNode")
                    or build_spec.get("node")
                    or (build_spec.get("placement") or {}).get("node")
                    or ""
                ).strip()
            if not resolved_region:
                resolved_region = str(
                    build_spec.get("target_region")
                    or build_spec.get("targetRegion")
                    or build_spec.get("region")
                    or (build_spec.get("placement") or {}).get("region")
                    or ""
                ).strip()

        builder = self.select_native_builder(
            platform,
            preferred_node=preferred_builder,
            target_node=resolved_node,
            target_region=resolved_region,
        )
        git_contexts = validate_git_contexts_spec(build_spec or {})

        replacement_key = f"{project_slug}:{branch}:{builder.platform}"
        if idempotency_key:
            for existing in self._pipeline_runs.values():
                if existing.get("idempotencyKey") == idempotency_key:
                    return existing

        now = int(time.time())
        superseded_ids: list[str] = []
        for run_id, run in list(self._pipeline_runs.items()):
            if (
                run.get("replacementKey") == replacement_key
                and run.get("status") in {"queued", "running"}
            ):
                self.cancel_build(run_id, reason=f"Superseded by newer commit on {branch}")
                superseded_ids.append(run_id)

        digest_seed = f"{project_slug}:{repo_url}:{revision}:{builder.platform}:{now}:{ len(self._pipeline_runs) + 1 }"
        short_id = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:12]
        run_name = f"bld-{project_slug}-{short_id}"
        arch_slug = builder.platform.replace("/", "-")
        image_repo = f"{self.registry_host}/{project_slug}"
        image_ref = f"{image_repo}:build-{short_id}"
        cache_ref = f"{image_repo}:cache-{arch_slug}"

        extra_flags = " ".join(
            f"--local context:{alias}=/workspace/source/contexts/{alias}"
            for alias in sorted(git_contexts)
        )

        pipeline_run_manifest = {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {
                "name": run_name,
                "namespace": self.namespace,
                "labels": {
                    "luma.io/project": project_slug,
                    "luma.io/branch": slugify(branch) or "main",
                    "luma.io/builder": builder.name,
                    "luma.io/platform": arch_slug,
                },
                "annotations": {
                    "luma.io/replacement-key": replacement_key,
                    "luma.io/idempotency-key": idempotency_key or run_name,
                },
            },
            "spec": {
                "pipelineRef": {"name": "luma-paas-build-pipeline"},
                "podTemplate": {
                    "terminationGracePeriodSeconds": 3,
                },
                "timeouts": {
                    "pipeline": "45m",
                    "tasks": "30m",
                    "finally": "3m",
                },
                "params": [
                    {"name": "repo-url", "value": repo_url},
                    {"name": "revision", "value": revision},
                    {"name": "buildkit-addr", "value": builder.address},
                    {"name": "platform", "value": builder.platform},
                    {"name": "context-subpath", "value": context_subpath},
                    {"name": "dockerfile-path", "value": dockerfile_path},
                    {"name": "image-ref", "value": image_ref},
                    {"name": "cache-ref", "value": cache_ref},
                    {"name": "git-contexts-json", "value": json.dumps(git_contexts, sort_keys=True)},
                    {"name": "extra-contexts-flags", "value": extra_flags},
                ],
                "workspaces": [
                    {
                        "name": "shared-source",
                        "EmptyDir": {},
                    }
                ],
            },
        }

        # Track active concurrency on builder (fresh lookup after superseded cancellations)
        current_builder = self._builders[builder.name]
        status = "running" if current_builder.available_slots > 0 else "queued"
        if status == "running":
            self._claim_slot(current_builder.name)

        record: Dict[str, Any] = {
            "buildRunId": run_name,
            "project": project_slug,
            "repoUrl": repo_url,
            "revision": revision,
            "branch": branch,
            "platform": builder.platform,
            "builderNode": builder.name,
            "builderAddress": builder.address,
            "imageRef": image_ref,
            "cacheRef": cache_ref,
            "gitContexts": git_contexts,
            "status": status,
            "targetNode": resolved_node,
            "targetRegion": resolved_region,
            "replacementKey": replacement_key,
            "idempotencyKey": idempotency_key or run_name,
            "supersededRuns": superseded_ids,
            "createdAt": now,
            "updatedAt": now,
            "pipelineRunManifest": pipeline_run_manifest,
        }
        self._pipeline_runs[run_name] = record
        return record
```

#### E. `cancel_build` and `complete_build` Concurrency Release
```python
    def cancel_build(self, build_run_id: str, *, reason: str = "Canceled by operator") -> Dict[str, Any]:
        """Hard-cancel a Tekton PipelineRun via CancelledRunFinally (<3s pod & BuildKit termination)."""
        record = self._pipeline_runs.get(build_run_id)
        if not isinstance(record, dict):
            raise LumaError(f"build run not found: {build_run_id}")
        current_status = str(record.get("status") or "")
        if current_status in TERMINAL_STATUSES:
            raise LumaError(
                f"cannot cancel build {build_run_id}: build is already in terminal state '{current_status}'"
            )
        now = int(time.time())
        if current_status == "running":
            self._release_slot(str(record.get("builderNode") or ""))

        manifest = record["pipelineRunManifest"]
        manifest.setdefault("spec", {})["status"] = "CancelledRunFinally"
        record.update(
            status="canceled",
            cancelReason=reason,
            canceledAt=now,
            completedAt=now,
            updatedAt=now,
            podTerminated=True,
            buildkitSessionAborted=True,
        )
        return record

    def complete_build(
        self,
        build_run_id: str,
        *,
        status: str = "succeeded",
        message: str = "",
    ) -> Dict[str, Any]:
        """Mark a build run complete (succeeded or failed) and release its builder concurrency slot."""
        record = self._pipeline_runs.get(build_run_id)
        if not isinstance(record, dict):
            raise LumaError(f"build run not found: {build_run_id}")
        current_status = str(record.get("status") or "")
        if current_status in TERMINAL_STATUSES:
            raise LumaError(
                f"cannot complete build {build_run_id}: build is already in terminal state '{current_status}'"
            )
        if status not in {"succeeded", "failed"}:
            raise LumaError(f"invalid completion status: {status}")

        now = int(time.time())
        if current_status == "running":
            self._release_slot(str(record.get("builderNode") or ""))

        record.update(
            status=status,
            message=message,
            completedAt=now,
            updatedAt=now,
        )
        return record
```

---

## 5. Verification Method

### 5.1 Test Execution Command
Run the PaaS engine unit test suite:
```bash
PATH="$PWD/.venv/bin:$PATH" ./.venv/bin/python -m unittest tests.test_paas_engine -v
```

### 5.2 Unit Test Assertions Blueprint
Add the following comprehensive unit test suite to `tests/test_paas_engine.py`:

```python
    def test_native_builder_selection_inference_and_concurrency(self) -> None:
        """Verify strict native builder selection, topology inference, conflict rejection, and concurrency."""
        # 1. Platform auto-inference without platform override
        # Target region == 'home' infers linux/arm64 -> macbook-pro
        b_home = self.builder.select_native_builder(target_region="home")
        self.assertEqual(b_home.name, "macbook-pro")
        self.assertEqual(b_home.platform, "linux/arm64")

        # Target node == 'macbook-pro' infers linux/arm64 -> macbook-pro
        b_node = self.builder.select_native_builder(target_node="macbook-pro")
        self.assertEqual(b_node.name, "macbook-pro")
        self.assertEqual(b_node.platform, "linux/arm64")

        # Target node == 'air3' infers linux/arm64 -> macbook-pro
        b_air3 = self.builder.select_native_builder(target_node="air3")
        self.assertEqual(b_air3.name, "macbook-pro")
        self.assertEqual(b_air3.platform, "linux/arm64")

        # No target specified defaults to linux/amd64 -> buildkit-amd64-0
        b_default = self.builder.select_native_builder()
        self.assertEqual(b_default.name, "buildkit-amd64-0")
        self.assertEqual(b_default.platform, "linux/amd64")

        # 2. Strict validation & rejection of multi-platform QEMU
        with self.assertRaisesRegex(LumaError, "cross-architecture emulation"):
            self.builder.select_native_builder("linux/amd64,linux/arm64")

        # 3. Explicit platform conflicts
        with self.assertRaisesRegex(LumaError, "conflicts with target region home"):
            self.builder.select_native_builder("linux/amd64", target_region="home")

        with self.assertRaisesRegex(LumaError, "conflicts with target node macbook-pro"):
            self.builder.select_native_builder("linux/amd64", target_node="macbook-pro")

        # 4. Preferred node validation
        b_pref = self.builder.select_native_builder("linux/arm64", preferred_node="macbook-pro")
        self.assertEqual(b_pref.name, "macbook-pro")

        with self.assertRaisesRegex(LumaError, "does not match native target platform"):
            self.builder.select_native_builder("linux/arm64", preferred_node="buildkit-amd64-0")

        with self.assertRaisesRegex(LumaError, "requested builder node is not registered"):
            self.builder.select_native_builder("linux/arm64", preferred_node="non-existent")

        # 5. Zero QEMU fallback: when no native builder exists, raise LumaError
        amd_only_builder = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[self.builders[0]],  # buildkit-amd64-0 only
        )
        with self.assertRaisesRegex(LumaError, "no ready native BuildKit endpoint available for linux/arm64"):
            amd_only_builder.select_native_builder("linux/arm64")

        # 6. submit_build with target_node and build_spec inference
        run_spec_region = self.builder.submit_build(
            project="home-app",
            repo_url="https://gcode.gaojiua.com/Signal/home-app.git",
            revision="1111222233334444555566667777888899990000",
            build_spec={"region": "home"},
        )
        self.assertEqual(run_spec_region["builderNode"], "macbook-pro")
        self.assertEqual(run_spec_region["platform"], "linux/arm64")
        self.assertEqual(run_spec_region["targetRegion"], "home")

        # Conflict rejection in submit_build
        with self.assertRaisesRegex(LumaError, "conflicts with target region home"):
            self.builder.submit_build(
                project="home-app",
                repo_url="https://gcode.gaojiua.com/Signal/home-app.git",
                revision="1111222233334444555566667777888899990000",
                platform="linux/amd64",
                build_spec={"region": "home"},
            )

        # 7. Active concurrency tracking on BuilderEndpoint
        mb = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 1)
        self.assertEqual(mb.available_slots, 3)

        # Cancel build and verify slot released
        self.builder.cancel_build(run_spec_region["buildRunId"])
        mb = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 0)
        self.assertEqual(mb.available_slots, 4)
```

### 5.3 Invalidation Conditions
This blueprint would be invalidated if:
1. Multi-platform fat manifest builds with QEMU cross-emulation were permitted in production (directly contradictory to Requirement R2).
2. The `home` region or `macbook-pro`/`air3` nodes were migrated from Apple Silicon ARM64 to AMD64 hardware.
3. `BuilderEndpoint` was modified from an immutable frozen dataclass to a mutable state machine outside `BuildController`.
