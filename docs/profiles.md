# Profiles

Profiles are Luma's bootstrap presets.

## `single-node`

One public server running:

- Docker
- Swarm manager
- Traefik
- Portainer
- egress gateway

Use:

```bash
luma node bootstrap aly --profile single-node
```

This profile also deploys Portainer. To repair only the Portainer control plane:

```bash
luma portainer setup aly
```

## `cn-edge`

Domestic public edge:

- Docker
- Swarm manager
- Traefik
- Portainer
- `public` overlay network

## `egress-gateway`

Outbound proxy node:

- `egress` overlay network
- mihomo stack
- Docker daemon proxy
- firewall hardening

## `home-node`

Home/private node:

- `region=home`
- suitable for `tailscale-relay` and `cloudflare-tunnel`

## `global-worker`

Overseas/external-network node:

- `region=global`
- `external_net=true`
- suitable for workers and `external-edge`
