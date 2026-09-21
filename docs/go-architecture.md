# Luma Go architecture and migration

Luma is moving to Go as a modular monolith. The public concepts remain service,
node, region, exposure and egress. Nomad remains the orchestrator and the React
Dashboard remains the UI. This change does not introduce a new database,
distributed coordinator, microservice fleet, or a second product API.

## Updated storage decision

The target Go product has no NFS capability. Persistent services use storage on
their deployment node. Storage does not select a separate server or storage class.
Existing data binds a deployment to its owner node; scheduling elsewhere must not
create an apparently successful service with an empty disk. Node changes require
an explicit data migration. Python's existing NFS implementation is retained only
as a migration reference while Go Control is unfinished, and is a remaining
retirement item, not a supported target feature. The complete scope is Go CLI,
Agent and Control plus a comprehensive migration guide.

Removing the capability does not imply copying application data. Inspect running
jobs, actual container mounts, volume driver options and backing filesystems first.
Services already on local disks retain their node, source path/volume name and
permissions. Only verified NFS consumers require the conditional data migration
procedure in [the migration guide](go-migration.md). Unknown mount evidence must
remain explicitly unresolved; unused classes or advertised Agent capabilities are
not proof of a runtime dependency.

## Ownership and dependency direction

Business modules own their rules and the interfaces they consume. Adapters own
external protocols. Executable entry points compose them. A module must never
import an HTTP handler, CLI command, database driver, or another module's private
implementation to complete a business operation.

```text
cmd/luma                  CLI process entry point
  internal/cli            argument parsing, local input, composition, output
    internal/deployment   service/Compose models, preview, submission, workflow rules
    internal/workflow     portable recipe grammar, matching, revisions and success evidence
      internal/region     placement names and exposure policy
      internal/storage    node-local volume validation and placement policy
    internal/adapters/
      manifest            YAML -> typed service/Compose and configuration
      nomad               typed deployment -> Nomad job and Traefik artifacts
      controlhttp         deployment/workflow ports, cluster inventory reads
      contexts            existing CLI context files, atomic private writes
      envfile             literal dotenv/user-config parsing and secret filtering
      repository          local Git origin metadata, with URL credentials removed

cmd/luma-agent            agent process entry point and private process supervisor
  internal/agentapp       explicit executor registration and lifecycle composition
    internal/node         leases, cancellation, terminal receipts, capability dispatch
    internal/registry     image identity and permitted mirror/cache destinations
    internal/observability typed container resource projections
    internal/adapters/
      agentstate          private config, single-instance lock, durable receipts, scratch
      controlhttp         separate node-token client for agent protocol
      hostexec            confined volume directories and Docker volume removal
      hostadmin           Docker daemon config, activation and recovery
      imageexec           Docker resolve/pull and crane mirror/cache operations
      process             bounded output, redaction, process groups, parent-death cleanup
      hostmetrics         independent host and container sampling

cmd/luma-control          migration Control entry point
  internal/controlapp     composition, loopback listener, graceful shutdown
    internal/credentials  management and node-agent principal validation
    internal/node         serial leasing, progress, terminal and restart policy
    internal/history      bounded history queries, portable cursors, projections
    internal/build        project queue-position policy (execution pending)
    internal/workflow     workflow catalog queries and transactional updates
    internal/adapters/
      controlstore        existing SQLite schema, identity checks, read/write transactions
      controlserver       HTTP authentication, routing, bounded requests, JSON errors
```

The arrows show dependencies. Adapters may import the business types whose ports
they implement; business packages cannot import adapters. `tests/goarchitecture`
checks these boundaries using Go's source parser. Go's compiler prevents import
cycles. No empty packages or placeholder executables are created for future work.

The current Go CLI is intentionally named `luma-go` in build artifacts so it can
be evaluated alongside the installed Python `luma`. It never launches Python as
an implicit fallback. `cmd/luma` is the future public executable entry point.

## Business module boundaries

| Module | Owns | Does not own |
| --- | --- | --- |
| deployment | Service and Compose models, plans, deployment state transitions, rollout identity, workflow guards | HTTP handlers, database connection, process execution |
| region | Region names, exposure policy, later region catalog use cases | Node processes, ingress configuration files |
| build | Project queue-position projection; durable queue, target replacement, cancellation fences, execution leases and restart recovery remain to migrate | Docker command execution or HTTP request lifetime |
| node | Agent dispatch, capabilities, busy heartbeat, task cancellation and durable terminal receipts; inventory authority remains on Control | Builder policy or registry retention policy |
| credentials | Management identity validation, scoped secret policy and literal substitution; remaining principal separation and credential leases still need migration | Rendering, cluster placement |
| history | Query validation, bounded pages, portable cursors and build/deployment projections | Database connections, queue mutation or HTTP routing |
| workflow | Explicit recipe grammar, repository matching, comparison, revision checks and separate success evidence | CLI execution, environment-file contents, Git clone or build execution |
| registry | Image identity and permitted cache destinations; retained references and deletion/GC plans remain on Python Control | Agent transport or generic deployment state |
| storage | Node-local volume validation; data ownership, node binding and explicit relocation policy | Remote filesystems, NFS exports, Registry blob deletion |
| observability | Container resource projections; logs, diagnostics and terminal lifecycle remain to migrate | Orchestration writes or raw application alert evaluation |

When these modules land, an application coordinator can compose their public
interfaces. Avoid a generic `manager`, `utils`, global state dictionary, or
cross-module imports of private functions. Keep protocols versioned at boundaries;
avoid exposing a Nomad job as the domain model for every Luma operation.

## Process boundaries

The target has three executables with one Go module and shared business packages:

- `luma`: CLI, local validation, rendering, API client and host installation commands.
- `luma-control`: HTTP/WebSocket transport, application composition, workers,
  local SQLite repository and read projections.
- `luma-agent`: reverse task leasing, process supervision, host adapters and
  task-specific executors. Builder remains a capability of the agent.

All three entry points have runnable slices. `cmd/luma-control` currently serves
history and an explicit Agent task transaction mode from a restored SQLite
directory. It cannot yet replace production Control. Terminal supervision and
process-group cancellation belong to the process adapter; queue cancellation and
remote-child fencing belong to build policy.

SQLite remains authoritative on one Manager. Each future repository owns a
bounded set of records and transaction operations. Do not recreate a `load all
state -> modify arbitrary dictionary -> save all state` interface in Go.
Transactions cannot span network calls. Schema compatibility, authority markers,
WAL backup and restore are a separate migration gate.

## Implemented deployment and CLI slices

The Go implementation covers single-service YAML validation, all six exposure
modes, Nomad task/network/service generation, resource reservations and ceilings,
health checks, named/bind mounts, relay routes, and Cloudflare tunnel sidecars.
It preserves `${SECRET}` references in previews and never reads deployment
secrets from the developer's process environment during compilation.

