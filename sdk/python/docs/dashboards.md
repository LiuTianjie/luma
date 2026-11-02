# Application dashboard configuration

The SDK includes an offline configuration tool. Applications own the JSON input,
metric names, dimensions and queries; the output is a standard Grafana dashboard.
The command uses local files and preserves the existing Grafana, query and
`luma secret` authentication setup.

```sh
luma-observe-dashboard tasks.config.json --output dashboard-output/application-tasks.json
```

Start with [`tasks.config.json`](../examples/dashboards/tasks.config.json) for SDK
framework metrics or [`custom-metrics.config.json`](../examples/dashboards/custom-metrics.config.json)
for application-defined signals. Give every application a unique `uid` and output
filename. Configure `service.name` and `deployment.environment.name` in SDK resource
attributes for the task template's service/environment filters. Metrics expose
these as `service_name` and `deployment_environment_name` in the current collector.

The tool supports the following configuration:

| Field | Meaning |
| --- | --- |
| `schema` | Configuration version, currently `1` |
| `uid`, `title` | Stable Grafana dashboard identity and title |
| `datasource` | Existing Prometheus-compatible data source UID; defaults to `victoria` |
| `refresh`, `time`, `timezone`, `tags` | Standard refresh period, time range, timezone and tags |
| `variables` | Named query, custom or constant Grafana variables |
| `panels` | Stat, timeseries, table, gauge or bar gauge panels |

A query variable has a `name`, `query`, optional `label`, `multi`, `includeAll`
and `allValue`. Query variables accept Grafana's existing Prometheus variable
query syntax, such as `label_values(metric, label)`. Use `${variable:regex}` in
regular-expression selectors for multi-value variables. Constant and custom
variables use Grafana's standard string values. Variables are filtering controls;
platform credentials and authorization remain the existing operator contract.

Each panel has a `title` and either a single `expr` (with optional `legend` and
`instant`) or a `targets` list of those query objects. Expressions are forwarded
unchanged to Grafana and may use Grafana time-range variables. Timeseries targets
default to range queries; other panel types default to instant queries. Empty
metrics remain empty so an unavailable signal can be distinguished from a
measured zero.

Use `unit`, `decimals`, `min`, `max` and native Grafana `thresholds` for concise
field configuration. Grafana's existing `fieldConfig`, `options` and
`transformations` objects allow application-specific legends, value mappings and
table columns. These native settings follow the deployed Grafana version. The
compiler validates its configuration contract and finite JSON values; Grafana
validates native panel options and the metrics backend validates PromQL.

The output is deterministic and caps dashboard size, variables, panels and
queries. Writing validates the whole definition, uses a temporary `.tmp` file,
then atomically replaces the `.json` output. A failed validation or replacement
preserves the previous dashboard.

Deliver the reviewed output through the existing operator file delivery process
to `/srv/luma/data/luma-observe/extensions/dashboards/`. Keep compiler input
files outside the extension tree, because Grafana recursively loads every JSON
file there. Grafana discovers the output in the Applications folder within
its existing 30-second provisioning interval. The command does not deploy files,
create data sources, create credentials or modify the observation stack.

Applications that already maintain full Grafana dashboard JSON can continue
delivering it directly. Standard vmalert/Prometheus rules remain ordinary YAML
under `extensions/rules/`, validated and loaded by the existing pipeline. Grafana
panel thresholds only change panel coloring; alert notifications use those rules
and the existing Alertmanager route.

Confirm the deployed dashboard, variable options and queries against received
application signals. The task template reports framework attempts and sampled
metric windows. It does not define business delivery, queue backlog or progress
deadlines for an application.

The template also includes process and active-task signals:

| Signal | Template behavior |
| --- | --- |
| `worker_heartbeat_seconds` | Collection-time Unix timestamp, displayed as `time() - worker_heartbeat_seconds` |
| `task_active` | Latest count for the selected tasks, limited to recently reporting processes |
| `task_oldest_active_age_seconds` | Monotonic age of the oldest active attempt, grouped by `task_name`, `task_queue` and `task_framework` within each process; only active groups emit samples |
| `task_active_tracking_overflow` | Number of in-flight attempts beyond the SDK's bounded tracking capacity; above zero, oldest active age is unavailable |

Service/environment options use heartbeat labels so a worker appears before it
executes its first task. Task options use active-count labels, so a newly started
task appears before its first completion. Oldest age follows the selected task
filter and shows the task, queue, framework and process labels in its table.
Heartbeat and tracking overflow describe the entire process and ignore that
task filter. A heartbeat means metrics collection is running; it does not prove
the business event loop is responsive. Oldest active age is attempt age, not
time since last progress. Applications can define separate progress events or
deadline metrics.

The `freshness_seconds` constant defaults to `120`, allowing several default
30-second exports plus delivery delay. Edit this value for the application's
export interval and tolerated delay. Change the heartbeat panel's numeric color
threshold separately if the same cutoff should be used for coloring. These
settings are dashboard configuration, not alert rules. Heartbeat age assumes
reasonably synchronized process and metrics-server clocks.

Active count and oldest age join heartbeat by `service_name`,
`deployment_environment_name` and `service_instance_id`, so a stopped process's
last active value is excluded once its heartbeat exceeds the cutoff. Oldest age
also requires a positive `task_active` with the same process, task, queue and
framework labels. This prevents a completed task's previous age sample from
remaining visible while a different task in the same process continues running.
Tracking overflow must equal zero, preventing the backend's previous age sample
from appearing during overflow. Preserve the SDK's unique per-process instance
identity for these joins.

Idle processes do not emit oldest-age samples. A fresh heartbeat with a reported
active count of zero can establish that the selected tasks are idle; absence of
age alone cannot. A recent heartbeat does not make every metric current: these
panels still show the latest samples in the backend's lookback window. Missing
signals stay empty; the template never turns absence into zero or infers that a
disappeared process completed its work. Dead workers eventually disappear from
the heartbeat table as backend lookback expires; use an application-owned
expected-worker or scheduling signal to alert on absence.
