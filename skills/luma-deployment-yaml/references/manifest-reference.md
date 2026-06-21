# Luma Deployment Reference

## Single-Service Manifest Fields

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `name` | yes | string | Service name. Luma slugifies it for stack, service, route, and deployment records. |
| `image` | yes* | string | Container image. `latest` or omitted tags may be resolved to `name@sha256:...` when Luma can validate the pull on the manager or a fixed target node. Docker Hub-style images prefer the requested image, then Docker daemon egress proxy retry, then configured `defaults.imageMirrors` fallback. Prefer pinned version tags or digests for production rollback; mutable tags can make an old Nomad job version pull newer image bytes. *Optional when a `build` block is present (`luma import`). |
| `build` | no | map | Source-to-image build for `luma import`. Subfields: `build.context` (default `.`), `build.dockerfile` (default `Dockerfile`), `build.platform` (default `linux/amd64`). When present, `image` may be omitted. Not used by `luma deploy`. |
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

## Build From GitHub Source (luma import)

`luma deploy` only deploys prebuilt images. `luma import` builds from source: clone a GitHub repo on a build node, build its Dockerfile, push to an in-cluster registry, then deploy.

One-time setup:

```bash
# 1. A node with docker buildx auto-advertises the docker-build capability.
luma node list

# 2. Start an in-cluster registry (deploys registry:2 on the build node and
#    wires insecure-registries on every ready Linux node for Tailscale pulls).
luma registry serve --node build-1

# 3. Private repos only: store a GitHub token in the secret store.
luma secret set GITHUB_TOKEN <github-pat>
```

Repo `.luma.yml` (a normal manifest with a `build` block instead of `image`):

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

Import and deploy:

```bash
luma import https://github.com/acme/myapp --build-node build-1
luma import https://github.com/acme/myapp --build-node build-1 --ref release --region cn --exposure cn-edge --domain myapp.example.com --port 8080
```

The built image is tagged `<build-node-tailscale-host>:5000/<owner>/<repo>:<git-sha>`. CLI flags (`--ref`, `--region`, `--exposure`, `--domain`, `--port`, `--platform`, `--registry-host`) override the repo's `.luma.yml`. When the build node is arm64 and target nodes are amd64, keep `platform: linux/amd64` and ensure the build node has `qemu`/`binfmt`.

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
python -m pip install "luma-infra==0.1.84"
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
