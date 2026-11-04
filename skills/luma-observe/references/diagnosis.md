# Diagnose observability availability

Start from the failing surface and endpoint, the observation window and the
deployed version. Reuse an already known application/manager identity. These are
read-only checks; an explanation request does not authorize a restart, stack
redeploy, credential change or a business-task retry.

## Start with a shared snapshot

Use `luma observe status --format json` for ingestion, export queues, storage,
query latency, unresolved container identities and log coverage. Then use
`luma observe app APP --since 1h --until TIMESTAMP --format json` and
`luma observe alerts --app APP --format json` to separate application signals
from collector/query failures. These commands share the Web API and do not mutate
runtime state. Missing self-metrics after a Control upgrade may require deploying
the separately versioned Observe stack; do not report missing series as zero.

Tempo local backend 2.10.10 enumerates block directories with `fs.WalkDir` and
propagates a disappearing directory as a poll error. Concurrent block deletion
is a plausible cause, not proof of corruption. Compare exact block IDs with
compaction/deletion logs and test a known persisted trace before changing storage.
Do not delete blocks or WAL to suppress this error.

## Get the evidence closest to the symptom

| Symptom | First evidence |
| --- | --- |
| Application dashboard list unavailable | Grafana `/api/search` status and error; the UI can collapse unrelated errors into one banner |
| Many tasks suddenly lose coverage | The shared Metrics query state and fresh heartbeat timestamps |
| Trace missing but lifecycle logs exist | Exact Trace-ID lookup, sampling/retention, then Collector/Tempo errors |
| Log query unavailable | Log-query error plus observe-access/VictoriaLogs evidence |
| Suspected overload | Fresh manager/container CPU, memory, disk and restart/error evidence |

Use the registered Observe stack's ID for `OBSERVE_APP`, not a service's composed
`fullName`. Select only the service relevant to the symptom:

```sh
luma app log-query OBSERVE_APP --since 1h --task grafana --contains 'path=/api/search' --limit 40 --format json
luma app log-query OBSERVE_APP --since 1h --task observe-access --errors --limit 40 --format json
luma app log-query OBSERVE_APP --since 1h --task collector --errors --limit 40 --format json
luma app log-query OBSERVE_APP --since 1h --task tempo --errors --limit 40 --format json
```

Service names above are the bundled defaults; confirm them from `luma app show`
if the installation uses another layout. Keep query windows bounded and inspect
pagination. Empty error samples are not a health proof.

## Classify the read failure

- **401/403:** distinguish Control authentication, Grafana's own session and the
  Observe read credential. Grafana's `session.token.rotate` / `token needs to be
  rotated` means session renewal is required; it does not establish that Observe
  is disabled or that an OTLP write token is invalid.
- **429:** an admission limit rejected the read. Keep the source unavailable;
  inspect the existing concurrency limit and overlapping callers before tuning.
- **Timeout / `context canceled`:** the caller may have abandoned a read at its
  deadline. Check the deployed client's deadline and the actual duration; this
  message alone does not locate the delay in the network, proxy or store.
- **200 with no samples:** inspect label/time scope, sampling and collection
  coverage separately. An empty window is not proof that a worker stopped.
- **A shared query fails:** report one unavailable data source and its affected
  tasks. Do not turn every affected task into an independent execution failure.

When a read can safely be repeated, compare one bounded retry to the failed
request. If an existing application query client is available, use its normal
authenticated read path and report per-query status/duration. A temporary local
deadline override can distinguish timeout from an oversized/invalid response;
it is diagnosis, not a deployed fix or a reason for unbounded retries. Keep the
existing concurrency/body limits. No polling loop or load test is needed to
establish a single intermittent failure.

## Resource and runtime checks

```sh
luma app show OBSERVE_APP --format json
luma app events SERVICE_FULL_NAME --format json
luma node status MANAGER --format json
```

Use the relevant service's `fullName` from `luma app show` for runtime events.
Read the relevant fields before printing JSON: app snapshots contain
`result.services`; registered nodes are under `result.nodes.items`. A node's
`metrics` holds host values and `containerStats` holds per-container CPU/RSS.
Check the agent's freshness. An app row with `actual.containers=0` and zero
usage can reflect missing attribution; it is not measured zero load. Compare
the actual Grafana/Collector/store containers and their memory limits.

Running replicas and Control health establish liveness, not successful reads or
complete ingestion. Conversely, one slow read does not prove sustained resource
exhaustion. Check export/refusal/drop messages and timestamps when ingestion is
the suspected failure. Query a specific task's logs with `luma-logs` when the
question is whether the business execution itself failed.

## Assign the change to its owner

- Luma owns Grafana integration/session handling, query entry limits, platform
  caching, collectors and storage. Preserve actual access boundaries when fixing
  session behavior.
- The SDK owns collection isolation and reusable query handling, deadlines,
  response limits and availability classifications.
- The application owns business health rules, admin permissions and how one
  unavailable source is presented to its operators.

Report the observed HTTP/error, timestamp, request budget and current evidence.
Separate a confirmed cause from remaining uncertainty and from proposed changes.
Use the existing storage and query paths; installing a skill alone changes no
Control/Observe deployment, cache, retention setting or application instrumentation.
