# Luma Observe SDK

An optional Python integration package for the shared Luma observability stack.
It wraps official OpenTelemetry providers and OTLP/HTTP exporters, adds generic
task lifecycle adapters, and generates standard Grafana dashboard JSON from
application-owned configuration. It is versioned separately from `luma-infra`
and imports no Control, Django, business model or database code.

Python 3.10 or newer is required. See the [Chinese integration guide](docs/integration.zh-CN.md).

## Install the distribution

The package name is `luma-observe-sdk`, current source version `0.6.1`.
`Observe SDK Checks` builds wheel/sdist artifacts independently of Control for
review and validation; expiring CI artifacts are not a permanent dependency source.
The independent SDK release workflow publishes checked wheel/sdist files and
`SHA256SUMS` to a permanent GitHub Release under `observe-sdk-v<version>`.
After publication, use the exact wheel URL with its SHA-256 from the release
notes, or pin an accessible, immutable Git commit containing the SDK. See the
[independent SDK release guide](docs/releasing.md) for dependency formats.
Uncommitted source has no usable remote SHA; placeholders must be replaced
with the published asset hash or reviewed, pushed commit.

To inspect a downloaded wheel, add only the framework extra used by that process:

```sh
python -m pip install './luma_observe_sdk-0.6.1-py3-none-any.whl[taskiq]'
# Celery processes use the [celery] extra; processes needing both use [taskiq,celery].
```

PyPI publication is optional and disabled unless the repository variable
`OBSERVE_SDK_PYPI_ENABLED` is `true` and its Trusted Publisher is configured.
Once that exact version is published there, consumers can use
`luma-observe-sdk[taskiq]==0.6.1` with their normal package configuration.
The existing `v*` Luma release workflow publishes `luma-infra`, not this package.

## Configure once per process

Luma-managed applications keep the existing Control-injected `OTEL_*` endpoint,
bearer headers and resource attributes. Existing `luma secret` values stay in use.
For task metrics, set `OTEL_METRICS_EXPORTER=otlp` in the application deployment;
Control's automatic default remains `none`.

```python
from luma_observe import configure, span, task

configure(
    service_name="document-worker",
    resource_attributes={"deployment.environment.name": "production"},
)

def convert(document):
    with task("document.convert", framework="manual", queue="documents",
              attributes={"document.format": "pdf"}) as run:
        with span("document.render"):
            result = render_document(document)
        if result.needs_review:
            run.outcome = "failed"
        return result
```

`task` records one attempt, elapsed time and the supplied outcome; it preserves
the original return and exception. An uncaught exception records its type and a
failed outcome. Use `succeeded`, `failed`, `retrying`, `skipped` or `cancelled`.
The application defines business completion separately from framework success.
Custom attributes belong to spans; task metrics use the fixed dimensions below.

Inside an adapter-observed task, use `current_task()` to annotate the current
attempt without creating another task span:

```python
from luma_observe import current_task

def mark_review_required():
    run = current_task()
    if run is not None:
        run.business_outcome = "requires_review"
        run.business_status = "pending"
```

The two business fields accept application-defined strings up to 256 characters;
they appear as `task.business.outcome` and `task.business.status` in spans and
optional lifecycle logs. The status is never a metric label; the outcome becomes
one only through the declaration below. `set_outcome()` accepts
only the five standard outcomes above. A framework failure or an escaping
exception takes precedence over a successful business classification.
`current_task()` is local to the execution context, restores an outer task after a
nested task ends, and returns `None` outside a live task or in an inherited child
process until that child starts its own attempt.

To count business outcomes, declare the application's finite set once, in
precedence order:

```python
configure(business_outcomes=("failed", "requires_review", "retrying", "succeeded"))
```

Each attempt's final `business_outcome` is then counted once in
`task.business_outcomes`; a value outside the declaration is counted as `other`
while spans and logs keep the original text. Without a declaration nothing is
counted. For batches where one failed item should decide the attempt, use
`run.escalate_business_outcome(value)`: it keeps the value listed first in the
declaration. Undeclared values rank last, and among equal ranks the latest wins,
so without a declaration it behaves like assigning `business_outcome`.