Supported commands are `validate`, `render`, `deploy --dry-run`, `deploy`,
`compose validate/render/deploy`, `workflow list/show/record/run`, `login`,
`context list/use`, `secret list/set/import/remove`, `registry list/login/remove`,
`import`, `build list/logs/cancel/retry/config/local`, `status`, and `version`.
Workflow replay supports image deployments, Compose deployments, remote repository
imports, local working-tree builds and retries.
`status` and workflow inspection use a JSON view
even in text mode. Preview output includes the artifacts and warnings; its
envelope is documented here and is not yet a byte-for-byte replacement for the
Python CLI output. Nomad JSON and parsed route YAML are compared structurally.

Online deployment submits the **original manifest** to the configured Control.
During migration this can be Python Control or the Go Control integrations listed
below; Go does not invoke Python. Control owns storage placement, credentials,
runtime configuration and rollout status. Remaining integrations are still gated
on Go Control. The CLI preview is an offline artifact preview, not proof that the exact preview artifact
can be applied directly to a live cluster. Build-only manifests still need the
existing build workflow to produce an image before deployment.

Before submission, the application checks the saved deployment workflow. A
changed workflow requires `--accept-workflow-change`, matching the existing
product rule. After success it records the workflow revision. A recording failure
is reported as a warning without misrepresenting a completed deployment as failed.
Recipes retain repository-relative manifest/config/env paths, the sanitized local
Git origin, timeout, engine, DNS options and optional workflow note. They never
contain connection credentials or env-file values. `workflow run --path CHECKOUT`
resolves paths in that checkout without changing the process working directory.
It validates the application name and locks the current Control connection;
neither a stored recipe nor its local env file can redirect those credentials.

Online commands load `.env` (or `--env-file FILE`) and `~/.luma.config.json`
(or `LUMA_USER_CONFIG`), with process values taking precedence. `--no-env`
disables those files. Values remain inside the command's environment view and
are never exported to the Go process. Local render/validate and native dry-run
do not load connection files. `--env FILE` is separate: it sends only keys
referenced as `${NAME}` in the original deployment source(s), without shell
evaluation or interpolation. Previews retain placeholders and never include
env-file values. A missing optional connection env file is allowed; a missing
explicit deployment secrets file fails before submission.

Compose services become tasks in one group, retaining YAML service order and
`service:127.0.0.1` sibling addresses. The compiler covers bridge/host networking,
all five Compose-supported exposures, health-check rollout gates, stop grace
periods, resources, local/bind/named mounts,
and namespaced ingress routes. Stateful or fixed-port groups never get canaries.
Mixed host/bridge modes, conflicting node/region pins, invalid local paths and
unsupported tunnel exposure fail validation. Ambiguous normalized service/port
names are also rejected. Compose remains the existing Luma subset of Docker
Compose: for example `entrypoint`, `ports`, `depends_on`, and `env_file` are not
translated by the existing renderer. The Go preview reports warnings for these
fields instead of suggesting they were applied.

`compose validate`, `compose render` and `compose deploy --dry-run` are offline.
No storage-class catalog or storage-node inventory is fetched. Volume declarations
use local paths or Docker named volumes on the deployment node. Legacy NFS
`storageClasses`, volume `storageClass/path`, native `storage` metadata and custom
Compose volume driver options fail with an explicit migration error. They are never
silently converted to empty local storage. Local-path previews require an explicit
deployment node; Go Control resolves online node ownership from stored bindings
and current Nomad placement. Online requests retain both original YAML texts.

`render` and `compose render` print raw Nomad JSON. The structured `validate` and
dry-run outputs include **all** job and route artifacts. They do not claim exact
legacy CLI envelope/text compatibility. Known flags used on the wrong command
are rejected, as are unknown flags.

HTTPS is required. Redirects are not followed with management credentials.
An endpoint override cannot borrow credentials or TLS overrides from another
saved cluster. Deployment POSTs are submitted once. A stream failure, timeout,
or Ctrl-C never triggers replay; Control may still be running the operation.
Success requires a valid terminal `done` event containing an object result.

The YAML adapter retains PyYAML's plain `yes/no/on/off` boolean interpretation.
It rejects duplicate keys, multiple documents, composite environment values,
non-boolean proxy values, out-of-range ports and fractional replica counts.
These are deliberate stricter validations, not claimed legacy compatibility.
Do not silently broaden compatibility by coercing ambiguous input.

## Build and verification

