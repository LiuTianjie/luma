# Luma CLI

Luma is the command line interface for installing nodes, wiring providers, rendering Nomad jobs, and deploying services. The orchestrator underneath is HashiCorp Nomad, so the unit of deployment is a Nomad job.

The default path is control-plane first and Nomad-backed:

```text
luma deploy service.yaml -> Luma Control API -> render jobspec on manager -> sync DNS -> Nomad API (/v1/jobs) -> docker driver
```

Luma Control is the authentication and orchestration layer. It renders the manifest into a Nomad jobspec and submits it directly to the Nomad HTTP API. Inspect deployments with `luma status`, the dashboard, or `nomad job status` on the manager.

## Install

CI runners should install the published package instead of running the shell installer:

```bash
python -m pip install "luma-infra==0.1.242"
```

The package distribution name is `luma-infra`, but the installed command is still `luma`.

For interactive machines, use the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

The installer uses a GitHub archive, not `git clone`. It installs into `~/.local/share/luma/venv`, writes `~/.local/bin/luma`, and adds `~/.local/bin` to your shell profile when needed. Use `~/.local/bin/luma` immediately, or open a new shell / run `exec $SHELL -l` before using the shorter `luma` command.

Install a pinned release:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.242 sh
```

Development checkout:

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

Uninstall the local CLI:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

By default uninstall keeps `~/.luma.config.json` and `~/.config/luma` so a reinstall can keep local prompts and login contexts. To remove those local files too:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

This only removes the local CLI install. It does not remove Docker, Nomad, Traefik, Luma Control, deployed services, or server-side `/opt/luma` state.

## CI Usage

CI can run Luma as a stateless control-plane client. It does not need SSH, Docker, Cloudflare, Nomad, or files under `~/.config/luma`.

PR validation:

```bash
python -m pip install "luma-infra==0.1.242"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
```

Main or release deployment:

```bash
python -m pip install "luma-infra==0.1.242"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma status --format json
luma deploy deploy/app.yaml --format ndjson --timeout 1800
```

The control context priority is CLI flags, then environment variables, then the local login context. CI commonly uses:

- `LUMA_CONTROL_URL`
- `LUMA_DEPLOY_TOKEN`
- `LUMA_INSECURE=true|false`
- `LUMA_RESOLVE_IP`

`LUMA_RESOLVE_IP` keeps the control hostname in the `Host` header and requires insecure TLS mode.

## Git Provider Credentials

Repository import can use saved GitHub/Gitea credentials instead of a one-off repo URL token. Tokens are write-only and are injected only into the builder's leased clone task.

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin

printf '%s' "$GITEA_TOKEN" | luma git-provider set gitea lin \
  --base-url https://gcode.example.com \
  --username lin \
  --token-stdin
```

List accounts and discover repositories:

```bash
luma git-provider list
luma git-provider repos gitea:lin
luma git-provider refs gitea:lin acme/app
```

Use the selected provider account with import:

```bash
luma import --provider-id gitea:lin --repository acme/app --build-node builder --env .env
```

For public GitHub repositories, the positional source can also be `owner/repo`; Luma expands it to `https://github.com/owner/repo.git`. Use a full URL or `--provider-id ... --repository ...` for Gitea/self-hosted Git.

## Configuration

`luma.yaml` is the single project config source:

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    edgeTarget: 203.0.113.10

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - nomad-server
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/liutianjie
  stackRoot: stacks
  routesRoot: routes
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
  engine: nomad
