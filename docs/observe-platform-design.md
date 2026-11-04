# General observability platform design {#observe-platform-design}

Luma Observe receives standard telemetry. Applications own their instrumentation, business fields, dashboards and alert definitions. Deployment management and observability integration are separate capabilities. The Python SDK is an optional thin wrapper around official OpenTelemetry components; other languages and existing OTel SDKs can connect directly.

This document distinguishes the source on this branch from future proposals. It does not mean production deployment or capacity acceptance is complete. See [Standard application observability](observe-applications.md) for connection settings and the [SDK guide](../sdk/python/README.md) for the Python API.

## Application identity and external services {#application-identity-and-external-services}

- Resource identity uses `service.namespace`, `service.name`, `service.instance.id` and `deployment.environment.name`. Applications choose stable service names. Execution and business IDs belong on spans, not metric labels.
- Processes deployed through Luma use the standard OTLP configuration injected by Control. External processes use an enabled HTTPS OTLP endpoint and the existing ingest credentials. The HTTPS example requires installation, DNS/TLS setup and connectivity verification; its presence in the repository does not mean the endpoint is live.
- External services can use the same Metrics, Trace and Grafana query capabilities without creating a Nomad Job. The Collector currently has no OTLP Logs pipeline, and the public gateway has no Logs ingest route. External logs still need a separate standard collection integration; they do not yet offer the complete Nomad live stdout experience.
- The current Prometheus Remote Write path requires cumulative monotonic counters and histograms. External SDKs must use cumulative temporality. DELTA conversion is a future platform capability; successful OTLP reception does not mean every temporality can be stored.
- Keep the existing credentials and `luma secret` workflow. Shared read credentials, Grafana variables and dashboard UIDs do not provide tenant isolation.

## Task execution and sampling {#task-execution-and-sampling}

Each framework execution starts an independent trace and connects to its publication context through a standard SpanLink. Redelivery preserves a link to the previous context. This expresses causality without combining two executions into one. A message preparation span does not confirm broker receipt. Producers without an exporter can still write the enqueue timestamp; explicitly disabling the SDK leaves publication labels unchanged.

Independent execution traces prevent queue wait from consuming the Collector's sampling decision window. Early completed child spans can still trigger a decision during execution. The Collector therefore sends CONSUMER root spans with no parent and with `task.name` / `task.framework` to a separate completed-summary sampling branch. It decides whether to retain them from their final error status, duration and baseline sampling ratio. Ordinary child spans retain the existing sampling behavior. Before storage sampling, each accepted span reaches either the default or opt-in service RED connector once. Opted-in applications additionally produce a `task_span_*` family for execution roots; see [Service-level span metrics](observe-service-span-metrics.md). Do not add RED calls, task-root counts and native task counters together: RED also includes producer and child spans.

This design preserves received summaries of failed or slow task roots. It does not guarantee every child span of a long task, or cover hard termination, process crashes, SDK head sampling or export loss. Services requiring complete long traces need quota-backed full collection or consistent trace-ID sampling for selected traces. Increasing `decision_wait` without a bound is not a solution.

The SDK's execution context owns attempt state; messages carry only serializable propagation labels. Taskiq closes attempts through its normal completion hook and an asyncio Task completion fallback, and performs a bounded flush on broker shutdown. Flushing does not close providers shared within the process. Celery uses public task and process signals. Business exceptions, return values, retries, result persistence and ACK ownership remain with the framework.

## High-frequency workloads and reliability {#high-frequency-workloads-and-reliability}

| Layer | Current mechanism | Boundary |
| --- | --- | --- |
| SDK | An in-memory trace queue of 512, batches of 64; local metric aggregation with a default 30-second export interval; bounded timeouts and flush | Business execution does not wait for platform recovery. Overflow, hard termination and export failures can lose telemetry. |
| Collector | Memory limits, batching and bounded sampling state; a persistent 128-request Tempo exporter queue with retries; a PRW queue of 128 OTLP metrics batches and dedicated WAL | Dedicated volumes protect the export stage. Restarts still lose pre-queue batch and tail-sampling state; the WAL has no hard disk quota. |
| Storage | Existing Tempo, VictoriaMetrics and VictoriaLogs disk storage and retention | Only data that has reached the relevant store is protected. The current single-manager, single-replica deployment is not HA. |
| Business reliability | Application transactions, outboxes, message queues and result stores | Billing, auditing and task delivery cannot depend on telemetry that may be sampled or lost. |

High-frequency instrumentation should aggregate locally through counters and histograms instead of making a network request per call. Label cardinality must be bounded. Task arguments, user IDs, execution IDs and full exception contents do not belong in general-purpose metric labels.

