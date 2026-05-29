# Bootstrap

Bootstrap is automated by the Luma CLI. The first supported target is Ubuntu 22.04+.

## 1. Prepare SSH

Add the server to `luma.yaml`:

```yaml
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

Make sure local SSH works:

```bash
ssh aly 'hostname'
```

If sudo requires a password:

```bash
export LUMA_SUDO_PASSWORD='...'
```

For unattended Tailscale login:

```bash
export TAILSCALE_AUTHKEY='...'
```

## 2. Bootstrap The Node

For the first all-in-one server:

```bash
luma node bootstrap aly --profile single-node
```

The command is idempotent and can be re-run. It installs Docker, initializes Swarm if inactive, creates overlay networks, applies labels, creates runtime directories, configures UFW, and deploys Traefik plus Portainer.
It also installs Tailscale. When `TAILSCALE_AUTHKEY` is set, it logs the node into the tailnet automatically.

## 3. Configure Providers

Cloudflare:

```bash
export CLOUDFLARE_API_TOKEN='...'
luma cloudflare connect --zone itool.tech
```

Egress:

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup aly
```

Portainer webhook:

1. Open Portainer on `https://<server-ip>:9443`.
2. Create or connect the Swarm environment.
3. Create a Git-backed stack for this repository.
4. Enable webhook for the stack.
5. Export the webhook URL locally:

```bash
export PORTAINER_WEBHOOK_URL='...'
```

## 4. Verify

```bash
luma doctor
ssh aly 'sudo docker service ls'
```

Expected core services:

```text
traefik_traefik
portainer_portainer
portainer_agent
egress_mihomo
```

## 5. First Public Service

```bash
luma deploy examples/public-cn-service.yaml --commit --push
```

Check DNS and Traefik:

```bash
curl -I https://whoami.itool.tech
```

## 6. Add More Nodes Later

Additional nodes need Swarm join automation before they can be fully hands-off. For the current version, use the first `single-node` path as the reference production setup, then extend `nodes` and profiles as the multi-node automation matures.
