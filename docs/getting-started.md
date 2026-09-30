# Getting started

This guide takes you from an empty Linux server to a running application: install the CLI, bootstrap one manager, deploy a first workload, then add more nodes. It assumes no prior Luma setup.

## What you need

| Requirement | Why |
| --- | --- |
| A Linux server, Ubuntu 22.04+ recommended, 2 CPU / 2 GB RAM to start | Runs Luma Control, Nomad and Traefik |
| A domain whose DNS is managed by Cloudflare | Control API, dashboard and application hostnames |
| A Cloudflare API token with **Zone Read** and **DNS Edit** for that zone | Luma creates and updates DNS records |
| Public ports 80 and 443 open, and an email address for Let's Encrypt | HTTPS certificates and ingress |
| Access to the container registry that hosts the Luma Control image (GHCR by default) | The manager pulls Control from it |

Tailscale, a builder node and a private registry are **not** needed for the first workload. Add them later when you need home nodes, relays or source builds.

## 1. Install the CLI

Run this on the manager and on every machine you want to deploy from:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma doctor --local
```

The installer needs Python 3.9+ and curl or wget. It installs the latest release into `~/.local/share/luma` and puts `luma` in `~/.local/bin`; open a new shell if that directory is not on your `PATH` yet. It does not change Docker, Nomad or firewall settings.

Other ways to install:

```bash
# A specific release, a branch or a commit
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.2.1 sh

# CI runners and virtual environments
python -m pip install "luma-infra==0.2.1"
```

To uninstall the CLI (server state under `/opt/luma` is not touched), run `scripts/uninstall-luma.sh`; add `--purge` to also remove saved settings and logins.

## 2. Bootstrap the manager

Run this **on the manager server**:

```bash
luma bootstrap --domain luma.example.com
```

`luma bootstrap` asks for anything it is missing and explains each value:

- `CLOUDFLARE_API_TOKEN` and `TRAEFIK_ACME_EMAIL` are required.
- `LUMA_DNS_EDGE_TARGET` (the public IP that DNS records point to) is asked when it cannot be inferred.
- `TAILSCALE_AUTHKEY` and `EGRESS_SUBSCRIPTION_URL` are optional; press Enter to skip.

Answers are saved to `~/.luma.config.json` (mode 0600) so reruns do not ask again. You can also export them as environment variables or put them in a `.env` file in the current directory; Luma reads only its own settings from `./.env`.

If the manager needs a proxy to reach GHCR, which is usually the case on mainland China hosts, set `EGRESS_SUBSCRIPTION_URL` before bootstrapping. Otherwise pass `--skip-egress`.

Bootstrap installs Docker and the Nomad server, applies node metadata, configures the firewall, deploys Traefik and Luma Control as Nomad jobs and creates the Control database at `/opt/luma/control/control.sqlite3`. Each step prints `[start]`, `[ok]` or `[fail]` with a `Fix:` hint. Bootstrap is safe to rerun after fixing a failure; individual layers can also be repaired with `luma manager egress`, `luma node tailscale` and `luma doctor`.

When it finishes, it prints:

```text
Control URL: https://luma.example.com
Management token: ...
Node join token: ...
Dashboard: https://luma.example.com/dashboard/
```

Keep both tokens private:

- The **management token** signs in the CLI, CI and dashboard, and allows every operation. Its environment variable is `LUMA_DEPLOY_TOKEN`.
- The **node join token** is only used by servers joining the cluster.

Node agents get their own internal credentials automatically; you never handle them.

To use your own Control image, publish it and set `LUMA_CONTROL_IMAGE` before bootstrapping. Luma never builds or reuses a stale local image during bootstrap or update.

## 3. Deploy a first workload

The fastest check is the dashboard: open the dashboard URL, paste the management token and choose **Applications → Create application → hello-world first install**. It deploys an internal service with no DNS or registry requirements and proves that scheduling works.

From your laptop, sign in and deploy a public service:

```bash
luma login https://luma.example.com --token-stdin < token.txt
luma init --name status --image traefik/whoami:v1.10.3 --region cn --domain status.example.com --port 80
luma validate status.yaml
luma deploy status.yaml --dry-run
luma deploy status.yaml
luma app list
```

`luma init` writes a commented manifest with a memory limit and a health check; edit it as needed. `--dry-run` shows the Nomad job that Control will submit. Replace `status.example.com` with a hostname in your Cloudflare zone. See [Deploying applications](deploying.md) for local builds, repository imports and Compose applications.

## 4. Add nodes

Run this **on each additional server**, using the node join token:

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name sg-1
```

- `--region` decides which services may run there: `cn`, `global`, `home`, or a region created with `luma region create`.
- `--name` is the node name that manifests use to pin a service with `node:`.

Home and other private nodes need Tailscale: set `TAILSCALE_AUTHKEY` or run `luma node tailscale` first. macOS nodes also need a running Docker (Docker Desktop or OrbStack).

`luma node list` shows registered nodes and their agents. `luma node exit` stops Nomad on the current machine and removes its Luma state; add `--endpoint` and `--token` to unregister it too.

### Required ports

Luma configures UFW on Linux nodes. Mirror these rules in cloud security groups and Tailscale ACLs:

| Port | Between | Purpose |
| --- | --- | --- |
| `80/tcp`, `443/tcp` | Internet → manager | HTTP(S) ingress and Let's Encrypt |
| `tcp-relay` ports | Internet → manager | Public TCP relays, for example `3306/tcp` |
| `4646/tcp` | Clients and Traefik → Nomad server | Nomad HTTP API |
| `4647/tcp` | Nomad clients → server | Nomad RPC |
| `4648/tcp`, `4648/udp` | All Nomad agents | Nomad gossip |

When the manager has a Tailscale address, Luma opens the Nomad ports only on `tailscale0`. Port `7890` (the egress proxy) is never exposed publicly.

## 5. Keep Luma up to date

```bash
luma update            # on the manager: CLI and Control; on a node: CLI and agent; elsewhere: CLI
luma update fleet      # from any client: every node with a ready agent (the manager is skipped)
```

Both install the latest release unless you pass `--install-ref`. The dashboard's **Nodes → Update center** does the same with progress and route checks, and is the preferred way to update the manager. `luma update` never restarts Docker or Nomad and never redeploys applications.

## 6. Check health

```bash
luma status
luma doctor
luma doctor --deep
```

`doctor` checks the login, Control, DNS configuration, node agents and Nomad; `--deep` also evaluates Docker and Nomad diagnostics reported by each node.

## Next steps

- [Concepts](concepts.md): regions, exposure and egress.
- [Deploying applications](deploying.md): images, local builds, repository imports, Compose and CI.
- [Operations](operations.md): logs, rollback, restart, removal and node maintenance.
- [Troubleshooting](troubleshooting.md): what to do when a step fails.
