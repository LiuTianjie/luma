# Profiles

Profiles are Luma's manager bootstrap presets. Ordinary worker joins do not use profiles; run `luma node join ... --region cn|global|home --name <node-name>` on each worker.

## `single-node`

One public server running:

- Docker
- Nomad server (also a client in `region=cn`)
- Traefik
- Luma Control API
- egress gateway

Use:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
```

## `cn-edge`

Domestic public edge:

- Docker
- Nomad server
- Traefik

## Worker regions

Worker nodes are now region-first:

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

- `region` is the scheduling boundary, written to the Nomad client `meta.region`.
- `name` is the Luma node name used in status output and service manifests; pinned scheduling uses `meta.luma_node_name`.
- Services that require the proxy declare `proxy: true`.
