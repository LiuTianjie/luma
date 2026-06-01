# Profiles

Profiles are Luma's manager bootstrap presets. Ordinary worker joins do not use profiles; run `luma node join ... --region cn|global|home --name <node-name>` on each worker.

## `single-node`

One public server running:

- Docker
- Swarm manager
- Traefik
- Portainer
- Luma Control API
- egress gateway

Use:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
```

This profile also deploys Portainer. To repair only the Portainer control plane:

```bash
luma portainer setup
```

## `cn-edge`

Domestic public edge:

- Docker
- Swarm manager
- Traefik
- Portainer
- `public` overlay network

## Worker regions

Worker nodes are now region-first:

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

- `region` is the scheduling boundary.
- `name` is the Luma node name used in status output and service manifests; pinned scheduling resolves it to the real Swarm NodeID.
- Services that require the proxy declare `proxy: true`.
