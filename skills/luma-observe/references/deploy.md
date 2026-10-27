# luma-observe deploy notes

## Layout

| Path | Role |
| --- | --- |
| `observe/docker-compose.yml` | Collector, Tempo, VictoriaMetrics, VictoriaLogs, Grafana, exporters, log shipper, vmalert, Alertmanager and Feishu webhook |
| `observe/luma.compose.yml` | Local-render fallback (`region: cn`, `node: manager`). Control rewrites node/region to the registered manager and injects Grafana's public URL. |
| `luma/nomad_render.py` | Traefik loopback Prometheus + OTLP flags |

## Loopback ports on manager

8082 Traefik metrics · 4318/4317 OTLP loopback · 4319 OTLP mesh (Tailscale, bearer) · 3200 Tempo · 8428 VictoriaMetrics · 9428 VictoriaLogs · 8880 vmalert · 9093 Alertmanager · 9107 nomad-exporter · 9108 log-shipper · 9095 Feishu webhook · 3100 Grafana loopback only

Open Dashboard → Observability → App monitoring after signing in to the console with a passkey or management token. `/grafana` requires a valid console session or management token on every request; unauthenticated requests return 401. Grafana uses anonymous Viewer internally, so no second login is needed. Control must be available for this route; manager-local `127.0.0.1:3100` remains the debugging endpoint. Do not publish Grafana directly.

## Traffic

Collector and Traefik share the manager host. Traefik scrapes and OTLP stay on loopback; application OTLP crosses the Tailscale mesh on authenticated port 4319. Application traffic does not pass through the collector. Grafana queries and notification delivery generate their own traffic. Do not remote-write to a public SaaS or bind OTLP to `0.0.0.0`.

## Notification paths

- Dashboard-managed rules run in Control. Its application p95, 5xx ratio and failed-allocation presets query VictoriaMetrics; node, queue and build rules do not need observe. Configure Feishu App ID, App Secret and chat ID in Dashboard → Observability → Channels, then associate the channel with a rule.
- The observe stack's bundled rules run in vmalert and route through Alertmanager to `feishu-webhook`. For a custom bot, add `FEISHU_WEBHOOK_URL: ${FEISHU_WEBHOOK_URL}` to that service's Compose `environment` and supply the scoped secret for `luma-observe`. For an application bot, use `FEISHU_APP_ID`, `FEISHU_APP_SECRET` and `FEISHU_CHAT_ID` instead. Redeploy through the recorded workflow to apply environment changes.
- With no webhook or complete app-bot credentials, the observe webhook returns 204 and skips delivery. Its health endpoint and Alertmanager's successful HTTP response do not prove a message was sent. Control channel tests send real messages; run them only when delivery testing is authorized.

## Storage and capacity

Metrics and logs retain 15 days; logs also have a 4 GiB cap. VictoriaMetrics and VictoriaLogs stop ingestion below 2 GiB of free manager disk. Tempo retains 72 hours of traces and bounds ingestion and compaction, but has no equivalent total-size or free-space guard. Monitor the manager disk instead of treating retention as a fixed disk quota.

The collector uses a Go memory target, memory limiter and bounded queues. Sustained overload can refuse or drop telemetry. Keep these limits and the Tempo ingestion bounds when tuning capacity; do not disable tracing as a default disk or memory workaround. Inspect the existing data paths and available manager resources before deployment.

## App instrumentation

After luma-observe is deployed, Control injects official OTel env into later
native and Compose jobs:

```
OTEL_SERVICE_NAME=<stack>-<task>
OTEL_RESOURCE_ATTRIBUTES=luma.stack=...,luma.task=...,luma.region=...
OTEL_EXPORTER_OTLP_ENDPOINT=http://<manager-tailscale>:4319
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <control-issued-token>
```

Traefik keeps using loopback `http://127.0.0.1:4318` without a bearer token.
Apps that already include an OpenTelemetry distro emit spans; export is
fail-open. The collector keeps HTTP 5xx, OTel errors, traces slower than 1s,
and a 10% baseline. Long-lived `/v1/node-agent/` and `/v1/terminal/` traffic is excluded from stored traces but remains in Traefik metrics. Memory limits and bounded queues can refuse or drop telemetry under pressure; they do not guarantee the process can never run out of memory. Do not create a Luma
SDK. Redeploy existing apps after enabling observe. Restart is not a redeploy.
Cross-node OTLP uses the manager Tailscale IP, never a public listener.
Manual spans: official OpenTelemetry API only, never set the OTLP endpoint.
See docs/observability.md § Application integration.