```

Secrets stay outside Git. For normal use, run the command you actually need:

```bash
luma bootstrap manager --domain luma.example.com
```

If required local values are missing, Luma prompts for them before continuing and saves them to `~/.luma.config.json` with mode `0600`. On worker servers, the same happens during:

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

`luma configure --role manager|worker` remains available if you want to edit local secrets ahead of time, and `luma configure --show` lists configured keys without printing values. Luma loads `.env` and `~/.luma.config.json` automatically. Use `--env-file <path>` to load another project-local env file or `--no-env` to disable local secret loading. Values already exported in your shell take priority. On the manager node, bootstrap and update copy the required Cloudflare values into `/opt/luma/control/control.json` so client machines do not need those secrets. If `CLOUDFLARE_API_TOKEN` is configured but `providers.dns` is missing, bootstrap and `luma update manager` infer the Cloudflare zone from the control domain and write the provider config before installing `/opt/luma/luma.yaml`. If no edge DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET`; non-interactive update uses the configured edge node public IP or an existing `LUMA_DNS_EDGE_TARGET`.

## Commands

Initialize a config:

```bash
luma init
```

Check local requirements and `.env`:

```bash
luma preflight
```

Show control-plane and cluster state from any logged-in client:

```bash
luma status
```

`luma status` prints DNS readiness, the orchestrator (Nomad) with its server leader, and registered Luma nodes from the control state with `role=client`.

Store private registry credentials for image pulls:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
luma registry remove ghcr.io
```

Deploy an in-cluster registry for source-to-image builds:

```bash
luma registry serve --node build-1
```

`luma registry serve` deploys a `registry:2` service on a `docker-build`-capable node and wires `insecure-registries` into every non-manager ready Linux node so they can pull built images over the Tailscale network. The builder pushes via `localhost:5000`; other nodes pull via `<build-node-tailscale-host>:5000`. Optional flags: `--port` (default `5000`), `--storage-class` (data volume storageClass, default `local`), `--image` (default `registry:2`), `--name` (default `luma-registry`), `--timeout` (default `1800`). See the repository-import walkthrough in [how-to-use-luma.md](how-to-use-luma.md) for the full source-to-image flow.

The same control plane also serves a read-only Web status panel:

```text
https://<control-domain>/dashboard/
```

Paste the management token to view readiness, nodes, services, and inferred traffic paths from a trusted browser.

Luma has two user-facing tokens:

- **Management token**: for trusted CLI clients and the dashboard. Use it with `luma login`, dashboard login, deployments, storage, secrets, registries, and node operations.
- **Node join token**: for servers that are joining the cluster or refreshing their local node agent. Use it with `luma node join` and, for older nodes without saved agent metadata, `luma update --control-url ... --token ...`.

The per-node agent credential is internal and is installed automatically on the node. Users should check agent status with `luma node status`, not copy or manage agent credentials.

List nodes from local `luma.yaml` only:

```bash
luma node list
```

Bootstrap the manager by running this directly on the manager server:

```bash
luma bootstrap manager --domain luma.example.com
```

For `single-node`, it installs Docker, connects Tailscale when configured, installs and starts the Nomad server, applies node `meta`, deploys Traefik and Luma Control as Nomad jobs, configures firewall rules, and sets up egress. Set `EGRESS_SUBSCRIPTION_URL` first when the manager needs a proxy to pull the configured control image. Mainland managers using the default GHCR control image should not use `--skip-egress`.

It streams progress:

```text
[start] Install Nomad server
[ok] Nomad server ready
[start] Deploy Luma Control
[fail] Deploy Luma Control
  Fix: Re-run luma bootstrap manager after fixing the error
