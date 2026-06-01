# Bootstrap

Bootstrap is automated by the Luma CLI. The first supported target is Ubuntu 22.04+.

## 1. Prepare Local CLI

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

Required local tools are Python 3.9+ and curl or wget. SSH is only needed for the legacy remote bootstrap path. Docker is only needed on servers that will run workloads.

## 2. Prepare The Manager Node

Run the first bootstrap directly on the full manager node. Add the server to `luma.yaml` so DNS targets and labels can be derived:

```yaml
nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress
```

If sudo requires a password:

```bash
export LUMA_SUDO_PASSWORD='...'
```

For unattended Tailscale login:

```dotenv
TAILSCALE_AUTHKEY=...
```

For the built-in egress gateway:

```dotenv
EGRESS_SUBSCRIPTION_URL=...
```

Put them in `.env`; Luma loads it automatically.

The default manager bootstrap uses the published control image configured in Luma. If you are developing the control API locally and want the manager to build it from the installed package, set `defaults.images.lumaControl: luma-control:local` or export `LUMA_CONTROL_IMAGE=luma-control:local`.

To publish a custom control image:

```bash
docker build -f Dockerfile.control -t ghcr.io/<you>/luma-control:latest .
docker push ghcr.io/<you>/luma-control:latest
export LUMA_CONTROL_IMAGE=ghcr.io/<you>/luma-control:latest
```

## 3. Bootstrap The Manager

For the first all-in-one server:

```bash
luma bootstrap manager --domain luma.example.com
```

The command is idempotent and can be re-run. It installs Docker, initializes Swarm if inactive, creates overlay networks, applies labels, creates runtime directories, configures UFW, and deploys Traefik, Portainer, and Luma Control.
It also installs Tailscale. When `TAILSCALE_AUTHKEY` is set, it logs the node into the tailnet automatically.
For profiles with the `egress` role, it also runs egress setup. Set `EGRESS_SUBSCRIPTION_URL` first, or use `--skip-egress` and repair egress later.

During bootstrap, Luma prints live step logs:

```text
[start] Install Docker
[ok] Docker installed
[start] Deploy Traefik
[fail] Deploy Traefik
  Fix: Re-run luma bootstrap manager after fixing the error
```

If a step fails, fix that layer and either re-run bootstrap or run the focused repair command:

```bash
luma tailscale connect
luma portainer setup
luma egress setup
```

To bootstrap the node first and configure egress later:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

The output includes:

```text
Control domain: luma.example.com
Control URL: https://luma.example.com
Portainer URL: https://203.0.113.10:9443
Portainer username: admin
Portainer password: sudo jq -r '.portainerAdminPassword' /opt/luma/control/control.json
Cluster: luma-...
Deploy token: ...
Join token: ...
```

Keep the deploy token, join token, and Portainer admin password private.

The `--domain` value is the Luma Control URL. Portainer is not routed through that domain by default.
Bootstrap exposes Portainer directly on the manager at `https://<manager-ip>:9443`, with Traefik disabled for
the Portainer stack. To read the generated Portainer password later:

```bash
sudo jq -r '.portainerAdminUsername, .portainerAdminPassword' /opt/luma/control/control.json
```

## 4. Login From A Client

