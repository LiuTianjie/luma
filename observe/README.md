# luma-observe

Independent application observability for a Luma cluster. It is a normal
Compose deployment, not part of Luma Control.

It colocates with Traefik on the manager host network and only binds loopback
listeners. Public 80/443 traffic and Aliyun outbound billing are unchanged
except for tiny Feishu posts.

## What it watches

- Traefik Prometheus on `127.0.0.1:8082` (per-router HTTP 5xx)
- Traefik OTLP on `127.0.0.1:4318` (optional traces → span metrics)
- Nomad job failed / running / restart counts via a local exporter

It does not scrape application stdout and does not evaluate alerts inside Control.

## Deploy

Pin is `node: manager`. Build and deploy from this directory:

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
| 4318 / 4317 | Collector OTLP HTTP / gRPC |
| 8428 | VictoriaMetrics |
| 8880 | vmalert |
| 9093 | Alertmanager |
| 9107 | nomad-exporter |
| 9095 | feishu-webhook |

Do not publish these on `0.0.0.0` or the public NIC.
