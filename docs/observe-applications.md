# Standard application observability

`luma-observe` accepts official OpenTelemetry signals and exposes the standard
VictoriaMetrics, VictoriaLogs and Tempo query APIs through an optional vmauth
gateway. Applications own instrumentation, business state, delivery budgets,
dashboards and alert expressions. Luma supplies a shared transport and storage
service; the stack can be deployed independently of Control.

## Data flow

```text
Official SDK ── OTLP ── Collector ── native Metrics ────── VictoriaMetrics
                            ├───── spanmetrics ───────── VictoriaMetrics
                            └───── tail sampling ─────── Tempo
Nomad stdout/stderr ── existing log shipper ────────────── VictoriaLogs

Backend / operator ── read credential ── vmauth ───────── standard query APIs
External producer ── ingest credential ── vmauth ──────── Collector OTLP HTTP
Application config ── read-only extension volume ──────── Grafana / vmalert
```

Native metrics bypass trace sampling. Span metrics are derived before the
storage branch samples successful fast traces at 10%; OTel errors, HTTP 5xx and
traces over one second remain eligible for storage. Metrics describe signals
received by the collector, not proof of complete coverage. SDK sampling,
bounded buffers, memory refusal and exporter failures can still leave gaps.
Collector diagnostics are scraped locally at `127.0.0.1:8888/metrics`; vmauth
diagnostics use `127.0.0.1:8429/metrics`.

The Nomad log shipper reads up to 512 KiB per source and 4 MiB across at most
64 sources per poll, sharing one directory listing per allocation. A poll that
exhausts its byte budget resumes with the next source. Failed exports retain
their checkpoints for retry; alert coverage still requires successful catch-up
of both streams. The backlog gauge sums the last observed unread bytes for all
discovered sources, including those not visited in the current poll.

Official auto-instrumentation alone cannot determine business completion.
Applications can use official custom metrics and spans. Keep metric attributes
bounded (service, task type, outcome), and place individual execution IDs on
spans or structured logs. Keep secrets and request contents out of telemetry.
Use asynchronous bounded exporters and preserve the application's return,
exception and retry behavior when telemetry is unavailable.

## Luma-managed producers

Deploy the shared stack, then redeploy an OTel-capable application. Control
injects the existing `OTEL_*` endpoint, bearer header and resource attributes.
The manager Tailscale listener `:4319` accepts both traces and native metrics.
Application requests and task execution do not pass through the collector.

The injected auto-instrumentation default remains `OTEL_METRICS_EXPORTER=none`
to avoid enabling automatic process metrics for every existing image. An
application that opts into official metric readers can configure
`OTEL_METRICS_EXPORTER=otlp`, or initialize its own official MeterProvider and
bounded reader. Applications can use the optional
[`luma-observe-sdk` Python package](../sdk/python/README.md) for bounded SDK setup,
Taskiq/Celery lifecycle adapters and custom spans/metrics. It is independently
versioned, uses the same official OTLP protocol and existing credentials, and
keeps business fields and outcomes in the application. Direct official OTel
integration remains supported. No extra event database or per-application
collector is needed.

Control 0.2.26 and later merges `OTEL_RESOURCE_ATTRIBUTES` by key: application
values win, and missing injected `luma.stack`, `luma.task` and `luma.region` keys
are appended. Other application environment values remain explicit overrides.
Applications that previously supplied their own resource-attribute string may
gain missing Luma labels once on redeployment, changing those series identities;
applications already using the complete injected attributes are unaffected by
this merge.

Applications with frequent worker replacement can explicitly choose
[service-level span metrics](observe-service-span-metrics.md). The exact resource
flag `luma.observe.span_metrics=service` aggregates only newly derived metrics;
original trace and native-metric instance identities remain intact. This requires
deploying the updated Observe stack separately and works with SDK 0.1.0.
Unflagged applications keep the default spanmetrics path.

## Optional machine access

The default `observe-access` service uses official vmauth 1.114.0. With empty
credentials it starts with zero authorized users. The startup script only
writes vmauth's configuration; upstream vmauth implements authentication,
routing, concurrency limits and timeouts.

