# Luma CLI

Luma is the command line interface for installing nodes, wiring providers, generating Swarm stacks, deploying services, and keeping Portainer as the operations UI.

The default path is control-plane first and Portainer-backed:

```text
luma deploy service.yaml -> Luma Control API -> render stack on manager -> sync DNS -> Portainer API -> Docker Swarm
```

Portainer is required and installed by bootstrap. It shows stacks, services, logs, tasks, and node placement. Luma Control is the authentication and orchestration layer; it does not replace Portainer.

## Install

CI runners should install the published package instead of running the shell installer:

```bash
python -m pip install "luma-infra==0.1.21"
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
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.21 sh
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

This only removes the local CLI install. It does not remove Docker, Swarm, Portainer, Traefik, Luma Control, deployed services, or server-side `/opt/luma` state.

## CI Usage

CI can run Luma as a stateless control-plane client. It does not need SSH, Docker, Cloudflare, Portainer, or files under `~/.config/luma`.

PR validation:

```bash
python -m pip install "luma-infra==0.1.21"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_DEPLOY_TOKEN"

luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
```

Main or release deployment:

```bash
python -m pip install "luma-infra==0.1.21"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_DEPLOY_TOKEN"

luma status --format json
luma deploy deploy/app.yaml --format ndjson --timeout 1800
```

The control context priority is CLI flags, then environment variables, then the local login context. CI commonly uses:

- `LUMA_CONTROL_URL`
- `LUMA_DEPLOY_TOKEN`
- `LUMA_INSECURE=true|false`
- `LUMA_RESOLVE_IP`

`LUMA_RESOLVE_IP` keeps the control hostname in the `Host` header and requires insecure TLS mode.

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
  portainer:
    webhookUrlEnv: PORTAINER_WEBHOOK_URL

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/liutianjie
  stackRoot: stacks
  routesRoot: routes
  publicNetwork: public
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
```

Secrets stay outside Git. For normal use, run the command you actually need:

```bash
luma bootstrap manager --domain luma.example.com
```

If required local values are missing, Luma prompts for them before continuing and saves them to `~/.luma.config.json` with mode `0600`. On worker servers, the same happens during:

```bash
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

`luma configure --role manager|worker` remains available if you want to edit local secrets ahead of time, and `luma configure --show` lists configured keys without printing values. Luma loads `.env` and `~/.luma.config.json` automatically. Use `--env-file <path>` to load another project-local env file or `--no-env` to disable local secret loading. Values already exported in your shell take priority. On the manager node, bootstrap copies the required Cloudflare and Portainer values into `/opt/luma/control/control.json` so client machines do not need those secrets. If `CLOUDFLARE_API_TOKEN` is configured but `providers.dns` is missing, bootstrap infers the Cloudflare zone from the control domain and writes the provider config before installing `/opt/luma/luma.yaml`. If no edge DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET` and writes it as `providers.dns.edgeTarget`.

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

`luma status` prints DNS and Portainer readiness, registered Luma nodes from the control state, and actual Docker Swarm nodes from the manager Docker socket.

Store private registry credentials for image pulls:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
luma registry remove ghcr.io
```

The same control plane also serves a read-only Web status panel:

```text
https://<control-domain>/dashboard/
```

Paste the deploy token to view readiness, nodes, services, and inferred traffic paths from a trusted browser.

List nodes from local `luma.yaml` only:

```bash
luma node list
```

Bootstrap the manager by running this directly on the manager server:

```bash
luma bootstrap manager --domain luma.example.com
```

For `single-node`, it installs Docker, connects Tailscale when configured, initializes Swarm, configures networks and labels, deploys Traefik, deploys Portainer, deploys Luma Control, configures firewall rules, and sets up egress. Set `EGRESS_SUBSCRIPTION_URL` first, or use `--skip-egress` and repair egress later.

It streams progress:

```text
[start] Create overlay networks
[ok] Overlay networks ready
[start] Deploy Portainer
[fail] Deploy Portainer
  Fix: Run: luma portainer setup
```

Skip egress only when you want to repair it later:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Login from any client machine:

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
luma context use <cluster-id>
```

Join additional servers by running this on each server:

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

`--name` is the Luma node name used by `luma status` and by service manifests. Luma labels the Swarm node with both `luma.node.name` and `luma.node.id`, then uses the NodeID label for pinned scheduling.

Leave Swarm and optionally unregister the node from the control plane:

```bash
luma node exit --endpoint https://luma.example.com --token <deploy-or-join-token> --name home-mac-mini
```

Remove a node from any logged-in client:

```bash
luma node remove home-mac-mini
```

