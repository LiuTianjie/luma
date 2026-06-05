# Secrets

Luma stores project topology in `luma.yaml`, but secrets stay outside Git.

By default, the CLI loads `.env` from the current working directory and `~/.luma.config.json` from the current user. Use `--env-file <path>` for another project-local file or `--no-env` to disable local secret loading.

For normal use, run the target command directly. If local values are missing, Luma prompts for them before continuing:

```bash
luma bootstrap manager --domain luma.example.com
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

This writes `~/.luma.config.json` with mode `0600`. `luma configure --role manager|worker` remains available for pre-filling values, and `luma configure --show` masks secret values.

`.env` remains useful for development or one-off overrides:

```bash
cp .env.example .env
$EDITOR .env
```

## Environment Variables

| Variable | When needed | Purpose |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | Required on the manager | Cloudflare DNS API token with Zone Read + DNS Edit. Used to create/update control and service DNS records. |
| `LUMA_DNS_EDGE_TARGET` | Usually needed on the manager | Public IP or DNS name that Cloudflare records should point to when no edge target is already configured in `luma.yaml`. |
| `TRAEFIK_ACME_EMAIL` | Required on the manager | Let's Encrypt account email used by Traefik for HTTPS certificates and expiration notices. |
| `EGRESS_SUBSCRIPTION_URL` | Required only when using egress | Proxy subscription URL used to generate the Mihomo config for image-pull proxying and services with `proxy: true`. |
| `TAILSCALE_AUTHKEY` | Needed for private/home/tailscale-relay nodes | Auth key for unattended Tailscale login. |
| `LUMA_SUDO_PASSWORD` | Only when sudo requires a password | Local fallback password for privileged setup commands. Prefer passwordless sudo when possible. |
| `LUMA_PORTAINER_ADMIN_PASSWORD` | Recovery only | Optional override when binding to an already-initialized Portainer admin account. |
| `LUMA_CONTROL_IMAGE` | Development/pinned release only | Control API image used during manager bootstrap. |

## Runtime Secret Files

Control-plane state is written on the manager:

```text
/opt/luma/control/control.json
```

It contains the cluster id, management token, node join token, Docker Swarm worker join token, and copied Cloudflare/Portainer environment values needed by the control API. This file must not be committed or copied to client machines.

Luma Control also mounts `/var/run/docker.sock` so it can apply node labels after workers join. Treat management and node join tokens as cluster-admin sensitive.

## Deployment Secrets

Service manifests can reference control-plane deployment secrets with `${NAME}` in fields such as `env`.

```bash
luma login https://luma.example.com --token <management-token>
luma secret set API_TOKEN
luma secret list
```

`luma secret list` prints only names, not values. During deploy, Luma Control resolves the referenced values on the manager before sending the stack to Portainer. If a manifest references a missing secret, deploy fails before Portainer is updated:

```yaml
env:
  API_TOKEN: ${API_TOKEN}
```

Egress configuration is written on the node:

```text
/opt/luma/egress-gateway/config.yaml
```

This file must not be committed.

## Registry Credentials

Private image registry credentials are stored separately from deployment secrets:

```bash
luma login https://luma.example.com --token <management-token>
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
```

Luma uses these credentials only for image pulls and Portainer/Swarm registry association during deploy. They are not rendered into stack YAML and are not passed to service containers as environment variables. `luma registry list` returns only the registry host and username.

`luma registry remove <host>` removes the credential from Luma Control and attempts to delete only the matching Luma-managed Portainer registry entry. It does not revoke provider-issued tokens and cannot remove auth snapshots already attached to existing Swarm services; rotate or revoke the token at the registry provider when access must be invalidated.

`.env` and `.env.*` are ignored by Git. `.env.example` is committed as the safe template.

Client login state is written per user:

```text
~/.luma.config.json
~/.config/luma/contexts/<cluster>.json
~/.config/luma/current-context
```

`~/.luma.config.json` contains local setup secrets such as Cloudflare, Tailscale, egress, and sudo values. The context contains only the control endpoint, cluster id, and management token. Both should be treated as secrets.

The Web status panel at `https://<control-domain>/dashboard/` also uses the management token. The browser stores it in local storage for that control domain, so use the panel only on trusted devices and clear browser storage or rotate the management token if the device is no longer trusted.

## Rotation

Rotate any token or subscription URL that appears in:

- chat history;
- shell history;
- logs;
- screenshots;
- Git commits.
