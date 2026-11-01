# Application log investigation

Use `luma app log-query` for retained historical logs and `luma app logs` for
current Nomad allocation tails and live streams. Historical reads require
luma-observe's existing VictoriaLogs service; no new deployment component is added.

```sh
luma app alerts my-app --format json
luma app log-query my-app --since 1h --errors --format json
luma app log-query my-app --since 2026-11-01T08:00:00Z --until 2026-11-01T09:00:00Z --task worker --contains 'request-id' --format json
luma app log-query my-app --cursor 'NEXT_CURSOR' --format json
```

## API

`GET /v1/logs/query` requires the same authenticated deploy-token or Dashboard
session access as other Control reads. VictoriaLogs stays on manager loopback;
clients do not need Grafana credentials or direct storage access.

| Parameter | Meaning |
| --- | --- |
| `app` | Required exact application/stack ID |
| `task`, `allocation` | Optional exact task and allocation ID |
| `stream` | Optional `stdout` or `stderr` |
| `since`, `until` | Unix seconds, timezone-qualified RFC3339, or duration relative to now; defaults last hour |
| `contains`, `exclude` | Literal substring filters, not regex or arbitrary LogsQL |
| `errors` | `true` enables a fixed error-text heuristic |
| `limit` | 1–500, default 100 |
| `cursor` | Opaque next page; send only `app`, `cursor` and optional `limit` |

Responses contain `source`, `app`, normalized `query`, absolute `range`, `entries`,
`returned`, `page: {limit, hasMore, nextCursor}`, `warnings` and `updatedAt`.
Each entry contains `observedAt`, `line`, `task`, `allocationId`, `stream`, `node`,
`app` and `truncated`. There is no duplicated legacy `logs` array or per-line cursor.
`returned` is this page's size, not a total match count. Common credential patterns
are redacted using the shared log sample sanitizer; arbitrary personal data is not.

Queries are scoped to one app, at most 24 hours, four concurrent backend reads,
five seconds of storage query time and an 8 MiB response. Cursor offsets stop at
10,000; narrow the window or filters to continue beyond that. Invalid parameters,
storage failure and timeouts are explicit errors, never successful empty results.

Pagination freezes the query window and uses deterministic observation-time sorting
with secondary keys. It is not an immutable storage snapshot. Late ingestion and
retention may change pages, and same-timestamp batches have no original line-order
guarantee. Identical lines are retained, not deduplicated. Time filtering refers to
collector observation time, not application timestamps embedded in the message.

To read traceback context, remove text/error filters and query the same task and
allocation around the observation time. Logs absent from collection or already
expired cannot be recovered by this API. Retention is configured by the observe
deployment, not by the CLI.

`luma app alerts APP` wraps `GET /v1/alerting/incidents?app=APP` with `limit`,
`cursor` and optional `status`. It preserves the existing incident IDs, evidence
summaries and links. A resolved incident is not proof of business-task recovery.

The [luma-logs skill](../skills/luma-logs/SKILL.md) provides the Agent workflow.
The query compiler uses the documented [LogsQL sort and offset pipeline](https://docs.victoriametrics.com/victorialogs/logsql/#sort-pipe).
