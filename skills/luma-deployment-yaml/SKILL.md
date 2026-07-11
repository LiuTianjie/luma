---
name: luma-deployment-yaml
description: Generate, review, validate, or fix Luma single-service deployment manifests and Compose sidecars for the infra-stacks/Luma project. Use when asked about luma deploy YAML, luma.compose.yml, repository import/build from GitHub or Gitea, builder nodes, in-cluster registry setup with luma registry serve, Docker/buildx/NO_PROXY issues, region/exposure choices, storageClass volumes, private registry credentials, service removal, CI deploys, cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, home services, global workers, or Nomad job / deploy errors.
---

# Luma Deployment YAML

Use this skill for Luma deployment artifacts:

- Single-service manifests consumed by `luma deploy`.
- Compose deployments made from a standard `docker-compose.yml` plus a Luma sidecar consumed by `luma compose deploy`.

Single-service manifests are not Docker Compose. They describe one service: image, region, optional node pin, exposure, domain, port, replicas, storage, and runtime options. Luma Control renders a Nomad job, syncs DNS, writes Traefik routes, and deploys via the Nomad API.

Compose files stay standard for local development. Put Luma-specific deployment semantics in `luma.compose.yml`: region, exposure, routing, service node pins, local storage pins, and references to manager-managed storage classes.

For complete field tables and examples, read `references/manifest-reference.md`.

For repository import, builder registry setup, or registry pull/proxy failures, read the "Builder Registry And Repository Import" section in `references/manifest-reference.md`.

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
5. Use `${ENV_NAME}` for secrets; never put secret values in YAML. If the project has a `.env`, recommend `luma deploy service.yaml --env .env`, `luma compose deploy luma.compose.yml --env .env`, or `luma import ... --env .env` so Luma stores referenced variables under the application/stack scope.
6. For private images, do not put registry tokens in YAML or container env. Use `luma registry login <host> --username <user> --password-stdin`.
7. Recommend targeted validation and dry runs, not live deploys, unless the user explicitly asks to deploy.

## Single-Service Rules

- Required: `name`, `region`, and either `image` or a `build` block.
- For ordinary prebuilt-image deploys, set `image` explicitly.
- For repository import builds, prefer omitting `image` and adding `build:`. Luma builds and pushes to the configured builder registry, removes the `build` block from the runtime deploy payload, and injects the built image reference.
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
- `build` is for source-to-image builds via `luma import` / Dashboard Repository Import, not plain `luma deploy`. Subfields: `context` (default `.`), `dockerfile` (default `Dockerfile`), `platform` (default `linux/amd64`), and optional `repo` to override the internal image repository path.

## Builder Registry Workflow

- Treat `luma import` / Dashboard Repository Import as a build pipeline, not just a manifest deploy: Git provider -> builder node clone/buildx -> internal registry push -> target node pull -> normal Luma deploy.
- Repository Import must support both single-service manifests and Compose deployments. If the repository has `luma.compose.yml`, build services declared with Compose `build:` blocks, rewrite those services to internal-registry `image:` references, then deploy through the normal Compose deployment path.
- Prefer a dedicated Luma node named `builder` when available. Confirm it is `ready`, Linux, and advertises `docker-build` in `luma status` before using it.
- Dashboard Repository Import should expose the dedicated `builder` node as the build/registry target. Other historical `docker-build` nodes such as `blg` should not appear in the main build-node dropdown unless the product explicitly adds an advanced override path.
- Prefer a builder-hosted registry for internal image distribution. Current expected shape is `registryHost: <builder-tailscale-ip>:5000` for target-node pulls and `pushHost: localhost:5000` for pushes from the builder itself. Re-check the live builder IP before hardcoding it; one known current cluster used `100.66.177.70:5000`.
- Start or refresh the registry with an explicit storage class. Do not rely on default `storageClass: local` unless the control plane actually has a `local` storage class:

```bash
luma registry serve --node builder --storage-class builder-registry-nfs --port 5000
```

- If `builder-registry-nfs` is unavailable, inspect `luma status` / `luma storage list` and use an existing working class such as `cn-nfs`, or register/fix storage first.
- Verify registry health before importing:

```bash
curl -I http://<builder-tailscale-ip>:5000/v2/
```

- Also verify from at least one target Linux node when diagnosing pull failures. A healthy `/v2/` from the manager alone does not prove Docker pulls on every target node are bypassing proxies.
- If the registry allocation is `running` and port `5000` is listening but `/v2/` returns `No route to host`, suspect stale Nomad CNI hostport state; recreate the registry allocation:

```bash
luma service restart luma-registry --mode recreate
```