```

Skip egress only when the control image registry is directly reachable, or when `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` points at a registry the manager can pull:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Login from any client machine:

```bash
luma login https://luma.example.com --token <management-token>
luma context list
luma context use <cluster-id>
```

Join additional servers by running this on each server:

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

`--name` is the Luma node name used by `luma status` and by service manifests. Luma writes it to the Nomad client `meta.luma_node_name` and uses it for pinned scheduling; the Nomad node identity is a stable UUID, so a rejoin under the same name keeps pinned services valid. Add `--engine nomad` to force the Nomad client agent path explicitly; it is the default.

Refresh a joined node agent after upgrading an older node:

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

Update every registered node that has a ready node agent:

```bash
luma update fleet
luma update fleet --install-ref v0.1.242 --timeout 900
luma update fleet --include-manager
```

`--install-ref` accepts a release tag, branch, or full 40-character Git commit.
Use a full commit for a coordinated candidate rollout so manager and node agents
cannot resolve different revisions while a branch moves.

Fleet update runs through the node agents. It updates the CLI on each ready non-manager node and then refreshes the local node-agent service and Tailscale watchdog. The Nomad server (manager) node is skipped by default; update the manager separately with `luma update manager` from the manager host. `--include-manager` is available for explicit repair workflows, but normal fleet updates should leave the active control plane alone. Nodes whose agent is too old to advertise `luma-update` are reported as skipped; run `luma update` once on those nodes, then they can participate in later fleet updates.

Drain the local Nomad client and optionally unregister the node from the control plane:

```bash
luma node exit --endpoint https://luma.example.com --token <management-or-node-join-token> --name home-mac-mini
```

Remove a node from any logged-in client:

```bash
luma node remove home-mac-mini
```

The control plane removes the Luma registration record and then drains the matching Nomad client on the manager. Matching uses the saved Nomad node ID, `meta.luma_node_name`, or the node name. Luma refuses to remove a Nomad server (manager) node through this command.

Connect Cloudflare and write `providers.dns.zoneId`:

```bash
luma cloudflare connect --zone example.com
```

Repair or refresh the outbound gateway:

```bash
luma egress setup
luma egress refresh
```

Install/login Tailscale:

```bash
luma tailscale connect
```

Managers and joined nodes install a lightweight Tailscale watchdog during bootstrap/update. The manager watchdog verifies Tailscale peers plus Nomad gossip/RPC TCP reachability; node watchdogs verify manager Tailscale plus the Nomad server ports. Consecutive failures restart local Tailscale. This is intended to recover tailnet TCP stalls without restarting Docker, Traefik, the Nomad agent, or application jobs.

Generate a service manifest interactively:

```bash
luma service new
```

Validate and render:

```bash
luma validate examples/public-cn-service.yaml
luma render examples/public-cn-service.yaml
luma render examples/public-cn-service.yaml --engine nomad
```

`luma render` renders locally. `--engine nomad` forces the Nomad jobspec renderer; this is also the default on current clusters.

Deploy through the control plane:

```bash
luma deploy examples/public-cn-service.yaml
```

Roll back or inspect version history of a deployed service:

```bash
luma history public-cn-service
luma rollback public-cn-service
luma rollback public-cn-service --to-version 3
```

`luma history` lists prior versions of the Nomad job (`GET /v1/job/<id>/versions`). `luma rollback` reverts to the previous version, or to the version given by `--to-version N` (`POST /v1/job/<id>/revert`). The web dashboard exposes the same operation from Applications -> Versions. Jobspecs also render `update { auto_revert = true }`, so a new version that fails its health checks rolls back automatically.

Rollback changes the running Nomad job only. It does not rewrite Git, update the stored manifest in Luma Control, roll back databases, or restore volumes. For predictable production rollback, deploy immutable image tags or digests rather than `latest`.

Restart a running service without redeploying:

```bash
luma service restart public-cn-service
luma service restart my-stack --service web --mode task
```

`--mode recreate` reschedules the allocation; `--mode task` restarts the task in place. Omitting `--mode` uses `recreate` for a whole stack and `task` when `--service` targets one task. After the runtime action, Control reconciles the saved deployment's routes and DNS and probes its public HTTP services; Compose reconciles every exposed service. Restart refuses the system stacks `traefik`, `egress`, and `luma-control`. See [operations.md](operations.md) for details.

Remove a deployed service:

```bash
luma service remove public-cn-service
luma service remove public-cn-service --dry-run
```

Run diagnostics:

```bash
luma doctor
```

## Service Manifest

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

Required fields:

- `name`
- `image`
- `region`: `cn`, `global`, or `home`
- `exposure`: `cn-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, `external-edge`, or `none`