Applications that also need an event and error sampling can call
`record_business_outcome("failed", status="rejected", failed=True,
error_type="provider_error")`. The SDK applies the configured priority, records
the status and lifecycle event, and marks the current task span as an error when
the caller sets `failed=True`. Passing `None` as the outcome records only the
optional status. Calls outside a task are no-ops; instrumentation errors preserve
application return values and exceptions. Business status-to-outcome mapping
remains in the application.

Long attempts can report progress through stages or explicit marks. The
module-level helpers work anywhere: inside an observed attempt they record, and
outside one they simply run the code, so business functions need no guards:

```python
from luma_observe import mark_progress, observed_stage, stage

@observed_stage("document.prepare")
def prepare(document): ...

with stage("document.render", attributes={"pages": 12}):
    render_pages()
mark_progress()
```

A stage is a child span plus a `task.stage` lifecycle event; it does not use the
attempt span's bounded event list. Entering and leaving it, like
`mark_progress()`, resets `task.no_progress_age` for that attempt. An
instrumentation failure never changes the wrapped function's return or exception.
`TaskRun.stage()` and `TaskRun.mark_progress()` remain available on `current_task()`.

Starting with 0.5.0, batch jobs can opt in to bounded stage summaries instead of
creating a child span on every loop iteration:

```python
with stage("document.read", attributes={"document.id": document_id}, aggregate=True):
    read_document(document_id)
```

The SDK owns timing, repeated-stage counts and error association on the existing
task attempt. At most eight distinct stage names are retained per attempt, with
one start log and one completion summary per name. Completion summaries use the
task root span; no child spans are added in this mode. The first failure and the
omitted-stage count each add at most one summary. Applications only supply stable
stage names and business attributes. Each stage retains the latest bounded attribute
set, or the first failed set; application fields cannot override its summary counters.
`observed_stage(..., aggregate=True)` offers
the same behavior for sync and async functions. Parallel intervals accumulate
work duration, which may exceed wall-clock task duration.

`configure(capture_error_details=True, lifecycle_logs=True)` also records the
first original exception as a `task.failure.detail` lifecycle JSON event with
the task's trace ID and an `exception` object. Message and stack/cause-chain
budgets are 4 KiB and 16 KiB of UTF-8, with an explicit `truncated` flag. The root
span receives the error type, a 256-character summary and source location. These
details are opt-in; default error recording keeps only the type. Exception logs
use the same bounded asynchronous queue and existing log sink. Application
`logger.exception(...)` calls keep their normal behavior. No logging filter,
business exception wrapper or application-owned stage recorder is required.

## Query existing telemetry

The optional `[query]` extra provides `luma_observe.query.ObserveClient` for
bounded asynchronous Metrics, lifecycle, error log and Trace reads. The SDK owns
protocol decoding, pagination and Trace correlation; applications supply their
scope, metric expressions, business field aliases and authorization. Querying
starts no collection runtime and creates no service or storage. See the
[query client guide](docs/querying.md).

Applications that own the transport call can observe the send result, which the
adapters' preparation spans do not claim:

```python
from luma_observe import publication

with publication("document.render", framework="taskiq", queue="documents", task_id=message_id):
    redis.xadd(stream, payload)
```

It records a producer span, `task.publications` with `outcome` `sent` or `failed`,
and an optional `task.published` lifecycle event, and re-raises the original error.
Celery applications get the same record from the adapter's `publications` option.

`get_tracer()` and `get_meter()` return official OTel instruments from the SDK's
providers for additional application-defined spans and metrics. Keep custom metric
dimensions bounded; put execution IDs on spans or structured logs. Direct use of
official instrument APIs follows their normal semantics.

The `counter()` and `histogram()` helpers preserve the same metric API while
isolating instrumentation errors. Applications choose the metric names and fields:

