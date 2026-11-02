# luma-observe

Optional application observability for a Luma cluster. Deploy it with Luma
like any other Compose app. Control and other clusters keep working if it
is absent.

It colocates with Traefik on the manager host network. Traefik metrics and traces use loopback; application OTLP uses the authenticated Tailscale mesh listener. Application requests do not pass through the collector. Dashboard queries and notification delivery generate their own traffic.

## What it watches

- Traefik Prometheus on `127.0.0.1:8082` (public HTTP rate, 5xx, p90/p95/p99)
- Traefik OTLP on `127.0.0.1:4318` (sampled traces stored in Tempo, 3 days)
- Nomad allocation stdout/stderr in VictoriaLogs (`127.0.0.1:9428`, 15 days, at most 4 GiB)
- Application OTLP on the manager Tailscale IP port `4319` (bearer token from Control)
- Nomad job running / current failed / live restarts via a local exporter

App names are the Luma stack/job names already in Nomad. Deploying observe
maps Traefik routers onto those names. Existing apps do not need a redeploy,
extra YAML, or an SDK.

After it is deployed, open Luma Dashboard → Observability → App monitoring.
That tab embeds Grafana at `/grafana` on the Control domain. Luma writes
the Traefik route; you do not add a second domain. Do not open Grafana on
Tailscale or port 3000. The route asks Control to check every request, so
`/grafana` needs a console session (or the management token) and returns 401
otherwise.

The bundled rules run in vmalert/Alertmanager. Separately, Control's dashboard-managed application p95, 5xx ratio and failed-allocation presets read VictoriaMetrics and use Control's own incident and notification storage. Allocation stdout/stderr is shipped to VictoriaLogs by observe. Dashboard → Observability → App monitoring → Logs opens Grafana Explore. `luma app logs` and the application-detail live tail
still go to Nomad through Control and are not a 15-day archive.
Deploying this stack is the observability component: later Luma app deploys
receive official OTel env vars automatically. Redeploy existing apps to pick
them up. Applications can use official OpenTelemetry directly or the optional
[Luma Observe Python SDK](../sdk/python/README.md). Restarting an old allocation
is not enough.

## Application integration

See [Standard application observability](../docs/observe-applications.md)
([中文](../docs/observe-applications.zh-CN.md)) for native metrics, optional
machine query/ingest credentials and application-owned dashboard/rule loading.

