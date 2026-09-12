# Observability

Luma exposes node/container samples, current task queues, resource history, runtime events, persistent alerts and resumable application logs. These observations are not a measurement of end-user availability. Public-route verification during deploy/restart remains a separate capability.


## Console navigation

Observability has separate pages for alert incidents, application monitoring, resource metrics, logs, alert rules and notification channels. Each uses its own compact page title. The engine summary belongs to alert incidents; notification channels focus on channel configuration and delivery history. Built-in alert pages refresh every 15 seconds, and resource history refreshes every 30 seconds. See the [console guide](dashboard-guide.md) for UI workflows and the distinction between route relationships and connectivity probes.


## Application alerts (`luma-observe`)

Control dashboards show node/container samples. Live allocation tails remain
an on-demand Control proxy. Application HTTP 5xx, latency (p90/p95/p99), Nomad
failed allocations, restart storms, traces and searchable stdout/stderr are an
optional [`observe/`](../observe/) Compose app. Deploy it with Luma; skip it
and Control still runs. Operators look at Dashboard → Observability → Apps,
which embeds Grafana at `/grafana` on the Control domain. HTTP, Nomad and trace series are labeled with the Luma app/stack name as soon as
observe is deployed; applications do not opt in. Failed gauges are the
allocations Nomad still wants to run, not lifetime failure counters.
The stack uses host networking and loopback listeners (`127.0.0.1:8082` Prometheus,
`127.0.0.1:4318` OTLP, `127.0.0.1:3200` Tempo). Application tasks send traces to the manager Tailscale mesh listener on port 4319 with a Control-issued bearer token. Control uses host networking and queries `http://127.0.0.1:8428` when observe is present. Grafana on `127.0.0.1:3100`
is local debug only. Alerts evaluate in vmalert/Alertmanager. Control SQLite
is not on this path. Trace and log storage retain 7 days; metric retention is
configured independently.

Deploy from `observe/` with `luma build local . --platform linux/amd64 --env .env`.
Refresh Traefik after the current CLI includes the loopback metrics/OTLP flags
so RED rules have a scrape target. Operators look at Dashboard → Observability → Apps.
The charts are per Luma app (Traefik HTTP rate, 5xx and p90/p95/p99 latency + Nomad health), not each container's `/metrics`. Traefik histogram buckets are `0.05,0.1,0.25,0.5,1,2.5,5,10` seconds so those quantiles are distinguishable.
Selecting observe also injects official OpenTelemetry environment variables into later Luma deploys (`OTEL_SERVICE_NAME`, `luma.stack` / `luma.task` / `luma.region`, OTLP HTTP to the mesh listener). Apps that already ship an OpenTelemetry distro emit spans without a Luma SDK. Redeploy existing apps after observe is first enabled. Apps export traces fail-open; the collector keeps HTTP 5xx, OTel errors, and traces slower than 1s, plus a 10% baseline. Near the collector memory limit it drops new traces instead of OOM.
Do not ship raw access logs or traces off the manager public interface.

## Application integration

Deploying [`observe/`](../observe/) is the cluster opt-in. After that, new Luma
deploys get OTel wiring automatically. A process restart of an old job is not
enough; the Nomad job must be rendered again. There is no Luma telemetry SDK
and no language agent injection.

What you get without changing the app:

- Traefik HTTP RED (rate, 5xx, p90/p95/p99) labeled by Luma app name
- Nomad running / current failed / restart counts labeled by Luma app name
- An ingress span for public HTTP (`cn-edge` / `external-edge`) stored in Tempo

What you get after a new deploy, only if the image already speaks OpenTelemetry:

- Process spans exported to Tempo with `luma.stack` / `luma.task` / `luma.region`
- Look at Dashboard → Observability → Apps → Traces, or Grafana `/grafana/d/luma-traces`

What you still do not get: nginx / static Go / images with no OTel SDK, traffic
that never hits Traefik, request bodies, SQL text, or every successful fast
request (5xx and >1s traces are kept; the rest are sampled at 10%). Export is
fail-open.

### Manual spans

Do not set `OTEL_EXPORTER_OTLP_ENDPOINT` or the bearer header. Control already
injects them. Use the official OpenTelemetry API and keep exporters fail-open.

Install the official distro plus OTLP exporter in the image, for example Python:

```text
opentelemetry-distro
opentelemetry-exporter-otlp
```

Then start with `opentelemetry-instrument` (or the equivalent for Node/Java) so
the SDK reads the injected env. Add a span around a business step:

```python
from opentelemetry import trace

tracer = trace.get_tracer("orders")

def charge(order_id: str) -> None:
    with tracer.start_as_current_span("charge_card") as span:
        span.set_attribute("order.id", order_id)
        provider.charge(order_id)
```

Node:

```javascript
const { trace } = require("@opentelemetry/api");
const tracer = trace.getTracer("orders");

async function charge(orderId) {
  return tracer.startActiveSpan("charge_card", async (span) => {
    span.setAttribute("order.id", orderId);
    try {
      return await provider.charge(orderId);
    } finally {
      span.end();
    }
  });
}
```

