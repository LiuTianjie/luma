# Luma Deployment Reference

## Single-Service Manifest Fields

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `name` | yes | string | Service name. Luma slugifies it for stack, service, route, and deployment records. |
| `image` | required unless `build` is set | string | Prebuilt container image. `latest` or omitted tags may be resolved to `name@sha256:...` when Luma can validate the pull on the manager or a fixed target node. For Repository Import, omit `image` and use `build:` so Luma can inject the built internal-registry image. |
| `build` | no | map | Source-to-image build for `luma import` / Dashboard Repository Import. Subfields: `build.context` (default `.`), `build.dockerfile` (default `Dockerfile`), `build.platform` (default `linux/amd64`), and optional `build.repo` to override the internal image repository path. When present, `image` may be omitted. Not used by plain `luma deploy`. |
| `region` | yes | `cn` / `global` / `home` | Runtime placement region. |
| `engine` | no | `nomad` | Orchestrator override. Omit to inherit the cluster default. |
| `node` | no | string | Luma node name from `luma node join --name`; control-plane deploy resolves it to a node-meta placement constraint and keeps the region constraint. Stable across node restarts. Do not use Docker hostnames for normal pins. |
| `exposure` | recommended | `none` / `cn-edge` / `external-edge` / `tailscale-relay` / `tcp-relay` / `cloudflare-tunnel` | Access mode. Use explicit exposure in new files. |
| `domain` | public only | string | Public hostname for exposed services. |
| `port` | public only | integer | Container internal port, not the cloud firewall or host port. |
| `publishPort` | public services | integer | Explicit Nomad bridge port mapping on Linux nodes: host `publishPort` -> container `port`. For `cn-edge` / `external-edge`, omit for dynamic ports. For Mac/OrbStack relay services, omit because they use docker host mode and routes target the real `port`. |
| `replicas` | no | integer | Defaults to `1`; must be at least `1`. |
| `env` / `environment` | no | map | Service environment. Use direct values for non-sensitive settings and `${SECRET_NAME}` for deployment secrets supplied by `--env` or stored with scoped `luma secret set`. |
| `command` | no | string/list | Overrides container command. |
| `constraints` | no | string[] | Extra placement constraints (`attr == value`). Luma adds region and node/storage constraints automatically. |
| `labels` | no | string[] | Extra service tags. Luma adds Traefik routing tags for `cn-edge` and `external-edge`. |
| `networks` | no | string[] | Extra network names for advanced renderers; Nomad services normally use bridge or host mode. |
| `volumes` | no | string[] | Compose-style service mounts. Named sources such as `data:/data` are mounted as named volumes (Nomad mount blocks). |
| `storage` | no | map | Maps named volumes from `volumes` to manager storage classes. Keys must reference named volume sources. |
| `storage.<name>.storageClass` | storage only | string | Registered Luma storage class name. |
| `storage.<name>.path` | no | string | Subdirectory under the storage class export. |
| `storage.<name>.accessMode` | no | `ReadWriteOnce` / `ReadWriteMany` | Informational and used for dashboard diagnosis. |
| `storage.<name>.initialize` | no | `empty` | Explicit fresh-path acknowledgement. |
| `storage.<name>.adopted` | no | boolean | Set after manual migration/adoption verification. |
| `proxy` | no | boolean | Runtime outbound proxy. When true, Luma adds egress network and default proxy env unless already set. Not used for image pulls. |
| `resources` | no | map | CPU/memory limits/reservations. Quote `cpus` values as fractional-core strings such as `"0.50"`; Luma converts to the engine's units (Nomad CPU MHz). |
| `healthcheck` | no | map | Container health check. Public HTTP services should probe `http://127.0.0.1:<port>/healthz` when possible. |
| `relay.host` | relay override | string | Optional advanced upstream host override. Usually omit. |
| `relay.url` | relay override | string | Optional full upstream URL override. Usually omit. |
| `tunnel.tokenEnv` | tunnel | string | Env var name for Cloudflare Tunnel token. Defaults to `CLOUDFLARE_TUNNEL_TOKEN`. |
| `dns.target` | no | string | Optional DNS target override. |
| `dns.type` | no | string | Optional DNS record type override. |
| `dns.proxied` | no | boolean | Optional Cloudflare proxied flag. |
| `stackPath` | no | string | Override generated stack path. Rare. |
| `routePath` | no | string | Override generated tailscale route path. Rare. |

