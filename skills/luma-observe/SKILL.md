---
name: luma-observe
description: Deploy and operate the independent Luma application observability stack (Traefik RED, Nomad alloc failures, OTLP, Feishu). Use for luma-observe, application-level alerts, Traefik metrics/OTLP, log-to-metric questions, or when the user wants app alerts without changing Luma Control.
---

# luma-observe

Application alerts live in the `observe/` Compose stack, not in Luma Control.
Control remains a deployment plane with node resource samples and live log tail.
Do not add log scanners, OTLP receivers, or app 5xx rules to Control.

## When to use

- Deploy, update, or debug `luma-observe`
- Application-level alerts (HTTP 5xx, Nomad failed/restarts)
- Traefik Prometheus/OTLP loopback listeners
- Questions about auto-instrumentation or an observe SDK

Do not use this skill for Dashboard node CPU/disk presets, Nomad job YAML for ordinary apps, or LAE tenant observability.

## Invariants

- Pin the stack to the Traefik node (`node: manager` on this cluster).
- Every Compose service uses `network_mode: host` and `exposure: none`.
- Listeners bind `127.0.0.1` only. Never publish 4318/8082/8428 on the public NIC.
- Alert evaluation is vmalert + Alertmanager + optional Feishu webhook.
- Auto-instrumentation is opt-in later via official OTel env vars. Do not create a Luma telemetry SDK.
- Fail open: Collector/Feishu down must not block user requests.

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
instrumentation contract.
