# luma-observe deploy notes

## Layout

| Path | Role |
| --- | --- |
| `observe/docker-compose.yml` | Collector, VictoriaMetrics, vmalert, Alertmanager, nomad-exporter, Feishu webhook |
| `observe/luma.compose.yml` | `region: cn`, pin `manager`, local volume `/srv/luma/data/luma-observe/victoria` |
| `luma/nomad_render.py` | Traefik loopback Prometheus + OTLP flags |

## Loopback ports on manager

8082 Traefik metrics · 4318/4317 OTLP · 8428 VictoriaMetrics (127.0.0.1 plus Nomad/docker0 via host-gateway) · 8880 vmalert · 9093 Alertmanager · 9107 nomad-exporter · 9095 Feishu webhook · 3000 Grafana loopback only; the Dashboard Apps tab is the operator view

Open Dashboard → 可观测性 → 应用 (Viewer, no login). Do not open port 3000 on the public NIC.

## Traffic

Collector and Traefik share the manager host. Scrapes and OTLP stay on loopback, so Aliyun public outbound does not grow except Feishu POSTs. Do not remote-write to a public SaaS. Do not bind OTLP to `0.0.0.0`.

## Later app instrumentation

Inject official OTel env only, opt-in:

```
OTEL_SERVICE_NAME=<luma job or compose service>
OTEL_RESOURCE_ATTRIBUTES=luma.stack=...,luma.task=...,luma.region=...
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

That endpoint is currently manager-local. Cross-node app OTLP needs a later Tailscale bind, not a public listener. Use the OpenTelemetry API/distro, not a Luma SDK. Sample in production (`parentbased_traceidratio`) and keep exporters fail-open.