## Exposure Matrix

| Goal | YAML |
| --- | --- |
| Domestic public HTTPS | `region: cn`, `exposure: cn-edge`, `domain`, `port` |
| Overseas/global public HTTPS | `region: global`, `exposure: external-edge`, `domain`, `port` |
| Home service through China edge and Tailscale | `region: home`, `exposure: tailscale-relay`, `domain`, `port`; optional `node` only when pinning |
| Public TCP service | `exposure: tcp-relay`, `domain`, `port`; use one service per published TCP port |
| Home/private Cloudflare Tunnel | usually `region: home`, `exposure: cloudflare-tunnel`, `domain`, `port`, optional `tunnel.tokenEnv` |
| Queue worker or internal service | `exposure: none`, no `domain` or `port` required |
| Runtime needs Luma egress proxy | add `proxy: true`; keep the desired scheduling `region` |

Rules:

- `cn-edge` requires `region: cn`.
- `external-edge` requires `region: global`.
- `tailscale-relay` requires `region: home`.
- `tcp-relay` derives its Traefik entrypoint from `publishPort` or `port`; ordinary MySQL should use port-exclusive routing instead of SNI multiplexing.
- Public services require `domain` and integer `port`.
- `public` has been removed. Use `exposure`.

## Render Behavior

- Every service gets a `${meta.region} == <region>` placement constraint.
- If `node` is set, Luma renders a `${meta.luma_node_name} == <node>` constraint. Node identities are stable across restarts, so a rejoining node keeps its pin with no constraint rewrite.
- `cn-edge` and `external-edge` register Traefik routing via the Nomad service provider (service tags), using a bridge network with `port` as the load-balancer target.
- `tailscale-relay` runs the container in docker host network mode and routes through the published port on the actual home node via a Traefik file-provider route, unless `relay.host`/`relay.url` overrides are set.
- `tcp-relay` adds the derived TCP entrypoint to Traefik, writes a TCP route, and forwards to the host port; the published port is exclusive to that TCP service.
- `cloudflare-tunnel` adds a `cloudflared` sidecar task in the same group using `${<tokenEnv>}`.
- `proxy: true` injects default `HTTP_PROXY`/`HTTPS_PROXY` pointing at the egress proxy unless those env vars are already present.
- Named `volumes` are rendered as Nomad docker `mount` blocks (`type=volume` for named volumes, `type=bind` for host paths) — never the docker `volumes` shorthand, which would bind an empty alloc directory. If a named volume is also declared in `storage`, the resolved storage-class endpoint is used.
- `resources` maps to Nomad `resources` (CPU MHz from fractional cores, MemoryMB); quote `cpus` as a YAML string such as `"0.50"`.
- `healthcheck` is rendered as a Nomad/Traefik check. Use it when task `running` is not enough to prove the app is listening.

## Deployment Secrets

Do not put secret values in manifests, Compose files, or examples. Use `${ENV_NAME}` placeholders in YAML:

```yaml
env:
  NODE_ENV: production
  DATABASE_URL: ${DATABASE_URL}
```

If the project already has a `.env`, prefer passing it at deploy time:

```bash
luma deploy service.yaml --env .env
luma compose deploy luma.compose.yml --env .env
```

The CLI filters the `.env` to variables referenced as `${NAME}` by the manifest or Compose content. Luma Control stores those values under the application scope, using the service or Compose `name`; `api/DATABASE_URL` and `worker/DATABASE_URL` do not overwrite each other.

Manual scoped secret management is available when needed:

```bash
luma secret set DATABASE_URL --scope api
luma secret import .env --scope api
```

Legacy global `luma secret set DATABASE_URL` still works for applications that have no scoped secrets, but new project deploys should prefer `--env` or scoped secrets to avoid cross-project collisions.

## Private Registry Credentials