A process heartbeat timestamp indicates that metric collection and export are working. Oldest active execution age, grouped by task name, queue and framework, reports how long an unfinished attempt has been running. Dashboards must match `task_active > 0` on the full task and process identity, and check heartbeat freshness and tracking overflow to exclude stale ages from completed tasks. Only a fresh heartbeat and an explicit zero active count establish that this process is idle. Increasing active age needs interpretation against the task's execution budget. A live metrics thread does not prove event-loop or business progress, and missing data does not mean zero. Broker backlog, the age of the oldest unconsumed message and business progress must come from application or broker metrics.

`service.instance.id` must distinguish concurrent processes writing SDK cumulative metrics. Simply removing it merges independent counters and histograms into one series and violates single-writer semantics. The opt-in service span-metrics path has a different writer: the shared Collector aggregates spans into service-level counters and histograms before exporting them. This already avoids per-child series for Celery completed-span statistics; native Taskiq metrics keep their process identity. Deployment acceptance must still measure process turnover, task/queue/outcome dimensions and actual new series in VictoriaMetrics. Rewriting labels on independently exported cumulative streams is not equivalent to Collector-owned span aggregation.

Remaining reliability and capacity acceptance:

1. This branch configures Tempo `file_storage`, the PRW-specific WAL and dedicated persistent mounts. Verify recovery after deployment, and define measured disk budgets and recovery procedures. Queue request limits are not hard disk quotas.
2. The platform status page and `luma observe status` expose refusals, export failures, queue occupancy, storage space, log gaps and freshness. Extend SDK-drop and disk-growth trends, and measure backlog recovery throughput. Limit ingestion against its budget, and keep client backoff bounded so unlimited retries cannot consume business resources.
3. Accept the deployment against representative payloads, spans/s, active traces, series counts and process turnover. Cover downstream outages, Collector restarts, disk pressure and overload. Queue size alone cannot establish throughput capacity.
4. Introduce Kafka or similar messaging infrastructure only when measured needs require cross-node durability, long replay windows or multiple consumers. Scaling collectors that perform tail sampling also requires trace-ID routing affinity and corresponding storage HA.

Persistent exporter queues still do not cover batch or tail-sampling state before enqueueing, or data that has not left an SDK process before it crashes. Document success-response timing and durability boundaries with the implementation; this is not end-to-end lossless delivery.

## Dashboards in application details {#dashboards-in-application-details}

The existing platform provides a Grafana JSON extension directory, an offline configuration tool and embedded Grafana in application details. Custom dashboards can reuse these capabilities. The following registration and UI proposals are not yet implemented:

- Associate an application with one or more Grafana dashboard UIDs and mappings from variables such as `service` and `environment` to its observation identity. Display these in the application's observability area, retaining log search, trace exploration and live log entry points.
- Keep queries, units, layouts and business fields in Grafana configuration. React pages handle dashboard selection, identity filters, embedding and loading/missing states without hard-coding each application's metrics.
- Give external services their own observation entries, register their observation identities and associate dashboards without inventing Luma deployment objects. Identity registration does not automatically grant data access.

The SDK dashboard compiler is an optional offline tool. Applications that already maintain standard Grafana JSON can deliver it directly without converting it into another format.

## Versions and releases {#versions-and-releases}

The SDK is versioned independently of `luma-infra`. After checks pass, the `observe-sdk-v*` release workflow publishes the wheel, source distribution and checksums from the same build to GitHub Releases as a stable dependency source. PyPI OIDC publishing requires a separate Trusted Publisher and the repository publishing variable to be enabled; workflow source does not mean the package is available on PyPI. Consumers pin the release wheel's checksum or a full Git SHA, rather than using expiring CI artifact URLs as permanent dependencies. See the [release guide](../sdk/python/docs/releasing.md).

The Collector distribution is pinned to contrib 0.122.1, which contains PRW component version 0.122.0. Its DELTA limitations, exporter-specific WAL, queue units and Alpha multi-worker feature gate are documented in the [PRW README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.122.0/exporter/prometheusremotewriteexporter/README.md) and [release manifest](https://github.com/open-telemetry/opentelemetry-collector-releases/blob/v0.122.1/distributions/otelcol-contrib/manifest.yaml). Revalidate these contracts when upgrading components.

Set PRW WAL `buffer_size: 1` explicitly. Version 0.122.0 defaults to 300 decoded requests and checks the flush timer only after reading another entry; idle input or memory refusal can strand a partial batch in memory. Flushing each entry preserves disk persistence and crash replay without that input dependency. `scripts/check-observe-wal.py` verifies delivery with no later input and persistent replay after downstream outage and collector termination; `--baseline` reproduces the default-buffer idle-flush failure. An empty exporter queue does not mean the WAL read buffer is empty: compare local pprof heap profiles with the latest stored samples.