The listener is `127.0.0.1:8427`. Every backend store and internal diagnostics
listener remains on loopback. A read credential grants cluster-wide telemetry
read access to its fixed routes; it is not a tenant-level authorization layer.
Use it only from trusted backend services or operator tools, never a browser
bundle. Luma management credentials are separate.

An application backend that only needs its own data should use a scoped reader
instead. `LUMA_OBSERVE_SCOPED_READERS` is a JSON list; each entry gives a reader
`name`, its own `token`, a `namespace` and the Luma `apps` whose logs it may read:

```json
[{"name": "orders", "token": "<32-512 chars>", "namespace": "orders", "apps": ["orders-workers"]}]
```

vmauth adds the filters on the server side, and a client cannot override them:

| Signal | Scoped reader access |
| --- | --- |
| Metrics `/metrics/api/v1/query`, `query_range` | Only series with `service_namespace=<namespace>` |
| Logs `/logs/select/logsql/query` | Only streams whose `app` is listed in `apps` |
| Traces `/traces/api/traces/<32-hex ID>` | Fetch by exact trace ID; search is not available |

Tempo has no gateway-level filter, so a scoped reader cannot search traces. It can
fetch a trace whose ID it already holds, normally taken from its own lifecycle logs.
Trace IDs are random 128-bit values and the reader has no way to list other
applications' IDs, but a leaked ID is readable; the application should still check
that returned spans carry its own `service.namespace`. Every credential, scoped or
not, must be distinct.

Enable each needed role explicitly in the deployment's
`observe/docker-compose.yml`, replacing that role's empty environment value:

```yaml
environment:
  LUMA_OBSERVE_READ_TOKEN: ${LUMA_OBSERVE_READ_TOKEN}
  LUMA_OBSERVE_INGEST_TOKEN: ${LUMA_OBSERVE_INGEST_TOKEN}
```

Keep the service's other environment settings. A role that stays disabled keeps
its literal `""` value. Luma supports `${NAME}` secret references; Compose shell
default syntax is not resolved by Luma. Configure only the enabled roles using
`luma secret set LUMA_OBSERVE_READ_TOKEN --scope luma-observe` and/or
`luma secret set LUMA_OBSERVE_INGEST_TOKEN --scope luma-observe`. Generate distinct
random bearer tokens of 32–512 characters. The CLI prompts privately. The safe
variable list is in [`observe/.env.example`](../observe/.env.example).

Keep enabled roles' `${NAME}` references in the actual deployment configuration
when upgrading Observe. Saved Secrets and container environment values are separate:
a saved Secret does not override a literal empty value in the template. Before
writing the new job, Control compares the previous generated job. If a role had
credentials, its deployment Secret is still saved, and the new environment clears
the credential, the update is rejected with the reference to retain. Fresh
installations still start with zero authorized users.

Rebuild and deploy this independently managed stack from `observe/` using the
existing build workflow, for example `luma build local . --platform linux/amd64`.
A CLI/Control update does not update the stack. The original storage paths,
Tempo 2.10.10 memory settings and 72-hour retention remain in use.

| Role | Allowed path | Upstream API |
| --- | --- | --- |
| Read | `/metrics/api/v1/query`, `/metrics/api/v1/query_range` | Prometheus-compatible instant/range queries |
| Read | `/logs/select/logsql/query` | LogsQL query |
| Read | `/traces/api/search` | Tempo TraceQL search |
| Read | `/traces/api/traces/<32-hex-trace-id>` | Tempo trace lookup |
| Ingest | `/otlp/v1/metrics`, `/otlp/v1/traces` | OTLP/HTTP, JSON or protobuf |

Each read identity, including an application-scoped reader, allows 16 concurrent
requests. The ingest identity stays at four, with 32 requests total across the
gateway and a three-second upstream response-header timeout. These admission
limits accommodate several dashboards sharing a reader; VictoriaMetrics still
executes at most four queries with a 64 MiB per-query memory budget. Its existing
query-result cache remains the cache for metric reads. The gateway does not add
a separate result cache or merge duplicate requests. Clients must handle overload
as unavailable data and keep their own overall timeout and response/row limits.
Incoming Authorization is
removed before the loopback upstream. Store management, reload, debug and write
APIs are outside the read route set. vmauth diagnostics use a separate listener.

