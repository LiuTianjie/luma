# Read existing observability data

Install `luma-observe-sdk[query]==0.6.0` in the server that serves an application's
administration UI. The optional extra supplies `httpx`; collection-only SDK users
keep the existing dependencies. Import the client from `luma_observe.query`.

```python
from luma_observe.query import ObserveClient, ReadEndpoints


async def execution_evidence(task_id: str, read_token: str):
    endpoints = ReadEndpoints.from_origin("https://observe.example.com", token=read_token)
    async with ObserveClient(
        endpoints,
        apps=("document-workers",),
        resource_attributes={
            "service.namespace": "documents",
            "deployment.environment.name": "production",
        },
        attribute_aliases={"document_id": "document.id"},
    ) as client:
        events = await client.events(task_id=task_id, hours=24)
        return {"events": events, "traces": client.traces(events)}
```

The caller owns authorization, the read credential, application scope and any
business field aliases. The client uses the existing Metrics, VictoriaLogs and
Tempo endpoints. It creates no exporter, background worker, cache, storage or
additional service, and works independently of the collection runtime's enabled
state. Keep read credentials in the server; OTLP write headers are never reused.

`ReadEndpoints.from_origin()` selects `/metrics`, `/logs`, `/traces` and `/grafana`.
Individual addresses can instead be supplied to `ReadEndpoints(...)`. Credentials
are sent only to the configured backend; redirects and environment proxies are
disabled. Applications continue to enforce their own management permissions.

## Queries

- `events(task_name=..., task_id=..., attributes={...}, hours=24)` reads SDK lifecycle
  records. Attribute filters use original attribute names, such as `document.id`.
  `attribute_aliases` adds caller-chosen output fields; standard task identity
  fields remain SDK-owned. `outcome="problem"` selects failure events, failed or
  retrying attempts, and the standard `failed` business outcome before pagination.
- Continue a page with its returned `until` and `next_offset`. The fixed time
  window prevents the window from moving; late ingestion and retention can still
  change stored pages. The SDK merges related attempt records and reports omitted
  evidence explicitly.
- `traces(events)` lists execution Trace IDs from already-read lifecycle records.
  A listed attempt can have no retained Trace because storage applies sampling.
- `trace_detail(trace_id)` normalizes retained OTLP/Tempo spans, IDs, parent-child
  relationships, events, links and error status. The explicit resource scope is
  applied before returning spans. Standard OTel fields and declared aliases are
  exposed; application-specific fields remain opt-in.
- `logs(trace_id, hours=24, until=...)` associates `task.failure.detail` with the
  exact structured Trace identity. Older attempts can use a second bounded read
  of the same allocation/task/stream around an exact lifecycle record; `context`
  marks those neighboring logs because they can include concurrent tasks.
- `metrics({name: promql}, require_data=(...))` runs caller-supplied vector queries
  and returns a state per query. The caller supplies scoped PromQL expressions;
  required empty sources return `no_data`, while one failing query leaves the
  remaining sources available.

## Read budgets

| Resource | Budget |
| --- | --- |
| Concurrent requests | 3 per client |
| Request deadline | Metrics 3 seconds; Logs/Trace 8 seconds |
| Response body | 2 MiB per request |
| Lifecycle page | 100 matches, plus at most 100 related events across 20 attempts |
| Query window / offset | 1–72 hours / 0–2000 |
| Raw logs | 60 matching records; legacy context up to 200 records in 20 seconds |
| Exception detail | Message 4 KiB; stack/cause chain 16 KiB UTF-8 |
| Trace detail | 1500 spans, 32 events and 16 links per span |

`state`, `possibly_truncated`, `related_truncated` and Trace truncation flags
describe partial results. Request errors remain unavailable data rather than
being treated as evidence of successful execution. The application owns health
thresholds, expected schedules, business database reads and the UI response.
