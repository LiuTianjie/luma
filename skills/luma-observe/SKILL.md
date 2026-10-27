---
name: luma-observe
description: Deploy and operate the independent Luma application observability stack (Traefik RED, Nomad alloc failures, OTLP, Feishu). Use for luma-observe, application-level alerts, Traefik metrics/OTLP, log-to-metric questions, or when the user wants app alerts without changing Luma Control.
---

# luma-observe

Application telemetry collection and storage live in the optional `observe/` Compose stack. Control also has built-in application alert presets that read its metrics; these are separate from the stack's vmalert/Alertmanager rules.
Deploy from that directory through the application's recorded workflow. Control's resource monitoring and alerting keep working if observe is absent.
Keep log shipping, OTLP receivers and telemetry storage in observe; use the existing Control alert presets for dashboard-managed application alerts.

The current release does not have an "enable observability" toggle that installs
this stack by itself. A new cluster still needs the Builder/Registry path and a
Compose deployment (the Dashboard can run that path without an SSH session).
Do not tell an operator that merely selecting the Observability page deploys the
stack. Application monitoring needs `luma-observe`; resource metrics, incidents,
rules and notification channels remain available without it.

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
- Auto-instrumentation is automatic after observe is deployed: Control injects official OTel env vars into later app jobs. Do not create a Luma telemetry SDK.
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
add labels, sidecars, or a Luma SDK for this view.

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