Public services also require:

- `domain`
- `port`

Optional fields:

- `engine`: `nomad`. Selects the orchestration backend for this service. Omit it to inherit the cluster default.
- `node`: Luma node name from `luma node join --name` for pinning the service to one node. The control plane renders it as a Nomad constraint on `${node.unique.name}` (or `meta.luma_node_name`) and still adds the `region` constraint.
- `env` / `environment`
- `command`
- `constraints`
- `labels`
- `networks`
- `proxy`: when `true`, runtime traffic uses the egress proxy; Luma attaches the egress proxy and default proxy env. Scheduling still follows `region`.
- `resources`: rendered into the Nomad task's `resources` block; supports `limits` and `reservations` for CPU and memory. Luma converts `cpus` to Nomad CPU MHz and the memory suffix string to Nomad memory MB.
- `stackPath`
- `routePath`
- `dns.target`
- `dns.type`
- `dns.proxied`
- `publishPort`
- `relay.host`: optional tailscale-relay upstream override; usually omit it.
- `relay.url`: optional full tailscale-relay upstream URL override; usually omit it.
- `tcp-relay` uses `publishPort` or `port` to derive the Traefik TCP entrypoint automatically.
- `tunnel.tokenEnv`

Example worker that needs the Luma egress proxy:

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
```

Example bounded service for a small manager:

```yaml
name: api
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

## Deploy Order

For `luma deploy service.yaml`, Luma does:

1. parse and validate the service manifest;
2. if `--env <file>` is provided, parse that `.env` locally and keep only variables referenced as `${NAME}` by the manifest;
3. read the current login context from `~/.config/luma`;
4. submit the manifest and filtered scoped env secrets to the manager's Luma Control API;
5. store incoming env secrets under the service `name` scope and resolve `${NAME}` before rendering;
6. render `stacks/<region>/<service>/<service>.nomad.json` (the jobspec) on the manager;
7. render `routes/<service>.yml` on the manager for `tailscale-relay` or `tcp-relay`;
8. upsert Cloudflare DNS unless skipped;
9. submit the job to Nomad through `PUT /v1/jobs` (create or update);
10. probe the public route for `cn-edge` and `external-edge` services.

The client prints local progress before submitting the request, while waiting for the control plane, and for each control-plane step. Luma validates generated Traefik file-provider routes, stages them outside the watched routes directory, then atomically publishes the final route file. A public route probe reports the HTTP status from `/`; an application-level `404` means the route is reachable but the application may not serve a root page, while Traefik's default `404 page not found` is treated as a missing router and a failed public route. When the probe reports the route unhealthy (Traefik router not found, or a transient `502`/`503`/`504`), Control recreates the service's allocation once and re-probes before failing the deploy. The default deploy response timeout is 1800 seconds because first deploys may pull large images on the target node; use `--timeout <seconds>` to override it.

Deploy is an upsert. Re-running `luma deploy service.yaml` with the same service `name` updates the existing Nomad job (the job id is the service slug) instead of creating a duplicate. The update uses the current rendered jobspec as the source of truth, and Nomad keeps the previous version so `luma rollback` or the dashboard's Applications -> Versions action can return to it.

Use `luma deploy service.yaml --env .env` when the project already has a deployment env file. Scoped env secrets are isolated by service name, so `api/DATABASE_URL` and `worker/DATABASE_URL` are distinct values. Legacy global `luma secret set NAME` values are still used only for applications that have no scoped secrets.

For source-to-image deploys, `luma import` uses the same scoped secret model:

```bash
luma import https://github.com/acme/app --build-node builder --env .env
luma import acme/app --build-node builder --env .env   # GitHub owner/repo shortcut
```

or, with a saved Git provider account:

