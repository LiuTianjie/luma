---
name: luma-logs
description: Quickly investigate Luma application errors and task execution by Trace ID, task/message ID, business reference, or an alert. Find related logs, execution attempts, failed stages and Grafana trace links through the authenticated CLI. For observability platform failures or deployment, use luma-observe.
---

# Luma log investigation

Use the installed `luma` CLI and its saved Control context. Do not extract tokens,
call Grafana's datasource proxy, or SSH to nodes for ordinary log analysis.
The CLI owns authentication and structured output; the Control API queries the
existing manager-local VictoriaLogs store. No additional collector is needed.

## Start with the known identifier

Use the application's stack ID and the requested time window immediately when
they are already known. Resolve missing context once; the commands below are
discovery options, not a checklist to repeat for every query.

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
If `log-query` is unknown, check the installed CLI outside a source checkout:
Python launchers can load repository code from the current directory and hide
an outdated installation. A supported Control capability does not upgrade the CLI.
If the user does not specify a period, start with the last hour. Query recent
alert incidents when the request concerns an alert. Report the period inspected.
An incident's `logDetails` supplies its task, observation window, sample and URL.
Follow `nextCursor` to inspect more incidents; counts of incidents are not counts
of unique task failures or log lines. Keep the same app/status filters for alerts.

## Choose the shortest query

| What is known | First read |
| --- | --- |
| Application, recent errors | `luma app log-query APP --since 1h --errors --limit 40 --format json` |
| Trace ID | `luma app log-query APP --since 1h --contains TRACE_ID --limit 100 --format json` |
| Task/message ID or business reference | `luma app log-query APP --since 1h --contains IDENTIFIER --limit 60 --format json` |
| Service and error text | Add `--task SERVICE --contains 'ERROR_TEXT'` |
| Alert | Use its `logDetails` task, observation window and allocation |

`--task` selects a deployed service such as a worker, not an SDK task function.
`--contains` is a candidate search. Check the actual structured field before
assigning a short business number or a mentioned Trace ID to an execution.

For compact output, pipe a JSON log page through the bundled, standard-library
helper. `SKILL_DIR` below means the absolute directory containing this file;
use the current endpoint from `luma context list` for `CONTROL_URL`.

```sh
luma app log-query APP --since 1h --contains TRACE_ID --limit 100 --format json |
  python3 SKILL_DIR/scripts/log_summary.py --trace-id TRACE_ID --control-url CONTROL_URL
```

It preserves the query window, pagination and warnings, extracts SDK Trace IDs,
and prints Grafana Explore links. `--full-lines` retains full returned messages;
ordinary output clips long previews with an explicit flag. The helper reads stdin
only and never loads credentials, queries a service or infers a task's final result.

Read [execution lookup](references/execution-lookup.md) when following retries,
locating a failed stage, retrieving an original stack, or opening a waterfall.
An SDK lifecycle record can outlive a sampled Trace; a generated link does not
prove that Tempo retained the spans.

## Keep the evidence window and pagination

```sh
luma app log-query APP --since 2026-11-01T16:00:00+08:00 --until 2026-11-01T17:00:00+08:00 --task TASK --contains 'TASK_OR_REQUEST_ID' --format json
luma app log-query APP --cursor 'NEXT_CURSOR' --limit 100 --format json
```

- `contains` and `exclude` are literal substrings. `--errors` is a text heuristic,
  not parsed severity; stderr also contains ordinary INFO messages.
- For stack traces, narrow the time range and task/allocation/stream, **remove the
  text filters**, and retrieve surrounding lines. Matching only an exception name
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
- Start with compact output. For large application or node snapshots, select the
  relevant services and fields before displaying JSON; avoid printing the entire
  cluster while locating one task.

For current allocation state or live output:

```sh
luma app events SERVICE_FULL_NAME --format json
luma app logs SERVICE_FULL_NAME --tail 100 --format json
luma app logs SERVICE_FULL_NAME --follow --format ndjson
```

Use the service's `fullName` from `luma app list` for these runtime commands.
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
Distinguish an execution failure from a failed observability read. A shared metric
query failing can make many tasks lose coverage at once; those are not independent
task failures. Route Grafana authentication, query timeout/rate-limit and ingestion
problems to `luma-observe` and its platform diagnosis reference.

Ordinary investigation is read-only. Do not restart, retry business tasks, close
alerts or change rules as part of a request to explain errors.

This skill is paired with the installed Luma release. `luma skills status` shows
pairing and local edits; `luma skills sync` refreshes existing suites, backing up
customizations. A CLI upgrade does not retroactively refresh an agent's already
loaded instructions; reload the skill or start a new conversation.
