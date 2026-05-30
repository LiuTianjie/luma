# Luma CLI

Luma is the command line interface for installing nodes, wiring providers, generating Swarm stacks, deploying services, and keeping Portainer as the operations UI.

The default path is control-plane first and Portainer-backed:

```text
luma deploy service.yaml -> Luma Control API -> render stack on manager -> sync DNS -> Portainer API -> Docker Swarm
```

Portainer is required and installed by bootstrap. It shows stacks, services, logs, tasks, and node placement. Luma Control is the authentication and orchestration layer; it does not replace Portainer.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
luma preflight
```

The installer uses a GitHub archive, not `git clone`. It installs into `~/.local/share/luma/venv` and writes `~/.local/bin/luma`.

Install a pinned release:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.0 sh
```

Development checkout:

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

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

Secrets stay outside Git. Put them in `.env`:

```bash
cp .env.example .env
$EDITOR .env
```

```dotenv
CLOUDFLARE_API_TOKEN=...
PORTAINER_WEBHOOK_URL=...
PORTAINER_WEBHOOK_API=...
EGRESS_SUBSCRIPTION_URL=...
LUMA_SUDO_PASSWORD=...
TAILSCALE_AUTHKEY=...
```

Luma loads `.env` automatically. Use `--env-file <path>` to load another file or `--no-env` to disable this behavior. On the manager node, bootstrap copies the required Cloudflare and Portainer values into `/opt/luma/control/control.json` so client machines do not need those secrets.

## Commands

Initialize a config:

```bash
luma init
```

Check local requirements and `.env`:

```bash
luma preflight
```

List configured nodes:

```bash
luma node list
```

Bootstrap the manager by running this directly on the manager server:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
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
luma bootstrap manager --domain luma.example.com --profile single-node --skip-egress
```

Login from any client machine:

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
luma context use <cluster-id>
```

Join additional servers by running this on each server:

```bash
luma node join https://luma.example.com --token <join-token> --profile global-worker --region global
```

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
public: true
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

- `env` / `environment`
- `command`
- `constraints`
- `labels`
- `networks`
- `stackPath`
- `routePath`
- `dns.target`
- `dns.type`
- `dns.proxied`
- `publishPort`
- `relay.host`
- `relay.url`
- `tunnel.tokenEnv`
- `portainer.webhookUrlEnv`
- `portainer.webhookUrl`

## Deploy Order

For `luma deploy service.yaml`, Luma does:

1. parse and validate the service manifest;
2. read the current login context from `~/.config/luma`;
3. submit the manifest to the manager's Luma Control API;
4. render `stacks/<region>/<service>/stack.yml` on the manager;
5. render `routes/<service>.yml` on the manager for `tailscale-relay`;
6. upsert Cloudflare DNS unless skipped;
7. create or update the service's Portainer stack through the Portainer API.

`--dry-run` renders locally and does not contact the control API. `--skip-dns` and `--skip-webhook` are sent to the control API. `--commit` and `--push` are deprecated in control-plane deploy mode.

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