Do not put registry tokens in manifests or service env. Store them in Luma Control:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
luma registry remove ghcr.io
```

During deploy, Luma matches credentials by image registry host and injects them into the Nomad job's docker `auth` block, so the placed client pulls the private image with the stored credentials.

Private registry image pulls are separate from runtime `proxy: true`. If `curl https://<registry>/v2/` reaches the registry but `docker pull` fails with EOF/timeout, inspect Docker daemon `HTTPProxy`/`HTTPSProxy` and add the private registry host to daemon `NO_PROXY`.

## Builder Registry And Repository Import

Use this section when the user wants Luma to build from GitHub/Gitea or to make a dedicated builder node act as both build host and internal registry.

### Target State

The ergonomic production shape is:

```text
build node: builder
builder capability: docker-build
registry service: luma-registry pinned on builder
registryHost: <builder-tailscale-ip>:5000
pushHost: localhost:5000
target Linux nodes: insecure-registry + Docker daemon NO_PROXY configured
```

For the user's current cluster, `builder` has been used as the intended build+registry node and has been observed at `100.66.177.70:5000`; always re-check live status before relying on that exact IP.

### Setup Or Refresh

1. Confirm the node exists and is build-capable:

```bash
luma status
```

Look for `Build (luma import)` and a ready Linux node named `builder` with `docker-build`.

2. If the node exists but does not advertise `docker-build`, check Docker buildx on the node:

```bash
docker buildx version
```

If needed, install Docker/buildx and refresh the Luma node agent with `luma update` on that node.

3. Start or refresh the in-cluster registry on the builder. Prefer an explicit storage class; do not rely on the default `local` storage class unless `luma storage list` confirms it exists:

```bash
luma registry serve --node builder --storage-class builder-registry-nfs --port 5000
```

If `builder-registry-nfs` is missing or unhealthy, use a known existing class such as `cn-nfs`, or repair/register storage before continuing.

`luma registry serve` deploys `registry:2` as `luma-registry`, pins it to `builder`, uses `localhost:5000` for builder-side push, and exposes `<builder-tailscale-ip>:5000` for other nodes to pull. It should configure `insecure-registries` on ready Linux worker nodes. It intentionally skips manager nodes because restarting the manager Docker daemon can kill Luma Control mid-request.

4. Verify health:

```bash
curl -I http://<builder-tailscale-ip>:5000/v2/
```

Run the same check from at least one target node when debugging pull failures.

### Common Failures

- `registry serve` fails immediately on `storageClass: local`: the control plane probably has no `local` storage class. Re-run with an existing class such as `builder-registry-nfs` or `cn-nfs`.
- A newly registered storage class times out or is missing afterwards: do not assume storage registration completed. Re-check with `luma storage list` before using it for registry data.
- Registry allocation is `running`, port `5000` is listening, but `/v2/` returns `No route to host`: suspect stale Nomad CNI hostport rules. Recreate the registry allocation:

```bash
luma service restart luma-registry --mode recreate
```

- Target-node pull from the internal registry fails with `502 Bad Gateway`: Docker daemon proxy captured private registry traffic. Check target Docker daemon `NO_PROXY`; it should include the registry host, `host:port`, and Tailscale range:

```text
<builder-tailscale-ip>
<builder-tailscale-ip>:5000
100.64.0.0/10
```

- Build fails before push with `invalid value "127.0.0.1", expecting k=v`: suspect buildx driver option parsing of comma-separated `NO_PROXY`. Update Luma or ensure commas are escaped in `--driver-opt env.NO_PROXY=...`; do not treat this as a registry health failure.

### Repository Import Semantics

Repository import should:

1. Resolve source from a saved Git provider account (GitHub or Gitea) or a manually entered repo URL.
2. Support multiple accounts per provider. Select provider type first, then account credential, then repository/ref.
3. Clone/build on the default builder node.
4. Push to the builder registry using `pushHost: localhost:5000`.
5. Use the repository's Luma manifest if present. For single-service import, scan root `.luma.yml`, `.luma.yaml`, `luma.yml`, `luma.yaml`, then nested `*.luma.yml` / `*.luma.yaml`, excluding Compose sidecar names such as `*.compose.luma.yml`, `*.luma.compose.yml`, and `docker-compose.luma.yml`. For Compose import, scan `luma.compose.yml`, `luma.compose.yaml`, `.luma.compose.yml`, `.luma.compose.yaml`, `*.luma.compose.yml`, `*.compose.luma.yml`, and `docker-compose.luma.yml`; the sidecar's `compose:` path points to the Docker Compose file.
6. If no manifest exists, allow manual single-service manifest input. Do not invent AI-filled manifests unless the user explicitly asks for that future workflow.
7. Inject the built deploy image after the build succeeds.
8. Accept runtime environment values through Dashboard's Environment input or CLI `--env .env`. These values are not build-time env and are not exposed to the builder. They are passed to the final deploy request as scoped deployment secrets, filtered by the resolved `.luma.yml` / `luma.compose.yml` plus Compose content, and saved under the service or stack scope.

Git provider tokens are write-only credentials. Do not store or echo PAT values in manifests, logs, or `agentTasks`; inject them only when the build task is leased by the builder node-agent.

Before using Dashboard's repository dropdown, verify the selected provider account can see the repository and refs:

```bash
luma git-provider repos github:personal
luma git-provider refs github:personal owner/repo
```

If refs return GitHub/Gitea `404`, treat it as a provider credential or repository-ownership problem, not a Luma manifest problem. Fine-grained GitHub tokens must include that private repository.

CLI import should be equivalent to Dashboard import:

```bash
luma import --provider-id gitea:lin --repository acme/app --ref main --build-node builder --env .env
```

For an unregistered source, use the positional URL:

```bash
luma import https://github.com/acme/app --build-node builder --env .env
```

For GitHub repositories only, the positional source also accepts `owner/repo`:

```bash
luma import acme/app --build-node builder --env .env
```

Luma expands that shortcut to `https://github.com/acme/app.git`. For Gitea or another self-hosted Git server, use `--provider-id ... --repository ...` or a full clone URL.

If the repository has no deployment file yet, provide one explicitly:

```bash
luma import --provider-id github:personal --repository acme/app \
  --build-node builder \
  --manifest deploy/app.luma.yml \
  --env .env
```

### Repository Manifest Image Semantics

There is no chicken-and-egg requirement to know the final image digest before the first build. Keep the current manifest model:

```yaml
name: app
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
build:
  context: .
  dockerfile: Dockerfile
  platform: linux/amd64
```

When Repository Import sees a manifest with `build:` and no `image`, that is valid. Control resolves the builder registry from build configuration, derives the internal repository path from the Git repository URL, asks the builder to build and push, then deploys with the built image injected.

The default predictable image coordinates are:

```text
registryHost/<owner>/<repo>:latest
registryHost/<owner>/<repo>:<git-sha>
```

`registryHost` is the pull host target nodes use, such as `<builder-tailscale-ip>:5000`. `pushHost` is usually `localhost:5000` and is only for the builder node. Users should not hardcode `pushHost` in deployment YAML.

The builder pushes both tags. Treat `:latest` as the predictable rolling alias that humans can know before the build. Treat `:<git-sha>` as the immutable build result that Luma should inject into the actual deploy payload.

If the default `<owner>/<repo>` path is not desired, set `build.repo`:

```yaml
name: app
region: cn
build:
  repo: apps/price
```

Do not add a second top-level field for this unless the Luma manifest schema changes. `image` remains for prebuilt external images; `build:` marks an image that Luma will produce and inject.

### Compose Repository Import

Compose repositories must be first-class in the same build+registry flow. A repository can contain:

```yaml
# luma.compose.yml
name: app-stack
compose: docker-compose.yml
region: cn
services:
  web:
    exposure: cn-edge
    domain: app.example.com
    port: 3000
```

```yaml
# docker-compose.yml
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
      platform: linux/amd64
    environment:
      NODE_ENV: production
  redis:
    image: redis:7-alpine
```

During Repository Import, Luma builds only Compose services with a `build:` block, pushes them to the builder registry, removes their `build:` block from the runtime Compose content, and injects `image:`. Services that already have only `image:` are preserved.

Default image coordinates are:

