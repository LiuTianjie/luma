# luma-observe

Optional application observability for a Luma cluster. Deploy it with Luma
like any other Compose app. Control and other clusters keep working if it
is absent.

It colocates with Traefik on the manager host network and only binds loopback
listeners. Public 80/443 traffic and Aliyun outbound billing are unchanged
except for tiny Feishu posts.

## What it watches

- Traefik Prometheus on `127.0.0.1:8082` (public HTTP rate, 5xx, p90/p95/p99)
- Traefik OTLP on `127.0.0.1:4318` (traces stored in Tempo, 7 days)
- Nomad allocation stdout/stderr in VictoriaLogs (`127.0.0.1:9428`, 7 days)
- Application OTLP on the manager Tailscale IP port `4319` (bearer token from Control)
- Nomad job running / current failed / live restarts via a local exporter

App names are the Luma stack/job names already in Nomad. Deploying observe
maps Traefik routers onto those names. Existing apps do not need a redeploy,
extra YAML, or an SDK.

After it is deployed, look in Luma Dashboard → 可观测性 → 应用.
That tab embeds Grafana at `/grafana` on the Control domain. Luma writes
the Traefik route; you do not add a second domain. Do not open Grafana on
Tailscale or port 3000.

It does not evaluate alerts inside Control. Allocation stdout/stderr is
shipped to VictoriaLogs by observe, not by Control. Dashboard → 可观测性 → 日志
opens Grafana Explore. `luma service logs` and the application-detail live tail
still go to Nomad through Control and are not a 15-day archive.
Deploying this stack is the observability component: later Luma app deploys
receive official OTel env vars automatically. Redeploy existing apps to pick
them up. There is no Luma SDK. Restarting an old allocation is not enough.

## Application integration

Public HTTP already has Traefik RED and an ingress span. Process spans need
the official OpenTelemetry distro in the image. Do not set the OTLP endpoint
or a Luma SDK; Control injects `OTEL_*` on the next deploy. The collector
keeps 5xx, OTel errors, and traces slower than 1s, plus a 10% baseline;
it drops new traces near its memory limit instead of OOM. Manual spans use
`opentelemetry.trace.get_tracer(...).start_as_current_span(...)` (or the
language equivalent). See [Application integration](../docs/observability.md#application-integration).

## Deploy

luma-observe colocates with Traefik and Control on the manager host network.
Control pins the stack to the registered manager node (not the name `manager`)
and injects Grafana's public URL from the Control domain. Do not edit node
names or hardcode a Grafana host.

Build and deploy from this directory:

```bash
luma build local . --platform linux/amd64
```

If Feishu is unset, Alertmanager still records incidents; the webhook returns
204 and does not retry. Traefik RED rules start after Traefik is refreshed
with the loopback metrics/OTLP flags from current `luma/nomad_render.py`
(`luma update manager` refreshes the Traefik job). Nomad failure alerts work
as soon as this stack is running.

## Listeners (manager loopback only)

| Port | Process |
| --- | --- |
| 8082 | Traefik Prometheus |
| 4318 / 4317 | Collector OTLP HTTP / gRPC (loopback, Traefik) |
| 4319 | Collector OTLP HTTP on the manager Tailscale IP (apps, bearer auth) |
| 3200 / 3201 | Tempo query (loopback) |
| 4418 / 4417 | Tempo OTLP ingest (loopback) |
| 8428 | VictoriaMetrics on 127.0.0.1; host-gateway also binds Nomad/docker0 so Control can scrape |
| 9428 | VictoriaLogs (allocation stdout/stderr, 7 days) |
| 8880 | vmalert |
| 9093 | Alertmanager |
| 9107 | nomad-exporter |
| 9108 | log-shipper |
| 9095 | feishu-webhook |
| 3100 | Grafana on 127.0.0.1; published at `/grafana` on the Control domain |

Do not publish these on `0.0.0.0` or the public NIC.
`host-gateway` is part of this optional stack: it exposes VictoriaMetrics on
the Nomad/docker0 bridge. Control itself is host-networked and queries
`http://127.0.0.1:8428`. Grafana listens on `127.0.0.1:3100` and is published
at `/grafana` on the Control domain.