Do not put secrets, tokens, or full request bodies on span attributes. Custom
span names show up under the same `luma.stack` as the process. If the image has
no OpenTelemetry SDK, these calls are no-ops unless you add the distro.

## Dashboard and logs

Dashboard → Observability opens incidents. Metrics have a dedicated page; Logs
opens Grafana Explore against VictoriaLogs (7 days). Rules and notification
channels have separate list and edit URLs. Storage governance is under
Infrastructure → Storage → Data governance.

The metrics page shows the actual sampled time span and retention, separates missing/stale/failed queries, and breaks resource lines across sampling gaps. Select a service for its resource history, or open observability logs filtered by its stack.

Observability logs are Nomad allocation stdout/stderr shipped by luma-observe
into VictoriaLogs. Filter by `app` (the Nomad job / Luma stack). Traefik JSON
access logs are the `traefik` job's stdout; unpack with LogsQL `unpack_json`.
Line time is observation time because Nomad frames have no event timestamps.
`luma service logs` and the application-detail live tail still follow Nomad
files through Control and are bounded excerpts, not this 15-day store.

Resources are retained in 30-second buckets, independently of the number of nodes reporting a service. `LUMA_METRICS_HISTORY_POINTS` defaults to 720 (approximately six hours), bounded between 60 and 10000. Resource history remains a separate bounded `metrics-history.json` file in the Control state directory; collection outside Control is still useful for longer investigations and independent outage detection. See [Control storage and recovery](control-storage.md) for backup scope. A service aggregate covers reporting nodes; missing contributions expire after 180 seconds. It is not proof that all replicas have reported.

## Built-in alert rules and notifications

The Dashboard alerting page manages rules, notification channels and incidents.
Rules, incident transitions and the notification outbox persist in the Control
SQLite database. The evaluator uses samples and task state already reported to
Control; creating a resource rule does not start a public HTTP probe.

Available presets cover node heartbeat age, sustained CPU, memory, disk and
inode usage, queued-task age, the latest failed build per application, and —
when luma-observe is deployed — application p95 latency, HTTP 5xx ratio and
Nomad failed allocations. Observe samples are instant PromQL reads from
VictoriaMetrics; if observe is down those rules keep their last state
(`noData: keep`) instead of firing.
Thresholds and the required continuous duration are configurable. A pending
incident becomes firing only when its condition lasts for that duration; a
healthy observation resolves it. One active incident is maintained per rule
and target, with a configurable repeat interval for continued failures.

Missing data is explicit: the `keep` policy preserves an existing firing state
without treating missing samples as recovery, while `alert` treats missing data
as a condition to evaluate. CPU rules use fresh Linux host CPU observations;
macOS load averages are not interpreted as CPU percentages.

Acknowledging an incident records an acknowledgement and stops its repeated
notifications; it does not mark the underlying condition healthy. Global and
per-rule timed silences suppress notifications while keeping evaluation and
incident history available. Pending/retry deliveries wait until silence expiry;
a silence cannot retract an HTTPS request already in flight. Changing a rule's
condition gives its old incidents the `closed` status with a rule-change event,
not a healthy `resolved` transition.
Dashboard overview cards that are temporarily hidden in one browser are a different UI preference and do not silence alerts.

The built-in Feishu channel uses an application bot. Fill in **App ID**,
**App Secret** and the destination **group chat ID** in the channel form.
Enable the application's bot capability, grant and publish
`im:message:send_as_bot`, and add the application bot to that group. Associate
the saved channel with a rule, then explicitly send a test notification.
Channel tests send a real message to the configured group and bypass silences.

Luma requests the tenant access token automatically and caches it only in
process memory, refreshing before expiry. Operators do not configure a tenant
access token. Channel reads return App ID, chat ID and `appSecretConfigured`;
they never return App Secret. The App Secret is stored in the private Control
SQLite database and therefore belongs to its protected backup scope. Omitting
App Secret while editing preserves the stored value; replace it to rotate the
credential and obtain a new cached token.

Evaluation runs independently about every 15 seconds. A separate delivery loop
checks the persistent outbox about every second, so a slow provider request
does not pause evaluation. Pending, sent, retrying and failed deliveries remain
visible. Network/rate-limit failures retry with backoff up to eight attempts;
invalid credentials, missing permission and an inaccessible group fail with an
actionable category. Each HTTPS request has an eight-second timeout within a
16-second delivery budget.

A successful response confirms acceptance by Feishu, not that a human read the
message. Outbox retries reuse a stable message UUID for provider deduplication;
this remains at-least-once delivery, not an unlimited exactly-once guarantee.
Check delivery history for failures before changing a rule to make it fire
again.
Resolved/closed incidents and final deliveries enter the reviewed storage
retention plan after its summary retention period (90 days by default); active
incidents and outstanding deliveries are protected. See [Control storage](control-storage.md).

Alerting management APIs under `/v1/alerting/` require the management token;
the metrics-only token cannot manage them. No notification channel is
provisioned automatically. The Control must remain available to evaluate and deliver its own alerts; retain an independent
Prometheus scrape or external probe for Manager/Control outages. Built-in
resource alerts do not probe public URLs. Application p95, 5xx ratio and
failed-allocation presets read luma-observe; they are not a substitute for an
external Control-down probe.

