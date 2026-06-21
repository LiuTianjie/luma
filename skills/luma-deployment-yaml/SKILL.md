---
name: luma-deployment-yaml
description: Generate, review, validate, or fix Luma single-service deployment manifests and Compose sidecars for the infra-stacks/Luma project. Use when asked about luma deploy YAML, luma.compose.yml, region/exposure choices, the optional engine field, the build block / luma import (build from GitHub source), luma registry serve (in-cluster registry), routing domains to workloads, storageClass volumes, manager-managed storage, private registry credentials, service removal, rollback/version history, CI deploys, cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, home services, global workers, or Nomad job / deploy errors.
---

# Luma Deployment YAML

Use this skill for Luma deployment artifacts:

- Single-service manifests consumed by `luma deploy`.
- Compose deployments made from a standard `docker-compose.yml` plus a Luma sidecar consumed by `luma compose deploy`.

Single-service manifests are not Docker Compose. They describe one service: image, region, optional node pin, exposure, domain, port, replicas, storage, and runtime options. Luma Control renders a Nomad job, syncs DNS, writes Traefik routes, and deploys via the Nomad API.

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
   - Public TCP service: `exposure: tcp-relay`; Luma derives the TCP entrypoint from `publishPort` or `port`
   - Home/private Cloudflare Tunnel: usually `region: home`, `exposure: cloudflare-tunnel`
   - Worker/internal: `exposure: none`
   - Runtime outbound proxy: add `proxy: true` and keep the chosen scheduling region.
3. Ask only for missing required facts: service or stack name, image or compose path, domain, container port, region/exposure, storage class, and Luma node name when pinning is required.
4. Emit only YAML unless the user asks for explanation.
5. Use `${ENV_NAME}` for secrets; never put secret values in YAML. If the project has a `.env`, recommend `luma deploy service.yaml --env .env` or `luma compose deploy luma.compose.yml --env .env` so Luma stores only referenced variables under the application scope.
6. For private images, do not put registry tokens in YAML or container env. Use `luma registry login <host> --username <user> --password-stdin`.
7. Recommend targeted validation and dry runs, not live deploys, unless the user explicitly asks to deploy.

## Single-Service Rules

- Required: `name`, `image`, `region`.
- Valid regions: `cn`, `global`, `home`.
- Valid exposures: `none`, `cn-edge`, `external-edge`, `tailscale-relay`, `cloudflare-tunnel`, `tcp-relay`.
- Public exposures require `domain` and integer `port`.
- `cn-edge` requires `region: cn`; `external-edge` requires `region: global`; `tailscale-relay` requires `region: home`.
- `tcp-relay` derives its entrypoint from `publishPort` or `port`. It is port-exclusive; ordinary MySQL should not be modeled as same-port SNI multiplexing.
- `port` is the container's internal listening port, not a cloud firewall or host port. For `tcp-relay`, `publishPort` is the task node host port that Traefik forwards to.
- `replicas` defaults to `1` and must be at least `1`.
- Do not use the removed `public` field; set `exposure` explicitly.
- `node` is the Luma node name from `luma node join --name`. Control-plane deploy resolves it to a placement constraint on the node's stable Luma identity and also keeps the region constraint. Do not use Docker hostnames for normal pins; hosts such as OrbStack may share generic names.
- Nomad node identities are stable across agent restarts, so a node that rejoins keeps its pin without needing constraint rewrites.
- Use `proxy: true` for container runtime HTTP/HTTPS egress through Luma. Do not hand-write the default `egress` network or default `HTTP_PROXY`/`HTTPS_PROXY`.
- `proxy: true` is not for image pulls. Image pulls use Docker daemon proxy and registry credentials managed by Luma. For private registries, Docker daemon `NO_PROXY` may need the registry host even when `curl https://<registry>/v2/` is reachable.
- `resources.limits.cpus` and `resources.reservations.cpus` are fractional cores, for example `"0.50"`. Quote them as strings in YAML; Luma converts cores to Nomad CPU MHz.
- `healthcheck` is passed to the running container's health check. Public HTTP services should probe the local app port, for example `http://127.0.0.1:<port>/healthz`.
- `engine` is optional. Omit it to inherit the cluster default. Set `engine: nomad` only when you need an explicit override.
- Single-service `storage` can map named volumes from `volumes` to manager storage classes.
- `image` is optional when a `build` block is present. `build` is for source-to-image builds via `luma import`, not `luma deploy`. Subfields: `context` (default `.`), `dockerfile` (default `Dockerfile`), `platform` (default `linux/amd64`). A manifest must have either `image` or `build`.

## Build From GitHub Source

`luma deploy` only deploys prebuilt images. `luma import` adds a build step: it clones a GitHub repo on a build node, builds the repo's Dockerfile, pushes to an in-cluster registry, then deploys. Use it when the user wants source-to-deploy from a GitHub repo without external CI.

Prerequisites (one-time):

- A Luma node with `docker buildx` (the agent auto-advertises the `docker-build` capability). Cross-arch builds also need `qemu`/`binfmt`.
- An in-cluster registry started with `luma registry serve --node <build-node>`. This deploys `registry:2` (fixed port 5000, persistent volume, Tailscale-internal, `exposure: none`) and configures `insecure-registries` on every non-manager ready Linux node so any node can pull over the Tailscale network. The manager is skipped (restarting its Docker would kill Control); configure the manager's daemon out-of-band if it also runs pulled workloads.
- For private repos: `luma secret set GITHUB_TOKEN <token>`. Public repos need nothing.