```bash
luma import --provider-id gitea:lin --repository acme/app --ref main --build-node builder --env .env
```

Import auto-discovers single-service Luma manifests (`.luma.yml`, `luma.yml`, nested `*.luma.yml`) and Compose sidecars (`luma.compose.yml`, `.luma.compose.yml`, `*.luma.compose.yml`, `*.compose.luma.yml`, `docker-compose.luma.yml`). Compose sidecar filenames are excluded from single-service manifest matching so `docker-compose.luma.yml` is treated as Compose, not as a service manifest.

If the repository does not contain a deployment file yet, provide the deployment file from the CLI:

```bash
luma import --provider-id github:personal --repository acme/app \
  --build-node builder \
  --manifest deploy/app.luma.yml \
  --env .env
```

Unlike plain `luma deploy`, import may discover the final deployment manifest on the builder after cloning the repository. Therefore the CLI sends the `.env` values to Luma Control, and the control plane keeps only values referenced by the resolved manifest or Compose content under the final service/stack scope.

For Compose repositories, `luma import` builds services that still have `build:` and injects the resulting `image:` before deployment. Plain `luma compose validate` and `luma compose deploy` do not build, so use import-mode validation when checking that path locally:

```bash
luma compose validate --import-mode luma.compose.yml
```

When a repository contains more than one deployment sidecar, select the exact
Compose sidecar inside the cloned repository instead of relying on discovery:

```bash
luma import https://github.com/acme/platform.git \
  --ref v1.4.0 \
  --build-node builder \
  --compose-sidecar deploy/staging.luma.compose.yml \
  --env .env
```

`--compose-sidecar` accepts only a canonical POSIX repository-relative path and
cannot be combined with `--manifest`. The CLI requires a Control capability
before starting the build; Control requires the Builder to echo the same path;
the Builder rejects absolute paths, `..`, missing/invalid YAML, and symlink
escapes. An explicit selection never falls back to an auto-discovered sidecar.
Update the manager and Builder agent first when either side lacks this
capability.

`--dry-run` renders locally and does not submit a deployment. When local rendering cannot read optional cluster context such as node or storage metadata, JSON output includes `validationMode: "degraded"` plus warnings; text output prints `[warn]` lines. `--skip-dns` and `--skip-orchestrator` are sent to the control API. `--commit` and `--push` are deprecated in control-plane deploy mode.

Luma records deployment state before running external operations. A successful deploy is marked `active`; if DNS, the Nomad submission, route rendering, or probing fails after earlier steps have changed the manager, the recorded deployment is kept with `status: failed_partial` so the dashboard and `luma service remove <name>` can still find the partially applied job.

For `luma service remove <name>`, Luma looks up the manifest recorded by the control plane during the last successful deploy and removes the matching single-service or Compose deployment slug. This recorded manifest is the source of truth, so remove and storage cleanup also work for deployments created from the web UI when the client running the command has no YAML file. By default Luma deletes Luma-managed Cloudflare DNS, deregisters and purges the Nomad job, and deletes generated jobspec files such as `stacks/<region>/<service>/<service>.nomad.json` or `stacks/compose/<name>/<name>.nomad.json`. `tailscale-relay` and `tcp-relay` route files are removed too. Use `--dry-run` to preview, `--skip-dns` to keep the DNS record, and `--skip-orchestrator` only when you intentionally want to remove generated Luma files without stopping the Nomad job. Storage data is preserved by default; add `--delete-storage` to delete removable storage declared by the recorded deployment. For single-service deployments this removes managed storage paths referenced by `storage.<volume>.path` and removes named Docker volume objects such as `data:/data`; bind mounts are skipped. For Compose deployments this removes managed storage paths referenced by the sidecar. `--delete-storage` cannot be combined with `--skip-orchestrator`. `cloudflare-tunnel` public hostnames are still managed in Cloudflare Zero Trust, so Luma reports that cleanup as skipped.
