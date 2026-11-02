# Service-level span metrics

[中文](observe-service-span-metrics.zh-CN.md)

Applications can opt into service-level metrics derived from spans by setting
the resource attribute `luma.observe.span_metrics=service`. The shared Collector
then aggregates those derived metrics across process instances. Applications
without that exact attribute value keep the existing spanmetrics behavior.
Stored traces and native OTLP metrics retain their original resources, including
`service.instance.id`.

## When to use this mode

This mode suits workers whose frequent process replacement creates many
historical span-metric series. It uses standard OTel spans and Collector
spanmetrics; applications still own task names, outcomes, dashboards and rules.
It does not reuse process identities or convert native cumulative counters.

Choose the signals for each application:

| Configuration | Available task information |
| --- | --- |
| Service span metrics, `OTEL_METRICS_EXPORTER=none` | Completed execution attempts and duration from received root spans; suitable for an opted-in Celery deployment with frequent child replacement |
| Default span metrics, `OTEL_METRICS_EXPORTER=otlp` | Existing instance-level spanmetrics plus SDK native task counters, duration, queue wait, active attempts and worker freshness metrics; retain this default mode for Taskiq when these metrics are needed |

The service flag and native metric exporter are independent. An opted-in
application may retain native metrics; they still have per-process identities.
Setting the metric exporter to `none` removes SDK queue-wait, active-attempt,
heartbeat and oldest-active-age measurements, along with application custom
native metrics. A completed-span counter cannot replace these measurements.
Nomad allocation monitoring remains available; broker backlog and expected work
need their own application or broker signals.

## Enable and roll out

Use **Control 0.2.26 or later** and deploy an Observe build containing the
service-span pipelines. These are separate deployments: updating Control does
not rebuild Observe. `luma-observe-sdk==0.1.0` is compatible; this feature requires
no SDK update. Other official OTel integrations can emit the same span contract.