- `luma registry serve` should configure `insecure-registries` on ready Linux worker nodes and add the registry host plus Tailscale/private ranges to Docker daemon `NO_PROXY`. It skips manager nodes to avoid killing Luma Control by restarting the manager Docker daemon.
- If target-node pulls fail with `502 Bad Gateway`, do not blame the registry first. Check target-node Docker daemon proxy and `NO_PROXY`; it must include the registry host, `host:port`, and Tailscale range (`100.64.0.0/10`).
- If buildx fails with `invalid value "127.0.0.1", expecting k=v`, suspect comma handling in buildx `--driver-opt env.NO_PROXY=...`; update Luma so the entire `env.NO_PROXY=...` driver opt is CSV-quoted. Backslash-comma escaping is not reliable for buildx driver opts.
- Repository Import build-node choices come from declared builder nodes, not every node advertising `docker-build`. Prefer `luma build config --node builder --registry-host <builder-ip>:5000 --push-host localhost:5000`; node role labels such as `builder` are also valid declarations. Dashboard and Control API should reject undeclared build targets, while preserving `builder` as the simple default for first setup.
- For GitHub/Gitea source imports, manage Git provider PATs with the Git Provider credential flow rather than the old special `GITHUB_TOKEN`. Multiple accounts per provider are expected. Tokens are write-only and injected only into leased build tasks.
- Before recommending Dashboard repository selection, verify the selected provider account can list or fetch refs for that repository. If `luma git-provider refs <provider-id> owner/repo` returns 404, the PAT probably lacks access to that private repo or the repo is not under that provider account; use a full public URL only for truly public repos.
- Runtime env for repository import is not a shell export. In Dashboard Repository Import, paste `.env` values into the Environment section. In CLI, pass `luma import ... --env .env`. The values are submitted to Luma Control as scoped deployment secrets, filtered by the final repository manifest/Compose content, and persisted under the service or stack scope for later redeploys.
- When reviewing a repo before import, list `${ENV_NAME}` placeholders in both the Luma manifest and Compose content. Ensure those names already exist in Luma secrets or are supplied through Dashboard Environment / CLI `--env`; local `.env` values are not automatically read by Dashboard.
- Repository import should scan for `.luma.yml`, `.luma.yaml`, `luma.yml`, `luma.yaml`, and nested `*.luma.yml` / `*.luma.yaml`; service manifest matching must exclude Compose sidecar names such as `*.compose.luma.yml`, `*.luma.compose.yml`, and `docker-compose.luma.yml`. If no manifest exists, support a manually entered single-service manifest first. AI-generated manifests are a later enhancement.
- Compose repository import should scan for `luma.compose.yml`, `luma.compose.yaml`, `.luma.compose.yml`, `.luma.compose.yaml`, `*.luma.compose.yml`, `*.compose.luma.yml`, and `docker-compose.luma.yml`. The sidecar's `compose:` path points to the Docker Compose file inside the repository.
- For local validation of a repository Compose sidecar that still has Compose `build:` blocks, use `luma compose validate --import-mode luma.compose.yml`. Plain `luma compose validate` / `luma compose deploy` still require runtime `image:` values because they do not build.
- In CLI import, bare `owner/repo` is a GitHub shortcut and should expand to `https://github.com/owner/repo.git`; use a full URL or a saved Git provider account for Gitea/self-hosted sources.
- Keep the current manifest model: do not add a new image-source field for repository import. In a repository `.luma.yml`, use a `build:` block and omit `image` when the image is produced by Luma. Control resolves the internal image name before build from the builder registry and repository path, then injects the built image into the deploy manifest.
- The default internal image path is predictable before the build: `registryHost/<owner>/<repo>:latest`, with `<owner>/<repo>` derived from the Git repository URL. The builder also pushes `registryHost/<owner>/<repo>:<git-sha>`, and deployment should use the immutable sha tag that build returns. Use `:latest` as the human-readable rolling alias, not as the preferred deployed reference.
- If the default `<owner>/<repo>` path is not desired, set `build.repo` in the repository manifest to the internal repository path to push under; keep `registryHost` and `pushHost` owned by Luma build configuration.
- For Compose imports, services with `build:` but no `image:` are valid. Luma injects `image:` after build. Services that already have only `image:` are not rebuilt. If multiple Compose services are built, default image repos are `registryHost/<owner>/<repo>/<service>:latest` and `:<git-sha>`; a single built service may use `registryHost/<owner>/<repo>:...`. A Compose service may set `build.repo` to override its internal image repository path.
- Build/import runs are operational objects. Use `luma build list`, `luma build logs <id>`, and `luma build retry <id>` to inspect or retry repository imports. Build runs must not persist Git tokens, registry auth, or raw runtime env secret values; credentials are re-injected from Git Provider / Registry credentials when tasks are leased.

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
python -m pip install "luma-infra==0.1.172"
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
