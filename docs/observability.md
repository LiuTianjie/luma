# Observability

Luma exposes node/container samples, current task queues, resource history, runtime events, persistent alerts and resumable application logs. These observations are not a measurement of end-user availability. Public-route verification during deploy/restart remains a separate capability.

## Dashboard and logs

The observability page shows the actual sampled time span and retention, separates missing/stale/failed queries, and breaks resource lines across sampling gaps. Select a service for its resource history, or open its logs from the application detail.

Logs follow Nomad allocation files using byte-position cursors. Select an allocation to distinguish replicas; reconnecting resumes from the last received cursor. Rotation or missing retained files is reported as a gap. The initial tail and snapshot download are bounded recent excerpts, not complete archives. Arbitrary time filtering is not offered because application stdout/stderr need not carry reliable timestamps. Log text is not deduplicated by content: identical repeated lines are distinct records.

Resources are retained in 30-second buckets, independently of the number of nodes reporting a service. `LUMA_METRICS_HISTORY_POINTS` defaults to 720 (approximately six hours), bounded between 60 and 10000. Resource history remains a separate bounded `metrics-history.json` file in the Control state directory; collection outside Control is still useful for longer investigations and independent outage detection. See [Control storage and recovery](control-storage.md) for backup scope. A service aggregate covers reporting nodes; missing contributions expire after 180 seconds. It is not proof that all replicas have reported.

## Built-in alert rules and notifications

The Dashboard alerting page manages rules, notification channels and incidents.
Rules, incident transitions and the notification outbox persist in the Control
SQLite database. The evaluator uses samples and task state already reported to
Control; creating a resource rule does not start a public HTTP probe.

Available presets cover node heartbeat age, sustained CPU, memory, disk and
inode usage, queued-task age and the latest failed build per application.
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
resource alerts do not currently establish end-user route availability, HTTP
error rates, p95/p99 latency or desired replica compliance.

## Host disk samples

Updated node agents sample the filesystem containing `/opt/luma`, or `/` if that directory does not exist. Set `LUMA_METRICS_DISK_PATH` in the node agent service environment to select another absolute path. The Dashboard identifies the sampled path; this does not measure every mount, remote NFS server capacity or Docker VM capacity on macOS. A missing filesystem or unsupported inode counter is unavailable, not zero usage.

Disk usage excludes reserved blocks from usable capacity, matching the usual `df` interpretation. The available-bytes value excludes reserved blocks too. Existing agents continue to work but will not supply these additional samples until updated.

## Prometheus endpoint

`GET /v1/metrics` emits Prometheus text exposition. It reads the persisted heartbeat snapshot without contacting Nomad, contacting agents or changing control state. It supports the management token, but a collector should use a dedicated read-only metrics token.

On the manager, provision a random token of at least 32 ASCII characters in `/opt/luma/control/metrics-token`. The file must be a private regular file, readable by the Control process (typically mode 0600), not a symlink. The existing `/opt/luma` Control mount makes this default path visible to the container. It is read on each request, allowing atomic rotation without restarting Control. No token is created automatically.

An explicitly configured `LUMA_METRICS_TOKEN_FILE` overrides that path. For custom Control installations, expose the absolute path inside the process/container and set the environment variable there; an arbitrary manager shell environment is not automatically forwarded to the Nomad job.

Copy the token securely into the Prometheus collector's credentials file. This dedicated token is accepted only by `/v1/metrics`; it cannot query the Dashboard, read application logs, deploy, restart or access LAE tenant APIs. Keep it distinct from every other Luma token. Endpoint output includes infrastructure names and region labels, so the endpoint remains authenticated.

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
