# Durable build platform {#build-platform}

The optional repository-build backend runs entirely inside your Luma infrastructure. Control remains the authenticated CLI/dashboard entry point. Temporal owns durable orchestration, Nomad places native build tasks, BuildKit creates images, PostgreSQL owns build metadata, Harbor stores OCI artifacts and caches, and S3-compatible storage holds source snapshots and log chunks. External CI can trigger Luma; it does not execute these builds.

This implementation is opt-in. Source-level checks are not production acceptance or evidence of ten-build throughput. Validate the deployment below before changing Control's backend.

## Ownership and recovery {#ownership}

| Fact | Authority |
| --- | --- |
| Accepted request, capacity, attempts, artifact, release generation | PostgreSQL |
| Activity retries, timers, worker recovery | Temporal |
| Actual executor termination | Nomad allocation state |
| Immutable image content | Harbor OCI digest |
| Source archive and acknowledged log bytes | Object storage |
| Combined old/new history | Rebuildable Control SQLite projection |

Admission and the workflow-start outbox commit together. Workflow and Nomad job IDs are stable. Nomad registration uses compare-and-set. Lost responses are reconciled before resubmission. Activity heartbeats allow a replacement worker to resume after a failed worker; recovery still depends on reachable dependencies.

A branch is resolved to a full commit when source preparation starts. The SHA is persisted before upload; subsequent preparation retries clone that SHA. Build retries also keep that SHA. To build a newer branch revision, submit a new request.

Each logical build runs its native platform tasks sequentially, releasing its execution slot between platforms. Each task owns a BuildKit daemon, CPU/memory allocation, and ephemeral-disk reservation. No host Docker socket is mounted. Registry cache exports have unique attempt tags; imports are scoped by recipe and native architecture. Dockerfile `RUN --mount=type=cache` contents are not transferred by the registry layer-cache exporter.

Build success records an immutable artifact before release. Release retries reuse that artifact. Target generations follow request admission order, so an older build finishing later cannot overwrite a newer applied release. A durable Nomad modify-index baseline also prevents an interrupted release from overwriting an intervening manual deployment. Control still serializes publication side effects such as DNS and ingress; build execution is independent of that lock.

Cancellation is a PostgreSQL command observed by the workflow. Capacity is released only after Nomad confirms all attempts terminated. Unknown/lost allocations remain canceling and hold capacity. During publication, cancellation is rejected; wait for the release result and use the existing rollback workflow. Source preparation cancellation waits for the current bounded clone/upload activity to return.

## Installation {#installation}

Run the service image with Python 3.13; the existing CLI and agents retain Python 3.9 compatibility. Optional dependencies are in the `build-platform` extra. Build both new images, publish the executor for amd64 and arm64, and pin deployed images by digest.

```sh
docker build -f Dockerfile.build-platform -t luma-build-platform:review .
docker build -f Dockerfile.build-executor -t luma-build-executor:review .
python -m luma.build_platform migrate
python -m luma.build_platform serve
python -m luma.build_platform worker
```

The last three commands need `deploy/build-platform/platform.env.example` configured in the process environment. Generate the Fernet encryption key once and back it up with PostgreSQL. Losing it makes stored deployment-secret overrides unrecoverable. The API token is internal; users continue using their existing Luma management token.

Provision a private PostgreSQL database, a self-hosted Temporal namespace, an S3 bucket, and a Harbor project. `deploy/build-platform/compose.yml` runs the Luma API and worker against these dependencies; it does not silently install databases or replace the existing registry. Use Temporal's supported server/schema tooling and Harbor's official installer for their lifecycles. Keep API, worker, schema, and executor releases coordinated; existing Temporal histories must remain replay compatible.

Nomad requirements:

- Explicitly enroll Linux build nodes through Control as described below. `LUMA_BUILD_POOL` is the actual Nomad NodePool (default `default`); selected clients must belong to it. No custom build-pool metadata tag is required.
- Enable privileged Docker tasks only on that trusted build pool. The executor runs BuildKit with container isolation inside a dedicated task. This is not a hostile multi-tenant sandbox.
- Create the `builds` namespace. The worker ACL example is `deploy/build-platform/nomad-worker-policy.hcl`. Nomad workload identity and Variables must be enabled; each task reads its bootstrap token from its job/group/task variable path.
- Make the pinned executor image pullable by the build clients before accepting builds, using client-side registry authentication or a preloaded image. Build robot credentials are leased after the task starts and cannot authenticate that initial pull.
- Route the build API and object-storage endpoint from every build client. The source URL expires after 15 minutes and is issued at lease time.

Use separate Harbor robots for build push/tag access and runtime pull-only access. Never give executor leases Harbor system-administrator credentials. The initial Harbor project must already exist. The service's optional GC endpoint requires credentials with the corresponding Harbor management permissions and defaults to dry-run; do not broaden the build robot to obtain those permissions.

## Control cutover {#cutover}

On Control configure `LUMA_BUILD_PLATFORM_URL`, `LUMA_BUILD_API_TOKEN`, `LUMA_BUILD_REGISTRY_URL`, and the two `LUMA_BUILD_REGISTRY_PULL_*` variables. `LUMA_BUILD_EGRESS_PROXY` supplies the pool's egress proxy; `proxyMode=direct` disables it per request. Repository imports require HTTPS clone URLs without inline credentials. Existing Git provider tokens are fetched only while preparing source.