```text
# one built compose service
registryHost/<owner>/<repo>:latest
registryHost/<owner>/<repo>:<git-sha>

# multiple built compose services
registryHost/<owner>/<repo>/<service>:latest
registryHost/<owner>/<repo>/<service>:<git-sha>
```

Use `build.repo` inside a Compose service to override the internal repository path:

```yaml
services:
  api:
    build:
      context: ./api
      dockerfile: Dockerfile
      repo: apps/price-api
```

The final Compose deployment still follows normal `luma compose deploy` rules: every runtime service must have an `image`, but Repository Import is allowed to produce that image before handing the Compose content to Control.

Use import-mode validation when checking a repository Compose sidecar before the image exists:

```bash
luma compose validate --import-mode luma.compose.yml
```

Plain `luma compose validate` and `luma compose deploy` do not build images. They require the runtime Compose content to already contain `image:` for every service.

## Single-Service Examples

### Public API

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
env:
  NODE_ENV: production
  DATABASE_URL: ${DATABASE_URL}
```

### Resource Limits

```yaml
name: bounded-api
image: ghcr.io/acme/api:1.0.0
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

### storageClass Volume

```yaml
name: home-db
image: postgres:16
region: home
exposure: none
volumes:
  - pg-data:/var/lib/postgresql/data
storage:
  pg-data:
    storageClass: db-storage
    path: home-db/pg-data
    accessMode: ReadWriteOnce
```

## Compose Sidecar Fields

`docker-compose.yml` stays standard. `luma.compose.yml` carries Luma deployment semantics:

| Field | Required | Notes |
| --- | --- | --- |
| `name` | yes | Stack/deployment name. Reusing it updates the same Nomad job. |
| `compose` | yes | Relative path to the standard Compose file. Control-plane deploy rejects absolute paths and `..`. |
| `region` | yes | Default service region: `cn`, `global`, or `home`. |
| `volumes.<name>.storageClass` | no | Registered manager storage class. |
| `volumes.<name>.path` | no | Subdirectory under the storage class export. Defaults to volume name when omitted. |
| `volumes.<name>.accessMode` | no | `ReadWriteOnce` or `ReadWriteMany`; informational and dashboard-facing. |
| `volumes.<name>.initialize` | no | `empty` for a deliberately fresh storage path. |
| `volumes.<name>.adopted` | no | `true` after verified manual migration/adoption. |
| `volumes.<name>.local.node` | no | Luma node name for explicit local bind storage. |
| `volumes.<name>.local.path` | no | Host path for `local.node` bind storage. |
| `services.<name>.region` | no | Per-service region override. |
| `services.<name>.node` | no | Explicit Luma node pin for that service. |
| `services.<name>.exposure` | no | `none`, `cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, or `cloudflare-tunnel`. |
| `services.<name>.domain` | public only | Public hostname. |
| `services.<name>.port` | public only | Container internal port. |
| `services.<name>.publishPort` | relay only | Explicit Nomad bridge port mapping on Linux nodes for `tailscale-relay` or `tcp-relay`; omit on Mac/OrbStack nodes. |
| `services.<name>.replicas` | no | Replica count; must be at least `1`. |
| `services.<name>.proxy` | no | Adds Luma egress env/network for runtime outbound traffic. |
| `services.<name>.relay` | relay only | Optional relay override. Usually omit. |
| `services.<name>.tunnel` | tunnel only | Optional Cloudflare Tunnel settings. |

Do not put non-empty `storageClasses` in production sidecars. Luma Control owns shared storage declarations through `luma storage set`.

### Compose Example

```yaml
name: app-stack
compose: docker-compose.yml
region: cn

volumes:
  pg-data:
    storageClass: cn-nfs
    path: postgres/pg-data
    accessMode: ReadWriteOnce

  cache-data:
    local:
      node: home-mac-mini
      path: /opt/luma/state/cache-data

services:
  app:
    exposure: cn-edge
    domain: app.example.com
    port: 3000

  postgres:
    region: home
```

## Storage Operations

Managed NFS:

```bash
luma storage set cn-nfs \
  --node <manager-or-storage-node> \
  --path /srv/luma \
  --region cn
