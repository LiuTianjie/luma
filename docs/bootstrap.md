# Bootstrap

Bootstrap is automated by the Luma CLI. The first supported target is Ubuntu 22.04+.

## 1. Prepare Local CLI

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

Required local tools are Python 3.9+ and curl or wget. Docker is only needed on servers that will run workloads.

## 2. Prepare The Manager Node

Run the first bootstrap directly on the full manager node. Add the server to `luma.yaml` so DNS targets and labels can be derived:

```yaml
nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - nomad-server
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

The default manager bootstrap uses the published control image configured in Luma. Publish a pullable image first, then pin that image through `defaults.images.lumaControl` or `LUMA_CONTROL_IMAGE`. Luma does not reuse stale local images or build a fallback image during bootstrap/update; if the configured image cannot be pulled, the operation fails.

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

The command is idempotent and can be re-run. It installs Docker, installs and starts the Nomad server agent if inactive, applies node `meta` (region / luma_node_name / ingress / egress), creates runtime directories, configures UFW, and deploys Traefik and Luma Control as Nomad jobs.
It also installs Tailscale. When `TAILSCALE_AUTHKEY` is set, it logs the node into the tailnet automatically.
For profiles with the `egress` role, it also runs egress setup. Set `EGRESS_SUBSCRIPTION_URL` first when the manager needs a proxy to pull the configured control image. Mainland managers using the default GHCR control image should not use `--skip-egress`.

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
luma egress setup
```

To bootstrap the node first and configure egress later, use `--skip-egress` only when the control image registry is directly reachable, or when `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` points at a registry the manager can pull:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

The output includes:

```text
Control domain: luma.example.com
Control URL: https://luma.example.com
Orchestrator: nomad
Nomad API: http://203.0.113.10:4646
Cluster: luma-...
Management token: ...
Node join token: ...
```

Keep the management token and node join token private.

Luma exposes only two user-facing tokens:

- **Management token**: used by trusted CLI clients and the dashboard to deploy, configure storage, manage secrets/registries, and operate nodes. For compatibility, the CLI environment variable is still `LUMA_DEPLOY_TOKEN`.
- **Node join token**: used only on servers that are joining the cluster or refreshing their local node agent.

Per-node agent credentials are internal. `luma node join` and `luma update` request and install them automatically, and Control stores only their hash.

The `--domain` value is the Luma Control URL. The Nomad HTTP API is not routed through that domain; it listens on the manager's Tailscale address at `4646` and is reachable only over the tailnet.

## 4. Login From A Client

From any machine that should be allowed to deploy:

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

The client stores the endpoint, cluster id, and management token in `~/.config/luma/contexts/`. It does not need Docker, SSH access, Cloudflare credentials, or Nomad credentials.

For a read-only Web view, open:

```text
https://luma.example.com/dashboard/
```

Paste the same management token to see control readiness, nodes, services, and inferred traffic paths. Use this only on trusted devices because the browser stores the token in local storage.

## 5. Join Worker Nodes

Run this directly on each additional server:

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

The node asks the manager for the Nomad gossip key and server retry-join address, then enrolls as a Nomad client locally.
After the local join succeeds, it calls back to Luma Control so the manager records the region and Luma node `meta` automatically. `--name` is the Luma node name used in status output and service manifests; it is written to the client's `meta.luma_node_name` and used for pinned scheduling. `--region` is the scheduling boundary, written to `meta.region`.

Add `--engine nomad` to force the Nomad client agent path explicitly; it is the default and rarely needs to be passed.

Nomad's `max_client_disconnect` (rendered into every Luma job) is what makes home workers reaching the server over Tailscale tolerant of transient drops: when a client briefly loses its RPC path, its local allocations keep running instead of being killed and rescheduled.

### Required node ports

Luma configures UFW on Linux nodes during bootstrap/join. If you use a cloud security group, host firewall, or Tailscale ACL, allow these paths too:

| Port | Direction | Purpose |
| --- | --- | --- |
| `80/tcp` | Internet to manager/edge | HTTP entrypoint and Let's Encrypt HTTP-01 challenge. |
| `443/tcp` | Internet to manager/edge | HTTPS entrypoint for public services and Luma Control. |
| `tcp-relay` published ports | Internet to manager/edge | Public TCP relay ports, for example `3306/tcp` for MySQL. Luma restores Traefik listeners from Control state; cloud firewalls/security groups must allow the same ports. |
| `4646/tcp` | clients/Traefik to Nomad server | Nomad HTTP API (deploy, status, service discovery). |
| `4647/tcp` | Nomad clients to server | Nomad RPC. |
| `4648/tcp` and `4648/udp` | node to node | Nomad Serf gossip (server membership). |

