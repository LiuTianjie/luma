# Luma

Luma is a small self-hosted deployment control plane for Docker Swarm. It uses Portainer to execute deployments, Traefik for HTTP/HTTPS ingress, Cloudflare for DNS, and a `luma` CLI that can run from any authenticated client machine.

It is meant for turning a few scattered servers into region-aware deployment targets:

```text
client laptop -> Luma Control -> Portainer -> Docker Swarm -> service tasks
```

## Who It Is For

| Good fit | Poor fit |
| --- | --- |
| You have one or a few VPS machines and want to deploy your own Web/API/worker services. | You already need Kubernetes-level multi-tenancy, network policy, and orchestration. |
| You want client machines to hold only a deploy token, not SSH, Docker, Cloudflare, or Portainer credentials. | You do not want to use a public domain or Cloudflare DNS. |
| You want to place services by regions such as `cn`, `global`, and `home`. | You only run a few local compose services on one host and do not need scheduling. |
| You want to expose some home/private services through Tailscale or Cloudflare Tunnel. | You cannot install Docker/Swarm/Traefik/Portainer on the manager. |

## Prerequisites

| Requirement | Required | Purpose |
| --- | --- | --- |
| A domain you control | Yes | Control API and public service domains, such as `luma.example.com` and `api.example.com`. |
| Cloudflare DNS API token | Yes | Luma creates and updates control and service DNS records. The token needs zone read + DNS edit permissions. |
| One Linux manager | Yes | Runs Docker Swarm manager, Traefik, Portainer, and Luma Control. A 2c2g host is enough for evaluation. |
| Public inbound 80/443 | For public services | Traefik needs to receive HTTP/HTTPS traffic. |
| Tailscale | As needed | Required for private multi-node joins, `home` nodes, and `exposure: tailscale-relay`. Not required for a single public manager. |
| Egress subscription | As needed | Used for image-pull proxying and runtime proxying for `proxy: true` services. You can start with `--skip-egress`. |

Client machines only need the CLI and network access to the control domain. They do not need Docker, SSH keys, Cloudflare tokens, Portainer passwords, or Portainer webhooks.

## Core Model

Luma's user-facing model is five words:

| Concept | Meaning |
| --- | --- |
| `node` | A machine joined to Swarm. It may be a manager, worker, or home node. |
| `region` | Scheduling boundary. A service with `region: cn` only runs on nodes labeled `region=cn`. |
| `exposure` | How the service is reached, such as `cn-edge`, `external-edge`, `tailscale-relay`, `cloudflare-tunnel`, or `none`. |
| `egress` | Outbound proxy capability for image pulls and runtime HTTP/HTTPS proxying for `proxy: true` services. |
| `service` | One deployment unit described by a Luma YAML manifest. |

`region` decides where a service runs. `exposure` decides how traffic enters. They are related, but not the same thing.
Set manifest `node` only when a service must be pinned to one Luma node name; Luma still keeps the `region` placement constraint and resolves the node name to the real Swarm NodeID before scheduling.

| Manifest | Scheduled on | Ingress path |
| --- | --- | --- |
| `region: cn` + `exposure: cn-edge` | `region=cn` nodes | Cloudflare DNS -> CN edge Traefik -> Swarm task |
| `region: global` + `exposure: external-edge` | `region=global` nodes | Cloudflare DNS -> global edge Traefik -> Swarm task |
| `region: home` + `exposure: tailscale-relay` | `region=home` nodes | Public Traefik -> Tailscale -> home service |
| `region: cn` + `exposure: none` | `region=cn` nodes | No public ingress; useful for workers/jobs |

A public `cn-edge` domain does not bypass the server and jump directly to a container. DNS points to the configured CN edge target, traffic enters Traefik on that node, and Swarm overlay forwards the request to the selected task. If you add several CN nodes, service replicas may run on them, but public traffic still enters through the selected edge Traefik.

## Install The CLI

Install without cloning the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

The installer creates a private venv and writes the command shim to `~/.local/bin/luma`. You can use `~/.local/bin/luma` immediately, or open a new shell / run `exec $SHELL -l` before using the shorter `luma`.

