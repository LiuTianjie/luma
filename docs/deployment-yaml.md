# Luma Deployment YAML

A Luma deployment manifest is not Docker Compose. It is a smaller service manifest where you describe the service entrypoint, image, region, and a small set of runtime parameters. `luma deploy` submits it to the control plane, which renders a Nomad jobspec, syncs DNS, configures Traefik routes, and deploys through the Nomad HTTP API.

## Minimal public service

A public service in the `cn` region typically looks like this:

```yaml
name: status
image: traefik/whoami:latest
region: cn
exposure: cn-edge
domain: status.example.com
port: 80
replicas: 1
```

Deploy it:

```bash
luma deploy status.yaml
```

Result:

- DNS writes the `status.example.com` record (Cloudflare, Alibaba Cloud DNS, or DNSPod);
- Traefik creates an HTTPS route for `Host(status.example.com)`;
- Nomad deploys the `status` job;
- requests forward to container `port: 80`.

## Field reference

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | Yes | string | Service name. Luma converts it to a slug used for the Nomad job/group/task names. |
| `image` | Yes* | string | Container image, such as `ghcr.io/acme/api:1.0.0`. `latest` or untagged references are resolved to `name@sha256:...` at deploy time. *May be omitted when a `build` block is provided and the image is produced by a build. |
| `build` | No | map | Build the image from source (`luma builder submit` / `luma build local`). Sub-fields: `context` (default `.`), `dockerfile` (default `Dockerfile`), `platform` (default `linux/amd64`), `target` (optional Dockerfile stage). When `build` is set, `image` may be omitted. See "Building and deploying from a Git provider" below. |
| `region` | Yes | `cn` / `global` / `home` or a created custom region | Region where the service runs. Custom regions must first be created with `luma region create`. |
| `engine` | No | `nomad` | Orchestration backend for this service. Defaults to Nomad and usually does not need to be set. |
| `node` | No | string | Pin the service to a Luma node name (the value passed to `luma node join --name`). Control renders a Nomad `${node.unique.name}` (or `meta.luma_node_name`) constraint while still preserving the `region` constraint. |
| `exposure` | No | see below | Ingress mode. New manifests must explicitly declare public, tunnel, or internal access semantics. |
| `domain` | Required for public services | string | Public domain name used by clients. |
| `port` | Required for public services | integer | Internal listening port inside the container, not the cloud firewall port. |
| `replicas` | No | integer | Replica count, rendered to Nomad group `count`. Defaults to `1`; must be `>= 1`. |
| `env` / `environment` | No | map | Environment variables written to the Nomad task `env` block. |
| `command` | No | string/list | Override the container startup command. |
| `constraints` | No | string[] | Additional Nomad placement constraints. Luma adds the `region` constraint automatically. |
| `labels` | No | string[] | Additional service tags/labels. Traefik routing tags for public services are generated automatically. |
| `networks` | No | string[] | Additional network declarations. Public ingress is discovered automatically by the Traefik Nomad provider. |
| `proxy` | No | boolean | Whether the running container routes outbound traffic through the Luma egress proxy. Setting `true` attaches egress proxy settings and proxy env vars; placement still follows `region`. This is separate from image-pull proxying. |
| `resources` | No | map | Rendered into the Nomad task `resources` block. CPU uses elastic `reservations.cpus` sharing (`limits.cpus` is not enforced). Memory reservation maps to `memory` and limit maps to `memory_max`; Nomad memory oversubscription is enabled automatically on first deploy so `limit` acts as the hard container ceiling. |
| `healthcheck` | No | map | For single-service `cn-edge` / `external-edge` / `node-edge`, rendered into a Nomad HTTP or TCP check; extractable HTTP URLs supply the path, otherwise falls back to TCP (arbitrary shell commands are not executed in the container). For other modes, inspect the rendered `Checks`. |
| `requirements` | No | map | Component dependency contract supporting `capabilities` (`cloudflare` / `tailscale` / `egress` / `registry`), `secrets`, `init`, and `notes`. `init` supports `cloudflare-dns`, `cloudflare-tunnel`, `tailscale-node`, `egress`, `registry`, and `secrets`. Preview merges explicit declarations with inferred requirements and blocks submission when required items are missing; deployment executes and records the init plan first. |
| `publishPort` | Optional for public services | integer | Explicitly enable Nomad bridge port mapping from host `publishPort` to container `port`. Native Mac/OrbStack single-service deploys still omit this; Mac Compose supports Docker-managed port mappings, see [Custom-region public ingress](region-public-ingress.md#mac-compose-networking). |
| `relay` | Optional for `tailscale-relay` | map | Override the Tailscale relay upstream. Derived by default from the node pinned via `node` and its Tailscale address; Compose can inherit a sibling service or local volume node constraint. |
| `tcp` | Optional for `tcp-relay` | map | Advanced upstream override for TCP relay. Normally omitted; the entrypoint is derived automatically from `publishPort` / `port`. |
| `tunnel` | Optional for `cloudflare-tunnel` | map | Cloudflare Tunnel settings such as `tokenEnv`. |
| `dns.servers` | No | IP or IP list | Container DNS resolver servers; inherits the default behavior when omitted. |
| `cdn` | No | string or map | Enable Alibaba Cloud ESA: `cdn: esa`, or configure `provider`, `scope` (`global` / `domestic` / `overseas`), `purge` (paths refreshed after deploy; defaults to the full hostname, `[]` disables purge), and `cache` (shared cache paths and TTL seconds). Supported for `cn-edge`, `external-edge`, and `tailscale-relay`. See [CDN](cdn.md). |
| `redirectFrom` | No | hostname list or map | Redirect `GET`/`HEAD` requests on these hostnames with HTTP 301 to `domain` (preserving path and query string), while forwarding other methods directly so callbacks are not lost. With `{hosts: [...], except: ["/api/*"]}`, matching paths never redirect on any method (useful for APIs sending `Authorization` headers). DNS always points directly to the edge with an A record, bypassing CDN. Supported for `cn-edge`, `external-edge`, and `tailscale-relay`. |
| `stackPath` | No | string | Override the generated jobspec path. Rarely needed. |
| `routePath` | No | string | Override the Tailscale route file path. Rarely needed. |

## Container DNS {#container-dns}

Single-service manifests use `dns.servers`:

```yaml
dns:
  servers: [100.100.2.136, 100.100.2.138]
```

Compose uses the standard service field in `docker-compose.yml`, not the Luma sidecar:

```yaml
services:
  worker:
    image: example/worker:1
    dns: [100.100.2.136, 100.100.2.138]
```

IPv4, IPv6, and a single IP string are supported. This configures only the container's runtime resolver and does not alter DNS for the host, Control, image pulls, or the egress proxy. Private cloud endpoints (such as Alibaba Cloud SLS private endpoints) require the cloud's internal DNS; other clouds or home nodes should specify resolvers reachable from those nodes.

Standalone workers and host-networked services use Docker `dns_servers`; Linux CNI bridge networking uses Nomad's network-level `DNS.Servers`; Mac Compose passes the setting to the shared-network container. All services sharing a network must declare the exact same `dns` setting, or deployment is rejected so one service cannot silently change another service's resolver.

Both CLI and Control must run a version supporting `container-dns-v1`. After editing the configuration in Git, run a deployment—upgrading Control or restarting an existing task does not reload Git changes. Once deployed, subsequent container recreations retain the submitted DNS settings.

## Update strategy

Luma renders Nomad `update` blocks with `auto_revert`, `max_parallel = 1`, and a health window enabled by default, so failed rollouts revert automatically to the previous version. Multi-replica services replace instances one at a time via Nomad rolling updates.

For single-replica `cn-edge` / `external-edge` / `node-edge` services using dynamic ports (no `publishPort`), Luma renders Nomad canary + auto-promote: a new allocation starts first, becomes healthy, and only then promotes and stops the old allocation. Configure `healthcheck` on public HTTP services so Nomad waits for service checks to stabilize; without `healthcheck`, health is judged only by the task running state.

Explicit `publishPort`, host-networked `tailscale-relay`, `tcp-relay`, internal services, and services with local persistent state skip canary by default because running old and new allocations simultaneously could collide on host ports or local data. They still use `auto_revert` and `max_parallel = 1`, though single-replica rollouts are not guaranteed zero-downtime.

Compose deployments use the same Nomad `update` baseline. Multi-replica single-service Compose stacks roll one instance at a time without an extra canary; see [Compose replicas](compose-storage.md#replicas) for details and limits. For single-replica `cn-edge` / `external-edge` / `node-edge` Compose services without `publishPort`, Luma uses Nomad dynamic ports and enables canary + auto-promote when the task group has no fixed host ports, `tailscale-relay` / `tcp-relay`, or persistent volumes. If any service in the Compose stack needs a fixed host port or persistent volume, canary is skipped to prevent port conflicts or state corruption.

## Choosing an exposure

| exposure | region | Requires domain/port | Best for |
| --- | --- | --- | --- |
| `cn-edge` | `cn` | Yes | Domestic public web/API traffic routed through the Manager's `cn` Traefik edge. |
| `external-edge` | `global` | Yes | Overseas public services such as external API gateways or tools. |
| `node-edge` | Any region with a public ingress node | Yes | Direct public ingress on a standalone public node (`luma node ingress enable <node> --public-ip <ip>`), bypassing Manager bandwidth and forwarding. |
| `tailscale-relay` | `home` or a custom region with relay enabled | Yes | Exposing services on private/home nodes through edge Traefik over Tailscale. |
| `tcp-relay` | Region with TCP relay enabled | Yes | Raw TCP services such as databases, using a dedicated public port through Traefik TCP -> task host port. |
| `cloudflare-tunnel` | Typically `home` | Yes | Exposing home/private services through Cloudflare Tunnel. |
| `none` | Any registered region | No | Internal jobs, workers, and queue consumers with no public ingress. |

Rules:

- `exposure: cn-edge` requires `region: cn`.
- `exposure: external-edge` requires `region: global`.
- `exposure: node-edge` requires pinning or targeting a node that has node-edge ingress enabled (`luma node ingress enable`). Traffic goes directly `DNS -> node public IP -> node Traefik -> container`.
- `cn-edge` / `external-edge` / `node-edge` use Nomad dynamic ports by default; set `publishPort` explicitly only if you need a fixed host port.
- `exposure: tailscale-relay` works on `home` and on custom regions enabled with `luma region update <name> --exposure tailscale-relay`. When `relay.host` / `relay.url` is omitted, a node constraint is required (Compose can inherit a sibling's node). Offline validation checks syntax only; Control validates against live region capabilities.
- `exposure: tcp-relay` automatically creates a Traefik TCP entrypoint from `publishPort` or `port`. Plain MySQL connections cannot rely on SNI routing, so each published port is exclusive to one TCP service.
- Public services must provide `domain` and an integer `port`.
- The legacy `public` field has been removed; use `exposure`.

## Common templates

### Domestic public API {#api}

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

## Environment variables and secrets

Non-sensitive configuration can be written directly in the manifest:

```yaml
env:
  NODE_ENV: production
  LOG_LEVEL: info
```

Do not put sensitive values in plaintext. Store secrets in Control instead:

```bash
luma secret set DATABASE_URL --scope api
luma secret set OPENAI_API_KEY --scope api
luma secret list
```

If your project already has a `.env` file, pass it at deploy time:

```bash
luma deploy service.yaml --env .env
```

The same works for Compose:

```bash
luma compose deploy luma.compose.yml --env .env
```

`--env` saves secrets isolated under the application name. For example, a service named `api` stores the referenced `DATABASE_URL` under `api/DATABASE_URL`; another service named `worker` with its own `DATABASE_URL` will not overwrite it. Variables in `.env` that are not referenced by the manifest are not uploaded.

Reference them in YAML with `${VAR}`:

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

At deploy time, the client submits only the manifest. Luma Control loads the referenced values from its secret store and writes them into the Nomad task `env`. `luma secret list` prints keys only, never values.

If a referenced variable is missing, deployment fails with:

```text
missing deployment secrets: DATABASE_URL. Run: luma secret set <NAME>
```

## Private container registries

Never put registry pull credentials in a manifest or pass them as application environment variables. Store them in Control instead:

```bash
luma registry login ghcr.io --username <user> --password-stdin
luma registry list
```

Then keep only the image reference in the manifest:

```yaml
image: ghcr.io/acme/private-api:1.0.0
```

When a Builder Registry is configured, Luma leases matching source-registry credentials to the builder, which copies the image and returns the verified internal image digest. The resulting Nomad job references the internal image; only if the internal registry itself requires authentication are its credentials written to Docker `config.auth`, so external source credentials are never distributed to runtime nodes. `luma registry list` displays only registry host and username, never passwords or tokens.

## Compose component requirements

Each `services.<name>` entry in a Compose sidecar can declare the same `requirements` contract as a single-service manifest:

```yaml
services:
  web:
    exposure: cn-edge
    domain: web.example.com
    port: 3000
    requirements:
      capabilities: [cloudflare]
      init: [cloudflare-dns]
```

Control checks these requirements per service before Compose preview and deployment. Unknown capabilities or init actions fail during parsing rather than being ignored silently.

Common GitHub pattern: GitHub Actions pushes application images to private GHCR while the same repository publishes documentation or marketing pages via GitHub Pages. With a Builder Registry configured, GHCR credentials are used only for image copying and runtime nodes pull from the internal registry—no GitHub token is needed in the manifest, and GitHub Pages publishing is unaffected.

Image fetching and runtime `proxy: true` are separate paths. `proxy: true` injects proxy variables only for outbound HTTP/HTTPS requests made by the running container. When a Builder Registry is configured, external source image access, process-level proxy handling, and `defaults.imageMirrors` fallback for Docker Hub-style images are handled by the builder rather than mutating runtime nodes' Docker egress proxy. Set `defaults.imageMirrors: []` to disable image mirror fallback.

When the final image comes from a managed HTTP registry, deployment checks all eligible Docker target nodes before submitting the Nomad job and requires every agent to advertise `managed-registry-access-v2` before modifying any host configuration. Newer agents preserve v1/v2 compatibility and share the same remediation logic for older Control instances. Linux checks Docker's active proxy fields; OrbStack checks its native proxy settings and effective system proxy under `auto` mode rather than assuming the built-in broker address implies an external proxy. The manager performs read-only verification; config updates require an idle engine. See [Operations](operations.md#private-registries-on-new-nodes) for HTTP address scope, compatibility notes, and maintenance boundaries.

## Building and deploying from a Git provider

Plain `luma deploy` only deploys prebuilt images. Remote source builds use the decoupled **Luma Build** addon (`luma-build`) and joined **Build Runners** (`luma runner`): a compatible runner clones a GitHub/Gitea repository, discovers `.luma.yml` or `luma.compose.yml`, builds the Dockerfile or Compose `build:` services, pushes immutable images to the configured OCI registry, and hands off deployment to Control.

One-time prerequisites:

1. Install the Builder addon (`luma addon install builder`) and attach at least one native-architecture runner (`luma builder token create` + `luma runner join`). See [Build runners](runners.md).
2. Configure registry credentials (`luma registry login` for cluster pull access and `luma-build` push credentials via scoped secrets / addon config).
3. For private repositories, save Git provider credentials once; public repositories can be submitted directly by URL.

```bash
printf '%s' "$GITEA_TOKEN" | luma git-provider set gitea team \
  --base-url https://gitea.example.com \
  --username team \
  --token-stdin

luma git-provider repos gitea:team
```

Place a `.luma.yml` in the repository root—it is a standard service manifest with a `build` block instead of `image`:

```yaml
name: myapp
region: cn
exposure: cn-edge
domain: myapp.example.com
port: 8080
build:
  context: .
  dockerfile: Dockerfile
  platform: linux/amd64   # default; set explicitly when target arch differs
env:
  NODE_ENV: production
```

Submit a build and deploy:

```bash
luma builder submit https://github.com/acme/myapp --ref main
```

GitHub repositories support `owner/repo` shorthand:

```bash
luma builder submit acme/myapp --ref main
```

The shorthand always means GitHub `owner/repo` and expands to `https://github.com/acme/myapp.git`. For Gitea or self-hosted Git, reference a saved provider:

```bash
luma builder submit --provider-id gitea:team --repository acme/myapp --ref main --env .env
```

Compose repositories are supported too: `luma builder submit` discovers `luma.compose.yml` / `.luma.compose.yml` / `*.luma.compose.yml` / `*.compose.luma.yml` / `docker-compose.luma.yml`, builds every service in `docker-compose.yml` that defines `build:`, pushes images to the configured registry, and injects the resulting `image:` digests into the deployment payload. Validate build-only Compose manifests locally with:

```bash
luma compose validate --import-mode luma.compose.yml
```

If a repository has separate staging/production sidecars, pass the exact repository-relative Compose sidecar path:

```bash
luma builder submit https://github.com/acme/app.git \
  --ref v1.2.3 \
  --compose-sidecar deploy/staging.luma.compose.yml
```

The path is resolved only after the runner clones the repository. Absolute or non-normalized paths, `..`, missing files, invalid Luma Compose sidecars, and symlink escapes are rejected. Explicit selection is fail-closed and never falls back to another discovered manifest.

Plain `luma compose validate` / `luma compose deploy` never builds images, so every service in runtime Compose must already specify `image:`.

The CLI streams clone -> build -> push -> deploy progress. Runners are selected automatically by native platform, cache affinity, and weight; `--env .env` passes runtime environment variables as scoped secrets filtered by the resolved `.luma.yml` or Compose sidecar. Inspect build history with `luma builder list`, view failure logs with `luma builder logs <id>`, retry with `luma builder retry <id>`, or cancel with `luma builder cancel <id>`.

The Dashboard **Create application** page also includes a Git repository submission form with provider/repository/ref selection and live build logs.

`build` block fields:

| Field | Default | Description |
| --- | --- | --- |
| `context` | `.` | Docker build context (repository-relative path). |
| `dockerfile` | `Dockerfile` | Dockerfile path (repository-relative path). |
| `platform` | `linux/amd64` | Target platform for `docker buildx build --platform`. |
| `target` | *(none)* | Optional literal Dockerfile build stage name. |

### Overseas worker {#worker}

```yaml
name: fetch-worker
image: ghcr.io/acme/fetch-worker:1.0.0
region: global
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
  OPENAI_BASE_URL: https://api.openai.com/v1
```

Rendered with the `region` constraint automatically:

```hcl
constraint {
  attribute = "${meta.region}"
  value     = "global"
}
```

### Pinning to a specific node

If a service must stay on a specific host—for example due to local disk state, a home Mac mini, or debugging a worker—set `node`:

```yaml
name: home-db
image: postgres:16
region: home
node: mac-mini-home
exposure: none
volumes:
  - home_db_data:/var/lib/postgresql/data
```

Control keeps the `region` constraint and renders the Luma node name as a Nomad node constraint:

```hcl
constraint {
  attribute = "${meta.region}"
  value     = "home"
}
constraint {
  attribute = "${meta.luma_node_name}"
  value     = "mac-mini-home"
}
```

`node` is the Luma node name, not the Docker hostname. This matters on OrbStack: multiple Macs may share the Docker hostname `orbstack`, whereas Luma targets a unique host via `meta.luma_node_name`.

Nomad node identity is a stable UUID. If a node leaves and rejoins with the same Luma node name, `meta.luma_node_name` remains identical and pinned constraints keep working without hardcoding Docker hostnames.

### Single service with a local volume {#local-volume}

Stateful single services use named volumes or bind mounts. Control records the owning node before the first deploy and pins subsequent updates to that node; explicitly changing `node` proceeds with a `Storage placement` warning, and data on the old node is not copied automatically. Writable persistent mounts require `replicas: 1`.

```yaml
name: home-db
image: postgres:16
region: home
exposure: none
replicas: 1
volumes:
  - home-db-data:/var/lib/postgresql/data
```

For Compose sidecars, host paths live under `volumes.<name>.local.path`; see [Compose and storage](compose-storage.md).

### Worker with outbound proxy {#worker-proxy}

If a service needs outbound internet access through the Luma egress proxy at runtime, set `proxy: true`. Do not hand-write `networks: [egress]` or `HTTP_PROXY` / `HTTPS_PROXY` just to use the default proxy; Luma injects them automatically (explicitly specified env vars with the same names are preserved).

`proxy: true` controls only the container's own outbound HTTP/HTTPS traffic and is independent of inbound exposure. For example, `region: home` + `exposure: tailscale-relay` + `proxy: true` is valid: inbound user traffic arrives via edge Traefik -> Tailscale -> home task, while outbound container requests route through `egress_mihomo`.

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
```

Rendered automatically with:

```hcl
env {
  HTTP_PROXY  = "http://egress_mihomo:7890"
  HTTPS_PROXY = "http://egress_mihomo:7890"
}
constraint {
  attribute = "${meta.region}"
  value     = "cn"
}
```

### Resource limits on small hosts

When running workloads on a small Manager (for example 2 vCPU / 2 GiB RAM), set explicit resource boundaries for non-core services. CPU uses elastic `reservations.cpus` scheduling shares (idle CPU remains usable; no hard CPU cap is enforced). `limits.memory` is the hard memory ceiling and `reservations.memory` is the scheduling reservation. Omitting CPU reservation defaults to 100 MHz; setting only a memory limit reserves `min(256 MiB, limit)`. Legacy `limits.cpus` emits a warning and is ignored rather than reserving extra scheduling budget:

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
resources:
  limits:
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

These rules apply consistently to single-service manifests and Compose `deploy.resources`. Memory reservation cannot exceed the limit; when no limit is set, Nomad keeps using the reservation as the container ceiling. Memory oversubscription does not add physical RAM—simultaneous spikes across services can still trigger OOM kills.

Control upgrades affect newly generated jobs only; redeploy existing services to apply updated rendering rules without needing a fleet-wide worker restart.

### Internal home service

```yaml
name: backup-job
image: ghcr.io/acme/backup-job:1.0.0
region: home
exposure: none
replicas: 1
```

### Home service via Tailscale Relay {#tailscale-relay}

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

By default, Luma Control inspects which home nodes run the Nomad allocation after deploy and points the relay route upstream to those nodes' host ports. Add `node` explicitly only when the service must stay on a specific host:

```yaml
node: home-mac-mini
```

You can also override the complete upstream URL manually:

```yaml
relay:
  url: http://home-1.your-tailnet.ts.net:8080
```

### Public TCP service {#tcp}

Service manifest:

```yaml
name: ledger-db
image: mysql:8.4.9
region: home
node: lab
exposure: tcp-relay
domain: ledger-db.example.net
port: 3306
publishPort: 3306
replicas: 1
```

Luma points DNS to the public edge, ensures Traefik listens on the `tcp-3306` entrypoint, and writes the Traefik TCP route. `domain` is used for DNS; plain MySQL connections do not carry an HTTP Host or reliable initial SNI, so each published port should be assigned to only one TCP service at a time.
`publishPort` is the host port on the target task node mapped to container `port` under Nomad bridge mode. If another container or non-Luma process on that host already binds `3306`, pick another port and adjust client connection settings accordingly. Native Mac/OrbStack single services omit `publishPort` and use the actual listening port; Mac Compose supports an independent `publishPort`.

### Cloudflare Tunnel service {#cloudflare-tunnel}

```yaml
name: home-tool
image: ghcr.io/acme/home-tool:1.0.0
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

## Pre-deploy checklist

- Verify `domain` is the actual hostname users should reach.
- Verify `port` is the internal container listening port, not the public port.
- Verify `region` and `exposure` are compatible.
- Verify public services define both `domain` and `port`.
- Pin image tags or digests; `latest` / untagged images resolve to a digest at deploy time, but immutable tags or digests keep Nomad rollbacks predictable.
- Keep secrets out of plaintext YAML: use `${ENV_NAME}` and supply values with `--env .env` or `luma secret set --scope <app>`.
- Keep workers on `exposure: none` without a public domain.
- Avoid placing high-traffic core public services on home nodes.

## Validation commands

```bash
luma validate service.yaml
luma deploy service.yaml --dry-run
```

Single-service `luma validate` validates and renders locally, printing a text summary by default (`--format json` includes the rendered artifacts). `luma deploy --dry-run` prints the locally rendered Nomad job and any applicable route without submitting to Control or querying live cluster placement, storage, or image state. Real deployments still resolve live nodes, storage ownership, secrets, and image digests on Control, so local preview does not guarantee an identical final job or prove runtime readiness.