When the URL is nonempty, new repository imports use the pool and default to automatic placement. An explicitly selected build node must be on the allowed list and match the native target. A single pinned node cannot build both architectures. Registry overrides must match the configured Harbor host. CLI streaming responses can return an accepted run ID; the CLI follows paginated events, and the dashboard opens its persistent history record. New-backend outages fail closed.

Previously accepted legacy builds keep their original backend. Existing local builds continue on the caller's machine and use the existing upload/publish API. Their workflow is not converted into a remote repository build. Old registry inventory/deletion controls still manage the old registry; the new Harbor has its own inventory and lifecycle. Keep the old registry online while deployments or rollback revisions reference its digests.

The build service exposes authenticated `/health`, `/v1/builds`, `/v1/builds/{id}`, `/v1/builds/{id}/cancel`, `/v1/builds/{id}/retry`, `/v1/registry/inventory`, and `/v1/registry/gc`. These are internal service endpoints. Application users continue through Control. A caller may supply `idempotencyKey` to repository admission: replaying the same key/body returns the same run; reusing it for different content fails. Use `autoDeploy: false` for artifact-only builds.

Set `LUMA_BUILD_PLATFORM_ENABLED=0` to return future admissions to the legacy backend. Keep the URL, build service and worker until accepted jobs finish and for new-run detail/action access; retain pull credentials for existing Harbor deployments. Do not downgrade a database whose schema is newer than the service. To rebuild Control history after restoring its SQLite backup, reset delivered `project` outbox rows in PostgreSQL and let the worker repopulate them; build logs stay in object storage.

## Select build nodes and native placement {#native-placement}

In the repository import page, expand **Configure build nodes**, select the registered Linux nodes, and save. The list is shared by CLI and dashboard requests. Its API is `POST /v1/builds/config` with a `nodes` array. Only this explicit list grants admission; legacy default-node and builder-role labels do not add nodes to the native pool. The CLI replaces the same list:

```sh
luma build config --node build-amd64-1 --node build-arm64-1
```

To pause new assignments, clear the list:

```sh
luma build config --clear-nodes
```

The worker filters the enrolled nodes by native architecture, live Nomad readiness/eligibility, healthy Docker driver, NodePool and resource requirements. Jobs carry a hard allowlist of Nomad node IDs and a CPU architecture constraint. The executor lease checks the latest enrollment list, and the executor compares its container and Nomad-host architecture with the target before downloading source. A removed node cannot obtain a new build lease; a task already submitted there may fail at startup rather than return to the queue. Already started builds continue. Choosing a node is authorization, not a benchmark guarantee: select hardware suitable for the workloads.

Deployment target architecture comes from registered identity even while a node is offline. Unknown architecture fails explicitly; it never defaults to amd64. Requests without a matching ready native builder remain queued without holding an execution slot. Source preparation has a separate bound of two concurrent checkouts per orchestration worker. At most one unplaced task per architecture holds a temporary execution reservation; its default placement deadline is 60 seconds (`LUMA_BUILD_PLACEMENT_TIMEOUT`). Once an allocation has a node, cold image pulls/bootstrap have a separate 600-second startup budget (`LUMA_BUILD_STARTUP_TIMEOUT`). Failed placement is stopped and confirmed before capacity is returned. Retry after correcting availability or configuration. The default ten slots remain a global cap.

Overlapping activity recovery is serialized per build through a PostgreSQL advisory lock. Attempts distinguish preparation, uncertain submission and confirmed submission. A create-only stopped job fences an uncertain initial submission before cancellation can release its slot; unknown running allocations continue to hold capacity.

## Capacity and retention {#capacity}

The defaults are ten active logical builds, one hundred additional outstanding requests, and ten per project. All worker replicas share PostgreSQL capacity accounting. Nomad additionally reserves actual CPU and memory. A default executor requests 2,000 MHz with Docker hard CPU limiting, 4 GiB memory, and 8 GiB ephemeral disk. CPU MHz is not a count of dedicated cores. Disk reservation is not a hard filesystem quota. Ten heavyweight builds therefore require an appropriately sized pool; changing the limit does not create hardware capacity. Multiple native targets consume one logical slot sequentially.

Harbor owns online GC and tag-retention mechanisms. Build tags are `build-bld2-*`; cache tags are per attempt. Initial deployment does not automatically delete artifacts. Before enabling retention, inventory live and rollback references from both Control backends, protect those digests, and preview Harbor's policy. Removing tags without retaining live/rollback digests can break replacement allocations even while existing containers still run. Do not run Luma's legacy offline registry GC against Harbor. Configure bucket lifecycle separately, preserving source/log objects for the required history period. Back up PostgreSQL, its encryption key, Temporal persistence, and artifact storage together.

## Acceptance {#acceptance}

After installing an isolated environment, validate: service and Compose imports; native amd64/arm64 manifests; Dockerfile ignores and dependency-cache reuse; ten concurrent builds plus a full queue; cancellation while queued/running; worker termination after Nomad submission; lost registry/Control replies; object-store outages; Control restart; a slow old build finishing after a newer release; artifact-only execution; release-only retry; manual deployment during retry; local-build compatibility; and Harbor retention with live/rollback images. Measure admission/read latency, queue time, build time, cache hits, memory, disk growth, and unacknowledged-log loss separately.

The unit checks do not establish these runtime or capacity results. Production cutover and automatic deletion remain separate operational steps.

References: [Temporal self-hosting](https://docs.temporal.io/self-hosted-guide), [Nomad Variables](https://developer.hashicorp.com/nomad/api-docs/variables), [BuildKit](https://github.com/moby/buildkit), [Harbor documentation](https://goharbor.io/docs/).
