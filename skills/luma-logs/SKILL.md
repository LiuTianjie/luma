---
name: luma-logs
description: Investigate application errors, historical logs and alert incidents in Luma using its authenticated CLI/API. Use for recent backend failures, task exceptions, deployment-versus-runtime diagnosis, and tracing a specific request or job. For installing or repairing the observability stack, use luma-observe instead.
---

# Luma log investigation

Use the installed `luma` CLI and its saved Control context. Do not extract tokens,
call Grafana's datasource proxy, or SSH to nodes for ordinary log analysis.
The CLI owns authentication and structured output; the Control API queries the
existing manager-local VictoriaLogs store. No additional collector is needed.

## Establish scope

```sh
luma context list --format json
luma version
luma app list --format json
luma app show APP --format json
luma app alerts APP --limit 50 --format json
luma app history APP --limit 10 --format json
```

`APP` is the stack/application ID from `stack`, not the composed `fullName`.
Use `--task` for the service inside that stack. Distinguish production and test.
If the user does not specify a period, start with the last hour and recent alert
incidents, then widen only if necessary. Report the period actually inspected.
An incident's `logDetails` supplies its task, observation window, sample and URL.
Follow `nextCursor` to inspect more incidents; counts of incidents are not counts
of unique task failures or log lines. Keep the same app/status filters for alerts.

## Query evidence

```sh
luma app log-query APP --since 1h --errors --limit 100 --format json
luma app log-query APP --since 2026-11-01T16:00:00+08:00 --until 2026-11-01T17:00:00+08:00 --task TASK --contains 'TASK_OR_REQUEST_ID' --format json
luma app log-query APP --cursor 'NEXT_CURSOR' --limit 100 --format json
```

- `contains` and `exclude` are literal substrings. `--errors` is a text heuristic,
  not parsed severity; stderr also contains ordinary INFO messages.
- For stack traces, narrow the time range and task/allocation, **remove the text
  filters**, and retrieve surrounding lines. Matching only an exception name
  loses its callers and underlying cause. Observation batches can share a
  timestamp; their returned tie order is not guaranteed to be original line order.
- Continue using `page.nextCursor` with only the app and optional limit. The
  cursor freezes the filters and time range. Check `hasMore` and `warnings`;
  do not describe a capped sample as an exhaustive scan.
- Each request spans at most 24 hours, returns at most 500 entries, and supports
  at most 10,000 rows of offset. Split larger work by time/task or narrower text.
- `observedAt` and range filters use **collector observation time**. Application
  timestamps embedded in `line` may be earlier and use a different timezone.
  Broaden the observation window when investigating a known application timestamp.
- Results are not a storage snapshot: late collection or retention can change
  pages. Logs from stopped allocations remain searchable only while retained.
- Common credential patterns are redacted; this is not a complete privacy filter.
  Do not repeat signed URLs, access tokens or unrelated personal payloads.
- Empty results do not prove health. An unavailable store is an explicit error,
  not an empty successful query. Check CLI/Control versions when an endpoint is
  missing; skill installation alone does not upgrade Control or deploy observe.

For current allocation state or live output:

```sh
luma app events APP --format json
luma app logs APP --tail 100 --format json
luma app logs APP --follow --format ndjson
```

Use bounded snapshots for ordinary analysis; stop a live stream once the needed
evidence is captured. Runtime `logs` and historical `log-query` have different
cursor/time semantics; do not interchange them.

## Explain causes and recovery accurately

Separate failed deployment, allocation crash/restart, and business task failure.
Use task IDs to distinguish retries from separate failures. `resolved` means an
alert condition cleared, **not** that a failed order/task was successfully retried.
Identify the service/version, concrete error, observed business impact and evidence
link. State which underlying causes remain unverified. When code inspection is
needed, compare against the deployed image's source revision, not merely local HEAD.

Ordinary investigation is read-only. Do not restart, retry business tasks, close
alerts or change rules as part of a request to explain errors.

This skill is paired with the installed Luma release. `luma skills status` shows
pairing and local edits; `luma skills sync` refreshes existing suites, backing up
customizations. A CLI upgrade does not retroactively refresh an agent's already
loaded instructions; reload the skill or start a new conversation.