```python
from luma_observe import counter, histogram

completed = counter("document.completed")
size = histogram("document.pages", unit="1")
completed.add(1, {"document.format": "pdf", "outcome": "succeeded"})
size.record(12, {"document.format": "pdf"})
```

The top-level `counter()` and `histogram()` handles can be declared before
`configure()` and reused by forked workers; each write uses that process's current
runtime. Instruments obtained from an explicit `Runtime`, `get_meter()` or
`get_tracer()` belong to that runtime and process: create them during worker setup.

## Framework integration

Taskiq:

```python
from luma_observe.instrumentation.taskiq import TaskiqMiddleware

broker.with_middlewares(TaskiqMiddleware(), existing_retry_middleware)
```

Place observation first so completion cleanup runs after the existing middleware
chain. Execution state stays in the callback context, so later middleware can
copy or rebuild messages. The carrier contains propagation fields and a private,
serializable invocation ID; live execution objects are not serialized.
Existing broker transport, retries, result persistence and ACK ownership
remain with Taskiq. Celery uses its public process/task signals:

```python
from luma_observe.instrumentation.celery import instrument_celery

observation = instrument_celery(app=app, publications=True)
```

Call this once during Celery app setup; `observation.uninstrument()` disconnects
the adapter after tasks drain. Providers initialize in the actual process.
`publications=True` wraps that app's `amqp.send_task_message`, the call that
actually hands the message to the broker, because Celery's publish signals cannot
report a failed send. Each send is recorded like `publication()` above. Its
`task_queue` is the routing key Celery publishes with, the same value the worker
side records: the queue name under the default direct exchange, otherwise the
explicit or queue-configured key. The original return and exception are preserved.
`uninstrument()` restores Celery's sender only while the SDK wrapper is outermost;
a wrapper added later stays in place and the SDK layer under it stops recording.

Both adapters read business arguments only through an optional application
callback. It receives `(task_name, args, kwargs)` and returns span fields for the
attempt, which also appear in its lifecycle events. A callback that declares an
`arguments` parameter also receives the call bound to the task function's
parameter names, so positional and keyword calls look the same:

```python
def business_fields(name, args, kwargs, arguments):
    return {"document.id": str(arguments.get("document_id", ""))}

TaskiqMiddleware(attributes=business_fields)
instrument_celery(attributes=business_fields)
```

Return identifiers that are safe to store in traces and logs. Framework fields
cannot be replaced, and a failing callback is counted in `observe.sdk.dropped`
without blocking the attempt. Taskiq worker and scheduler processes declare their
role at broker startup, and workers export their registered task names with a
`task.active` value of 0, so a task that never started in that worker reads as
idle rather than missing. Celery
pool processes do the same from `worker_process_init`, Beat from `beat_init`.
Taskiq's default thread pool carries execution context; application functions
inside a custom process pool need their own initialization/propagation, while the
receiver's attempt can still be observed. Both adapters propagate standard W3C
context and baggage. Every framework execution starts a new trace with a Span Link
to its producer context, allowing the execution's completion to be sampled
independently of an earlier producer. Manual `task()` and `span()` retain their
normal parent semantics; explicit `context` and `links` are also supported.
The accompanying Collector source separates completed execution roots from early
child spans for tail sampling. This preserves the completed root summary, not
every child span in a long trace. Existing Collector deployments need that source
rebuilt and deployed before this sampling behavior is available; installing the
SDK alone does not update a running Collector.
The adapters create short producer preparation spans. Preparation
spans describe local message preparation, not broker receipt or send success.
Send results come only from `publication()` or Celery's `publications` option;
broker persistence and reliable publication state remain the application's concern.
Use only one task adapter per framework; avoid instrumenting the same task twice
with another OTel adapter.

Adapters observe attempts that reach execution hooks. Revocation before execution
and hard process termination can leave no completed span. Taskiq's standard
SmartRetry `NoResultError` conversion identifies a retrying attempt; configurations
that keep the original error (`no_result_on_retry=False`) expose a failed attempt
without a definitive retry result. Queue state and business delivery evidence
remain separate from these attempt metrics.

