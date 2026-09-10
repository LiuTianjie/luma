# luma-observe deploy notes

## Layout

| Path | Role |
| --- | --- |
| `observe/docker-compose.yml` | Collector, VictoriaMetrics, vmalert, Alertmanager, nomad-exporter, Feishu webhook |
| `observe/luma.compose.yml` | `region: cn`, pin `manager`, local volume `/srv/luma/data/luma-observe/victoria` |
| `luma/nomad_render.py` | Traefik loopback Prometheus + OTLP flags |

## Loopback ports on manager

8082 Traefik metrics · 4318/4317 OTLP loopback · 4319 OTLP mesh (Tailscale, bearer) · 3200 Tempo · 8428 VictoriaMetrics (127.0.0.1 plus Nomad/docker0 via host-gateway) · 8880 vmalert · 9093 Alertmanager · 9107 nomad-exporter · 9095 Feishu webhook · 3100 Grafana loopback only; 3000 is already itool.tech; the Dashboard Apps tab is the operator view

Open Dashboard → 可观测性 → 应用 (Viewer, no login). Do not open port 3000 on the public NIC.

## Traffic

Collector and Traefik share the manager host. Scrapes and OTLP stay on loopback, so Aliyun public outbound does not grow except Feishu POSTs. Do not remote-write to a public SaaS. Do not bind OTLP to `0.0.0.0`.

## App instrumentation

After luma-observe is deployed, Control injects official OTel env into later
native, Compose and LAE jobs:

```
OTEL_SERVICE_NAME=<stack>-<task>
OTEL_RESOURCE_ATTRIBUTES=luma.stack=...,luma.task=...,luma.region=...
OTEL_EXPORTER_OTLP_ENDPOINT=http://<manager-tailscale>:4319
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <control-issued-token>
```

Traefik keeps using loopback `http://127.0.0.1:4318` without a bearer token.
Apps that already include an OpenTelemetry distro emit spans; export is
fail-open and sampled (`parentbased_traceidratio=0.1`). Do not create a Luma
SDK. Redeploy existing apps after enabling observe. Cross-node OTLP uses the
manager Tailscale IP, never a public listener.