From any machine that should be allowed to deploy:

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
```

The client stores the endpoint, cluster id, and deploy token in `~/.config/luma/contexts/`. It does not need Docker, SSH access, Cloudflare credentials, or Portainer webhooks.

For a read-only Web view, open:

```text
https://luma.example.com/dashboard/
```

Paste the same deploy token to see control readiness, nodes, services, and inferred traffic paths. Use this only on trusted devices because the browser stores the token in local storage.

## 5. Join Worker Nodes

Run this directly on each additional server:

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

The node asks the manager for the Swarm join token and manager address, then joins the cluster locally.
After the local join succeeds, it calls back to Luma Control so the manager applies the region and Luma node labels automatically. `--name` is the Luma node name used in status output and service manifests; Luma stores the real Swarm NodeID separately and uses that NodeID for pinned scheduling. `--region` is the scheduling boundary.

### Required node ports

Luma configures UFW on Linux nodes during bootstrap/join. If you use a cloud security group, host firewall, or Tailscale ACL, allow these paths too:

| Port | Direction | Purpose |
| --- | --- | --- |
| `80/tcp` | Internet to manager/edge | HTTP entrypoint and Let's Encrypt HTTP-01 challenge. |
| `443/tcp` | Internet to manager/edge | HTTPS entrypoint for public services and Luma Control. |
| `9443/tcp` | trusted clients to manager | Direct Portainer UI/API access. Restrict this when possible. |
| `2377/tcp` | workers to manager | Docker Swarm control-plane join and manager communication. |
| `7946/tcp` and `7946/udp` | node to node | Docker Swarm node discovery and overlay-network gossip. |
| `4789/udp` | node to node | Docker overlay/VXLAN data path. |

`7890/tcp` and `7890/udp` are intentionally denied for public inbound access; the egress proxy is for local Docker/service outbound traffic only.

If a node needs to leave a broken or rebuilt manager before joining again, run this on that node:

```bash
luma node exit
```

By default this leaves Docker Swarm and removes `/opt/luma`, while keeping Tailscale login state and Docker caches. Add `--endpoint <control-url> --token <token>` to unregister the Luma node name from the control plane during exit. Add `--tailscale` only when the node should leave the tailnet too; add `--prune-docker` only when unused Docker cache and volumes should be removed.

## 6. Configure Or Refresh Providers

Provider config and secrets are copied into the manager control state during `luma bootstrap manager`. If you change `luma.yaml`, Cloudflare settings, or legacy Portainer webhook env vars after bootstrap, rerun:

If the local manager config has `CLOUDFLARE_API_TOKEN` but no `providers.dns`, bootstrap infers the Cloudflare zone from the control domain, looks up the zone id, and writes `providers.dns` before installing `/opt/luma/luma.yaml`. If no DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET` and writes it as `providers.dns.edgeTarget`.

```bash
luma bootstrap manager --domain luma.example.com
```

Use `luma update` after upgrading Luma itself:

```bash
luma update
```

The update command always refreshes the local CLI first. On a manager, default `luma update` detects `/opt/luma/control/control.json` and refreshes manager bootstrap without comparing control API versions. On a client or worker node, it skips manager bootstrap and exits after updating the CLI. Use `luma update manager` to force an idempotent manager bootstrap refresh when manager state is missing or when you need to pass `--domain`. Pass `--domain` only when the manager state is missing or you intentionally changed the control domain. Bootstrap refreshes manager config/state, pulls the current published Luma Control image, and redeploys the control service without purging Portainer data, tokens, Swarm nodes, or existing app stacks.

If the installed CLI is too old to recognize `luma update`, run the installer once and then retry the update command.

Verify the manager is really running the new control API:

```bash
luma version --control-url https://luma.example.com
```

The expected output includes `Node join model: region-first`.

Cloudflare:

```bash
export CLOUDFLARE_API_TOKEN='...'
export LUMA_DNS_EDGE_TARGET='203.0.113.10'
luma cloudflare connect --zone example.com
```

Portainer is initialized automatically during bootstrap. Luma uses the Portainer API by default, so new users do not need to create stack webhooks. If you intentionally use legacy Git-backed Portainer stacks, export the webhook URL on the manager before `luma bootstrap manager`, or rerun bootstrap after adding it:

```bash
export PORTAINER_WEBHOOK_URL='...'
```

Bootstrap stores the relevant Cloudflare and Portainer values in `/opt/luma/control/control.json` on the manager. Client machines do not need these values.

## 7. Verify

```bash
luma doctor
luma doctor --legacy-ssh --deep  # optional, from a machine that can SSH to nodes
```

Expected core services:

```text
traefik_traefik
portainer_portainer
portainer_agent
egress_mihomo
```

## 8. First Public Service

```bash
luma deploy examples/public-cn-service.yaml
```

Check DNS and Traefik:

```bash
curl -I https://whoami.example.com
```

## Legacy SSH Bootstrap

`luma node bootstrap <node> --profile ...` remains available as a transition and repair path for existing SSH-based setups. New documentation and the default experience should use local manager bootstrap, local worker join, and login-based deploy.
