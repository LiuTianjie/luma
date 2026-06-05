# Luma Deployment Reference

## Single-Service Manifest Fields

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `name` | yes | string | Service name. Luma slugifies it for stack, service, route, and deployment records. |
| `image` | yes | string | Container image. `latest` or omitted tags are resolved to `name@sha256:...` during deploy. Prefer pinned version tags for production rollback. |
| `region` | yes | `cn` / `global` / `home` | Runtime placement region. |
| `node` | no | string | Luma node name from `luma node join --name`; control-plane deploy resolves it to a Swarm NodeID constraint and keeps the region constraint. |
| `exposure` | recommended | `none` / `cn-edge` / `external-edge` / `tailscale-relay` / `cloudflare-tunnel` | Access mode. Use explicit exposure in new files. |
| `domain` | public only | string | Public hostname for exposed services. |
| `port` | public only | integer | Container internal port, not the cloud firewall or host port. |
| `publishPort` | relay only | integer | Host-mode published port for `tailscale-relay`; defaults to `port` when omitted. |
| `replicas` | no | integer | Defaults to `1`; must be at least `1`. |
| `env` / `environment` | no | map | Service environment. Use direct values for non-sensitive settings and `${SECRET_NAME}` for values stored with `luma secret set`. |
| `command` | no | string/list | Overrides container command. |
| `constraints` | no | string[] | Extra Swarm placement constraints. Luma adds region and node/storage constraints. |
| `labels` | no | string[] | Extra service labels. Luma adds Traefik labels for `cn-edge` and `external-edge`. |
| `networks` | no | string[] | Extra external overlay networks. |
| `volumes` | no | string[] | Compose-style service mounts. Named sources such as `data:/data` are rendered as stack volumes. |
| `storage` | no | map | Maps named volumes from `volumes` to manager storage classes. Keys must reference named volume sources. |
| `storage.<name>.storageClass` | storage only | string | Registered Luma storage class name. |
| `storage.<name>.path` | no | string | Subdirectory under the storage class export. |
| `storage.<name>.accessMode` | no | `ReadWriteOnce` / `ReadWriteMany` | Informational and used for dashboard diagnosis. |
| `storage.<name>.initialize` | no | `empty` | Explicit fresh-path acknowledgement. |
| `storage.<name>.adopted` | no | boolean | Set after manual migration/adoption verification. |
| `proxy` | no | boolean | Runtime outbound proxy. When true, Luma adds egress network and default proxy env unless already set. Not used for image pulls. |
| `resources` | no | map | Swarm `deploy.resources` limits/reservations. Quote `cpus` values as strings. |
| `healthcheck` | no | map | Swarm service healthcheck. Public HTTP services should probe `http://127.0.0.1:<port>/healthz` when possible. |
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
| Home/private Cloudflare Tunnel | usually `region: home`, `exposure: cloudflare-tunnel`, `domain`, `port`, optional `tunnel.tokenEnv` |
| Queue worker or internal service | `exposure: none`, no `domain` or `port` required |
| Runtime needs Luma egress proxy | add `proxy: true`; keep the desired scheduling `region` |

Rules:

- `cn-edge` requires `region: cn`.
- `external-edge` requires `region: global`.
- `tailscale-relay` requires `region: home`.
- Public services require `domain` and integer `port`.
- `public` has been removed. Use `exposure`.

## Render Behavior

- Every service gets `node.labels.region == <region>`.
- If `node` is set and the control plane knows the node, Luma renders `node.labels.luma.node.id == <node-id>`; otherwise local render may use `node.labels.luma.node.name == <node>`.
- `cn-edge` and `external-edge` add Traefik labels, attach the public overlay network, and use `port` as the load-balancer server port.
- `tailscale-relay` deploys the stack first, inspects running tasks, then routes through host-mode published ports on the actual home nodes unless `relay.host`/`relay.url` overrides are set.
- `cloudflare-tunnel` adds a `cloudflared` sidecar using `${<tokenEnv>}`.
- `proxy: true` adds the configured egress overlay network and default `HTTP_PROXY=http://egress_mihomo:7890` / `HTTPS_PROXY=http://egress_mihomo:7890` values unless those env vars are already present.
- Named `volumes` are rendered as stack volumes. If a named volume is also declared in `storage`, Luma renders Docker local driver options for the resolved storage class endpoint.
- `resources` is copied to Swarm `deploy.resources`; `cpus` should be a YAML string such as `"0.50"` for Portainer/Compose compatibility.
- `healthcheck` is copied to the rendered service. Use it when task `running` is not enough to prove the app is listening.

