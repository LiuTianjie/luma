# Secrets

Luma stores project topology in `luma.yaml`, but secrets stay outside Git.

By default, the CLI loads `.env` from the current working directory and `~/.luma.config.json` from the current user. Use `--env-file <path>` for another project-local file or `--no-env` to disable local secret loading.

For normal use, run the target command directly. If local values are missing, Luma prompts for them before continuing:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

This writes `~/.luma.config.json` with mode `0600`. `luma configure --role manager|worker` remains available for pre-filling values, and `luma configure --show` masks secret values.

`.env` remains useful for development or one-off overrides:

```bash
cp .env.example .env
$EDITOR .env
```

## Environment Variables

```dotenv
CLOUDFLARE_API_TOKEN     Cloudflare DNS API token
LUMA_DNS_EDGE_TARGET     public IP or DNS target for control and edge records
PORTAINER_WEBHOOK_URL    optional legacy Portainer stack webhook
PORTAINER_WEBHOOK_*      optional legacy per-service Portainer GitOps webhooks
LUMA_PORTAINER_ADMIN_PASSWORD  optional recovery override for an already-initialized Portainer admin
EGRESS_SUBSCRIPTION_URL  proxy subscription URL
LUMA_SUDO_PASSWORD       optional sudo password for local or legacy remote bootstrap
TAILSCALE_AUTHKEY        optional auth key for unattended Tailscale login
LUMA_CONTROL_IMAGE       optional published control API image for bootstrap
TRAEFIK_ACME_EMAIL       optional Let's Encrypt account email
```

## Runtime Secret Files

Control-plane state is written on the manager:

```text
/opt/luma/control/control.json
```

It contains the cluster id, deploy token, join token, Swarm worker join token, and copied Cloudflare/Portainer environment values needed by the control API. This file must not be committed or copied to client machines.

Luma Control also mounts `/var/run/docker.sock` so it can apply node labels after workers join. Treat deploy and join tokens as cluster-admin sensitive.

Egress configuration is written on the node:

```text
/opt/luma/egress-gateway/config.yaml
```

This file must not be committed.

`.env` and `.env.*` are ignored by Git. `.env.example` is committed as the safe template.

Client login state is written per user:

```text
~/.luma.config.json
~/.config/luma/contexts/<cluster>.json
~/.config/luma/current-context
```

`~/.luma.config.json` contains local setup secrets such as Cloudflare, Tailscale, egress, and sudo values. The context contains only the control endpoint, cluster id, and deploy token. Both should be treated as secrets.

## Rotation

Rotate any token or subscription URL that appears in:

- chat history;
- shell history;
- logs;
- screenshots;
- Git commits.
