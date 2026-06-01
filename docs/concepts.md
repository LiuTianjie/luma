# Concepts

Luma exposes five concepts.

## Node

A server that runs Luma locally as either the manager or a worker. New installs do not require the client machine to SSH into nodes; each server runs `luma bootstrap manager` or `luma node join` on itself.

```yaml
nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
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

Image pulls use the Docker daemon proxy configured by `luma egress setup`. Service runtime proxy is explicit: set `proxy: true` in the service manifest. Luma then attaches the service to the `egress` overlay network and injects default `HTTP_PROXY` / `HTTPS_PROXY`. Scheduling still follows the service `region`.

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

Set `node: <luma-node-name>` only when the service must run on one specific machine. The value is the name passed to `luma node join --name`. Luma still adds the `region` placement constraint, then resolves that name to the real Swarm NodeID and adds a `node.labels.luma.node.id == <node-id>` constraint.