The control plane removes the Luma registration record and then removes the matching Docker Swarm worker node on the manager. Matching uses the saved Swarm NodeID, `luma.node.id`, `luma.node.name`, or the Swarm hostname. Luma refuses to remove a Swarm manager node through this command.

Connect Cloudflare and write `providers.dns.zoneId`:

```bash
luma cloudflare connect --zone example.com
```

Repair or refresh the outbound gateway:

```bash
luma egress setup
luma egress refresh
```

Repair Portainer:

```bash
luma portainer setup
```

Install/login Tailscale:

```bash
luma tailscale connect
```

Generate a service manifest interactively:

```bash
luma service new
```

Validate and render:

```bash
luma validate examples/public-cn-service.yaml
luma render examples/public-cn-service.yaml
```

Deploy through Portainer:

```bash
luma deploy examples/public-cn-service.yaml
```

Remove a deployed service:

```bash
luma service remove examples/public-cn-service.yaml
luma service remove examples/public-cn-service.yaml --dry-run
```

`depoly` is accepted as a compatibility alias:

```bash
luma depoly examples/public-cn-service.yaml
```

Run diagnostics:

```bash
luma doctor
luma doctor --legacy-ssh --deep  # optional legacy node checks
```

Legacy SSH bootstrap remains available for older setups:

```bash
luma node bootstrap manager-1 --profile single-node
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
- `exposure`: `cn-edge`, `tailscale-relay`, `cloudflare-tunnel`, `external-edge`, or `none`

Public services also require:

- `domain`
- `port`

Optional fields:

- `node`: Luma node name from `luma node join --name` for pinning the service to one node. The control plane resolves it to the Swarm NodeID and still adds the `region` constraint.
- `env` / `environment`
- `command`
- `constraints`
- `labels`
- `networks`
- `proxy`: when `true`, runtime traffic uses the egress proxy; Luma adds the egress network and default proxy env. Scheduling still follows `region`.
- `resources`: passed through to Swarm `deploy.resources`; supports `limits` and `reservations` for CPU and memory.
- `stackPath`
- `routePath`
- `dns.target`
- `dns.type`
- `dns.proxied`
- `publishPort`
- `relay.host`: optional tailscale-relay upstream override; usually omit it.
- `relay.url`: optional full tailscale-relay upstream URL override; usually omit it.
- `tunnel.tokenEnv`
- `portainer.webhookUrlEnv`
- `portainer.webhookUrl`

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
2. read the current login context from `~/.config/luma`;
3. submit the manifest to the manager's Luma Control API;
4. render `stacks/<region>/<service>/stack.yml` on the manager;
5. render `routes/<service>.yml` on the manager for `tailscale-relay`;
6. upsert Cloudflare DNS unless skipped;
7. create or update the service's Portainer stack through the Portainer API;
8. probe the public route for `cn-edge` and `external-edge` services.

The client prints local progress before submitting the request, while waiting for the control plane, and for each control-plane step. A public route probe reports the HTTP status from `/`; `404` means the route is reachable but the application may not serve a root page. The default deploy response timeout is 1800 seconds because first deploys may pull large images through the manager; use `--timeout <seconds>` to override it.

Deploy is an upsert. Re-running `luma deploy service.yaml` with the same service `name` updates the existing Portainer stack instead of creating a duplicate. The update uses the current rendered manifest as the source of truth; resources removed from the manifest can be pruned by Portainer.

`--dry-run` renders locally and does not contact the control API. `--skip-dns` and `--skip-webhook` are sent to the control API. `--commit` and `--push` are deprecated in control-plane deploy mode.

For `luma service remove service.yaml`, Luma submits the manifest to the control plane and removes the matching service slug. By default it deletes Luma-managed Cloudflare DNS, removes the Portainer stack, and deletes generated stack files. `tailscale-relay` route files are removed too. Use `--dry-run` to preview, `--skip-dns` to keep the DNS record, and `--skip-portainer` only when you intentionally want to remove generated Luma files without stopping the stack. `cloudflare-tunnel` public hostnames are still managed in Cloudflare Zero Trust, so Luma reports that cleanup as skipped.

Legacy Portainer webhooks are still supported for existing GitOps stacks. For more than one GitOps stack, use per-service webhook env vars:

```yaml
name: api
portainer:
  webhookUrlEnv: PORTAINER_WEBHOOK_API
```

Or centralize the mapping in `luma.yaml`:

```yaml
providers:
  portainer:
    webhooks:
      api: PORTAINER_WEBHOOK_API
      web: PORTAINER_WEBHOOK_WEB
```

When webhooks are configured, Luma resolves them in this order:

1. `service.portainer.webhookUrl`
2. `service.portainer.webhookUrlEnv`
3. `providers.portainer.webhooks.<service name or slug>`
4. global `providers.portainer.webhookUrlEnv`
