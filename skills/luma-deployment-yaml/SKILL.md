---
name: luma-deployment-yaml
description: Generate, review, validate, or fix Luma single-service deployment manifests and Compose sidecars for the infra-stacks/Luma project. Use when asked about luma deploy YAML, luma.compose.yml, region/exposure choices, routing domains to workloads, storageClass volumes, manager-managed storage, private registry credentials, service removal, CI deploys, cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, home services, global workers, or Portainer stack YAML errors.
---

# Luma Deployment YAML

Use this skill for Luma deployment artifacts:

- Single-service manifests consumed by `luma deploy`.
- Compose deployments made from a standard `docker-compose.yml` plus a Luma sidecar consumed by `luma compose deploy`.

Single-service manifests are not Docker Compose. They describe one service: image, region, optional node pin, exposure, domain, port, replicas, storage, and runtime options. Luma Control renders Swarm stack YAML, DNS, Traefik routes, and Portainer deployment actions.

Compose files stay standard for local development. Put Luma-specific deployment semantics in `luma.compose.yml`: region, exposure, routing, service node pins, local storage pins, and references to manager-managed storage classes.

For complete field tables and examples, read `references/manifest-reference.md`.

## Token Vocabulary

- Say "management token" for the trusted control-plane token used by CLI clients and the dashboard. The historical env var is still `LUMA_DEPLOY_TOKEN`.
- Say "node join token" for `luma node join` and old node-agent update examples.
- Do not ask users for internal node-agent credentials. Luma installs and refreshes them through the manager.

## Workflow

1. Choose the deployment path:
   - Use single-service YAML for one container or a simple service.
   - Use Compose when the app has multiple services, existing Compose config, dependencies, shared networks, or named volumes across services.
2. Identify the service access pattern:
   - China public HTTPS: `region: cn`, `exposure: cn-edge`
   - Global public HTTPS: `region: global`, `exposure: external-edge`
   - Home service through China edge and Tailscale: `region: home`, `exposure: tailscale-relay`
   - Home/private Cloudflare Tunnel: usually `region: home`, `exposure: cloudflare-tunnel`
   - Worker/internal: `exposure: none`
   - Runtime outbound proxy: add `proxy: true` and keep the chosen scheduling region.
3. Ask only for missing required facts: service or stack name, image or compose path, domain, container port, region/exposure, storage class, and Luma node name when pinning is required.
4. Emit only YAML unless the user asks for explanation.
5. Use `${ENV_NAME}` for secrets and tell the user to run `luma secret set ENV_NAME`; never put secret values in YAML.
6. For private images, do not put registry tokens in YAML or container env. Use `luma registry login <host> --username <user> --password-stdin`.
7. Recommend targeted validation and dry runs, not live deploys, unless the user explicitly asks to deploy.

## Single-Service Rules

- Required: `name`, `image`, `region`.
- Valid regions: `cn`, `global`, `home`.
- Valid exposures: `none`, `cn-edge`, `external-edge`, `tailscale-relay`, `cloudflare-tunnel`.
- Public exposures require `domain` and integer `port`.
- `cn-edge` requires `region: cn`; `external-edge` requires `region: global`; `tailscale-relay` requires `region: home`.
- `port` is the container's internal listening port, not a cloud firewall or host port.
- `replicas` defaults to `1` and must be at least `1`.
- Do not use the removed `public` field; set `exposure` explicitly.
- `node` is the Luma node name from `luma node join --name`. Control-plane deploy resolves it to a Swarm NodeID constraint and also keeps the region constraint.
- Use `proxy: true` for container runtime HTTP/HTTPS egress through Luma. Do not hand-write the default `egress` network or default `HTTP_PROXY`/`HTTPS_PROXY`.
- `proxy: true` is not for image pulls. Image pulls use Docker daemon proxy and registry credentials managed by Luma.
- `resources.limits.cpus` and `resources.reservations.cpus` should be quoted strings, for example `"0.50"`. Portainer/Compose expects `cpus` to be a string; current Luma normalizes numeric YAML for compatibility, but generated examples should still quote it.
- `healthcheck` is copied to Swarm service healthcheck. Public HTTP services should probe the local app port, for example `http://127.0.0.1:<port>/healthz`.
- Single-service `storage` can map named volumes from `volumes` to manager storage classes.

