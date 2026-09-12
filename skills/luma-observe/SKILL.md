---
name: luma-observe
description: Deploy and operate the independent Luma application observability stack (Traefik RED, Nomad alloc failures, OTLP, Feishu). Use for luma-observe, application-level alerts, Traefik metrics/OTLP, log-to-metric questions, or when the user wants app alerts without changing Luma Control.
---

# luma-observe

Application alerts live in the optional `observe/` Compose stack, not in Luma Control.
Deploy it from that directory with `luma build local . --platform linux/amd64`. Control keeps working if it is absent.
Do not add log scanners, OTLP receivers, or app 5xx rules to Control.

The current release does not have an "enable observability" toggle that installs
this stack by itself. A new cluster still needs the Builder/Registry path and a
Compose deployment (the Dashboard can run that path without an SSH session).
Do not tell an operator that merely selecting the Observability page deploys the
stack; the page is empty until `luma-observe` is active.

## When to use

- Deploy, update, or debug `luma-observe`
- Application-level alerts (HTTP 5xx, Nomad failed/restarts) labeled by Luma app name automatically
- Traefik Prometheus/OTLP loopback listeners
- Questions about auto-instrumentation or an observe SDK

Do not use this skill for Dashboard node CPU/disk presets, Nomad job YAML for ordinary apps, or LAE tenant observability.

## Invariants

- Control pins the stack to the registered manager (Traefik/Control node) on deploy and rewrites sidecar `node`/`region`. `observe/luma.compose.yml` values such as `node: manager` are a local-render fallback only; do not assume the manager is named `manager`.
- Every Compose service uses `network_mode: host` and `exposure: none`.
- Collectors bind `127.0.0.1` only. Control uses host networking to read VictoriaMetrics at `127.0.0.1:8428`. Never publish 4318/8082/8428/9428/3100 on eth0 or the public NIC. Port 4319 is the manager Tailscale mesh OTLP listener with a Control-issued bearer token; do not bind it on the public NIC.
- After observe is deployed, look at Dashboard → Observability → Apps. It embeds Grafana at `/grafana` on the Control domain.
- Alert evaluation is vmalert + Alertmanager + optional Feishu webhook.
- Auto-instrumentation is automatic after observe is deployed: Control injects official OTel env vars into later app jobs. Do not create a Luma telemetry SDK.
- This injection is configuration wiring, not magic code instrumentation. A business image
  must include the official OpenTelemetry distro/exporter and start it with the
  language's supported auto-instrumentation entrypoint before it emits process spans.
- Do not add a collector sidecar to every application container. Use the shared
  manager collector; consider one collector/eBPF agent per Linux node only after
  there is a measured network, buffering, or host-observation need.
- Fail open: Collector/Feishu down must not block user requests.
- Grafana is currently embedded as an anonymous Viewer route. Treat all data
  visible under `/grafana` as public to anyone who can reach the Control domain;
  do not put secrets or sensitive payloads into logs or trace attributes.

Operators look at Dashboard → Observability → Apps. Traefik routers and Nomad
jobs are mapped to Luma app/stack names by observe itself. Do not ask apps to
add labels, sidecars, or a Luma SDK for this view.

## Deploy

Work from `observe/`. Recorded workflow is Local Build:

```bash
luma build local . --platform linux/amd64
```

First deploy does not require secrets. Nomad HTTP on the manager is used
without a token. Add Feishu later with scoped secrets and Compose env
`${FEISHU_WEBHOOK_URL}` (custom bot preferred) or app-id fields.

Traefik RED requires the current `luma/nomad_render.py` Traefik flags
(`127.0.0.1:8082` Prometheus, `127.0.0.1:4318` OTLP). Refresh Traefik with
`luma update manager` after that code is on the CLI used for the update.
Nomad failed-allocation alerts work as soon as the observe stack is running.

Read [references/deploy.md](references/deploy.md) for ports, traffic, and
instrumentation contract. Read [references/instrumentation.md](references/instrumentation.md)
before adding application spans.
