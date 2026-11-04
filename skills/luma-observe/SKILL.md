---
name: luma-observe
description: Deploy, operate and diagnose the independent Luma observability stack, including missing telemetry, intermittent Metrics/Logs/Trace queries, Grafana authentication or unavailable dashboards, OTLP ingestion and application alerts. For one application's task or Trace ID investigation, use luma-logs.
---

# luma-observe

For read-only investigation of an application's errors and historical logs, use
`luma-logs` and `luma app log-query`. This skill owns the observability stack's
deployment and operation, not routine application log analysis.

For intermittent missing metrics, unavailable dashboards, query limits or a
suspected overloaded collector/store, start with the read-only workflow in
[platform diagnosis](references/diagnosis.md). It separates query, authentication,
ingestion and resource evidence before selecting a fix.

Application telemetry collection and storage live in the optional `observe/` Compose stack. Control also has built-in application alert presets that read its metrics; these are separate from the stack's vmalert/Alertmanager rules.
Deploy from that directory through the application's recorded workflow. Control's resource monitoring and alerting keep working if observe is absent.
Keep log shipping, OTLP receivers and telemetry storage in observe; use the existing Control alert presets for dashboard-managed application alerts.

The current release does not have an "enable observability" toggle that installs
this stack by itself. A new cluster still needs the Builder/Registry path and a
Compose deployment (the Dashboard can run that path without an SSH session).
Do not tell an operator that merely selecting the Observability page deploys the
stack. Application monitoring needs `luma-observe`; resource metrics, incidents,
rules and notification channels remain available without it.

## Read platform and application health

This skill and the observation commands ship together in `luma-infra` wheels and
source distributions. Other operators install or upgrade the CLI through their
existing installation method, then use `luma skills install --agent codex`,
`--agent claude`, or `--path PATH` for a first-time assistant installation.
Existing suites synchronize on normal CLI startup after an upgrade. Check pairing
with `luma skills status --format json`; do not copy another machine's credentials
or skill directories. Client upgrades do not upgrade Control or deploy Observe.

Prefer the CLI over SSH or hand-written HTTP queries. Check `luma observe --help`
when the installed CLI or Control may predate these commands. An unavailable
command/endpoint means an upgrade is needed; retain the read-only fallback in
[platform diagnosis](references/diagnosis.md), without silently upgrading a cluster.

```sh
luma observe status --format json
luma observe app APP --since 1h --format json
luma observe alerts --app APP --format json
luma observe tasks APP --view overview --since 1h --format json
luma observe tasks APP --view events --outcome problem --since 6h --format json
luma observe tasks APP --view trace --trace-id TRACE_ID --format json
luma observe tasks APP --view logs --trace-id TRACE_ID --since 1h --format json
```

Use `--until` with RFC3339 including a timezone (or Unix seconds) to freeze the
same investigation window across commands. Application summaries and task reads
also accept `--environment`, `--service`, and `--instance`. Task log/trace searches
accept `--keyword`; numeric summaries do not filter by log text. `status` and
`alerts` read current state and do not accept a historical window.
`--service` is the reported OTel service name, not a Compose display name.
Read returned source states: `empty` is no samples, not zero; `timeout`,
`rate_limited`, and `unreachable` are query failures, not application failures.
The response's `sampledAt` is query time; the freshness signal and coverage
records describe telemetry age. Log coverage and external alerts are current
state even when a historical application window is selected. Task Trace
retention/sampling can leave a partial trace or no trace after an actual execution.

`observe alerts` reads both Control and Alertmanager. It does not acknowledge,
silence, close, or resend notifications. Preserve each source's ownership and
availability when reporting; a missing source cannot establish no incidents.
For a single execution, continue with `luma-logs` and exact task/Trace identifiers.

## First-use task metric alerts

Public Control releases include task heartbeat age, queue-wait p95, SDK drops
and active-tracking overflow presets. A fresh database initializes automatically;
never copy another project's rule IDs, credentials or private dashboard files.
Deploy Observe and instrument each application's processes with the public SDK.
Set `OTEL_METRICS_EXPORTER=otlp` explicitly (the injected default is `none`) and
preserve `luma.stack`, the OTLP endpoint and bearer headers. Confirm actual metrics
with `luma observe tasks APP --view overview` before claiming coverage.