With nested inline Taskiq in-memory execution, a child middleware error caught by
the parent can leave the child observation context active until the next SDK hook
restores the parent. Do not use that interim context as proof of parent progress.
Hard process termination still cannot run completion hooks.

## Disable and bound telemetry

`configure(enabled=False)`, `LUMA_OBSERVE_ENABLED=false` or
`OTEL_SDK_DISABLED=true` disables the complete SDK, including optional lifecycle
logging. With no OTLP endpoint configured, providers and background threads are
not started. Endpoint/header/protocol/sampler settings use the official `OTEL_*`
environment contract. This package exports `http/protobuf`.
Producers without a local endpoint still timestamp publication for worker queue
wait measurements and preserve carried context; explicit disablement leaves the
carrier untouched. `telemetry_disabled()` reads those explicit switches without
initializing a runtime.

Inspect `get_runtime().enabled` to check whether the runtime initialized.
`get_runtime().initialization_error` contains only the initialization exception's
class name when initialization fails; the runtime stays disabled. It does not
log configuration or exception contents on the business call path.

Defaults are a 512-span queue, batches of 64, a five-second trace export interval,
a 30-second metric interval, a 1.5-second HTTP export budget and a two-second
shutdown budget. Lifecycle JSON logging is opt-in with `lifecycle_logs=True` and
uses a bounded background queue. It writes INFO records to `luma_observe.events`.
For the existing Nomad log shipper, let the SDK write them to stdout itself:

```python
from luma_observe import configure

configure(lifecycle_logs=True, log_stream="stdout")
```

With `log_stream=None` (the default) the records go through the application's own
logging configuration for that logger. The SDK does not create another log store
or collector.

Call `shutdown()` at the application's final process shutdown; atexit is also
registered. Celery uses its process shutdown signals. Within the same budget,
shutdown writes lifecycle events already queued, so a recycled worker's last
`task.finished` records are not lost. Closing one Taskiq broker
calls bounded `force_flush()` and does not stop telemetry shared by other brokers.
Applications may also call `force_flush(timeout=2)` before a known worker exit;
it returns whether the queued flush completed within its budget and never starts
a missing runtime. It does not close providers or prove downstream persistence.
Export failure can discard telemetry;
it does not make task execution wait for a collector response. Fault and load
acceptance still belongs to each application's integration rollout.

## Metrics and dashboards

The shared Collector's Prometheus conversion exposes:

| Metric | Meaning |
| --- | --- |
| `task_executions_total` | Completed attempts by `outcome` |
| `task_duration_seconds_bucket` | Attempt duration histogram |
| `task_queue_wait_seconds_bucket` | Known enqueue-to-start duration histogram |
| `task_active` | Active attempts in the process |
| `worker_heartbeat_seconds` | Exporter process collection time as Unix seconds |
| `task_oldest_active_age_seconds` | Oldest active attempt age by task name, queue and framework |
| `task_active_tracking_overflow` | Active attempts beyond the bounded age tracker |
| `task_no_progress_age_seconds` | Longest time since an active attempt started, changed stage or marked progress |
| `task_last_started_seconds` | Unix time the process last started the task |
| `task_last_finished_seconds` | Unix time the process last finished the task, by `outcome` |
| `task_business_outcomes_total` | Finished attempts by final declared `task_business_outcome` (`other` when undeclared) |
| `task_publications_total` | Application-observed sends by `outcome` (`sent`, `failed`) |
| `task_registered` | Tasks a worker process registered with its framework |
| `task_process_started_seconds` | Process start time for each `task_framework`, `task_role` and `task_queue` |
| `observe_sdk_dropped_total` | Telemetry discarded inside the SDK, by `reason` |