For a producer outside the mesh, explicitly install
[`traefik-access.example.yml`](../observe/traefik-access.example.yml) in the
manager's existing Traefik file-provider directory after substituting a DNS/TLS
host. Resolve that host through the existing ingress. The example publishes
only the authenticated gateway; it creates no route automatically and changes
no existing `/grafana` session authentication.

An external official SDK uses:

```text
OTEL_EXPORTER_OTLP_ENDPOINT=https://observe-api.example.com/otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<ingest-token>
```

External applications do not have to be deployed by Luma. Identify them with
standard `service.namespace`, `service.name`, `service.instance.id` and
`deployment.environment.name` resources to use the same Metrics, Trace and
Grafana facilities. This does not create a Nomad deployment entry or provide its
live stdout view. There is currently no external OTLP Logs ingestion route.

The pinned Prometheus Remote Write exporter requires cumulative temporality for
monotonic counters and histograms. The Python SDK defaults to cumulative;
external SDKs or an explicit DELTA preference must use cumulative instead.
The current pipeline has no delta-to-cumulative conversion.

The header follows the official OTel SDK environment-variable format; URL-encode
the token when required by that SDK. Existing Luma-managed applications keep the
automatically injected mesh endpoint and token. A backend query client uses the
same HTTPS host with `/metrics`, `/logs`, `/traces` base paths and its separate
read token. Log ingestion remains the existing Nomad stdout/stderr shipper;
this gateway adds no log write endpoint.

For rotation, replace the appropriate scoped secret and redeploy the stack,
then update consumers. The gateway accepts one credential per role, so plan a
short read outage or telemetry loss during rotation. Application execution
must remain independent of telemetry delivery. To disable a role intentionally,
first remove its saved scoped Secret, then restore its literal empty environment
value and redeploy. For legacy global credentials, remove the global Secret.
Remove the explicit Traefik file
when HTTPS machine access is no longer needed.

## Application-owned dashboards and rules

The optional Python package includes `luma-observe-dashboard`, an offline JSON
configuration compiler for standard Grafana dashboards. Applications choose their
metrics, variables, panel types, units and thresholds in configuration; see the
[SDK examples](../sdk/python/examples/dashboards/). Its output uses the existing
extension delivery below and does not need another service or credential.

Dashboard configuration can live independently of application source. For private
dashboards in an open-source checkout, keep the configuration and generated files
in a local directory excluded through `.git/info/exclude`, and back them up
through your internal file-delivery process. Do not publish business-specific
queries or identifiers in the Luma repository.

### Task investigations and traces

An application's **Dashboards** page provides Metrics, Tasks, Executions and Traces. Metrics loads the application's associated Grafana JSON. The other views read existing VictoriaMetrics, VictoriaLogs and Tempo data using standard SDK signals.

- **Tasks** shows observed tasks, execution outcomes, duration and queue P95, activity, oldest execution, progress age, process heartbeats and SDK drop counters. Schedule declarations, actual Redis queue depth and business database state belong to the application.
- **Executions** filters by task name, task ID, queue, outcome or reported attribute. Attempts are grouped by task, with retry outcomes, accumulated phase durations, exception summaries and lifecycle events. Pagination fixes the end of the window and indicates partial evidence and read limits.
- **Traces** supports filtered search, minimum duration and exact Trace IDs. The waterfall includes parent/child spans, collapsing, zoom, error filtering, self time, attributes, events and Span Links. Exception details and related logs load on demand; nearby process logs are explicitly identified as context.

Choose a 1, 6, 24 or 72-hour window. Tasks optionally refreshes every 30 seconds while visible; execution details and traces are queried on demand. Missing samples remain unknown. Traces are sampled and retained for a limited period; use Metrics for execution counts. Generic views preserve reported names; application-specific display aliases can continue to live in custom Grafana dashboards.