Create a rule for one application and its own notification channel. Heartbeat
rules require the expected OTel `service_name`, not a guessed Compose name;
use `noData: alert` when loss of every series must notify. Queue-wait p95 requires
at least 20 consumed-task observations per service/queue over 15 minutes and
currently uses Taskiq metrics. SDK drops and tracking overflow do not cover every
Collector or OTLP loss. The platform has no project-specific defaults, and a
cleared alert is not proof of business-task compensation.

## When to use

- Deploy, update, or debug `luma-observe`
- Application-level alerts (HTTP 5xx, Nomad failed/restarts) labeled by Luma app name automatically
- Traefik Prometheus/OTLP loopback listeners
- Questions about auto-instrumentation or an observe SDK

Do not use this skill for Dashboard node CPU/disk presets or Nomad job YAML for ordinary apps.

## Invariants

- Control pins the stack to the registered manager (Traefik/Control node) on deploy and rewrites sidecar `node`/`region`. `observe/luma.compose.yml` values such as `node: manager` are a local-render fallback only; do not assume the manager is named `manager`.
- Every Compose service uses `network_mode: host` and `exposure: none`.
- Collectors bind `127.0.0.1` only. Control uses host networking to read VictoriaMetrics at `127.0.0.1:8428`. Never publish 4318/8082/8428/9428/3100 on eth0 or the public NIC. Port 4319 is the manager Tailscale mesh OTLP listener with a Control-issued bearer token; do not bind it on the public NIC.
- After observe is deployed, look at Dashboard → Observability → App monitoring. It embeds Grafana at `/grafana` on the Control domain.
- The stack evaluates its bundled rules with vmalert + Alertmanager and can notify through its Feishu webhook service. Dashboard-managed rules use Control's persistent alert engine and notification channels; application p95, 5xx ratio and failed-allocation presets read VictoriaMetrics. Check which rule owns an incident before changing either path, and avoid duplicate notifications for the same condition.
- After observe is deployed, Control injects official OTel env vars into later app jobs. Applications can use official language instrumentation directly or the independently versioned optional Python `luma-observe-sdk` package in `sdk/python/`. Keep its APIs business-agnostic and reuse official OTel transport, existing tokens and scoped secrets.
- This injection is configuration wiring, not magic code instrumentation. A business image
  must include the official OpenTelemetry distro/exporter and start it with the
  language's supported auto-instrumentation entrypoint before it emits process spans.
- Do not add a collector sidecar to every application container. Use the shared
  manager collector; consider one collector/eBPF agent per Linux node only after
  there is a measured network, buffering, or host-observation need.
- Fail open: Collector/Feishu down must not block user requests.
- `/grafana` requires a console session or the management token (Traefik
  forwardAuth to Control); inside, Grafana is anonymous Viewer. Anyone signed
  in to the console can read every app's logs and traces, so still keep secrets
  and sensitive payloads out of logs and trace attributes.

Operators look at Dashboard → Observability → App monitoring. Traefik routers and Nomad
jobs are mapped to Luma app/stack names by observe itself. Do not ask apps to
add labels, sidecars, or an application SDK for this infrastructure view.

## Deploy

Work from `observe/` and follow its recorded deployment workflow. For a first deployment from a local checkout, use Local Build and resolve the manager's container architecture. On an AMD64 manager:

```bash
luma build local . --platform linux/amd64
```

First deploy does not require notification secrets. Nomad HTTP on the manager is used
without a token. For the stack's Alertmanager path, add scoped secrets and matching
Compose environment references to the Feishu webhook service. A saved secret alone
does not inject an environment variable; redeploy to apply it. Control's built-in
Feishu channels instead use App ID, App Secret and chat ID in the Dashboard.
See [deployment notes](references/deploy.md#notification-paths) before configuring delivery.

Traefik RED requires the current `luma/nomad_render.py` Traefik flags
(`127.0.0.1:8082` Prometheus, `127.0.0.1:4318` OTLP). Refresh Traefik with
`luma update manager` after that code is on the CLI used for the update.
Nomad failed-allocation metrics are available once the observe stack is running.
Verify the selected rule and delivery configuration before claiming notifications work.

Read [references/deploy.md](references/deploy.md) for ports, traffic, and
instrumentation contract. Read [references/instrumentation.md](references/instrumentation.md)
before adding application spans.