Public HTTP already has Traefik RED and an ingress span. Process spans need
official OpenTelemetry instrumentation in the image, directly or through the
optional [Luma Observe Python SDK](../sdk/python/README.md). Control injects
the existing `OTEL_*` endpoint, credentials and resource settings on the next deploy. The collector
keeps 5xx, OTel errors, and traces slower than 1s, plus a 10% baseline;
it refuses telemetry above its memory budget; sustained overload can drop data.
Manual spans use
`opentelemetry.trace.get_tracer(...).start_as_current_span(...)` (or the
language equivalent). See [Application integration](../docs/observability.md#application-integration).

## Deploy

Collector has a 768 MiB container ceiling, a 384 MiB Go memory target, a
512 MiB memory limiter and bounded batches/export queues. Spans of node agent
requests to Control (`/v1/node-agent/`, `/v1/terminal/`) are dropped first:
they are long-lived by design and would otherwise all count as slow. Tail sampling keeps
at most 5,000 pending traces; inactive span-metric series expire after five
minutes. These bounds preserve room for transient and non-heap allocations.

VictoriaMetrics retains 15 days under a 1 GiB container ceiling. Its cache
allowance is 384 MiB, with at most four concurrent queries and 64 MiB per query;
cache limits alone do not cap total process memory.

Tempo uses 2.10.10, including the [Parquet compactor row-pool memory fix](https://github.com/grafana/tempo/pull/4985).
Its container ceiling is 3 GiB with a 2 GiB Go memory target,
leaving room for file cache and non-Go allocations. Parquet row groups are
limited to a 25 MB target instead of the upstream 100 MB default, alongside
one concurrent ingester flush. Search-buffer and retention concurrency settings
do not bound compactor memory. A small container can spend most of its time
reclaiming and rereading file pages even without an OOM kill or a full disk.
The Go memory target is soft and does not reserve file cache. Verify actual container limits and observe
compaction, WAL recovery and query I/O after deployment; these settings do not
isolate the manager's disk from Tempo.

Disk is shared with Control on the manager.
VictoriaLogs keeps 15 days but drops its oldest days beyond 4 GiB (about
230 MB/day at current traffic). VictoriaLogs and VictoriaMetrics stop
accepting new data when less than 2 GiB is free. Tempo has no size limit or
free-space guard; its time retention and ingest rate are not a disk quota.
It keeps 3 days of
tail-sampled traces (errors, slow requests and a 10% baseline) with ingest
limited to 2 MiB/s and 2 MiB per trace. Older Tempo blocks, including those
from the previous 14-day setting, are removed by the compactor after the
next deploy. Local storage paths are unchanged.

Traefik exports ingress metrics through Prometheus only, and retains bounded,
asynchronous OTLP trace export. Its 512 MiB container has a 384 MiB Go memory
target. A Nomad check of the local metrics endpoint restarts Traefik after
three failed checks, with a one-minute startup grace period. Collector failure
must not make public HTTP requests wait for telemetry delivery.

luma-observe colocates with Traefik and Control on the manager host network.
Control pins the stack to the registered manager node (not the name `manager`)
and injects Grafana's public URL from the Control domain. Do not edit node
names or hardcode a Grafana host.

### First deployment

Use `observe/` from the current Luma source checkout or release archive. Have a
working Control login and internal Registry configured for Local Build; the
machine running the build needs Docker Buildx. Check the manager's architecture,
available RAM and free disk before adding the stack. Tempo alone reserves 1 GiB
for scheduling and can use up to 3 GiB; allow that headroom in addition to
Control, Traefik, the other observe services and any colocated applications.
The reservation is not a promise that the complete stack fits in 1 GiB.

Build and deploy from this directory (AMD64 manager example):

```bash
luma build local . --platform linux/amd64
```

Control prepares the local data directories and pins the whole stack to the
registered manager. No manual Tempo start, config copy, node-name change or
public port is needed. The Tempo image build verifies its bundled config with
the actual pinned binary before it can be deployed. For ARM64, use
`--platform linux/arm64` with an ARM64 manager.

If Feishu is unset, Alertmanager still records incidents; the webhook returns
204 and does not retry. Traefik RED rules start after Traefik is refreshed
with the loopback metrics/OTLP flags from current `luma/nomad_render.py`
(`luma update manager` refreshes the Traefik job). Nomad failure alerts work
as soon as this stack is running.

### Upgrade or restore a previously disabled Tempo

Update the source used by the stack's recorded build workflow, then rebuild and
redeploy `luma-observe`. For Local Build, run the command above from the updated
`observe/` directory. For a saved repository workflow, point it at the updated
ref and rebuild. Updating the Luma CLI/Control or restarting old containers does
not update this independently deployed stack's images, memory limits or config.

If Tempo was removed manually, deploy the complete current Compose definition,
including the collector's `otlphttp/tempo` exporter and trace pipeline. Reconcile
any saved Compose override or pinned images first: rebuilding only Tempo can
leave the collector discarding all traces. Keep existing volume names and local
paths; `initialize: empty` permits first creation and does not erase an existing
volume. Do not delete the WAL or old blocks to make the upgrade start. Retention
marks expired blocks on its next scan and removes them after the compacted-block
retention delay (one hour by default), so reclaimed space is not immediate.

After deployment, verify the running Tempo version, its actual 3 GiB container
ceiling and `GOMEMLIMIT=2GiB`, not just the source YAML. On the manager, `/ready`
on `127.0.0.1:3200` must return 200. Query a recent sampled trace in
**App monitoring → Traces**; readiness alone does not prove collector delivery
or storage. Observe memory, OOM/restarts, compaction errors, disk I/O wait and
free space through recovery and compaction. A blocks-only query
(`/api/traces/<trace-id>?mode=blocks`) proves persisted retrieval; allow the
default five-minute block flush and subsequent five-minute blocklist scan.

### Maintainer verification

From the repository root, with the Python package dependencies and Docker
installed:

```bash
python scripts/check-observe-tempo.py --platform linux/amd64
python scripts/check-observe-protocols.py --platform linux/amd64
```

The source CI runs this check on PRs and main pushes. It builds the bundled
collector and Tempo, starts with empty storage using the configured memory
limits, sends a slow successful trace through tail sampling, flushes it, then
recreates Tempo and queries the persisted block. It uses an isolated network
namespace with no published ports and removes its containers, volume and image
tags afterward. It does not contact a cluster or simulate production load;
capacity and old-block compaction still need observation on the target manager.

## Listeners (manager loopback only)

| Port | Process |
| --- | --- |
| 8082 | Traefik Prometheus |
| 4318 / 4317 | Collector OTLP HTTP / gRPC (loopback, Traefik) |
| 4319 | Collector OTLP HTTP on the manager Tailscale IP (apps, bearer auth) |
| 3200 / 3201 | Tempo query (loopback) |
| 4418 / 4417 | Tempo OTLP ingest (loopback) |
| 8428 | VictoriaMetrics on 127.0.0.1; host-gateway also binds Nomad/docker0 so Control can scrape |
| 9428 | VictoriaLogs (allocation stdout/stderr, 15 days, at most 4 GiB) |
| 8880 | vmalert |
| 8888 | Collector internal Prometheus diagnostics |
| 8427 | vmauth optional machine query/OTLP gateway (zero users by default) |
| 8429 | vmauth internal metrics/diagnostics |
| 9093 | Alertmanager |
| 9107 | nomad-exporter |
| 9108 | log-shipper |
| 9095 | feishu-webhook |
| 3100 | Grafana on 127.0.0.1; published at `/grafana` on the Control domain |

Do not publish these on `0.0.0.0` or the public NIC.
`host-gateway` is part of this optional stack: it exposes VictoriaMetrics on
the Nomad/docker0 bridge. Control itself is host-networked and queries
`http://127.0.0.1:8428`. Grafana listens on `127.0.0.1:3100` and is published
at `/grafana` on the Control domain.
