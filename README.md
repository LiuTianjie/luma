# Luma

Luma is a Portainer-first, self-hosted deployment control plane for Docker Swarm.

It turns scattered servers into named deployment regions, then lets you deploy a service from a small YAML manifest.

## Core Concepts

Luma keeps the user-facing model small:

```text
node / region / exposure / egress / service
```

The runtime stack is:

```text
Luma CLI        install, configure, generate, diagnose
Portainer       default deployment control plane and UI
Docker Swarm    runtime and scheduler
Traefik         public HTTP/HTTPS ingress
Cloudflare      DNS and optional Tunnel
Egress Gateway  outbound proxy for image pulls and selected services
```

## 5 Minute Quickstart

Install the CLI:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

Create or edit `luma.yaml`:

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
```

Bootstrap the first node:

```bash
export LUMA_SUDO_PASSWORD='...'
luma node bootstrap aly --profile single-node
```

Portainer is part of the default control plane. If it needs repair:

```bash
luma portainer setup aly
```

Connect Cloudflare:

```bash
export CLOUDFLARE_API_TOKEN='...'
luma cloudflare connect --zone itool.tech
```

Set up outbound proxy:

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup aly
```

Deploy a service:

```bash
luma deploy examples/public-cn-service.yaml --commit --push
```

Run diagnostics:

```bash
luma doctor
luma doctor --deep
```

## Daily Workflow

Create a service manifest:

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

Deploy through Portainer:

```bash
luma deploy app.yaml --commit --push
```

Emergency direct deploy:

```bash
luma deploy app.yaml --direct --node aly
```

## Docs

- `docs/concepts.md`: node / region / exposure / egress / service.
- `docs/profiles.md`: built-in bootstrap profiles.
- `docs/secrets.md`: environment variables and secret handling.
- `docs/troubleshooting.md`: common failures and fixes.
- `docs/exposure-model.md`: traffic routing modes.
- `docs/egress-gateway.md`: outbound proxy gateway.

## Safety

Do not commit API tokens, Portainer webhooks, or proxy subscription URLs.

If a token or subscription URL has been pasted into a chat or log, rotate it before publishing the repository.