Last-start and last-finish times stay in the process's memory and are bounded
like task names (2048 keys). Use the maximum across instances, for example with
a recording rule, to keep them across worker recycling. `task_registered` and
`task_process_started_seconds` let a dashboard tell a task without any live
worker, or a missing scheduler, from an idle one. `observe_sdk_dropped_total`
counts lifecycle events dropped by the bounded writer queue (`lifecycle_logs`)
and exceptions the SDK swallowed to protect the application
(`instrumentation_error`).

Task metric labels are `task_name`, `task_queue` and `task_framework`, plus outcome
for executions. Standard resource attributes supply `service_name`,
`deployment_environment_name` and the existing Luma resource dimensions.
Counters describe collected telemetry, not a financial or exact delivery ledger.
Keep process liveness and metric staleness in mind when aggregating active tasks.
Heartbeat and overflow are process-level gauges. Oldest age uses the same bounded
task dimensions as `task_active`: `task_name`, `task_queue` and `task_framework`.
It reports the oldest currently active attempt in each group, using a monotonic
clock. This identifies a long-running task type; it does not measure time since
the application's last progress event. Choose age thresholds for each task type.

Tracking is capped at 8192 active attempts per process and keeps no historical
task-key cache. Idle groups have no new age sample; overflow also suppresses age
samples. The example dashboard joins each age to `task_active > 0` with the same
process identity and all three task labels, then filters fresh heartbeat and zero
overflow. This excludes old age samples retained in the store's query lookback.
Only a fresh heartbeat plus an explicitly zero active count establishes local
idle; a missing, stale or overflowed age sample does not.

Heartbeat freshness shows that the process's export path is alive, not that its
event loop or business work is progressing. Local idle does not establish that a
broker queue is healthy or empty. Queue backlog and oldest unconsumed message age
remain application/broker-provided metrics.

Process instance identities remain distinct so simultaneous cumulative writers
do not overwrite one series. Worker recycling therefore creates historical
series; measure the workload's process churn, task dimensions and storage budget
before production acceptance. The protocol check records a synthetic multi-process
series inventory, not a production capacity result. See the
[dashboard guide](docs/dashboards.md) for freshness settings and query semantics.

Copy an example from [`examples/dashboards/`](examples/dashboards/), select your
service/metrics, and render it offline:

```sh
luma-observe-dashboard application.json --output application-dashboard.json
```

The output is ordinary Grafana JSON. Install the reviewed output under the existing
`/srv/luma/data/luma-observe/extensions/dashboards/` directory using the current
operator file-delivery workflow. Grafana discovers it in Applications within the
next 30-second scan. Each application owns its dashboard UID and query expressions.
Custom fields and panels do not require SDK or Control code changes.

Prometheus/vmalert rule YAML still goes under `extensions/rules/`; validate it with
the existing pinned vmalert binary and use existing Alertmanager routes. See the
[standard platform integration](../../docs/observe-applications.md) for scopes,
read APIs, authentication and extension delivery.

## Validate and build

From the Luma repository root, in an isolated Python environment:

```sh
python -m pip install -e './sdk/python[taskiq,celery,query,test]'
bash scripts/check-observe-sdk.sh --output-dir /tmp/luma-observe-sdk-dist
```

The script runs SDK checks, builds wheel/sdist in a temporary source copy, validates
metadata and imports the installed wheel. It removes its temporary build/install
directories and retains only the explicitly requested distributions. It does not
contact production, deploy the observability stack or publish a package.
CI covers Python 3.10 with the minimum supported OTel 1.38.0 and Python 3.12
with current dependencies.

With Docker available, run the separate SDK protocol check:

```sh
python -m pip install -e .
python scripts/check-observe-sdk-protocols.py --platform linux/amd64
```

This CI job installs the SDK in an isolated container, exports native task/custom
metrics through the authenticated Collector to VictoriaMetrics, then provisions
the generated Grafana examples and queries their PromQL expressions against that
data. It uses synthetic credentials, no host ports, and removes its own containers,
volumes and temporary image tags. The existing platform protocol checks cover
the other shared ingestion/query paths. Application throughput, lifecycle coverage
and production data freshness still require integration acceptance after deployment.
