# Investigation & Implementation Blueprint: Tekton <3s Build Cancellation Guarantee & State Machine Lifecycle

- **Author**: Explorer M1-3
- **Target Subsystem**: PaaS Build Controller & Tekton Build Pipeline
- **Affected Files**:
  - `luma/paas/build/controller.py`
  - `deploy/paas-stack/tekton/luma-build-pipeline.yaml`
  - `tests/test_paas_engine.py`

---

## 1. Observation

### 1.1 `submit_build` Omits `podTemplate` and `timeouts`
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py` lines 264–285:
```python
264:             "spec": {
265:                 "pipelineRef": {"name": "luma-paas-build-pipeline"},
266:                 "params": [
267:                     {"name": "repo-url", "value": repo_url},
...
278:                 "workspaces": [
279:                     {
280:                         "name": "shared-source",
281:                         "EmptyDir": {},
282:                     }
283:                 ],
284:             },
```
- Direct observation: `pipeline_run_manifest["spec"]` defines only `pipelineRef`, `params`, and `workspaces`.
- Impact: Kubernetes defaults `terminationGracePeriodSeconds` to 30 seconds for task pods. Under cancellation, SIGTERM is followed by a 30s delay before SIGKILL, violating the `<3s` build cancellation SLA. Furthermore, missing `timeouts` leaves the PipelineRun vulnerable to hanging indefinitely without task, finally, or pipeline deadlines.

### 1.2 `luma-buildkit-build-push` Runs `buildctl` in Foreground Without Signal Traps
In `/Users/turning4th/infra-stacks/deploy/paas-stack/tekton/luma-build-pipeline.yaml` lines 78–101:
```yaml
78:       script: |
79:         #!/bin/sh
80:         set -eu
81:         SRC="$(workspaces.source.path)/primary/$(params.context-subpath)"
82:         DOCKERFILE="$(workspaces.source.path)/primary/$(params.dockerfile-path)"
83:         METADATA_FILE="/tmp/buildkit-metadata.json"
84: 
85:         buildctl --addr "$(params.buildkit-addr)" build \
...
96:           --progress=rawjson
97: 
98:         DIGEST="$(sed -n 's/.*"containerimage.digest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${METADATA_FILE}" | head -n 1)"
99:         printf "%s" "${DIGEST}" > "$(results.image-digest.path)"
100:         cat "${METADATA_FILE}" > "$(results.build-metadata-json.path)"
```
- Direct observation: `buildctl` runs synchronously in the foreground.
- Impact: When Tekton signals container termination via SIGTERM, the shell script process does not trap or forward the signal to `buildctl`. The remote BuildKit gRPC session is not promptly aborted, keeping remote daemon worker threads alive until hard container kill.

### 1.3 `luma-paas-build-pipeline` Lacks a `finally:` Block and Cleanup Task
In `/Users/turning4th/infra-stacks/deploy/paas-stack/tekton/luma-build-pipeline.yaml` lines 102–176:
- Direct observation: `luma-paas-build-pipeline` ends with task `buildkit-build-push`. It contains no `finally:` block. There is no `Task` resource named `luma-build-cleanup-finally`.
- Impact: When `spec.status: CancelledRunFinally` is applied, Tekton triggers the `finally:` block while terminating tasks. Because no `finally:` task exists, ephemeral sockets (`/tmp/*.sock`, `/run/buildkit/*.sock`) and cloned git workspaces (`primary`, `contexts`) are left unpurged on disk/volumes.

### 1.4 `cancel_build` Lacks Terminal State Validation
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py` lines 310–327:
```python
310:     def cancel_build(self, build_run_id: str, *, reason: str = "Canceled by operator") -> Dict[str, Any]:
311:         """Hard-cancel a Tekton PipelineRun via CancelledRunFinally (<3s pod & BuildKit termination)."""
312:         record = self._pipeline_runs.get(build_run_id)
313:         if not isinstance(record, dict):
314:             raise LumaError(f"build run not found: {build_run_id}")
315:         now = int(time.time())
316:         manifest = record["pipelineRunManifest"]
317:         manifest.setdefault("spec", {})["status"] = "CancelledRunFinally"
318:         record.update(
319:             status="canceled",
320:             cancelReason=reason,
321:             canceledAt=now,
322:             completedAt=now,
323:             updatedAt=now,
324:             podTerminated=True,
325:             buildkitSessionAborted=True,
326:         )
327:         return record
```
- Direct observation: `cancel_build` checks only that `record` exists. If `record["status"]` is already `"succeeded"`, `"failed"`, or `"canceled"`, `cancel_build` unconditionally mutates the record back to `"canceled"` and overwrites `completedAt` and `updatedAt`.
- Impact: Violates build state machine integrity, allowing already completed or failed builds to be overwritten as canceled.

### 1.5 Builder Concurrency Slots Are Never Tracked or Released
In `/Users/turning4th/infra-stacks/luma/paas/build/controller.py` lines 27–45 and 299:
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
- Direct observation: In `submit_build` (line 299), `status = "running" if builder.available_slots > 0 else "queued"`, but `builder.active_builds` is never incremented. In `cancel_build` (and there is no `complete_build`), active concurrency is never released.
- Impact: `available_slots` never reflects real-time concurrency. When builds complete or are canceled, slots are neither tracked nor freed.

---

## 2. Logic Chain

1. **Guaranteeing `<3s` Pod Termination**:
   - Observations 1.1 & 1.2 demonstrate that Kubernetes default 30s grace period and untrapped shell process trees cause delays.
   - Injecting `"podTemplate": {"terminationGracePeriodSeconds": 3}` into `pipeline_run_manifest["spec"]` mandates that kubelet sends SIGKILL exactly 3 seconds after SIGTERM if the pod has not already exited.
   - Injecting `"timeouts": {"pipeline": "45m", "tasks": "30m", "finally": "3m"}` bounds the entire build lifecycle, ensuring cleanup tasks receive up to 3 minutes while regular tasks cannot run past 30 minutes.

2. **Immediate Signal Forwarding to BuildKit**:
   - In container execution, Tekton's entrypoint executes `/bin/sh`. When SIGTERM is sent to the container, running `buildctl` in the background with `$!` PID tracking and attaching `trap 'kill -TERM ${BUILDCTL_PID} 2>/dev/null || true' TERM INT` immediately forwards the signal to `buildctl`.
   - `buildctl` immediately aborts its active gRPC session to `buildkit-addr` and releases server-side resources within milliseconds.

3. **Ephemeral Resource Finalization**:
   - Setting `spec.status: CancelledRunFinally` instructs the Tekton controller to skip subsequent normal tasks and invoke all tasks in `spec.finally`.
   - Adding Task `luma-build-cleanup-finally` to `deploy/paas-stack/tekton/luma-build-pipeline.yaml` and referencing it in `luma-paas-build-pipeline.spec.finally` ensures that `${WORKSPACE}/primary`, `${WORKSPACE}/contexts`, and `/tmp/*.sock` are unconditionally cleaned up on cancellation, failure, or normal completion.

4. **Terminal State Integrity in `cancel_build`**:
   - Builds follow a strict lifecycle: `{"queued", "running"} -> {"succeeded", "failed", "canceled"}`.
   - When `cancel_build` is called on a run where `record["status"] in {"succeeded", "failed", "canceled"}`, raising `LumaError(f"cannot cancel build {build_run_id}: build is already in terminal state '{current_status}'")` prevents illegal state transitions and preserves audit records.

5. **Concurrency Slot Lifecycle Management**:
   - Because `BuilderEndpoint` is a `@dataclass(frozen=True)`, mutations must use `dataclasses.replace` to produce an updated endpoint stored in `self._builders[builder_name]`.
   - When a build is submitted and `status == "running"`, the controller calls `_claim_slot(builder.name)` (`active_builds += 1`).
   - When a build is cancelled via `cancel_build` (or completed via `complete_build`), if its previous status was `"running"`, the controller calls `_release_slot(builder_name)` (`active_builds = max(0, active_builds - 1)`).
   - If a build was `"queued"`, it never held an active slot, so cancellation does not decrement `active_builds`.

---

## 3. Caveats

1. **In-Cluster vs Remote BuildKit Daemons**:
   - Trapping SIGTERM and relaying to `buildctl` terminates the client-side session immediately. The remote daemon (`macbook-pro` or cluster `buildkitd`) terminates the ongoing build execution graph upon connection close.
2. **Queued Run Resumption**:
   - Releasing a concurrency slot increases `builder.available_slots`. Promoting queued runs can be performed on subsequent submissions or an explicit queue pump if scheduled; this scope focuses on slot release upon completion/cancellation.
3. **Immutability of `BuilderEndpoint`**:
   - `BuilderEndpoint` remains `frozen=True` to preserve thread-safety and immutable dataclass semantics. Updates must always be applied via `self._builders[name] = replace(builder, active_builds=...)`.

---

## 4. Conclusion & Implementation Blueprint

### 4.1 Target Code Changes: `luma/paas/build/controller.py`

#### A. Dataclass imports & Helper methods
```python
# In luma/paas/build/controller.py
from dataclasses import dataclass, replace
...
TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "canceled"}
```

Add slot management helpers to `BuildController`:
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

#### B. `submit_build` Changes
1. Inject `podTemplate` and `timeouts` into `pipeline_run_manifest["spec"]`:
```python
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
```
2. Claim active slot if initial status is `"running"`:
```python
        initial_status = "running" if builder.available_slots > 0 else "queued"
        if initial_status == "running":
            self._claim_slot(builder.name)

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
            "status": initial_status,
            "replacementKey": replacement_key,
            "idempotencyKey": idempotency_key or run_name,
            "supersededRuns": superseded_ids,
            "createdAt": now,
            "updatedAt": now,
            "pipelineRunManifest": pipeline_run_manifest,
        }
```

#### C. `cancel_build` Changes
Validate terminal state and release active concurrency slot:
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
```

#### D. `complete_build` Addition
Support marking builds as succeeded or failed while releasing slots:
```python
    def complete_build(
        self,
        build_run_id: str,
        *,
        status: str = "succeeded",
        error: str = "",
    ) -> Dict[str, Any]:
        """Mark a build run as completed (succeeded or failed) and release its builder concurrency slot."""
        record = self._pipeline_runs.get(build_run_id)
        if not isinstance(record, dict):
            raise LumaError(f"build run not found: {build_run_id}")
        current_status = str(record.get("status") or "")
        if current_status in TERMINAL_STATUSES:
            raise LumaError(
                f"cannot complete build {build_run_id}: build is already in terminal state '{current_status}'"
            )
        if status not in {"succeeded", "failed"}:
            raise LumaError(f"invalid completion status: {status} (must be 'succeeded' or 'failed')")

        now = int(time.time())
        if current_status == "running":
            self._release_slot(str(record.get("builderNode") or ""))

        record.update(
            status=status,
            completedAt=now,
            updatedAt=now,
        )
        if error:
            record["error"] = error
        return record
```

---

### 4.2 Target YAML Changes: `deploy/paas-stack/tekton/luma-build-pipeline.yaml`

#### A. Signal Trapping in `luma-buildkit-build-push` Step
In `luma-buildkit-build-push` Task (lines 76–101):
```yaml
    - name: build-and-push
      image: moby/buildkit:v0.16.0
      script: |
        #!/bin/sh
        set -eu
        SRC="$(workspaces.source.path)/primary/$(params.context-subpath)"
        DOCKERFILE="$(workspaces.source.path)/primary/$(params.dockerfile-path)"
        METADATA_FILE="/tmp/buildkit-metadata.json"

        buildctl --addr "$(params.buildkit-addr)" build \
          --frontend dockerfile.v0 \
          --local context="${SRC}" \
          --local dockerfile="$(dirname "${DOCKERFILE}")" \
          --opt filename="$(basename "${DOCKERFILE}")" \
          --opt platform="$(params.platform)" \
          $(params.extra-contexts-flags) \
          --export-cache "type=registry,ref=$(params.cache-ref),mode=max,registry.insecure=true" \
          --import-cache "type=registry,ref=$(params.cache-ref),registry.insecure=true" \
          --output "type=image,name=$(params.image-ref),push=true,registry.insecure=true" \
          --metadata-file "${METADATA_FILE}" \
          --progress=rawjson &
        BUILDCTL_PID=$!
        trap 'kill -TERM ${BUILDCTL_PID} 2>/dev/null || true' TERM INT
        wait ${BUILDCTL_PID}
        trap - TERM INT

        DIGEST="$(sed -n 's/.*"containerimage.digest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${METADATA_FILE}" | head -n 1)"
        printf "%s" "${DIGEST}" > "$(results.image-digest.path)"
        cat "${METADATA_FILE}" > "$(results.build-metadata-json.path)"
```

#### B. Adding `luma-build-cleanup-finally` Task Resource
Append before Pipeline definition:
```yaml
---
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: luma-build-cleanup-finally
  namespace: luma-builds
spec:
  params:
    - name: buildkit-addr
      type: string
      default: ""
  workspaces:
    - name: source
  steps:
    - name: cleanup-ephemeral-state
      image: moby/buildkit:v0.16.0
      script: |
        #!/bin/sh
        set +e
        echo "Running finally cleanup for ephemeral buildkit sessions and workspaces..."
        WS="$(workspaces.source.path)"
        if [ -n "${WS}" ] && [ -d "${WS}" ]; then
          rm -rf "${WS}/primary" "${WS}/contexts" 2>/dev/null || true
        fi
        rm -rf /tmp/buildkit-* /tmp/buildctl* /tmp/*.sock /run/buildkit/*.sock 2>/dev/null || true
        exit 0
```

#### C. Adding `finally:` Block to `luma-paas-build-pipeline`
In `luma-paas-build-pipeline` Pipeline definition:
```yaml
  finally:
    - name: build-cleanup-finally
      taskRef:
        name: luma-build-cleanup-finally
      params:
        - name: buildkit-addr
          value: $(params.buildkit-addr)
      workspaces:
        - name: source
          workspace: shared-source
```

---

## 5. Verification Method

### 5.1 Automated Unit Test Assertions
In `tests/test_paas_engine.py`, add the test method `test_tekton_cancellation_guarantee_and_state_machine`:

```python
    def test_tekton_cancellation_guarantee_and_state_machine(self) -> None:
        import yaml
        from pathlib import Path

        # 1. Verify PipelineRun manifest injection (<3s SLA + timeouts)
        run = self.builder.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            branch="main",
            platform="linux/arm64",
        )
        spec = run["pipelineRunManifest"]["spec"]
        self.assertEqual(
            spec.get("podTemplate"),
            {"terminationGracePeriodSeconds": 3},
            "spec.podTemplate must enforce terminationGracePeriodSeconds: 3",
        )
        self.assertEqual(
            spec.get("timeouts"),
            {"pipeline": "45m", "tasks": "30m", "finally": "3m"},
            "spec.timeouts must specify pipeline, tasks, and finally timeouts",
        )

        # 2. Concurrency slot tracking: Builder active_builds was incremented
        macbook = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(macbook.active_builds, 1)

        # 3. Successful cancellation releases concurrency slot
        cancelled = self.builder.cancel_build(run["buildRunId"])
        self.assertEqual(cancelled["status"], "canceled")
        self.assertEqual(cancelled["pipelineRunManifest"]["spec"]["status"], "CancelledRunFinally")
        macbook_after = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(macbook_after.active_builds, 0)

        # 4. State machine: Attempting to cancel an already canceled build raises LumaError
        with self.assertRaises(LumaError) as cm:
            self.builder.cancel_build(run["buildRunId"])
        self.assertIn("already in terminal state 'canceled'", str(cm.exception))

        # 5. Complete build releases slot and prevents subsequent cancellation
        run2 = self.builder.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="b2c3d4e5f60718293a4b5c6d7e8f901234567890",
            branch="feature",
            platform="linux/arm64",
        )
        macbook_busy = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(macbook_busy.active_builds, 1)
        completed = self.builder.complete_build(run2["buildRunId"], status="succeeded")
        self.assertEqual(completed["status"], "succeeded")
        macbook_free = next(b for b in self.builder.list_builders() if b.name == "macbook-pro")
        self.assertEqual(macbook_free.active_builds, 0)

        with self.assertRaises(LumaError) as cm2:
            self.builder.cancel_build(run2["buildRunId"])
        self.assertIn("already in terminal state 'succeeded'", str(cm2.exception))

        # 6. Verify Tekton YAML structure (signal trapping and finally cleanup task)
        tekton_yaml_path = Path("deploy/paas-stack/tekton/luma-build-pipeline.yaml")
        self.assertTrue(tekton_yaml_path.exists())
        with open(tekton_yaml_path, "r", encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))

        tasks = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Task"}
        pipelines = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Pipeline"}

        # Task: buildkit build-push must trap SIGTERM
        build_task = tasks["luma-buildkit-build-push"]
        build_script = build_task["spec"]["steps"][0]["script"]
        self.assertIn("trap 'kill -TERM", build_script)
        self.assertIn("wait", build_script)

        # Task: cleanup finally task exists
        self.assertIn("luma-build-cleanup-finally", tasks)
        cleanup_script = tasks["luma-build-cleanup-finally"]["spec"]["steps"][0]["script"]
        self.assertIn("/tmp/*.sock", cleanup_script)
        self.assertIn("primary", cleanup_script)

        # Pipeline: references cleanup task in finally
        pipeline = pipelines["luma-paas-build-pipeline"]
        finally_tasks = pipeline["spec"].get("finally", [])
        self.assertTrue(any(t["name"] == "build-cleanup-finally" for t in finally_tasks))
```

### 5.2 Verification Commands
Run unit tests:
```bash
./.venv/bin/python -m unittest tests.test_paas_engine -v
```

Full repository gate:
```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

### 5.3 Invalidation Conditions
- If Tekton controller changes `CancelledRunFinally` naming or status semantics.
- If `podTemplate.terminationGracePeriodSeconds` is overridden by a cluster-wide Tekton ConfigMap with an immutable lower bound.
