# Luma CLI

Luma is the command line interface for installing nodes, wiring providers, generating Swarm stacks, and triggering Portainer deployments.

The default path is Portainer-first:

```text
luma deploy service.yaml -> render stack -> sync DNS -> commit/push -> trigger Portainer webhook
```

`--direct` exists for bootstrap and emergency recovery only.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
luma --help
```

## Configuration

`luma.yaml` is the single project config source:

```yaml
project: itool

providers:
  dns:
    type: cloudflare
    zone: itool.tech
    zoneId: ac7105f330b0107c778ea8769bdfdc00
    apiTokenEnv: CLOUDFLARE_API_TOKEN
  portainer:
    webhookUrlEnv: PORTAINER_WEBHOOK_URL

nodes:
  aly:
    host: aly
    publicIp: 8.130.148.30
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/turning4th
  stackRoot: stacks
  routesRoot: routes
  publicNetwork: public
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
```

Secrets stay outside Git:

```bash
export CLOUDFLARE_API_TOKEN='...'
export PORTAINER_WEBHOOK_URL='...'
export EGRESS_SUBSCRIPTION_URL='...'
export LUMA_SUDO_PASSWORD='...'
```

## Commands

Initialize a config:

```bash
luma init
```

List configured nodes:

```bash
luma node list
```

Bootstrap a node:

```bash
luma node bootstrap aly --profile single-node
```

Connect Cloudflare and write `providers.dns.zoneId`:

```bash
luma cloudflare connect --zone itool.tech
```

Install or refresh the outbound gateway:

```bash
luma egress setup aly
luma egress refresh aly
```

Repair Portainer:

```bash
luma portainer setup aly
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
luma deploy examples/public-cn-service.yaml --commit --push
```

Emergency direct deploy:

```bash
luma deploy examples/public-cn-service.yaml --direct --node aly
```

Run diagnostics:

```bash
luma doctor
luma doctor --deep
```

## Service Manifest

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
public: true
exposure: cn-edge
domain: app.itool.tech
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

## Deploy Order

For `luma deploy service.yaml --commit --push`, Luma does:

1. parse and validate the service manifest;
2. render `stacks/<region>/<service>/stack.yml`;
3. render `routes/<service>.yml` for `tailscale-relay`;
4. run local stack validation when Docker is available;
5. upsert Cloudflare DNS unless skipped;
6. commit and push generated changes when requested;
7. trigger the Portainer webhook.

If `PORTAINER_WEBHOOK_URL` is missing, default deploy fails with an explicit fix. Use `--direct` only when Portainer is unavailable.