## Private Registry Credentials

Do not put registry tokens in manifests or service env. Store them in Luma Control:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
luma registry remove ghcr.io
```

During deploy, Luma matches credentials by image registry host, pre-pulls with Docker registry auth, and associates the matching Portainer/Swarm registry credential with the stack through the Portainer API path.

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
| `name` | yes | Stack/deployment name. Reusing it updates the same Portainer stack. |
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
| `services.<name>.exposure` | no | `none`, `cn-edge`, `external-edge`, `tailscale-relay`, or `cloudflare-tunnel`. |
| `services.<name>.domain` | public only | Public hostname. |
| `services.<name>.port` | public only | Container internal port. |
| `services.<name>.publishPort` | relay only | Host-mode published port. |
| `services.<name>.replicas` | no | Swarm replicas; must be at least `1`. |
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
python -m pip install "luma-infra==0.1.63"
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

## Remove Behavior

```bash
luma service remove <name> --dry-run
luma service remove <name>
```

Luma removes by deployed name, not by local YAML path. The control plane uses the manifest or sidecar recorded during the last successful deploy, so removal also works for web-dashboard deployments.

By default, Luma removes Luma-managed DNS, Portainer stack, generated stack files, and tailscale-relay route files. Storage data is preserved. Add `--delete-storage` only when intentionally deleting removable managed storage referenced by the recorded deployment:

```bash
luma service remove <name> --dry-run --delete-storage
luma service remove <name> --delete-storage
```

`--delete-storage` cannot be combined with `--skip-portainer`. For single-service manifests it removes managed storage paths and named Docker volume objects from the recorded manifest while skipping bind mounts. For Compose it removes managed storage subdirectories referenced by the recorded sidecar, not the storage class itself.

## Review Checklist

- Does `domain` match the actual user-facing hostname?
- Is `port` the container's internal port?
- Is `region` compatible with `exposure`?
- If `node` is set, does it match a registered Luma node name and the selected region?
- Are secrets represented as `${ENV_NAME}` and backed by `luma secret set`?
- Are private registry credentials stored with `luma registry login` instead of YAML/env?
- Does the image include a meaningful tag? If not, remember that Luma resolves mutable tags to digests during deploy.
- If the service needs runtime outbound network access, is `proxy: true` used instead of manual proxy boilerplate?
- Are CPU `cpus` values quoted strings?
- On small manager nodes, are `resources` limits/reservations reasonable?
- For stateful services, is storage declared through `storageClass` or an explicit `local.node` pin?
- For Compose, are storage backend changes guarded with `adopted: true` or `initialize: empty`?
- Should the service be public, or is `exposure: none` safer?

## Required Platform Ports

Manifest `port` is the container's internal listening port. It is separate from Luma platform ports:

| Port | Required path | Purpose |
| --- | --- | --- |
| `80/tcp` | public clients to edge manager | HTTP redirect and Let's Encrypt challenge. |
| `443/tcp` | public clients to edge manager | HTTPS ingress for public services and Luma Control. |
| `9443/tcp` | trusted operators to manager | Direct Portainer UI/API access. |
| `2377/tcp` | workers to manager | Docker Swarm control plane. |
| `7946/tcp`, `7946/udp` | all Swarm nodes | Swarm discovery and overlay gossip. |
| `4789/udp` | all Swarm nodes | Overlay/VXLAN data path. |

Current Luma Portainer stacks keep the endpoint URL `tcp://tasks.agent:9001` for compatibility, but schedule `portainer_agent` only on manager nodes. This prevents worker gossip failures from blocking unrelated deploys.