Install a tagged release:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.29 sh
```

Develop from source:

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

The installer only installs the local CLI. It does not modify system DNS, Docker, Swarm, Tailscale, or firewall state; host-level changes happen during `luma bootstrap manager` or `luma node join`.

If `luma` reports `permission denied: luma` from inside the repository, your shell is resolving the `luma/` Python package directory instead of the venv command. Use:

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

Uninstall the local CLI:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

Also remove local login contexts and config:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

The uninstall script does not remove Docker, Swarm, Portainer, Traefik, Luma Control, deployed services, or server-side `/opt/luma` state.

## First Manager

Run this on the manager server:

```bash
cp .env.example .env
$EDITOR .env
luma bootstrap manager --domain luma.example.com
```

Common values for `.env` or the interactive CLI prompts:

```bash
CLOUDFLARE_API_TOKEN=...
LUMA_DNS_EDGE_TARGET=203.0.113.10
TRAEFIK_ACME_EMAIL=ops@example.com
EGRESS_SUBSCRIPTION_URL=...
```

| Variable | When needed | Purpose |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | Required on the manager | Cloudflare DNS token. Luma uses it to create/update records for the control domain and public service domains. Needs Zone Read + DNS Edit permissions. |
| `LUMA_DNS_EDGE_TARGET` | Usually needed | Public IP or DNS name that Cloudflare A/CNAME records should point to. Bootstrap asks for it when `luma.yaml` has no edge target or edge node public IP. |
| `TRAEFIK_ACME_EMAIL` | Required on the manager | Email used by Traefik/Let's Encrypt for HTTPS certificate registration and expiration notices. |
| `EGRESS_SUBSCRIPTION_URL` | Required only for egress | Proxy subscription URL. Luma uses it to generate Mihomo config for image-pull proxying and runtime proxying for services with `proxy: true`. |
| `TAILSCALE_AUTHKEY` | Needed for private/home/tailscale-relay nodes | Lets servers join your tailnet. Not required for an ordinary single public manager or ordinary public services. |
| `LUMA_SUDO_PASSWORD` | Only when sudo needs a password | Local fallback password for sudo commands. It stays in the local user config and is not distributed to clients. |

You can skip editing `.env` and run `luma bootstrap manager --domain ...` directly. When local values are missing, the CLI explains each value and prompts interactively.

`EGRESS_SUBSCRIPTION_URL` is optional. If you do not have it yet:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Bootstrap installs/checks Docker, initializes Swarm, creates overlay networks, deploys Traefik, Portainer, and Luma Control, configures the firewall, and sets up egress when requested. It prints a deploy token and a join token.

If one layer fails, re-run bootstrap or repair only that layer:

```bash
luma portainer setup
luma egress setup
luma tailscale connect
```

The default control API image is `ghcr.io/liutianjie/luma-control:latest`. For source-checkout development, set `LUMA_CONTROL_IMAGE=luma-control:local` before bootstrap, or set `defaults.images.lumaControl` in `luma.yaml`.

## Command Map

| Machine | Task | Command |
| --- | --- | --- |
| manager | First control-plane install | `luma bootstrap manager --domain luma.example.com` |
| manager | Update CLI and control plane | `luma update` |
| worker/home node | Join the cluster | `luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1` |
| client laptop | Login to control plane | `luma login https://luma.example.com --token <deploy-token>` |
| client laptop | Deploy a service | `luma deploy app.yaml` |
| browser on trusted device | View status panel | `https://luma.example.com/dashboard/` |
| any logged-in client | Manage deploy secrets | `luma secret set DATABASE_URL` |
| any logged-in client | Manage private image registry credentials | `printf '%s' "$GHCR_TOKEN" \| luma registry login ghcr.io --username <user> --password-stdin` |
| any machine | Show local version | `luma version` |
| any machine | Diagnose local environment | `luma doctor` |

The installer is the same on every machine. The next command defines the role:

- manager: `bootstrap manager`, `update`
- worker/home node: `node join`, `node exit`
- client: `login`, `deploy`, `secret`, `registry`, `context`

## Add Nodes

Bootstrap prints a join token. Run this on each new server itself:

```bash
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

`--name` is the Luma node name used in status output and service manifests. Luma also records the real Swarm NodeID so pinned services target the intended machine even when Docker hostnames are not unique.

`--region` is the scheduling label. Service manifests match it through `region`:

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

For macOS home nodes, install and start Docker Desktop and Tailscale first. When `luma node join --region home ...` runs and the node is not connected to Tailscale yet, the CLI requires `TAILSCALE_AUTHKEY` before registering the node and joining Swarm. For non-apt Linux distributions, install Docker manually before joining.

Before removing or rebuilding a node, run this on that node:

```bash
luma node exit
```

By default it leaves Swarm and removes local Luma runtime state under `/opt/luma`, while keeping Tailscale and Docker image/volume cache. Add `--endpoint <control-url> --token <token>` to unregister the Luma node name from the control plane during exit. Add `--tailscale` to also log out of Tailscale. Add `--prune-docker` only when you intentionally want to remove unused Docker cache and volumes.

To remove a node from the control plane and Swarm, run `luma node remove <name>` from any logged-in client. The manager deletes the Luma registration and the matching Swarm worker node; manager nodes are protected.

## Deploy Services

Minimal public service:

```yaml
name: status
image: traefik/whoami:latest
region: cn
exposure: cn-edge
domain: status.example.com
port: 80
replicas: 1
```

Deploy it:

```bash
luma validate status.yaml
luma deploy --dry-run status.yaml
luma deploy status.yaml
```

When application services share a small manager, set explicit resource limits:

```yaml
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

Services that need runtime proxying use `proxy: true`:

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
```

Luma automatically joins the service to the `egress` overlay network and injects `HTTP_PROXY` / `HTTPS_PROXY`. This affects runtime outbound requests from the container; it is not the same as image-pull proxying.

For private images, keep registry credentials out of the manifest. Save them once from any logged-in client:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
```