```sh
bash scripts/build-go.sh
.local-notes/go-bin/luma-go version --local
.local-notes/go-bin/luma-go validate templates/hello-world.yml --format json
.local-notes/go-bin/luma-go deploy templates/hello-world.yml --dry-run --format json
.local-notes/go-bin/luma-go compose validate observe/luma.compose.yml --format json
.local-notes/go-bin/luma-go compose deploy path/to/luma.compose.yml --env prod.env --format ndjson
.local-notes/go-bin/luma-go workflow run app-name --path path/to/checkout
bash scripts/build-go.sh .local-notes/go-bin/luma-agent-go luma-agent
.local-notes/go-bin/luma-agent-go --version
bash scripts/build-go.sh .local-notes/go-bin/luma-control-go luma-control
.local-notes/go-bin/luma-control-go --version

# Pure Go policy, protocol, context, CLI and architecture checks
bash scripts/check-go.sh

# Python/Go behavioral comparison against current Python source
.venv/bin/python -m unittest tests.test_go_contract tests.test_go_compose_contract tests.test_go_agent_contract tests.test_go_control_contract tests.test_go_control_agent_contract

# Complete shared repository gate (Go toolchain required)
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

`scripts/build-go.sh` reads the existing version from `pyproject.toml`; it does
not create another version source. `GOOS` and `GOARCH` select build targets, with
`CGO_ENABLED=0`. The default artifact is local and ignored by Git. No existing
installer, Control image, live Manager or node is switched by these commands.

## Implemented Agent slices

`luma-agent-go --config PRIVATE_AGENT_JSON` uses the existing node configuration
and node token. `--once` leases at most one task; when a receipt already exists it
delivers that receipt and exits without leasing another task. Management context
credentials are never used by the agent. The current installer still starts the
Python agent; never start both implementations against the same live node.

The node worker owns serial leasing, independent busy heartbeats, cancellation,
bounded progress delivery and terminal acknowledgement retry. Host operations do
not perform their own task leasing or acknowledgement. An explicit dispatcher
assigns each action to one executor and rejects duplicate registrations. Current
host actions are:

- `prepare-managed-volume-path`, `remove-managed-volume-path`, `remove-docker-volume`.
- `resolve-docker-image`, `diagnose-docker-pull`.
- `mirror-control-image`, `mirror-system-image`, `cache-runtime-image`.
- `configure-insecure-registry`, `configure-docker-egress-proxy` (privileged Linux).
- `repair-nomad-cni-hostports` (privileged Linux with Docker and iptables).
- `build-image` (Git, Docker and the Buildx plugin).

Capability advertisement includes only implemented groups and checks required
executables. Volume preparation uses the narrow `managed-volume-path` capability;
Python Control now schedules those tasks with that capability (already advertised
by Python agents). Host administration registration additionally checks platform,
privilege and required tooling. NFS hosting/client capabilities and actions have
been removed from the Go agent. Repository image execution is implemented; source analysis,
build-plan execution, terminal, updater, Nomad join and registry maintenance remain
unimplemented and are not advertised. Go Control leases build-image only when the task
and a Go-owned build run reference one another and the selected node matches.

New agents also send `supportedActions`, because some historical Control tasks
have no capability requirement. Updated Control checks this explicit list both
before queueing and before leasing; unsupported existing tasks remain queued.
An empty list permits no actions. A legacy Python heartbeat omits this field and
clears the restriction while retaining existing capability checks. Deploy this
Control compatibility change before evaluating a Go agent against a mixed fleet;
older Control versions ignore the field.

Before delivering an outcome, the worker atomically writes a mode-0600 receipt
next to the config. Delivery retries resend that exact decision while continuing
busy heartbeats. Startup recovers it before any idle lease, verifies the Control
authority and node identity, and never reruns the host mutation. A corrupt or
mismatched receipt fails closed. Graceful shutdown allows bounded time to report;
an unacknowledged receipt survives for the next startup. If a process dies before
an outcome is durable, Control's interrupted-task reconciliation remains the
authority; the agent does not automatically replay a partially executed action.

Only outcomes are stored in receipts. Registry credentials use a separate 0700
scratch directory and 0600 per-task Docker config, with no ambient Docker login
fallback. Task completion and failure remove that config; startup removes crash
residue under the exclusive agent lock before leasing. Passwords and Basic auth
encodings are redacted before output truncation and progress framing. Mirror
plans require matching destination repository paths and explicit tags; only the
builder-local and public registry hosts may differ. Conflicting credentials for
one registry, malformed digest output and destinations outside `luma-control`,
`luma-system/`, or `luma-cache/` fail before reporting a verified mirror.

Commands are argv generated by typed executors, never shell text from a lease.
Process groups stop on cancellation or timeout. A private helper process watches
a parent-owned pipe, so SIGKILL of the agent also stops the tool process group.
The repository image executor additionally stops its owned BuildKit container on completion
or cancellation and reconciles an interrupted builder before another lease. Registry
maintenance and other builder executors still need their own container lifecycle handling.

Host metrics and Docker container statistics sample independently of lease and
heartbeat requests. Only Nomad-labelled containers are reported, bounded to 200,
and malformed numeric values cannot poison heartbeat JSON. Sampling snapshots
do not wait for Docker. CPU sampling uses the interval between host samples;
`--once` does not block leasing on an initial telemetry collection. Detailed
Docker/Nomad diagnostics are still pending.

Host administration uses typed payloads, confined file access and argv-based tools.
It no longer installs NFS packages, edits exports or runs NFS services.

Docker configuration preserves unrelated JSON settings and refuses malformed or
duplicate keys. Allocation IDs are captured before restarting Docker; an unavailable
allocation snapshot fails before changing configuration. Proxy unit values use
systemd quoting and literal-percent escaping, and proxy credentials are redacted
from task results. The proxy file uses mode 0600. See the upstream
[systemd unit specification](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)
for literal-percent escaping.

Configuration replacement is atomic and preserves existing ownership/mode where
appropriate. A private per-file activation marker is written before replacement
and cleared only after service activation succeeds. An identical config with a
pending marker still retries activation. On failure or cancellation, the old file
is restored and service recovery gets a separate bounded context. If recovery
fails, the marker remains and the task reports failure. Concurrent administrator
edits are detected before replacement and before rollback; newer files are
preserved instead of overwritten. This is task-scoped recovery, not a transaction
over package installation, Docker allocations or the whole host.

## Remaining migration and acceptance gates

### Implemented Control read slice

`luma-control-go --state-dir RESTORED_DIRECTORY --read-only` starts on
`127.0.0.1:9081`. Both flags are mandatory; an explicit non-loopback listener is
rejected. There is no default production state directory and no automatic import,
initialization or schema upgrade. The process does not dispatch tasks or call
Nomad, DNS, Docker or Python.

The SQLite adapter uses the pinned pure-Go
[modernc SQLite driver](https://pkg.go.dev/modernc.org/sqlite), retaining static
cross-compilation. It requires schema 1, initialized metadata, private regular
database/authority files and matching database identity. Startup checks integrity,
foreign keys and JSON validity. Each snapshot rechecks authority and database
identity, and refuses a replaced file. The existing authority marker is mandatory
for this migration lane; restore a complete verified state archive.

In read-only mode, connections use `mode=ro` and `query_only`; every pooled connection rejects writes.
WAL commits remain visible, so the adapter does not use `immutable=1`. SQLite may
create or update private WAL/shared-memory coordination files even in read-only
mode; persistent records, schema and authority markers are not changed.

Implemented endpoints:

- `GET /v1/health`: reports `history-read-v1`, `deployment-workflow-read-v1` and `readOnly: true`.
- `POST /v1/auth/login/verify`: verifies the existing management token and returns
  cluster identity; it performs no mutation. Join and node tokens are not accepted.
- `GET /v1/history` and `GET /v1/history/{kind}/{id}`.
- `GET /v1/builds` and `GET /v1/deployments/history`, including individual details.
- `GET /v1/workflows`, `GET /v1/workflows/{name}` and `POST /v1/workflows/check`;
  the POST checks a supplied recipe without changing state.

Queued build detail `/v1/builds/{id}` includes project queue position and the build
it is waiting for. The repository streams active candidates for that project;
build policy counts owners and earlier queued entries with constant memory,
excluding local caller uploads that do not hold the project. The projection is
read-only and does not resume or mutate a queue.
Other GETs return 404; state mutations return 501. Health does not advertise
unimplemented deployment, builder, node, terminal or storage capabilities.

Repositories query only requested rows and streams in a read transaction. Pages
retain insertion watermarks and event upper bounds. Existing Python cursors can
resume in Go and vice versa, including Unicode filter/record names. New backdated
records and appended events do not enter an existing page window. Query values
are SQL parameters. Management authentication reads only three configuration
keys; it does not materialize node/task history or private build-queue payloads.
HTTP errors never return SQL, paths, stored payloads or credentials.

Cross-language tests create the database using the real Python initializer and
repository, start the Go executable, and compare HTTP results and bidirectional
cursors to Python. They exercise authority mismatch, newer/missing/corrupt state,
private file modes, refusal of stale JSON fallback, authentication, queue-position
parity and write refusal.
These tests use isolated temporary state, not a production backup or cutover.
The SQLite, identity, history, queue-position and startup-policy tests also run
with the race detector in a network-disabled, read-only Linux container with a
temporary writable test directory. Linux/macOS amd64/arm64 Control binaries
cross-compile without CGO. HTTP parity currently runs against the native macOS
executable; this does not establish systemd installation or a live Manager restore.

### Implemented Control Agent task transactions

The mutually exclusive `--agent-tasks` mode requires `--tls-cert` and `--tls-key`
and still requires a restored state directory and loopback listener. It adds the
existing `/v1/node-agent/lease`, `/heartbeat`, `/tasks/progress` and `/tasks/complete`
POST routes. Health reports `node-agent-tasks-v1` and `readOnly: false`. Other
unmigrated writes remain unavailable. It does not create a general remote-command
API or claim that deployment/build workers are ready.

The node domain owns lifecycle decisions; the SQLite adapter provides transactions
scoped to the authenticated node and its tasks. The writer uses `BEGIN IMMEDIATE`
and full synchronous durability; reads retain separate read-only connections.
Unknown node/task fields and unrelated event streams survive updates. Task pages
are bounded, and progress keeps the newest 300 events with a cumulative offset.
Long polling releases the transaction between polls and stops on cancellation.

The writer takes an exclusive `control-writer.lock`; the updated Python SQLite
adapter holds a shared lock for each connection, including schema initialization.
Another Go writer or a Python connection cannot write through those adapters at
the same time. Read-only inspection remains possible. Never unlink the lock to
bypass it. Older Python versions and direct SQLite tools do not participate in
this guard, and external host operations are not covered: drain work and stop the
old Control before any real cutover. The backup tool already excludes `.lock` files.

Node tokens are checked against their stored SHA-256 hash and known node identity.
Management and join tokens cannot use the Agent routes; a node cannot report
another node's task. Capability/action negotiation is preserved across identity-only
completions, and an idle Go Agent explicitly sends `activeTaskId: ""` so task-owner
recovery can distinguish a fresh idle observation from a missing field.
Missing or null task observations preserve the prior ownership fence, even after
the grace period; only an explicit observation may reconcile a running task.

Control leases only its migrated volume, image and Docker-configuration actions.
NFS, unsupported actions and tasks linked to unmigrated builder/build parents
remain unleased. There is at most one running task per node, including the lease
handoff interval. A fresh authenticated observation reconciles unowned tasks after
the 60-second handoff grace as failed/canceled, never queued again. A disconnected
node or a Control restart alone does not prove a host action stopped.

Terminal acknowledgement is idempotent: the first committed decision wins, even
when a later completion disagrees. Never-leased tasks cannot be completed. Late
progress cannot change a terminal task. Existing durable cancellation requests
are returned in busy heartbeats; parent-operation cancellation APIs remain to migrate.
Registry credentials are resolved only into the lease response, independently
for source and destination. They are not written back into durable task payloads.
Legacy payloads containing embedded ephemeral credentials require explicit task
migration rather than silently redistributing those stored secrets.

TLS integration uses the actual Go Control and Go Agent binaries with temporary
SQLite/volume paths, on native macOS and inside an isolated Linux container with
network access disabled outside its loopback interface. It verifies concurrent leasing, principal separation, progress
retention, terminal replay, restart reconciliation, registry credential isolation,
Python/Go writer exclusion and Python readback of Go changes. A proxy drops a
successful completion response, then both processes restart; the retained receipt
is acknowledged without another lease or host mutation. Full build/deploy parent
coordination, readiness/join, telemetry history and task-retention policy are pending.

### Implemented Control workflow catalog

The `workflow` domain validates recipes independently of the CLI parser, compares
deployment semantics, resolves equivalent URL/provider repository identities and
disambiguates monorepo sidecars. Its URL parser performs no network operations.
Recipes contain argv data, never shell programs; control connection options,
credentials, recursive workflows, confirmation bypasses and skipped deployments
are rejected. URL userinfo, query strings and fragments are stripped before saving.
Environment-file contents are never read by this module.

`GET /v1/workflows`, `GET /v1/workflows/{name}` and `POST /v1/workflows/check` work
in both migration modes. `POST /v1/workflows` requires the existing writable
`--agent-tasks` lane and reports `deployment-workflow-v1` in health. This is a
catalog operation; it does not execute the stored deployment/build recipe.

Writes reauthenticate the management token inside the SQLite transaction and
compare `expectedRevision` before changing state. Concurrent callers using the
same revision cannot overwrite one another. A manual edit retains the separate
`lastSuccess` recipe/evidence; it does not claim the edited recipe already deployed.
Omitted notes preserve the existing note; an explicit empty note clears it.
Schema 1's `deploymentWorkflows` configuration value is retained for Python
readback, with bounds of 16 MiB and 10,000 records; unknown record extensions and
unrelated configuration survive edits. This limit rejects oversized legacy state
rather than partially reading or dropping records. Git provider reads select only
names/base URLs, without loading provider credentials into the catalog.

Compatibility tests cover native/Compose, remote/local build and retry recipe
records, canonical comparisons, provider/URL equivalence, ambiguous monorepos,
concurrent revision conflicts, URL sanitization, prior-success retention and
restart/Python readback. The actual Go CLI exercises record/show/list over TLS
against Go Control. Build recipe storage does not imply Go build execution is
implemented; that execution lane remains pending.

### Implemented internal deployment coordinator

`internal/deployment` owns orchestration and rollout success; `internal/storage`
owns node-local data placement; `adapters/nomadruntime` owns Nomad HTTP;
`adapters/controlstore` persists bindings/history; `adapters/controlserver` adapts
existing ordinary, preview and NDJSON deployment routes. No domain package owns
HTTP, SQLite or filesystem operations.

`--agent-tasks --nomad-url EXPLICIT_ISOLATED_ENDPOINT` enables existing-image,
internal native and Compose deployments. The coordinator resolves existing data
ownership before rendering, pins stable Nomad node IDs, saves the pending binding,
submits one job and waits for that registration's evaluation, job version,
deployment and healthy allocations. It writes the terminal result into schema 1
and deployment history. Online Compose leaves unresolved placement to Control;
local render/validate still require explicit local-volume pins.

An isolated real Nomad 1.9.7/Docker cluster verified native create/update and
single-service host-network Compose through the real Go CLI and TLS Go Control.
The resulting containers read preexisting test data on the same node/source,
and Python read back the active records. Repeatable HTTP contract tests additionally
cover preview, adopting Python-era records from current Nomad placement, history
origin and refusing changed data owners/sources. Relay ingress
and build execution remain unported
and return explicit errors. This is not the complete production Control.

### Implemented edge deployment integrations

An explicit `--config` (or `LUMA_CONTROL_CONFIG`) loads the existing YAML defaults,
node addresses and DNS provider settings. Registered node addresses and manager
local ingress flags are merged for rendering. `cn-edge` and `external-edge`
requests now use the existing Nomad/Traefik discovery labels and do not create
static routes. After a healthy rollout, only that service's old file-provider
shadow is removed within the configured route root. Generated jobs are written
atomically as private files under the configured stack root.

DNS planning is pure deployment policy; the Cloudflare adapter reads only named
infrastructure credentials at execution time, honors native service DNS overrides,
and creates or updates the matching record. The route probe adapter separately
reports public reachability, distinguishes application 404 from Traefik router
misses and marks transport/TLS uncertainty inconclusive. It does not restart the
application or infrastructure. `--route-ca-file` optionally trusts a private CA
while preserving TLS hostname verification. Native health remains an object;
Compose health remains a list, matching the existing API response shape.

Unit and TLS contract tests cover DNS create/update, configuration-based render
settings, node local ingress, native/Compose file cleanup and actual TLS probe
responses. An isolated Nomad 1.9.7/Docker/CNI 1.6.2/Traefik 3.1 run also
completed native and Compose cn-edge deployments through the real Go CLI and
Control; HTTPS requests through Traefik returned the exact file mounted into each
nginx task. The isolated edge used a self-signed certificate; private-CA/hostname
verification is covered separately by the TLS contract. Linux race and TLS
contract tests pass. Live Cloudflare writes are not part of these isolated tests.
Registry/cache maintenance policy and build execution still need their remaining integrations.

### Implemented repository image executor

`build` owns source URL/ref validation and target architecture policy. `buildexec` owns
Git checkout, bounded repository reads, Docker/Buildx processes and private credential files;
its files separate source handling, build planning and execution. Agent composition registers
`build-image` / `docker-build` only when Git, Docker and a Buildx plugin are available.

The executor checks out branch/tag or full commit, discovers the nearest unambiguous native
or Compose manifest, or honors an explicit Compose sidecar. Context/Dockerfile paths must
resolve within that checkout. Build platforms come from the deployment node/region inventory,
not the Builder CPU. Compose literal build arguments, per-service image repositories and
unambiguous shared-image consumers are preserved. A worker using an image built by a sibling
is rewritten to the same commit-tagged registry image; ambiguous aliases are left unchanged.

Git credentials live in private askpass files removed after checkout. Registry credentials
live in a private Docker config removed at task completion; the executor does not use ambient
Docker logins. An explicitly direct build clears inherited proxy variables. Proxy configuration
reaches Git, BuildKit and Dockerfile RUN stages. The managed registry's HTTP transport is
configured for both BuildKit image resolution and export, matching the existing platform.

Each Agent owns one deterministic Buildx builder under its private persistent build directory.
An ownership marker is recorded before creation. Completion/cancellation stops that BuildKit
container while retaining its named cache volume; startup and the next execution clean up an
interrupted owned builder before doing more work. Cleanup failure retains the ownership marker
and prevents reporting success. This acts on the Agent's builder only, not shared builders or
application containers. Process lifetime and output redaction remain owned by the process adapter.

Actual authenticated Git and Registry tests ran the real Go Agent through the existing Control
protocol: a native image and two Compose images were built by BuildKit, pushed and exported;
each exported filesystem contained the exact repository fixture content, and a Compose worker
followed the newly built image. The old Control was a transitional test harness, not called by
the Go executor.

Go Control now owns repository build runs, the private durable request queue, and the
build-to-deploy handoff. POST `/v1/builds` and `/v1/builds/stream` accept requests; queue mode
returns an attempt identity immediately. GET build/history routes expose the persisted
attempt, and POST `/v1/builds/{id}/cancel` requests cancellation. Four Control workers serialize
one project while allowing different projects to execute. Known replacement targets wait for
previous execution to stop. HTTP disconnects do not discard accepted work. On restart queued
work is retained; interrupted work is recorded as failed/canceled rather than replayed.

A child Agent task and its Go build owner are attached atomically. Credentials are loaded only
at lease: a selected Git provider is restricted to its configured clone origin/path prefix;
the global GitHub token is used only for HTTPS github.com; registry credentials may come from
the configured push or public host. Durable Agent payloads reject credential fields. The
private build queue preserves explicit empty environment-secret maps, while public requests
omit their values. Build completion uses the Agent's Compose content and image aliases, strips
build declarations, and enters the existing deployment coordinator with local placement intact.

`tests/test_go_build_contract.py` exercises the real Go Control binary over TLS against an
isolated Nomad API fixture: native handoff, Compose sibling image preservation, cancellation
before submission, ephemeral credentials, queue cleanup and persisted deployment results.
Agent completion is simulated in this repeatable contract. A separate isolated run also
completed native and Compose builds through the actual Go Control, Go Agent, authenticated
Git, BuildKit and authenticated Registry, exported all three built images to verify their
files, and handed the results to the Nomad API fixture. It exposed and fixed acceptance of
short Git SHA result tags and single-path repository URLs. Actual Nomad execution of this
combined build chain remains to be verified.

Go CLI `import` now checks the existing workflow, submits one queued attempt and follows its
ID through paginated build events using `resumeAfter`. The durable request includes the checked recipe and revision; Control records it only after the
actual deployment succeeds, even after the observing CLI exits; observer interruption cannot submit a second build. `--env`
loads import secrets (including an explicit empty map), and `--proxy-mode direct` persists
that intent separately from proxy credentials. Build list/logs support existing history
filters/cursors, and build cancel uses the owned-run cancellation endpoint.

`workflow record/run` accepts remote-build recipes, resolves only local manifest/env files
under the selected checkout, and keeps the current Control connection pinned. Repository URLs,
remote contexts and sidecar paths are not resolved as local files. Replay supplies the expected
application name; the Control handoff rejects a different discovered name before deployment.
When one repository produces a new sibling application, its workflow is created at revision
zero instead of reusing another application's revision. Existing records remain protected.

Authenticated Agent progress is appended to its owning build's event stream in the same
transaction. Deployments retain typed Git source metadata, selected builder/registry, revision,
sidecar and build ID, excluding Git/registry credentials and deployment secret values. Build
history's application index follows the resulting service/deployment rather than the repository.
The repeatable TLS contract now also runs real Go CLI import/list/logs and verifies workflow
and Git-source persistence. The separate authenticated Git/BuildKit/Registry run also starts
through real Go CLI, with native and Compose workflows both recorded. Nomad remains an API
fixture in this combined proof.

Go CLI `build retry ID` and `workflow record/run` retry recipes now use the saved repository
request. Control exposes `/v1/builds/{id}/retry` (and `/stream`) plus
`/v1/applications/update` (and `/stream`). Retries require terminal remote builds, create a new
attempt with `retryOf`/`retryRootId`, and leave the parent intact. A successful parent's actual
application name is enforced on retry; application updates restore typed Git provenance and
require the existing application's name. Direct image deployments cannot be rebuilt from Git.
Known application names also serialize replacement work across repositories.

Accepted queue requests retain the checked recipe and optimistic revision privately. Control
validates the recipe/repository/target before accepting it and records success without a client
callback. A newer workflow edit is preserved: the build still succeeds and its result reports
`workflow.saved: false` plus a warning. Nonqueue streams drain paginated build events, including
Agent compiler output, before emitting the final result. TLS contracts cover retry ancestry,
active/local rejection, CLI retry and server recording, native/Compose Git update, name mismatch,
unobserved queued completion, concurrent workflow edits and more than one page of stream logs.

Go Control exposes GET/POST `/v1/builds/config`; Go CLI `build config` reads current defaults
and applies explicit builder/registry/direct-egress options. Node names, display aliases and
registered IDs resolve to canonical inventory keys, and invalid/ambiguous references abort the
whole change. Omitted fields and unrelated configuration survive, an empty direct-egress list
clears the override, and changed registry endpoints invalidate the saved registry setup check.
Read-only Control permits inspection only. Build configuration summaries exclude private node
metadata and honor the same Agent/Node readiness rules used for execution. Repository builds
require a declared builder, rather than accepting any node advertising Docker.

Tests exercise Go CLI configuration over TLS, restart/read-only inspection, atomic changes,
secret-free summaries and the actual leased build payload: direct nodes omit the proxy; after
clearing that setting a cn builder receives the manager proxy. Source analysis/build-plan/artifact
broker flows remain separate work.

The Go build executor now also accepts an explicit local working-tree path. It reads origin,
HEAD and dirty state without cloning, discovers native/Compose manifests and their target
nodes/regions, and builds the actual local files. Local uploads use the reserved project image
path even when a manifest contains `build.repo` or Compose repository overrides; sibling image
aliases follow the built image. Docker auth, credential-helper settings, plugin directories and
named-context metadata are available through a private temporary Docker configuration. Existing
explicit Buildx builders are borrowed without creation/removal; automatically created local
builders and their cache are removed at completion. Agent-owned builders retain their existing
cache policy. Unit tests cover dirty metadata, mixed targets, repository binding, private auth,
context selection, borrowed-builder preservation and owned-resource cleanup. An isolated real
run built native and Compose working trees with uncommitted file changes, pushed three images
to an authenticated Registry, exported their files to verify the dirty contents, checked the
shared worker reference and verified the selected builder stayed running. Control and Nomad
were not involved in that executor proof.

Go CLI `build local [PATH]` and `workflow record/run` now connect that executor to Go Control.
`/v1/builds/local/prepare` reserves the registry/project/tag and checks all target architectures;
it needs deployment node inventory but no remote build Agent. `/v1/builds/local/{id}/complete`
validates the reserved image scope, tag, aliases and application name before atomically moving
the upload into the shared deployment queue. Duplicate completion cannot create a second job.
`/{id}/fail` closes an upload reservation; queued work is observed by its original build ID.
Uploads do not hold the project execution slot, but accepted deployment completions use the
same project/replacement serialization as remote builds. Expired uploads are closed; unexpired
uploads and queued completions survive Control restart. Running deployments are not replayed.
Local completion uses a separate reserved-tag validation path; remote builds still require a
commit tag. Deployment records retain Git source and dirty revision provenance, and Control
records the checked local workflow after success without a client callback.

Tests cover local reservation without a remote build Agent, mixed target platforms, wrong
project/tag/digest/application rejection, one accepted completion under concurrent requests,
expiration, queue ownership and local native/Compose deployment. A separate real Go CLI -> Go
Control -> local dirty checkout -> authenticated Registry -> Go Control deployment test exported
three initial images and a fourth rebuilt through `workflow record/run`, verified all dirty
contents and the shared Compose alias, and preserved a borrowed Buildx builder. Nomad remains
an API fixture in this combined proof.

### Builder snapshot and artifact storage

`internal/build` defines task-scoped snapshot handles and the closed analysis artifact
descriptors. `adapters/builderfs` owns deterministic PAX archive creation, logical tree
hashing, bounded extraction and content-addressed storage. The tree hash uses the existing
canonical JSON record format and traversal order; the archive digest identifies its exact
bytes. Different tar writers may produce different archive digests for the same tree.
Existing archives are consumed as stored, without regeneration or relocation.

The store retains `sha256/<prefix>/<digest>.tar` and
`artifacts/{evidence,deployment-plan,build-plan-candidate}/sha256/<prefix>/<digest>.json`
under the configured Builder snapshot root. It uses the existing execution flock shared
with cleanup, held until an opened content stream is closed. Snapshot and artifact writes
verify copied bytes before publication; reads verify size/digest and reject symlink paths.
Extraction checks path, entry count and disk limits and binds the materialized bytes to the
reserved digest. It requires a fresh destination and preserves source executability.

Tests exercise existing archives and artifacts, task-scoped handles, UTF-8/PAX paths,
symlinks, dirty files, deterministic metadata, cleanup exclusion, corruption and extraction
limits. The cross-language harness supplies old-format files and independently checks Go
output. Python is a temporary test reference, not a runtime dependency. This storage adapter
is a prerequisite for the pending analyzer/build-plan executors; it does not yet advertise
those Agent capabilities or implement the Control artifact download API.

The Go `buildexec.AnalysisSource` stage now connects HTTPS source acquisition to
that file adapter through `build.AnalysisSourceFiles`. It supports authenticated
Git branches/tags or exact commits and repository subdirectories, plus leased
HTML/ZIP objects. Git uses a private askpass directory and an explicitly isolated
process environment; neither credentials nor Git configuration enter the analyzer
workspace. Object downloads verify HTTPS, the leased host, size and digest, reject
redirects and do not use ambient HTTP proxy settings. Transport errors never include
the signed URL. ZIP materialization rejects traversal, links, special files,
encrypted entries and Unicode case-fold collisions, and requires `index.html`.

Source preparation applies the task deadline and watches aggregate workspace disk
use while fetching and creating the snapshot. It persists the exact archive before
discarding the checkout, then materializes only `workspace`, `input` and `output`
with trusted task/source metadata. Tests use a real authenticated HTTPS Git backend
with an older pinned commit and HTTPS HTML/ZIP servers, alongside budget cancellation
and environment isolation checks. This completes source intake; rootless analyzer
execution, output validation/enrichment and Builder Control orchestration remain
pending, so this stage does not independently advertise an Agent action.

### Implemented relay and fixed-port execution

The deployment domain resolves HTTP/TCP relay upstreams from canonical node addresses,
validates shared TCP entrypoint ownership and reports new static ingress ports. The SQLite
adapter projects legacy manifests or persisted `tcpRelayPorts` and repeats ownership checks
inside `BeginDeployment`, before any external side effects. An unchanged deployment may
reuse its own ports; active/pending deployments of either kind cannot collide.

After rollout, Control dispatches scoped `repair-nomad-cni-hostports` commands to the current
allocation nodes. The privileged Linux host adapter advertises `nomad-cni-repair` only with
Docker and iptables available; it compares Docker allocation labels with CNI DNAT rules and
removes only stale rules shadowing an active allocation on a requested port. Commands use
argv and never evaluate rule text in a shell. Unsupported/failed repairs are reported
separately from allocation health and public probing. HTTP relays use HTTPS probes; TCP
relays use their published TCP port and report inconclusive connectivity honestly.

Static Traefik entrypoints/firewalls still require manager refresh for a new TCP port; the
remaining manager lifecycle migration owns that step. Compose groups pinned to macOS reject
unsupported bridge exposure. Explicit Compose HTTP upstream lists are honored, while the
existing Compose priority remains unchanged. Internal Compose services with `publishPort`
now actually map that port, correcting the old renderer's silently ignored declaration;
Python comparison tests normalize only this intentional behavior change.

TLS contracts cover canonical node aliases, persisted port reservations, cross-kind conflicts,
new-port advisories and the Agent command lease/completion path. An isolated real Nomad
1.9.7/Docker/CNI 1.6.2/Traefik 3.1 run deployed both native and Compose services with fixed
ports through Go CLI/Control and an actual Linux Go Agent. HTTP requests through each TCP
entrypoint returned the exact mounted application content. That runtime check exercised
CNI inspection with no stale rules; scoped deletion is covered by executor tests. Traefik's
static ports were explicitly configured in the isolated fixture, not automatically installed
by Go Control. Linux race and TLS contract checks passed.

### Implemented secret management and deployment substitution

The credentials domain owns name/scope validation and pure secret resolution.
Controlstore owns schema 1 global/scoped values; management writes revalidate the
management principal inside their transaction. CLI `secret list/set/import/remove`
uses typed metadata-only responses. Interactive input does not echo and restores
the terminal on completion or interruption. Read-only Control permits name listing
and refuses writes.

The deployment coordinator reads only selected secret names. Existing scoped values
or an explicit incoming env map enable strict application scope: missing references
cannot fall back to global values. An explicitly empty env file remains distinct
from no env file. Legacy global-mode deployments remain supported. Only referenced
imports are persisted, atomically with the pending deployment and storage binding.

Only the outgoing Nomad task Env is substituted, with a single literal pass.
Preview, generated artifacts and deployment history retain placeholders; responses
report names/counts rather than values. The private Control database and Nomad task
configuration contain the actual values. The runtime never invokes Python.

TLS contract tests cover management, scope isolation, empty env files and native/
Compose deployment imports. Actual PTY tests cover hidden input and cancellation.
An isolated real Nomad/Docker run verified that both native and Compose containers
received exact quoted/multiline values, including literal `${...}`, while artifacts
and history retained placeholders.

### Runtime egress and native Tunnel integration

Region policy now resolves built-in/custom egress modes and requires registered
custom regions for online deployments. Controlstore projects only routing metadata
from schema 1; the deployment domain resolves the manager gateway in existing
RPC/config/registered-manager/HTTP fallback order. The Nomad compiler injects
HTTP_PROXY/HTTPS_PROXY only into opted-in tasks and preserves all explicit env
values. Direct regions receive no defaults. Missing required gateway defaults
fail before deployment effects instead of silently sending traffic directly.
No gateway installation or Docker daemon mutation occurs in this path.

Native Tunnel deployments include the structured tokenEnv reference when filtering
CLI env files and resolving scoped secrets. The token is injected only into the
outgoing cloudflared task; ordinary DNS records and Traefik routes are skipped.
Tunnel-only jobs now use a bridge allocation without a published host port so the
app and sidecar share localhost. This deliberately corrects the legacy host-mode
Docker namespace separation; the Python comparison documents this specific delta.
The topology follows [Nomad group network semantics](https://developer.hashicorp.com/nomad/docs/job-specification/network).

Real isolated Nomad/Docker tests verify native and Compose requests reaching an
owned proxy with injected environment. A separate real Nomad/CNI/Docker test
verifies the Tunnel sidecar reaching its origin over localhost, substituting only
the external connector process with an in-container probe. No production Tunnel
token or Cloudflare public connection was used; public Tunnel readiness remains
unverified. Compose Tunnel remains unsupported. Gateway administration and build
execution still need migration.

### Implemented component prerequisites and node-local directory preparation

`requirements.capabilities`, `requirements.secrets`, `requirements.init` and notes
now participate in native and Compose Control previews and deployments. Reports
contain six checks: region capacity, Cloudflare, Tailscale, egress, referenced
secrets and image registry credentials. The store projects presence/configured
flags and setup-check status, never credential values. Scoped env presence follows
the same deployment secret rules, including explicitly empty values.

CLI deployment enforces explicit requirements; Dashboard-origin deployment also
enforces inferred requirements and verified Cloudflare/egress setup checks, matching
the existing contract. Checks run before pending state or deployment side effects.
The declarative init list validates the corresponding prerequisites. It does not
install providers or run a second set of operations: actual DNS, image resolution
and runtime work remain owned by their deployment steps. Provider setup/management
APIs are still part of the remaining Control work.

Declared Compose `local.path` volumes are prepared through the selected node's
Agent after ownership is established. The shared `node.CommandTaskRunner` port
handles image and directory work; it does not expose arbitrary shell execution.
Only a ready Agent advertising `managed-volume-path` and the prepare action can
execute the owned task. Duplicate paths on the same node are prepared once, and
Control verifies the returned path before submitting Nomad. Named Docker volumes
remain the runtime's responsibility; native bind mounts may be files and are not
silently turned into directories.

Every local directory task uses `preserveExisting: true`: missing directories are
created writable, existing directories retain data, owner and permissions.
`initialize: empty` is accepted as fresh-volume intent and never clears an existing
path. `adopted: true` does not bypass the node/source ownership invariant. No storage
server, NFS export, remote copy or mount operation is introduced.

TLS tests verify secret-free preview, explicit CLI versus verified Dashboard checks,
init validation and Agent preparation before job submission. An isolated actual
Go CLI/Control/Agent + Registry/Nomad/Docker test created a missing local directory,
read preexisting data from an adopted directory, wrote data through the container,
then updated the same Compose job. The node and mount sources stayed fixed; both
the preexisting UID/mode and the data written by the first container survived.
Four directory tasks across two deployments completed without NFS operations.

### Implemented private image authentication and Builder caching

`registry list/login/remove` use the existing `/v1/registries` and
`/v1/registries/remove` API contracts. Login accepts a non-echoing terminal prompt
or `--password-stdin`, normalizes the host and writes credentials through a bounded
SQLite transaction with current management-token validation. Listing projects only
host, server address, username and configured state; mutation replies contain no
password. Updates invalidate the existing registry setup check without changing
unrelated checks. Read-only Control supports listing only. Registry serving,
inventory, retention/deletion policy and GC are separate remaining work.

The deployment coordinator reads `build.registryHost`, `build.pushHost` and
`build.defaultNode` from the existing catalog. When the internal registry is
configured, external images are cached by a declared, ready Builder advertising
`runtime-image-cache-v1` and `cache-runtime-image`. An explicit
`LUMA_LAE_BUILDER_REGISTRY_INSECURE=0|1` selects the transfer policy. References
already in the internal registry are retained, with push addresses rewritten to
the runtime pull address where necessary. Partial registry configuration is an
input error, not a reason to bypass the cache.

`registry.PlanCache` owns deterministic repository/tag identity; `node.CommandTasks`
queues and waits on one owned Agent task; `controlstore` checks current management
authority, deployment ownership and Agent capabilities inside its write transaction.
Failed or interrupted deployments cancel their queued work and request cancellation
of running tasks. Storage placement excludes actual Builders; missing Builder names
and empty node aliases do not turn ordinary data nodes into Builders.

The Agent leases only the source and push credentials needed for the transfer.
Control accepts the cache result only when its source/push/destination match the
request and its digest is valid. Native and Compose jobs use the resulting internal
`repository@sha256:…` reference. Duplicate image/platform pairs in one deployment
share a cache operation. Unqualified Docker Hub images may use configured mirrors
after a cache attempt fails; Builder region and direct-egress settings select the
transfer proxy. Pinned-node platform metadata handles existing string/object forms
and Agent reports, including Linux containers on macOS.

Runtime credentials are looked up again for the final pull registry and injected
only into the outgoing Nomad task config. Preview, generated files, deployment
records and durable Agent tasks do not contain these passwords. The authenticated
Nomad job itself necessarily contains runtime pull credentials. Source, push and
runtime addresses may have separate accounts; TLS contract tests cover that split.

An isolated real test used Go CLI, TLS Go Control, Go Agent, crane 0.20.7, two
authenticated Registry 2 instances and Nomad 1.9.7/Docker. Credentials were registered
through Go CLI `registry login --password-stdin`. It copied a private source
image, checked the destination digest, and started one native and two Compose
containers from the internal registry. Two total cache tasks confirm Compose
deduplication. It used fixture credentials and loopback-only registry ports, with
no production registry changes.

Without Builder registry settings, a pinned node advertising `docker-image` and
`resolve-docker-image` receives an owned pre-pull task before job submission. Source
auth is attached only at lease. `latest` and implicit latest force a pull; Control
requires a returned digest for these mutable references. Other tags may reuse the
node's local image. Docker repository aliases are normalized while preserving the
requested repository, and Control rejects unrelated repositories, changed pinned
digests and mismatched result platform/identity. Docker Hub mirror fallback and
Compose deduplication also apply; pre-pull deduplication includes node identity.

When no node is pinned, or the selected node does not support pre-pull, mutable
latest references resolve through Registry V2 to an immutable digest before
submission. Versioned tags retain scheduled-node pulling. The `registryhttp`
adapter uses go-containerregistry 0.20.7 for Basic/Bearer authentication and HEAD
manifest lookup, with an explicit catalog credential and bounded context; it never
uses a local Docker credential helper. Multi-platform index digests remain indexes
so Nomad/Docker can choose the actual runtime platform. Preview does not query the
registry or enqueue Agent image tasks.

TLS Control contracts cover consecutive latest deployments resolving different
digests, source credentials kept out of tasks, node aliases/platforms and same-node
Compose deduplication. Protocol tests cover Basic and scoped Bearer authentication,
invalid digests and credential-safe errors. A separate actual Go CLI/Control/Agent
test used an authenticated Registry 2 and Nomad/Docker to launch four containers:
one unpinned native service, one pinned native service and a pinned two-task Compose
group. It verified registry resolution, real Docker pre-pulls, immutable image
references and exactly two Agent tasks. No production resources were changed.

Automatic Docker pull-proxy repair and allocation reconciliation after its daemon
restart remain to be migrated. A pull failure is currently reported; it does not
trigger an incomplete daemon change.

| Stage | Deliverable | Gate before replacement |
| --- | --- | --- |
| 1 — implemented | Go deployment compiler and CLI lane | Python/Go artifact parity, transport failure tests, architecture checks, cross-compilation |
| 2 — implemented | Compose compiler and deployment CLI workflow/env support | Existing Compose corpus, networking/storage parity, workflow recipe and output contracts |
| 3 — in progress | Go Agent worker, volume/image/host-administration executors and telemetry implemented; remaining executors, installer and updater pending | Linux/macOS lifecycle, heartbeats, PTY, cancellation, credential handling and actual Agent execution |
| 4 — in progress | Go Control authentication/history, workflow catalog, Agent task transactions and SQLite writer fencing implemented; internal deployment coordinator implemented; remaining APIs and build workers pending | API/NDJSON/WebSocket contracts, SQLite compatibility and actual deployment/build execution |
| 5 | Remaining management modules and Control cutover | One state backup, one authoritative writer, completed in-flight work, Agent reconnection and real deployment verification |

Module migrations are complete only when their behavior is verified against
existing contracts and actual execution. Keep the current Python implementation as the executable reference until
the corresponding gate passes. Keep contract comparison fixtures outside the Go
runtime: production Go binaries must not require Python or invoke tests.

The retained compatibility corpus contains 50 native and 37 Compose artifact comparisons
(with the documented Tunnel shared-network correction),
plus explicit rejection checks for the four removed NFS scenarios,
invalid-input cases, Go race checks, environment/credential isolation and no-replay
tests, and TLS calls through the real Python ASGI workflow/stream routes with
isolated SQLite. The integration fixture substitutes deployment side effects and
node status. Cross-compilation covers Linux/macOS on amd64/arm64; these checks do
not by themselves establish real Nomad execution, Linux runtime behavior or production cutover;
the separate internal deployment proof above covers its narrower runtime path.

Agent verification additionally covers actual temporary filesystem mutations via
TLS and Python Control, lost completion responses, SIGKILL and receipt recovery,
subprocess parent-death cleanup, credential redaction/cleanup, and slow telemetry.
The Agent core tests also run inside an isolated Linux container; macOS tests use
the local runtime. An opt-in Docker integration test inspects an existing local
image and forbids pulls or container mutations. These checks do not establish
Linux systemd installation, macOS launchd installation, Docker daemon restarts, build or terminal behavior.
Actual authenticated registry transfer is covered by the separate image test above.
Host-administration tests mutate temporary files and inject service failures;
they do not modify the developer machine's `/etc` or Docker service.

The next Control migration must use a restored private state copy and an isolated
Nomad test cluster. Do not shadow live mutation requests or run both Control
implementations as independent writers. API compatibility alone does not establish
state-transition compatibility. After a cutover, rollback must account for changes
already made to Nomad, DNS and application state, not only the SQLite snapshot.

### Remaining implementation ledger

This ledger tracks the full rewrite; the completed compiler and Agent slices are
not acceptance of the whole migration.

- Agent host administration: Nomad join, registry storage/maintenance and crash cleanup of owned
  helper containers.
- Agent execution: analyze-source, build-plan, artifact export,
  builder storage, credential broker integration and durable parent/child build cancellation.
  The Go build-image executor and Control repository queue/dispatch/handoff are implemented;
  the separate Builder broker workflows remain pending.
- Agent lifecycle: WebSocket host/container PTYs, diagnostics, installer,
  self-update, manager-update handoff and watchdog integration.
- Remaining CLI: bootstrap, node/registry/storage/operations
  administration and executable workflow recipes for those paths.
- Control: writable state repositories and schema evolution, remaining principal separation,
  remaining HTTP/NDJSON/WebSocket routes, Nomad/Cloudflare/
  registry adapters, and Luma's LAE-facing APIs. The independent `lae/` workspace
  is not part of this rewrite.
- Remove legacy NFS Control/CLI/Dashboard operations and convert affected shipped
  manifests, including the Luma-facing LAE storage contract. Preserve existing data
  and require explicit migration; do not turn former remote volumes into empty disks.
- Deliver the complete [Go migration guide](go-migration.md), including executable
  release/install paths, schema/API/config changes, node-local data migration,
  validation evidence and concise troubleshooting. Its current draft is not a cutover approval.
- Remove the old Luma Python runtime and its packaging/install dependencies after
  Go replacement is complete. Python readback is transitional test evidence, not
  a runtime bridge. The independently owned `lae/` workspace remains outside scope.
- Replacement: isolated end-to-end deployment/build tests and release/install artifacts.
  Replace Control, Agent and CLI while retaining Nomad, Docker, Traefik, applications
  and node-local data. A state backup and successful reconnection/deployment are
  required; exhaustive failure, mixed-version and rollback rehearsals are not prerequisites.