```

External NFS:

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

Checks and preparation:

```bash
luma storage list
luma storage check luma.compose.yml
luma storage apply luma.compose.yml --dry-run
luma storage apply luma.compose.yml
```

Storage rules:

- A named volume referenced in `luma.compose.yml` must exist in `docker-compose.yml` and be mounted by at least one service.
- Managed cross-region storage needs the storage node's Tailscale address from node registration.
- If the same Compose volume is used by services in different regions and resolves to different storage endpoints, split it into multiple volume names.
- Luma does not auto-copy data when switching storage backends. Use `adopted: true` only after verifying migration; use `initialize: empty` only for intentionally fresh paths.
- Removing a deployment preserves data by default.

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
luma compose deploy luma.compose.yml --dry-run
```

CI:

```bash
python -m pip install "luma-infra==0.1.170"
export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"
luma validate service.yaml --format json
luma deploy service.yaml --dry-run --format json
luma compose validate luma.compose.yml --format json
luma storage check luma.compose.yml --format json
```

Live deploys can stream events:

```bash
luma deploy service.yaml --format ndjson --timeout 1800
luma compose deploy luma.compose.yml --format ndjson --timeout 1800
```

When deploying manifests that reference secrets and the project has a `.env`, include `--env .env` so Luma imports scoped deployment secrets.

## Remove Behavior

```bash
luma service remove <name> --dry-run
luma service remove <name>
```

Luma removes by deployed name, not by local YAML path. The control plane uses the manifest or sidecar recorded during the last successful deploy, so removal also works for web-dashboard deployments.

By default, Luma removes Luma-managed DNS, the Nomad job, generated jobspec files, and `tailscale-relay` / `tcp-relay` route files. Storage data is preserved. Add `--delete-storage` only when intentionally deleting removable managed storage referenced by the recorded deployment:

```bash
luma service remove <name> --dry-run --delete-storage
luma service remove <name> --delete-storage
```

`--delete-storage` for single-service manifests removes managed storage paths and named Docker volume objects from the recorded manifest while skipping bind mounts. For Compose it removes managed storage subdirectories referenced by the recorded sidecar, not the storage class itself.

## Review Checklist

- Does `domain` match the actual user-facing hostname?
- Is `port` the container's internal port?
- Is `region` compatible with `exposure`?
- If `node` is set, does it match a registered Luma node name and the selected region?
- If `node` is set, does it match a registered Luma node name and the selected region? (Node identity is stable across restarts, so a rejoined node keeps its pin.)
- Are secrets represented as `${ENV_NAME}` and supplied through `--env .env` or scoped `luma secret set --scope <app>`?
- Are private registry credentials stored with `luma registry login` instead of YAML/env?
- Does the image include a meaningful tag? If not, remember that Luma resolves mutable tags to digests during deploy.
- If the service needs runtime outbound network access, is `proxy: true` used instead of manual proxy boilerplate?
- Are CPU `cpus` values quoted strings?
- On small manager nodes, are `resources` limits/reservations reasonable?
- For stateful services, is storage declared through `storageClass` or an explicit `local.node` pin?
- For `tailscale-relay` or `tcp-relay`, is `publishPort` free on the target node?
- For Compose, are storage backend changes guarded with `adopted: true` or `initialize: empty`?
- Should the service be public, or is `exposure: none` safer?

## Required Platform Ports

Manifest `port` is the container's internal listening port. It is separate from Luma platform ports:

| Port | Required path | Purpose |
| --- | --- | --- |
| `80/tcp` | public clients to edge manager | HTTP redirect and Let's Encrypt challenge. |
| `443/tcp` | public clients to edge manager | HTTPS ingress for public services and Luma Control. |
| `4646/tcp` | Control / operators to manager | Nomad HTTP API. |
| `4647/tcp` | clients to server | Nomad RPC. |
| `4648/tcp`, `4648/udp` | all Nomad nodes | Nomad Serf gossip. |

The Nomad cluster ports (`4646`/`4647`/`4648`) are kept private to the Tailscale interface by the host firewall and public-iface guards; they are not exposed on the public internet.