After that, manifests still only contain the image name, for example `image: ghcr.io/acme/private-api:1.0.0`. During deploy, Luma matches the registry host, pre-pulls with Docker registry auth, and sends Portainer/Swarm the registry auth needed by the node that receives the task. This is useful for private GHCR images produced by GitHub Actions, including images built from repositories that also publish docs or marketing pages through GitHub Pages.

Do not put sensitive values directly in manifests. Store them in the control plane:

```bash
luma secret set DATABASE_URL
```

Then reference them from YAML:

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

See `docs/deployment-yaml.md` for all fields and `examples/` for service templates.

## Common Tasks

| Question | What to do |
| --- | --- |
| Update the manager | Run `luma update` on the manager. If local manager state exists, it updates the CLI and hot-refreshes only Luma Control, without restarting Traefik, Portainer, Docker, or app stacks. |
| View whole cluster status | Run `luma status` from any logged-in client. It prints control, DNS, Portainer, registered nodes, and actual Swarm nodes. |
| View the Web status panel | Open `https://<control-domain>/dashboard/` and paste the deploy token on a trusted device. |
| What happens if I run `luma update` on a client or worker? | It updates only the local CLI and skips manager control-plane refresh. |
| When does `luma update` need `--domain`? | Only when you intentionally changed the control domain. If manager state is missing, run `luma bootstrap manager --domain ...` for first install or repair. |
| Move service A to another region | Edit the manifest `region`, adjust `exposure` if needed, then run `luma deploy app.yaml` again. |
| Pin service A to one node | Set manifest `node` to the Luma node name passed to `luma node join --name`, keep the matching `region`, then deploy again. Control resolves it to the Swarm NodeID before scheduling. |
| Make a public service internal | Change `exposure` to `none`, remove public-only domain/ingress config if no longer needed, then deploy again. |
| Make an internal service public | Set a matching `region` + `exposure`, add `domain` and `port`, then deploy again. |
| Deploy a private GHCR image | Save the credential with `luma registry login ghcr.io --username <user> --password-stdin`, then deploy the normal manifest. |
| Add a CN worker | Run `luma node join ... --region cn --name ...` on the new machine. |
| Add a global worker | Run `luma node join ... --region global --name ...` on the new machine. |
| Add a home node | Prepare Docker Desktop/Tailscale first, then run `luma node join ... --region home --name ...`. If Tailscale is not connected, the CLI requires `TAILSCALE_AUTHKEY`. |
| Manager is only 2c2g | Set `resources.limits` and `resources.reservations` on application manifests so apps do not starve the control plane. |
| Tailscale was not connected during bootstrap/join | Run `luma tailscale connect` on the relevant machine; it requires `TAILSCALE_AUTHKEY`. |
| Egress failed or subscription was added later | Set `EGRESS_SUBSCRIPTION_URL`, then run `luma egress setup`. |
| Check control-plane version | Run `luma version --control-url https://luma.example.com`. |
| A public service returns 404 on `/` | The route usually reached the app; verify with the real app path such as `/admin/`. |

## Docs Map

| Document | Covers |
| --- | --- |
| `docs/concepts.md` | node / region / exposure / egress / service concepts. |
| `docs/deployment-yaml.md` | service manifest fields, secrets, resources, and exposure examples. |
| `docs/exposure-model.md` | `cn-edge`, `external-edge`, Tailscale relay, and Cloudflare Tunnel traffic models. |
| `docs/bootstrap.md` | manager bootstrap details and profiles. |
| `docs/node-labels.md` | node labels, regions, and ingress labels. |
| `docs/operations.md` | daily operations and troubleshooting commands. |
| `docs/secrets.md` | secret and environment variable handling. |
| `docs/troubleshooting.md` | common failures and fixes. |
| `docs/release.md` | publishing tags, installer, and control image releases. |

## Agent Skill

Agents can use the installable skill in `skills/luma-deployment-yaml` to generate or review deployment YAML. In Codex, ask:

```text
Install the skill from https://github.com/LiuTianjie/luma/tree/main/skills/luma-deployment-yaml
```

Manual install:

```bash
mkdir -p ~/.codex/skills
tmp="$(mktemp -d)"
git clone --depth 1 https://github.com/LiuTianjie/luma.git "$tmp/luma"
rm -rf ~/.codex/skills/luma-deployment-yaml
cp -R "$tmp/luma/skills/luma-deployment-yaml" ~/.codex/skills/
rm -rf "$tmp"
```

Restart Codex after installing so the skill is loaded.

## Security Boundary

- Do not commit API tokens, Portainer webhooks, deploy tokens, join tokens, or proxy subscription URLs.
- Do not write registry tokens into manifests or container environment variables. Use `luma registry login` and rotate/revoke the provider token if it is exposed.
- Client machines should not need SSH/Docker/Cloudflare/Portainer credentials; distribute deploy tokens instead.
- The Web status panel uses the deploy token and stores it in browser local storage. Use it on trusted devices only.
- Join tokens should only be used on servers that are joining the cluster.
- If a token or subscription URL is pasted into chat, logs, or issues, rotate it before publishing.