1. Deploy the updated Control so application resource attributes merge by key.
2. Rebuild and redeploy the independent Observe stack through its existing
   [deployment workflow](../observe/README.md#deploy).
3. Set the application resource flag and redeploy that application, starting in
   its test environment. Keep its existing endpoint, credentials and other
   environment values.

Example environment fragment for a service that chooses span-derived task metrics:

```yaml
environment:
  OTEL_SERVICE_NAME: document-worker
  OTEL_RESOURCE_ATTRIBUTES: service.namespace=documents,deployment.environment.name=production,luma.observe.span_metrics=service
  OTEL_TRACES_EXPORTER: otlp
  OTEL_METRICS_EXPORTER: none
```

Use the application's own stable service and environment names. If it already
sets `OTEL_RESOURCE_ATTRIBUTES`, add the flag to that existing value. The match
is exact and case-sensitive: an absent flag or another value uses the default
branch. Verify the flag on emitted span resources if application code also
supplies resource attributes. SDK instrumentation and task lifecycle hooks are
still required; environment injection alone does not create task spans.

Control preserves each application-defined resource key, including an explicitly
empty value, and appends only missing injected keys such as `luma.stack`,
`luma.task` and `luma.region`. It does not replace the application's other
environment settings. Applications that previously supplied their own resource
attribute string may gain these missing labels once on redeployment, changing
their series identities. Applications that already received the complete
injected attributes have no additional change from this merge.

## Identities and metric contract

The opt-in derived resources retain these six identity keys:

| OTel resource key | Prometheus label |
| --- | --- |
| `service.name` | `service_name` |
| `service.namespace` | `service_namespace` |
| `deployment.environment.name` | `deployment_environment_name` |
| `luma.stack` | `luma_stack` |
| `luma.task` | `luma_task` |
| `luma.region` | `luma_region` |

They also carry the fixed label `luma_observe_span_metrics="service"`. Keep this
mode label: it separates opt-in and default writers sharing the
`traces_span_metrics_*` namespace, including during a rollout. Other resource
attributes, including process identity and SDK version, are removed only from
these opt-in derived metrics. The original trace and native-metric resources
remain intact. Grouping all six identities prevents unrelated services,
environments, Luma tasks or regions from being merged.

Opted-in spans still produce the generic `traces_span_metrics_*` RED family.
Each accepted span reaches either the default or service RED connector once.
The opt-in task family is an additional view of matching execution roots;
do not add RED calls, `task_span_calls_total` and native `task_executions_total`
together to calculate task volume. RED includes producer and child spans.
The additional task family receives only spans satisfying all of these conditions:

- Span kind is `CONSUMER`.
- There is no parent span.
- Both `task.name` and `task.framework` attributes are present.

Producer spans and nested child spans do not enter the task family. Taskiq and
Celery adapters in SDK 0.1.0 emit the matching execution roots. Each received
completed root represents an **execution attempt**; a retry followed by success
can contribute two attempts. A cancellation contributes only if a completed
root is emitted and received.

| Metric | Meaning |
| --- | --- |
| `task_span_calls_total` | Received completed execution roots |
| `task_span_duration_seconds_bucket` | Attempt duration histogram buckets |
| `task_span_duration_seconds_sum` / `task_span_duration_seconds_count` | Total duration and histogram observation count |

The task dimensions are `task_name`, `task_framework`, `task_queue` and
`task_outcome`, alongside the connector's standard span dimensions and the
service identity labels above. Source outcomes such as `succeeded`, `failed`,
`retrying`, `skipped` and `cancelled` remain separately selectable. Missing queue
or outcome attributes are not inferred as business success. Arbitrary business
fields do not become additional task-metric dimensions.

## Query examples

Use selectors for the intended service, environment and mode. Add `luma_stack`,
`luma_task` or `luma_region` when the query should cover only one deployment.
These examples use the environment fragment above.

Observed attempts over one hour, separated by outcome:

```promql
sum by (task_name, task_framework, task_queue, task_outcome) (
  increase(task_span_calls_total{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service"}[1h])
)
```

Failed attempts over the same window; retries and cancellations are separate outcomes:

```promql
sum by (task_name) (
  increase(task_span_calls_total{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service", task_outcome="failed"}[1h])
)
```

P95 duration of successful attempts, in seconds:

```promql
histogram_quantile(0.95,
  sum by (le, task_name) (
    rate(task_span_duration_seconds_bucket{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service", task_outcome="succeeded"}[5m])
  )
)
```

Choose windows long enough for the task's normal frequency and metric export
interval. Sparse observations, absent series and counter resets affect
`rate`/`increase`; these expressions are operational estimates, not an exact
execution ledger. Application Grafana JSON uses the existing
[dashboard extension directory](observe-applications.md#application-owned-dashboards-and-rules).

## No-execution alert template

The following is an example for a task expected to finish regularly. Choose
the task, window and `for` duration from the application's workload, and apply
its working hours or maintenance silences. An idle queue can be healthy; no
completed spans can also mean long-running work or failed telemetry delivery.
This document does not install an alert or enable notifications.

```yaml
groups:
  - name: application-observation-example
    rules:
      - alert: ApplicationHasNoObservedExecutions
        expr: |
          (
            sum(increase(task_span_calls_total{
              service_name="document-worker",
              service_namespace="documents",
              deployment_environment_name="production",
              luma_observe_span_metrics="service",
              task_name="document.convert"
            }[30m])) or vector(0)
          ) == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "No completed execution spans observed for document.convert"
```

This template treats missing series as no observed executions; it does not prove
that the worker is down. Review it with deployment health, expected task demand
and Collector freshness. Validate and deliver an approved rule through the
existing [application rule workflow](observe-applications.md#application-owned-dashboards-and-rules).

## Delivery limits and reconciliation

The task counters describe completed roots **received by the Collector before
storage tail sampling**. SDK head sampling, full export queues, timeout or
delivery failures, Collector memory refusal, hard-killed workers and restarts
can reduce coverage. Retransmitted duplicate roots can also change counts.
Low average QPS does not establish burst safety: measure peak traffic, span
volume per task, buffer refusal and exporter failures for the actual deployment.

The spanmetrics connectors keep cumulative state in Collector memory. The
existing five-minute idle expiration for a resource group and Collector
restarts can reset that state; individual metric series may retain state until
their resource group is evicted. Historical storage samples remain, so a
`rate`/`increase` window crossing these boundaries needs care. This mode does
not add persistent counter state or change the existing expiration policy.

Compare logs and metrics using the same time window and timezone, task,
framework, queue and outcome. Distinguish attempts from final business jobs,
and account for the delivery gaps and reset boundaries above. A daily metric
increase is not guaranteed to equal a daily log total. Service aggregation
reduces process-driven series churn for stable span/task dimensions; it is not
a storage quota and does not bound arbitrary span names or task cardinality.

## Disable and verify

Remove only `luma.observe.span_metrics=service` from the application's resource
attributes and redeploy it. Newly received spans return to the default
spanmetrics branch. Existing derived series remain queryable until lookback
and storage retention exclude them; they are not deleted on redeploy. Keep the
mode selector in dashboards during a transition. If native metrics were
disabled, explicitly restore `OTEL_METRICS_EXPORTER=otlp` when they are wanted;
removing the resource flag does not change that exporter setting.

After enabling or disabling, verify a representative application:

- The opt-in derived series have the service mode label and no process-instance
  label; an unflagged application still has its previous spanmetrics identity.
- Stored traces still identify the original process. Native metrics retain
  their original resources when their exporter is enabled.
- Task counts and duration include the expected completed roots and outcomes,
  while producer and child spans remain outside the task family.
- Query source freshness, exporter/receiver failures and actual series growth
  alongside the application's task and log observations.

Platform verification and standard access paths are documented in
[Standard application observability](observe-applications.md#verification).