## Compose And Storage Rules

- Keep `docker-compose.yml` standard. Put Luma semantics in `luma.compose.yml`.
- Create a sidecar with `luma compose init --compose docker-compose.yml --output luma.compose.yml`.
- Do not put deployment-side `storageClasses` in production sidecars. Luma Control owns storage classes; sidecars only reference them by name.
- Register managed NFS storage with:

```bash
luma storage set home-nfs \
  --node home-nas \
  --path /srv/luma \
  --region cn \
  --region home
```

- Register an external NFS server with:

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

- A sidecar volume can reference registered storage with `storageClass: <name>` and optional `path`, `accessMode`, `adopted`, or `initialize: empty`.
- `local.node` is the explicit local bind storage escape hatch. Luma pins every service using that volume to the specified Luma node.
- Bare Compose named volumes are allowed but unmanaged; Swarm rescheduling can land the task on a node with different local data.
- Switching an already deployed volume to another backend is blocked unless `adopted: true` follows verified migration, or `initialize: empty` explicitly accepts a fresh path.
- Managed cross-region NFS requires the storage node to have a `tailscaleIP`; otherwise validation/render/deploy should block.
- If one top-level Compose volume is used by services in different regions and managed storage would resolve to different endpoints, split it into separate region-specific volume names.

## Templates

China public service:

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
```

Home Tailscale relay:

```yaml
name: home-panel
image: ghcr.io/acme/home-panel:1.0.0
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
```

Bounded worker on a small manager:

```yaml
name: bounded-worker
image: ghcr.io/acme/bounded-worker:1.0.0
region: cn
exposure: none
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

Compose sidecar:

```yaml
name: app-stack
compose: docker-compose.yml
region: cn

volumes:
  pg-data:
    storageClass: home-nfs
    path: postgres/pg-data
    accessMode: ReadWriteOnce

services:
  app:
    exposure: cn-edge
    domain: app.example.com
    port: 3000

  postgres:
    region: home
```

## Validation Commands

Single service:

```bash
luma validate service.yaml
luma deploy service.yaml --dry-run
```

Compose:

```bash
luma compose validate luma.compose.yml
luma compose render luma.compose.yml
luma storage check luma.compose.yml
luma storage apply luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --dry-run
```

Deploy with event streaming only when the user asks for a real deploy:

```bash
luma deploy service.yaml --format ndjson --timeout 1800
luma compose deploy luma.compose.yml --format ndjson --timeout 1800
```

## CI Usage

For generic CI, install the PyPI package. The distribution is `luma-infra`, but the command remains `luma`:

```bash
python -m pip install "luma-infra==0.1.63"
```

CI should authenticate statelessly and should not run the shell installer, Docker, SSH bootstrap, Cloudflare setup, or Portainer setup:

```bash
export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"
```

PR checks should validate and dry-run:

```bash
luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
luma compose validate luma.compose.yml --format json
luma storage check luma.compose.yml --format json
```

## Operational Notes

- `latest` or omitted image tags are resolved by the manager to `name@sha256:...` during deploy. Prefer pinned version tags for production rollback.
- Private registry credentials are stored with `luma registry login` and matched by image registry host during deploy. Luma pre-pulls with auth and links the Portainer/Swarm registry credential through the Portainer API path.
- `luma service remove <name>` uses the manifest recorded by the control plane during the last successful single-service or Compose deploy. This also works for deployments created from the web dashboard.
- Storage data is preserved by default. Add `--delete-storage` only when intentionally deleting removable managed storage referenced by the recorded deployment. It cannot be combined with `--skip-portainer`.
- `region` controls workload scheduling; Portainer deployment itself runs through the manager.
- Current Portainer stacks constrain the agent to manager nodes while keeping endpoint compatibility, so worker agent gossip issues should not be diagnosed as manifest region errors.
- Required platform ports are separate from manifest `port`: public `80/tcp` and `443/tcp`, trusted operator `9443/tcp`, Swarm `2377/tcp`, node gossip `7946/tcp` and `7946/udp`, and overlay `4789/udp`.
- `luma update` on a manager refreshes Luma Control only. Use `luma bootstrap manager --domain <control-domain>` for first install or explicit ingress/egress/bootstrap repair.
