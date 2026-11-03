# Find one execution

Use the existing `luma app log-query` command. The current CLI has no native
`trace` or `trace-query` subcommand. This flow retrieves lifecycle and error logs;
Grafana/Tempo provides the retained span waterfall. Application code that needs
programmatic reads can use the optional `luma-observe-sdk[query]` client, with its
own configured read endpoints and scope.

## Task or business reference to Trace ID

1. Query the identifier within the known application and time window. Start with
   60 rows and preserve `range` and `page` in the result.
2. Parse an SDK JSON message's top-level `trace_id` and `attributes`. The framework
   message ID is `attributes["messaging.message.id"]`; the task function is
   `attributes["task.name"]`. Business attribute names belong to the application:
   check its documented mapping or the returned field, rather than assuming a
   universal business-ID key. A substring hit alone is not an exact association.
3. Query each relevant Trace ID using the **same returned since/until window**.
   Start with the failed or latest relevant attempt; follow additional attempts
   when needed to establish the eventual result. Query a small batch of relevant
   attempts, and paginate or widen the window only when the evidence requires it.

```sh
luma app log-query APP --since SINCE --until UNTIL --contains TRACE_ID --limit 100 --format json |
  python3 SKILL_DIR/scripts/log_summary.py --trace-id TRACE_ID --control-url CONTROL_URL
```

The helper labels matching structured Trace IDs as `exact`, other structured
executions as `other_trace`, and plain text mentions as `mention`. An exact ID
establishes trace membership; compare `span_id` and task identity before combining
lifecycle records if a trace contains several tasks. Neighboring or mentioning
logs are context. `traces` lists structured IDs found on the page, not all attempts ever
executed. Follow `page.nextCursor` when the requested attempt is absent or its
start/end is outside the loaded page.

## Read the attempt before declaring its result

| Evidence | Meaning |
| --- | --- |
| `task.published` | A producer's send outcome; its Trace can differ from execution |
| `task.started` | An execution attempt began |
| `task.stage` or `task.phase.started` | A stage was reached; this alone does not prove it is still running |
| `task.phase.finished` | A phase result/summary; repeated work can have cumulative duration/count |
| `task.failure` | Failure evidence for this attempt |
| `task.failure.detail` | Structured exception detail, when enabled in the emitting SDK |
| `task.finished` | Framework outcome and elapsed time for this attempt |
| `task.business.outcome` attribute or `business_outcome` event | Application-declared business result; it can differ from framework success |

A retry can retain the same message ID while receiving a new Trace ID. Report
each relevant attempt separately and check later results before calling a task
finally failed. Missing completion means unknown until framework/process evidence
establishes the result. Some SDK versions or tasks have no phase/detail records;
absence is an instrumentation or evidence gap, not a guessed failed stage.

## Original exception and surrounding logs

Prefer the exact Trace's `task.failure.detail.exception`: `error_type`,
`error_message`, `stacktrace`, and `truncated`. Use `--full-lines` for the helper
when the message is clipped; the upstream `truncated` flag still applies.

For older or unstructured logs, anchor on an exact lifecycle record, then read a
small observation-time window from the same service/allocation/stream with the
text/error filter removed. Start around the record's `observedAt`, for example
ten seconds on each side. Expand if collection delay or the stack requires it.

```sh
luma app log-query APP --since START --until END --task SERVICE --allocation ALLOCATION_ID --stream stderr --limit 100 --format json
```

Use the stream containing the original logger output; the lifecycle itself can
be on stdout. Include exception causes and final exception lines. Parallel tasks
share a process, so identify surrounding lines as context unless their own IDs
confirm the association.

## Waterfall and evidence limits

Pass the known Control origin to `log_summary.py --control-url` to generate an
authenticated `/grafana/explore` link using the bundled `tempo` datasource. In
Grafana, open the Trace ID to see retained parent/child spans and span events.
An aggregate phase can be a root-span event instead of a child span; do not
interpret cumulative batch time as wall-clock duration.

If the Trace is missing, retain the lifecycle evidence and check sampling,
retention and exporter/store availability separately. Empty/missing Trace data,
query timeouts, authentication errors and an actual failed task are distinct
outcomes. Present the affected attempt, known failed/last stage, original error,
later retry result, and explicit evidence gaps.