Control installs the published `luma-observe-sdk[query]==0.6.2` through its `control` extra. Reads use fixed local observation endpoints, application scopes and the existing console authentication. Links preserve views, filters and Trace IDs. These interactive views require console sign-in. Password-protected Grafana shares continue to serve their explicitly shared metrics dashboards. Update Control to receive these views; collection has its own deployment lifecycle.

### Link dashboards to an application

Add the exact tag `luma-app:<application-name>` to each dashboard's `tags` array,
in the compiler input or standard Grafana JSON. For example:

```json
"tags": ["luma-app:orders-workers", "tasks"]
```

After installation, open **Applications → orders-workers → Dashboards**. The
page discovers up to 100 tagged dashboards through the existing Grafana Search
API when opened; **Refresh list** discovers later additions. Choose a dashboard
or open it in a new tab. The `dashboard=<uid>` URL parameter preserves a selected
dashboard in shared links. Dashboards with an `app` variable receive the current
application name; fixed-query dashboards must include their own scope filters.

Tags associate dashboards with navigation; they do not enforce data isolation.
Access uses the existing Control login and Grafana permissions. The page stores
no telemetry, adds no query backend, and performs no background list polling.
An unavailable Grafana only prevents this tab from loading. Dashboard panels use
their configured refresh intervals and existing Observe data sources.

Updating dashboard files requires no Control or application redeployment. The
Dashboards tab itself requires a Control version that includes this feature.

### Install and update definitions

The `observe-extensions` volume is a shared read-only config mount in Grafana
and vmalert. Its manager path is
`/srv/luma/data/luma-observe/extensions`; initial creation accepts an empty
directory. Existing store volumes keep their names and paths.

Install reviewed application configuration through the operator's normal file
delivery process. Use unique dashboard UIDs, rule group names and recording
metric names across applications:

```text
extensions/
  dashboards/<application>.json    # standard Grafana dashboard JSON
  rules/<application>.yml          # standard vmalert/Prometheus rule groups
```

Grafana's Applications folder discovers dashboard JSON recursively. vmalert
loads `rules/*.yml` and `rules/*.yaml` alongside the bundled infrastructure
rules. Both re-scan every 30 seconds. Use an atomic rename from a temporary
file with a different extension, with directories readable/traversable and
files readable by Grafana/vmalert. Treat application rule configuration as
trusted operator input; validate expressions and cardinality before installation.

Validate rule files with the pinned `victoriametrics/vmalert:v1.114.0` binary
using `-rule=<file> -dryRun`. Verify the actual dashboard and `/api/v1/rules`
after installation; a parse failure keeps the prior valid rules active.
Delete an application's file to remove that configuration on the next reload.
This loads definitions, not a new notification receiver: use the existing
Alertmanager routing, grouping, maintenance silences and authorized receivers.

## Verification

From the repository root with Luma dependencies and Docker installed:

```bash
python scripts/check-observe-protocols.py --platform linux/amd64
python scripts/check-observe-tempo.py --platform linux/amd64
python scripts/check-observe-sdk-protocols.py --platform linux/amd64
```

CI runs all three. The platform protocol check uses official packaged components
and generic synthetic signals. It verifies zero-user startup, independent roles, protected
mesh metrics, native counters, span metrics before sampling, retained error and
slow traces, LogsQL/TraceQL reads, internal diagnostics, empty extension startup,
and live dashboard/rule reload. It disables notification delivery and removes
its isolated containers, volumes and temporary image tags. The separate Tempo
check retains the fresh-storage and persisted-block restart verification.
The SDK check installs the independent package, exports native task/custom metrics
through Collector to VictoriaMetrics, provisions both generated Grafana examples,
and evaluates their PromQL expressions against the collected data.

After deployment, verify these paths using the actual configured roles, then
check a representative instrumented application. Observe exporter failures,
receiver refusal, source freshness, CPU/RSS, disk growth and storage retention.
These protocol checks are not a throughput benchmark or a disk quota: Tempo
still shares the manager disk and has no free-space guard. Existing storage and
memory protections remain necessary; application-specific budgets and acceptance
criteria belong in each application's deployment runbook.
