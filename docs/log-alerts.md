# Service log alerts

Dashboard → Observability → Rules → Service log matches. Select one application
(the Nomad job ID), optionally one task, and enter a case-sensitive text match,
an exclusion, or a JSON field/value pair. Conditions are ANDed. JSON fields use
dotted paths. Preview returns aggregate counts and one bounded log sample. It does not save or send a notification. A threshold of 4 means more
than 4 matching lines; threshold 0 means at least one. Count summary events, not
stack frames. Retried attempts are separate lines, not unique business failures.

Control stores the rule and reads small cached aggregates. `observe/log-alerts`
executes native VictoriaLogs queries, outside Control and outside SQLite locks.
Existing Control incidents, acknowledgement, silence, recovery and notification
outbox remain authoritative. These rules do not also send through Alertmanager.
Neither an observe outage nor an absent preview blocks deployments or heartbeats.

## Budgets and failure semantics

- Maximum 20 log rules, one application per rule, windows 60–900 seconds.
- One serial query, at least 2 seconds between queries, each rule at most once
  per 60 seconds (evaluation can take longer when all rules are busy). VictoriaLogs query timeout 1 second; client timeout 2 seconds.
- A single coalesced preview slot, at most one new preview per 10 seconds; previews
  share the oldest-first query budget, never run in Control request handlers. UI polling stops after
  50 seconds. Users may request again if pending.
- Aggregate responses capped at 256 KiB; rule exchange at 64 KiB. Control has a
  500 ms loopback timeout, independent of query execution. No log files, temp
  directories, disk caches, recording-rule files, volumes or background retries.
- Runtime root filesystem is read-only, bytecode disabled, memory limit 96 MiB,
  CPU scheduling share 50 MHz. Luma uses elastic CPU shares, not hard CPU quotas.
  VictoriaLogs remains the existing bounded-retention data store; query workload
  is bounded by scope, serial concurrency and deadline, not claimed zero-cost.
- Source coverage requires the shipper to catch up every allocation/stream for
  the selected task(s). Failed reads/exports and stale coverage make samples
  unavailable. Detected export loss or log rotation gaps keep the affected window
  unavailable until the gap ages out. Coverage state is capped at 2048 streams;
  over this limit alert coverage is unavailable. Statistics older than 150 seconds
  are ignored. Missing configured JSON fields are unavailable, not a zero count.
- No source logs in the window is unknown, not zero/recovered. Idle services can
  remain unknown; use a heartbeat or application metrics when silence is normal.
- Logs are counted by collection time, as in the existing shipper. They are not
  deduplicated events; delayed ingestion/restarts can distort a window. A quieter
  error window is not proof that lost business tasks were compensated.

## Delivery

Upgrade Control (API + bundled Dashboard) and redeploy `observe/` to install the
loopback-only log-alerts service on 9109 and updated shipper coverage endpoint.
Older observe installations return unavailable, never fabricated success. No
business application changes are required for text logs. No rules are enabled
by deployment; the operator creates rules and selects notification channels.
The optional `logQuery` field is preserved on rule edits and changes reset active
incidents, just like metric/threshold edits.

`luma/log_alert_rules.py` is the shared contract. The identical committed copy
in `observe/log_alert_rules.py` keeps the existing self-contained observe build
context (without uploading the repository); a regression check enforces equality.

First-version configuration is intentionally declarative. Arbitrary LogsQL,
unbounded regular expressions, cross-service queries and historical backfills
are not accepted by this API. Metrics-based SLO alerts remain the recommended
next step for high-value business completion and backlog monitoring.

## Incident evidence and application navigation

Application details include an Alerts tab. The central incident and rule views
also accept an application filter. Incident counts and pagination are scoped in
SQL using application indexes; global app rules remain visible in scoped rule
lists. Infrastructure alerts are not attributed to an app by a coincidental name.

The existing native aggregate also selects at most three representative log rows
with `json_values(...) limit 3`; it does not sort or issue a second log query.
Samples are redacted for common credential patterns and bounded to 512 UTF-8 bytes
of message text (head and tail), 900 encoded bytes per row. The 64 KiB rule/result
exchange cap, serial query schedule and 96 MiB worker limit are unchanged.

An incident replaces one bounded evidence snapshot only on a new breached sample;
no append-only sample history is created. Recovery keeps the last breached window.
Lists receive a single summary, while detail views read the saved snapshot from
SQLite without querying VictoriaLogs. Notifications include the same window,
counts, source and error samples, plus a link to the app's incident page. Opening
log search explicitly uses the recorded app/task and original time range, capped
at 100 initial lines. Deleted rules do not erase incident ownership or evidence.

These are log-line samples, not complete traces or unique business failures.
The arbitrary sample order and deduplication may yield fewer than three examples.
Logs in snapshots follow existing alert-history retention. Older incidents without
evidence show an explicit missing-evidence state; current logs are not substituted
for historical failures.
