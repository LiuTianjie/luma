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
    roles: [nomad-server, edge, egress]
```

## Region

Where a service should run. A node belongs to exactly one region. `replicas` then spread across ready nodes in that region; you only pin `node` when a service must stay on one machine.

Built-in regions also carry ingress topology and default egress:

- `cn`: domestic public services and core workloads. Join/image-pull uses the manager egress proxy. Allows `cn-edge`.
- `global`: overseas or external-network workers/services. Direct egress. Allows `external-edge`.
- `home`: home or private nodes. Join/image-pull uses the manager egress proxy. Allows `tailscale-relay`.

Create additional scheduling pools with `luma region create <name>` (or the Nodes page). Custom regions default to `exposure: none` and an explicit `egress: proxy|direct`. Join nodes with `--region <name>`, then deploy with the same `region` and a replica count.

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

Image pulls use the Docker daemon proxy configured by `luma egress setup`. Service runtime proxy is explicit: set `proxy: true` in the service manifest. Luma then attaches the egress proxy to the service and injects default `HTTP_PROXY` / `HTTPS_PROXY`. Scheduling still follows the service `region`.

## Service

A small YAML manifest that Luma turns into a Nomad job:

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

Set `node: <luma-node-name>` only when the service must run on one specific machine. The value is the name passed to `luma node join --name`. Luma still adds the `region` placement constraint, then renders that name into a Nomad constraint on `${node.unique.name}` (or `meta.luma_node_name`).
