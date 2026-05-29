# Concepts

Luma exposes five concepts.

## Node

A server Luma can reach over SSH.

```yaml
nodes:
  aly:
    host: aly
    publicIp: 8.130.148.30
    region: cn
    roles: [swarm-manager, edge, egress]
```

## Region

Where a service should run:

- `cn`: domestic public services and core workloads.
- `global`: overseas or external-network workers/services.
- `home`: home or private nodes.

## Exposure

How public traffic reaches a service:

- `cn-edge`: Cloudflare DNS -> CN Traefik -> CN service.
- `tailscale-relay`: Cloudflare DNS -> CN Traefik -> Tailscale -> home service.
- `cloudflare-tunnel`: Cloudflare Tunnel -> private service.
- `external-edge`: Cloudflare DNS -> global edge -> global service.
- `none`: no public ingress.

## Egress

Outbound proxy for image pulls, dependency downloads, and selected services.

It is not a public ingress.

## Service

A small YAML manifest that Luma turns into a Swarm stack:

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```