## Host disk samples

Updated node agents sample the filesystem containing `/opt/luma`, or `/` if that directory does not exist. Set `LUMA_METRICS_DISK_PATH` in the node agent service environment to select another absolute path. The Dashboard identifies the sampled path; this does not measure every mount, remote NFS server capacity or Docker VM capacity on macOS. A missing filesystem or unsupported inode counter is unavailable, not zero usage.

Disk usage excludes reserved blocks from usable capacity, matching the usual `df` interpretation. The available-bytes value excludes reserved blocks too. Existing agents continue to work but will not supply these additional samples until updated.

## Prometheus endpoint

`GET /v1/metrics` emits Prometheus text exposition. It reads the persisted heartbeat snapshot without contacting Nomad, contacting agents or changing control state. It supports the management token. A remote collector should use a dedicated read-only metrics token.

The bundled luma-observe collector colocates on the manager host network and scrapes `http://127.0.0.1:8080/v1/metrics` without a token — the same loopback trust as Traefik `:8082`. A request that presents an `Authorization` header is still validated. Remote Prometheus scrapes continue to need the dedicated token.

On the manager, provision a random token of at least 32 ASCII characters in `/opt/luma/control/metrics-token`. The file must be a private regular file, readable by the Control process (typically mode 0600), not a symlink. The existing `/opt/luma` Control mount makes this default path visible to the container. It is read on each request, allowing atomic rotation without restarting Control. No token is created automatically.

An explicitly configured `LUMA_METRICS_TOKEN_FILE` overrides that path. For custom Control installations, expose the absolute path inside the process/container and set the environment variable there; an arbitrary manager shell environment is not automatically forwarded to the Nomad job.

Copy the token securely into the Prometheus collector's credentials file. This dedicated token is accepted only by `/v1/metrics`; it cannot query the Dashboard, read application logs, deploy, restart or access LAE tenant APIs. Keep it distinct from every other Luma token. Endpoint output includes infrastructure names and region labels. Remote scrapes remain authenticated; the colocated luma-observe scrape is loopback-only.

The example [Prometheus scrape config](./examples/monitoring/prometheus.yml) and [alert rules](./examples/monitoring/luma-alerts.yml) are opt-in templates. Replace the hostname and credential path, check with your Prometheus version, and configure an Alertmanager destination before expecting notifications. These external templates are separate from Luma's built-in alert rules and Feishu channels described above. Applying Prometheus/Alertmanager configuration is a separate infrastructure change; it does not create a Luma notification channel.

Important metrics:

| Metric | Meaning |
| --- | --- |
| Prometheus `up{job="luma-control"}` | Scrape success, including connectivity and authentication; not application health |
| `luma_node_agent_up` | Heartbeat within 120 seconds; not Nomad scheduling eligibility |
| `luma_node_heartbeat_timestamp_seconds` | Last heartbeat received by Control |
| `luma_node_metrics_timestamp_seconds` | Last host sample received by Control |
| `luma_node_cpu_used_ratio` | Linux host CPU fraction; Darwin load estimate is deliberately excluded |
| `luma_node_load1` | Host one-minute load average |
| `luma_node_memory_*` | Host memory capacity, availability and used fraction |
| `luma_node_filesystem_*` | Sampled filesystem capacity, availability, usage and inode fraction, labeled by path |
| `luma_service_cpu_cores` | Container CPU summed by service and node, expressed in cores |
| `luma_service_memory_bytes` | Container memory summed by service and node |
| `luma_service_observed_containers` | Containers present in the agent snapshot, not desired replica count |
| `luma_node_unresolved_containers` | Containers lacking a persisted job identity and omitted from service gauges |
| `luma_tasks` | Current queued/running records by task kind, not a cumulative counter |
| `luma_task_queue_oldest_age_seconds` | Oldest dated queued task; kinds can refer to the same workflow and must not be summed blindly |

Host/container resource series expire after 120 seconds without fresh samples; they are omitted rather than kept at stale values or reported as zero. Separate collection timestamps are available after Control is updated; older stored records fall back to their heartbeat time until replaced. Historical counters, latency percentiles, desired replicas and HTTP error rates cannot be derived from this endpoint.

Service resource labels preserve the reported task identity. Registered Compose jobs map `job + task` to the Dashboard's `job_task` name; other jobs retain their job name plus a separate task label. Containers known only by allocation ID are counted as unresolved instead of guessing a service name or making a scrape query Nomad. Check that unresolved count when judging coverage.

For request rate, status codes and latency, enable Traefik metrics and join the router/service labels to Luma application identity. Private workers and routes that bypass Traefik need separate instrumentation. For Control outages, run the collector or an independent probe outside the Control process/failure domain.

Official references: [Prometheus scrape configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/), [Traefik metrics](https://doc.traefik.io/traefik/observe/metrics/), [Alertmanager grouping and silences](https://prometheus.io/docs/alerting/latest/alertmanager/).
