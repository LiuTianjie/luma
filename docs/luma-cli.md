# Luma CLI

Luma is the repository CLI for turning small service manifests into Docker Swarm stacks.

## Install for local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

If editable installation fails on an older system pip, upgrade pip inside the virtualenv first:

```bash
python -m pip install --upgrade pip
pip install -e .
```

After installation, the CLI is available as:

```bash
luma --help
```

## Platform config

`luma.yaml` stores provider-level defaults:

- `defaults.stackRoot`: where generated stack files are written.
- `defaults.routesRoot`: where Traefik file-provider routes are written for `tailscale-relay`.
- `defaults.publicNetwork`: external overlay network used by Traefik.
- `dns.provider`: currently supports `cloudflare`.
- `dns.edgeTarget`: the public ingress IP or CNAME target.
- `portainer.webhookUrlEnv`: environment variable containing the Portainer stack webhook.

Secrets stay in environment variables:

```bash
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ZONE_ID=...
export PORTAINER_WEBHOOK_URL=...
```

## Service manifest

```yaml
name: ai-gateway
image: ghcr.io/your-org/ai-gateway:latest
region: global
public: true
exposure: external-edge
domain: ai.example.com
port: 3000
replicas: 1
```

Required fields:

- `name`
- `image`
- `region`: `cn`, `global`, or `home`
- `public`
- `exposure`: `cn-edge`, `tailscale-relay`, `cloudflare-tunnel`, `external-edge`, or `none`

Public services also require:

- `domain`
- `port`

Optional fields:

- `env`: service environment mapping.
- `command`: Docker command.
- `constraints`: extra Swarm placement constraints.
- `labels`: extra Docker service labels.
- `networks`: extra external networks.
- `stackPath`: explicit output path.
- `dns.target`: per-service DNS target override.
- `dns.type`: per-service DNS record type override.
- `dns.proxied`: per-service Cloudflare proxy setting.
- `publishPort`: host port for `tailscale-relay`.
- `relay.host`: Tailscale hostname for `tailscale-relay`.
- `relay.url`: full upstream URL override for `tailscale-relay`.
- `tunnel.tokenEnv`: environment variable containing the Cloudflare Tunnel token.

## Exposure examples

Domestic public service:

```bash
luma deploy examples/public-cn-service.yaml --commit --push
```

Home service through the CN edge and Tailscale:

```bash
luma deploy examples/home-tailscale-relay.yaml --commit --push
```

Cloudflare Tunnel service:

```bash
luma deploy examples/cloudflare-tunnel-service.yaml --commit --push
```

Internal/global worker:

```bash
luma deploy examples/global-worker.yaml --skip-dns --skip-webhook
```

## Commands

Render a stack without writing it:

```bash
luma render examples/public-cn-service.yaml
```

Validate the service manifest and show the rendered stack:

```bash
luma validate examples/public-cn-service.yaml
```

Generate stack, sync DNS, optionally commit, and trigger Portainer:

```bash
luma deploy examples/public-cn-service.yaml --commit --push
```

Local dry run:

```bash
luma deploy examples/public-cn-service.yaml --dry-run
```

Skip external side effects while generating stack files:

```bash
luma deploy examples/public-cn-service.yaml --skip-dns --skip-webhook
```

Sync DNS only:

```bash
luma dns-sync examples/public-cn-service.yaml
```

If `depoly` is typed by mistake, Luma returns a correction hint.

## Git and Portainer order

When Portainer deploys from a remote Git repository, use:

```bash
luma deploy service.yaml --commit --push
```

The order is:

1. write the generated stack file;
2. validate the generated stack;
3. sync DNS if enabled;
4. commit and push Git changes if requested;
5. trigger the Portainer webhook if configured.

For `tailscale-relay`, Luma also writes `routes/<service>.yml`. The CN Traefik node must have that route directory available at `/opt/luma/routes`.