The repo must contain a Dockerfile and a `.luma.yml` (a normal service manifest using a `build` block instead of `image`):

```yaml
name: myapp
region: cn
exposure: cn-edge
domain: myapp.example.com
port: 8080
build:
  context: .
  dockerfile: Dockerfile
  platform: linux/amd64
```

Import and deploy (streams clone -> build -> push -> deploy):

```bash
luma import https://github.com/acme/myapp --build-node build-1
```

CLI flags override `.luma.yml`: `--ref`, `--region`, `--exposure`, `--domain`, `--port`, `--platform`, `--registry-host`. The built image is tagged `<build-node-tailscale-host>:5000/<owner>/<repo>:<git-sha>`. The dashboard's Create application page also has a "Import from GitHub" entry that lists only `docker-build`-capable ready nodes.

The build node's `git clone` reaches GitHub through the manager egress gateway for `cn`/`home` build nodes (resolved automatically, same gateway as image pulls and node join); `global` build nodes go direct. Override with `--proxy <url>` if needed.

Upgrade and rollback: re-running the same `luma import` is the upgrade path. Each build tags the image by git SHA (`...:<git-sha>`) and injects that immutable tag; the Nomad job id is the `.luma.yml` `name`, so same name + new SHA is a rolling update of the same job (identical to `luma deploy`'s same-name-is-update behavior). Because each version pins its own `:<git-sha>` and the registry keeps old images, `luma history <name>` / `luma rollback <name>` reliably revert to a prior image. Changing `name` creates a new app instead of upgrading.

Cross-arch caveat: when the build node is arm64 (e.g. a Mac mini) and target nodes are amd64, the image must be built for `linux/amd64` (the default). Ensure the build node has `qemu`/`binfmt`.

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
- Bare Compose named volumes are allowed but unmanaged; pin stateful services to the node that owns the local data.
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

If the target home node already has something bound to host `8080`, keep container `port: 8080` but choose a free `publishPort`, for example `publishPort: 18080`. `publishPort` is the task node host port, not the container port.

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

If the manifest uses `${ENV_NAME}` placeholders and the project has a `.env`, add `--env .env` to the deploy command.

Inspect and roll back a deployed application only when the user asks for runtime rollback:

```bash
luma history <app>
luma rollback <app>
luma rollback <app> --to-version <N>
```

The dashboard exposes the same Nomad job-version rollback from Applications -> Versions. Treat rollback as a running Nomad job revert; it does not rewrite Git, update the stored manifest, reverse migrations, or restore volume data. Compose rollback reverts the whole stack.

## CI Usage

For generic CI, install the PyPI package. The distribution is `luma-infra`, but the command remains `luma`:

```bash
python -m pip install "luma-infra==0.1.84"
```

CI should authenticate statelessly and should not run the shell installer, Docker, SSH bootstrap, or Cloudflare setup:

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

- `latest` or omitted image tags may be resolved to `name@sha256:...` when Luma can validate the pull on the manager or a fixed target node. Prefer pinned version tags or digests for production rollback; mutable tags can make an old Nomad job version pull newer image bytes.
- Private registry credentials are stored with `luma registry login` and matched by image registry host during deploy. Luma injects them into the Nomad job's docker `auth` block so the placed client pulls the private image. If Docker daemon proxying causes EOF/timeout against a private registry, check daemon `NO_PROXY`; do not try to fix it with manifest `proxy: true`.
- For Docker Hub-style images, Luma should prefer the requested image, then configure Docker daemon egress proxy and retry when the target node reports registry network failures, and only then fall back to configured `defaults.imageMirrors`. `defaults.imageMirrors: []` disables mirror fallback.
- `luma service remove <name>` uses the manifest recorded by the control plane during the last successful single-service or Compose deploy. This also works for deployments created from the web dashboard.
- Storage data is preserved by default. Add `--delete-storage` only when intentionally deleting removable managed storage referenced by the recorded deployment.
- `region` controls workload scheduling via a node-meta constraint; the deploy itself is issued by Luma Control through the Nomad API on the manager.
- A service stays running on its node even if that node briefly disconnects from the manager (Nomad keeps the local allocation alive and reconnects), so transient node-down blips should not be diagnosed as manifest region errors.
- Required platform ports are separate from manifest `port`: public `80/tcp` and `443/tcp`, `tcp-relay` published ports such as `3306/tcp`, and the Nomad cluster ports `4646/tcp` (HTTP API), `4647/tcp` (RPC), `4648/tcp`+`4648/udp` (Serf). These cluster ports are kept private to the Tailscale interface.
- `luma update` on a manager refreshes Luma Control only and never touches running service allocations. Use `luma bootstrap manager --domain <control-domain>` for first install or explicit ingress/egress/bootstrap repair.
- `luma update fleet` updates ready non-manager node agents from a logged-in client. It skips the manager so the active control plane is not updated through a remote fleet task. Update the manager separately with `luma update manager` from the manager host.
- Bootstrap/update installs Tailscale watchdogs on supported manager and node hosts. When diagnosing a node-down heartbeat failure, first separate Docker/container health from Tailscale peer TCP reachability on the Nomad RPC/Serf ports `4647`/`4648`.
