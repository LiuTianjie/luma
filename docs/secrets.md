# Secrets

Luma stores project topology in `luma.yaml`, but secrets stay outside Git.

## Environment Variables

```bash
CLOUDFLARE_API_TOKEN     Cloudflare DNS API token
PORTAINER_WEBHOOK_URL    Portainer stack webhook
EGRESS_SUBSCRIPTION_URL  proxy subscription URL
LUMA_SUDO_PASSWORD       optional remote sudo password for bootstrap
TAILSCALE_AUTHKEY        optional auth key for unattended Tailscale login
```

## Runtime Secret Files

Egress configuration is written on the node:

```text
/opt/luma/egress-gateway/config.yaml
```

This file must not be committed.

## Rotation

Rotate any token or subscription URL that appears in:

- chat history;
- shell history;
- logs;
- screenshots;
- Git commits.