`7890/tcp` and `7890/udp` are intentionally denied for public inbound access; the egress proxy is for local Docker/service outbound traffic only. Luma installs host firewall, raw `PREROUTING`, and Docker `DOCKER-USER` guards for this because Docker-published ports and Docker's userland proxy can bypass plain UFW deny rules.

When the manager has a Tailscale address, Luma opens `4646/tcp`, `4647/tcp`, `4648/tcp`, and `4648/udp` only on the `tailscale0` interface; Nomad control-plane traffic should use Tailscale in that topology. Agents bind to `0.0.0.0` but advertise their Tailscale IP, so binding wide is safe behind the interface-scoped UFW rules. If you intentionally run Nomad over public IPs without Tailscale, restrict these ports with a cloud security group or trusted source ACL.

If a node needs to leave a broken or rebuilt manager before joining again, run this on that node:

```bash
luma node exit
```

By default this drains the local Nomad client and removes `/opt/luma`, while keeping Tailscale login state and Docker caches. Add `--endpoint <control-url> --token <management-token-or-node-join-token>` to unregister the Luma node name from the control plane during exit. Add `--tailscale` only when the node should leave the tailnet too; add `--prune-docker` only when unused Docker cache and volumes should be removed.

## 6. Configure Or Refresh Providers

Provider config and secrets are copied into the manager control state during `luma bootstrap manager`. If you change `luma.yaml` or Cloudflare settings after bootstrap, rerun:

If the local manager config has `CLOUDFLARE_API_TOKEN` but no `providers.dns`, bootstrap and `luma update manager` infer the Cloudflare zone from the control domain, look up the zone id, and write `providers.dns` before installing `/opt/luma/luma.yaml`. If no DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET`; non-interactive update uses the configured edge node public IP or an existing `LUMA_DNS_EDGE_TARGET`.

```bash
luma bootstrap manager --domain luma.example.com
```

Use `luma update` after upgrading Luma itself:

```bash
luma update
```

The update command always refreshes the local CLI first. On a manager, default `luma update` detects `/opt/luma/control/control.json` and preserves existing tokens, nodes, and user jobs while reconciling the manager control plane: firewall TCP relay ports, Traefik when the manager has the `edge` role, the Tailscale watchdog, control config/state metadata, inferred DNS provider config when local Cloudflare credentials are available, and the `luma-control` Nomad job. It pulls the configured Control image, submits the job with Nomad auto-revert, and refreshes the manager's local node agent when possible. It does not restart Docker or the Nomad agent, run egress setup, or redeploy user services.

On a joined worker/home node, `luma update` updates the CLI and refreshes the local node agent. If an older node has no saved agent metadata, pass the Control URL and node join token once:

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

From any logged-in client, update ready non-manager node agents:

```bash
luma update fleet
```

Fleet update depends on the node agent already being new enough to support the `luma-update` task. If a node is reported as skipped because it does not support fleet update, run `luma update` once on that node; future fleet updates can then refresh it remotely. Fleet update also refreshes node-side support services such as the Tailscale watchdog.

Fleet update skips the Nomad server (manager) node by default so a client-side fleet operation cannot disrupt the active control plane. Update the manager separately from the manager host with `luma update manager`.

On an ordinary client, `luma update` only updates the CLI. Use `luma update manager` on a manager to force the same manager control-plane reconciliation when you need to pass `--domain`.

Use `luma bootstrap manager --domain ...` for first install or explicit infrastructure repair. Full bootstrap can touch Docker, firewall, Traefik, the Nomad agent, and egress, so treat it as a maintenance-window operation. If an old installed CLI still implements the previous update behavior, update the CLI first through the installer or package manager, then rerun the new `luma update manager` reconciliation.

If the installed CLI is too old to recognize `luma update`, run the installer once and then retry the update command.

Manager bootstrap/update installs a systemd Tailscale watchdog when Tailscale and systemd are available. Joined node-agent install/update does the same on Linux with systemd and on macOS with LaunchDaemon. The watchdog only restarts local Tailscale after consecutive peer TCP failures, so application deploys should not take down Docker or the Nomad agent.

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

Nomad is initialized automatically during bootstrap. Luma deploys through the Nomad HTTP API.

Bootstrap stores the relevant Cloudflare values in `/opt/luma/control/control.json` on the manager. Client machines do not need these values.

## 7. Verify

```bash
luma doctor
```

Expected core Nomad jobs:

```text
traefik
luma-control
egress-mihomo
```

## 8. First Public Service

```bash
luma deploy examples/public-cn-service.yaml
```

Check DNS and Traefik:

```bash
curl -I https://whoami.example.com
```
