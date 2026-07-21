# Luma

[English](README.md) | [中文](README.zh-CN.md)

Luma is a small self-hosted deployment control plane built on HashiCorp Nomad. It drives the Nomad HTTP API to execute deployments, uses Traefik for HTTP/HTTPS ingress, Cloudflare for DNS, and a `luma` CLI that can run from any authenticated client machine.

It is meant for turning a few scattered servers into region-aware deployment targets:

```text
client laptop -> Luma Control -> Nomad API -> Nomad client -> docker driver -> container
```

## Who It Is For

| Good fit | Poor fit |
| --- | --- |
| You have one or a few VPS machines and want to deploy your own Web/API/worker services. | You already need Kubernetes-level multi-tenancy, network policy, and orchestration. |
| You want client machines to hold only a management token, not SSH, Docker, Cloudflare, or Nomad credentials. | You do not want to use a public domain or Cloudflare DNS. |
| You want to place services by regions such as `cn`, `global`, and `home`. | You only run a few local compose services on one host and do not need scheduling. |
| You want to expose some home/private services through Tailscale or Cloudflare Tunnel. | You cannot install Docker/Nomad/Traefik on the manager. |

## Prerequisites

| Requirement | Required | Purpose |
| --- | --- | --- |
| A domain you control | Yes | Control API and public service domains, such as `luma.example.com` and `api.example.com`. |
| Cloudflare DNS API token | Yes | Luma creates and updates control and service DNS records. The token needs zone read + DNS edit permissions. |
| One Linux manager | Yes | Runs the Nomad server, Traefik, and Luma Control. A 2c2g host is enough for evaluation. |
| Public inbound 80/443 | For public services | Traefik needs to receive HTTP/HTTPS traffic. |
| Tailscale | As needed | Required for private multi-node joins, `home` nodes, and `exposure: tailscale-relay`. Not required for a single public manager. |
| Egress subscription | As needed | Used for image-pull proxying and runtime proxying for `proxy: true` services. For mainland managers using the default GHCR control image, configure it before bootstrap. |

Client machines only need the CLI and network access to the control domain. They do not need Docker, SSH keys, Cloudflare tokens, or Nomad credentials.

## Token Model

Users only need to handle two Luma tokens:

| Token | Used by | Purpose |
| --- | --- | --- |
| Management token | Trusted clients, dashboard, and CI | Login, deploy apps, manage secrets, registries, storage, and nodes. For compatibility, the CLI environment variable is still `LUMA_DEPLOY_TOKEN`. |
| Node join token | Servers that are joining or refreshing their node agent | `luma node join ... --token ...` and one-time old-node repair with `luma update --control-url ... --token ...`. |

Node agent credentials are internal. Luma signs one for each joined node, writes it to that node's local agent config, and stores only a hash in Control. Users should only see agent connection status, not copy or manage agent credentials.

## Core Model

Luma's user-facing model is five words:

| Concept | Meaning |
| --- | --- |
| `node` | A machine joined to the cluster. The manager runs the Nomad server; other machines run as Nomad clients (worker or home). |
| `region` | Scheduling boundary. A service with `region: cn` only runs on nodes whose Nomad client `meta.region=cn`. |
| `exposure` | How the service is reached, such as `cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, or `none`. |
| `egress` | Outbound proxy capability for image pulls and runtime HTTP/HTTPS proxying for `proxy: true` services. |
| `service` | One deployment unit described by a Luma YAML manifest. |

`region` decides where a service runs. `exposure` decides how traffic enters. They are related, but not the same thing.
Set manifest `node` only when a service must be pinned to one Luma node name; Luma still keeps the `region` placement constraint and renders the node name into a Nomad constraint on `${node.unique.name}` (or `meta.luma_node_name`). The Nomad node identity is a stable UUID, so a pinned service keeps targeting the right machine across rejoins.

| Manifest | Scheduled on | Ingress path |
| --- | --- | --- |
| `region: cn` + `exposure: cn-edge` | `region=cn` nodes | Cloudflare DNS -> CN edge Traefik -> Nomad allocation |
| `region: global` + `exposure: external-edge` | `region=global` nodes | Cloudflare DNS -> global edge Traefik -> Nomad allocation |
| `region: home` + `exposure: tailscale-relay` | `region=home` nodes | Public Traefik -> Tailscale -> home service |
| `region: home` + `exposure: tcp-relay` | `region=home` nodes | Cloudflare DNS -> edge Traefik TCP entrypoint -> task host port |
| `region: cn` + `exposure: none` | `region=cn` nodes | No public ingress; useful for workers/jobs |

A public `cn-edge` domain does not bypass the server and jump directly to a container. DNS points to the configured CN edge target, traffic enters Traefik on that node, and Traefik routes to the Nomad allocation it discovered through the Nomad provider. If you add several CN nodes, service allocations may run on them, but public traffic still enters through the selected edge Traefik.

## Install The CLI

For CI runners, install the published Python package. It provides the `luma` command without running the shell installer:

```bash
python -m pip install "luma-infra==0.1.274"
```

Install without cloning the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

The installer creates a private venv and writes the command shim to `~/.local/bin/luma`. You can use `~/.local/bin/luma` immediately, or open a new shell / run `exec $SHELL -l` before using the shorter `luma`.

Install a tagged release:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.274 sh
```

Develop from source:

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

The installer only installs the local CLI. It does not modify system DNS, Docker, Nomad, Tailscale, or firewall state; host-level changes happen during `luma bootstrap manager` or `luma node join`.

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

The uninstall script does not remove Docker, Nomad, Traefik, Luma Control, deployed services, or server-side `/opt/luma` state.

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

### Manager-side LAE Control files

When Luma Control serves LAE, keep Builder and Runtime identities in separate
private files on the manager. The Nomad job mounts `/opt/luma/control`; every
principal, broker, and admin file passed to the job must therefore live below
that directory. Files must be regular, non-symlink files readable only by the
owner (`0600` is recommended). Token contents are read by Luma Control at
runtime and are never rendered into the Nomad Job or `control.json`.

Create one token file per identity, then create the two principal files. A
`tokenFile` is a file in the same directory as its principal file:

```json
{
  "lae-builder": {
    "tokenFile": "lae-builder.token",
    "tenantRefs": ["*"],
    "applicationRefs": ["*"]
  }
}
```

Save that as `/opt/luma/control/lae-builder-principals.json`. Save the Runtime
configuration separately as `/opt/luma/control/lae-runtime-principals.json`:

```json
{
  "lae-runtime": {
    "tokenFile": "lae-runtime.token",
    "tenantRefs": ["*"],
    "applicationRefs": ["*"],
    "builderPrincipalRefs": ["lae-builder"],
    "scopes": [
      "runtime:volumes:prepare",
      "runtime:deployments:write",
      "runtime:deployments:read",
      "runtime:logs",
      "runtime:metrics",
      "runtime:secrets:issue"
    ]
  }
}
```

Install all four files privately and configure the manager's `.env` before
`luma bootstrap manager` or `luma update manager`:

```bash
sudo install -d -m 700 /opt/luma/control
sudo chmod 600 \
  /opt/luma/control/lae-builder.token \
  /opt/luma/control/lae-runtime.token \
  /opt/luma/control/lae-builder-principals.json \
  /opt/luma/control/lae-runtime-principals.json

LUMA_LAE_SERVICE_PRINCIPALS_FILE=/opt/luma/control/lae-builder-principals.json
LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE=/opt/luma/control/lae-runtime-principals.json
```

Optional credential/object brokers and the LAE super-admin proxy use the same
file-only pattern:

```bash
LUMA_CREDENTIAL_BROKER_URL=https://lae-api.internal/v1/internal/credential-leases/redeem
LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS=5
LUMA_CREDENTIAL_BROKER_TOKEN_FILE=/opt/luma/control/lae-broker.token

LUMA_OBJECT_SOURCE_BROKER_URL=https://lae-api.internal/v1/internal/object-source-leases/redeem
LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS=5
# Optional when it intentionally reuses LUMA_CREDENTIAL_BROKER_TOKEN_FILE:
LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE=/opt/luma/control/lae-object-broker.token

LUMA_LAE_ADMIN_API_URL=https://lae-api.internal
LUMA_LAE_ADMIN_TIMEOUT_SECONDS=8
LUMA_LAE_ADMIN_TOKEN_FILE=/opt/luma/control/lae-admin.token
```

Only the documented HTTPS URLs, bounded timeouts, and file paths are copied
into the `luma-control` Job. Inline legacy variables such as
`LUMA_LAE_SERVICE_TOKEN` and `LUMA_LAE_*_PRINCIPALS_JSON` remain available for
direct/local compatibility but are deliberately not forwarded by manager
bootstrap; production Nomad managers should use the file configuration above.

You can skip editing `.env` and run `luma bootstrap manager --domain ...` directly. When local values are missing, the CLI explains each value and prompts interactively.

`EGRESS_SUBSCRIPTION_URL` is optional only when the manager can pull the configured control image directly. Mainland hosts using the default GHCR control image should set it before bootstrap and should not use `--skip-egress`.

Use `--skip-egress` only when the control image registry is directly reachable, or when you have pinned `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` to a registry the manager can pull:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Bootstrap installs/checks Docker, installs and starts the Nomad server, deploys Traefik and Luma Control as Nomad jobs, configures the firewall, and sets up egress when requested. It prints a management token and a node join token.

If one layer fails, re-run bootstrap or repair only that layer:

```bash
luma egress setup
luma tailscale connect
```

The default control API image is `ghcr.io/liutianjie/luma-control:latest`. For predictable upgrades, prefer a published immutable tag and set `LUMA_CONTROL_IMAGE=ghcr.io/<you>/luma-control:<tag>` before bootstrap/update, or set `defaults.images.lumaControl` in `luma.yaml`. Luma fails if the configured control image cannot be pulled. When egress is enabled, Luma configures the Docker daemon proxy before pulling default GHCR control images.

## Command Map

| Machine | Task | Command |
| --- | --- | --- |
| manager | First control-plane install | `luma bootstrap manager --domain luma.example.com` |
| manager | Update CLI and control plane | `luma update` |
| logged-in client | Update ready non-manager node agents | `luma update fleet` |
| browser on trusted device | Update Control and nodes, then verify every public route | `https://luma.example.com/dashboard/fleet` |
| worker/home node | Join the cluster | `luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1` |
| client laptop | Login to control plane | `luma login https://luma.example.com --token <management-token>` |
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

Bootstrap prints a node join token. Run this on each new server itself:

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

`--name` is the Luma node name used in status output and service manifests. It is written to the Nomad client's `meta.luma_node_name`, so pinned services target the intended machine even when Docker hostnames are not unique.
Because the Nomad node identity is a stable UUID, a node that leaves and later rejoins with the same Luma node name keeps the same `meta.luma_node_name`, so pinned service constraints stay valid. Do not depend on Docker hostnames for pinning.

`--region` is the scheduling boundary, written to `meta.region`. Service manifests match it through `region`:

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

For macOS home nodes, install and start Docker Desktop (or OrbStack) and Tailscale first. When `luma node join --region home ...` runs and the node is not connected to Tailscale yet, the CLI requires `TAILSCALE_AUTHKEY` before registering the node and enrolling the Nomad client. For non-apt Linux distributions, install Docker manually before joining. Add `--engine nomad` to force the Nomad client path explicitly; it is the default.

`--name` is the Luma node name. Luma writes it to the Nomad client `meta.luma_node_name`, so hosts with generic Docker names such as OrbStack's `orbstack` can still be targeted safely.

Before removing or rebuilding a node, run this on that node:

```bash
luma node exit
```

By default it drains the local Nomad client and removes local Luma runtime state under `/opt/luma`, while keeping Tailscale and Docker image/volume cache. Add `--endpoint <control-url> --token <management-token-or-node-join-token>` to unregister the Luma node name from the control plane during exit. Add `--tailscale` to also log out of Tailscale. Add `--prune-docker` only when you intentionally want to remove unused Docker cache and volumes.

To remove a node from the control plane and the cluster, run `luma node remove <name>` from any logged-in client. The manager deletes the Luma registration and drains the matching Nomad client; manager nodes (Nomad servers) are protected.

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

In CI, pass the control endpoint and management token through environment variables instead of creating a login context:

```bash
python -m pip install "luma-infra==0.1.274"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma validate status.yaml --format json
luma deploy status.yaml --dry-run --format json
luma deploy status.yaml --format ndjson --timeout 3000
```

CI clients do not need SSH, Docker, Cloudflare, Nomad, or persistent files under `~/.config/luma`.

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

Luma automatically attaches the egress proxy to the service and injects `HTTP_PROXY` / `HTTPS_PROXY`. This affects runtime outbound requests from the container; it is not the same as image-pull proxying.

For private images, keep registry credentials out of the manifest. Save them once from any logged-in client:

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
```

After that, manifests still only contain the source image name, for example `image: ghcr.io/acme/private-api:1.0.0`. When Builder Registry is configured, Luma leases matching source credentials only to Builder, copies the image into `registryHost/luma-cache/...`, verifies the digest, and renders that internal digest into the Nomad job. Runtime nodes therefore pull only from Builder Registry; deployment does not dynamically rewrite their Docker daemon proxy. Docker Hub sources may fall back to configured `defaults.imageMirrors` during the Builder copy. Set `defaults.imageMirrors: []` to disable source mirror fallback.
Private registry image pulls are separate from runtime `proxy: true`. If a scheduled node has a global Docker proxy and a private registry fails before auth, check `docker info` proxy settings on that node and make sure that registry host is in Docker daemon `NO_PROXY`; `curl https://<registry>/v2/` returning `401` usually means the registry is reachable and Docker proxy routing is the next thing to inspect.

Do not put sensitive values directly in manifests. If the project already has a `.env` file, pass it during deploy:

```bash
luma deploy app.yaml --env .env
```

Luma imports only the variables referenced by the manifest and stores them under the application scope, so two services can both use names like `DATABASE_URL` without overwriting each other. Reference them from YAML as usual:

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

You can also manage scoped secrets manually:

```bash
luma secret set DATABASE_URL --scope app
```

See [docs/deployment-yaml.md](docs/deployment-yaml.md) for all fields and [examples](examples) for service templates.

## Common Tasks

| Question | What to do |
| --- | --- |
| Update the whole Luma cluster | Prefer Dashboard → Nodes → Update center. Enter an immutable release tag and capture a public-route baseline. On confirmation, the Builder caches and verifies the Control image in the internal registry before the manager rollout starts; the page reconnects and probes routes again. Then update only stale non-manager nodes. Image, manager, and per-node results remain visible across page closes and Control restarts. |
| CLI update fallback | Use `luma update manager` and `luma update fleet` only when the Dashboard is unavailable or when adopting a historical agent that predates `luma-update`. Routine upgrades require no node SSH. |
| View whole cluster status | Run `luma status` from any logged-in client. It prints control, DNS, the orchestrator (Nomad) with its leader, and registered nodes with `role=client`. |
| View the Web status panel | Open `https://<control-domain>/dashboard/` and paste the management token on a trusted device. |
| What happens if I run `luma update` on a joined node or client? | On a joined node it updates the CLI and refreshes the local node agent; on a client it updates only the CLI and skips manager control-plane refresh. |
| When does `luma update` need `--domain`? | Only when you intentionally changed the control domain. If manager state is missing, run `luma bootstrap manager --domain ...` for first install or repair. |
| Move service A to another region | Edit the manifest `region`, adjust `exposure` if needed, then run `luma deploy app.yaml` again. |
| Pin service A to one node | Set manifest `node` to the Luma node name passed to `luma node join --name`, keep the matching `region`, then deploy again. Control renders it as a Nomad constraint on the node identity. |
| Roll back service A | Run `luma history app` and `luma rollback app` (or `--to-version <N>`), or open the dashboard and choose Applications -> Versions. Rollback is a Nomad job-version runtime revert; use pinned image tags/digests when you need predictable production rollback. |
| Rejoined node | Keep the same Luma node name and rerun `luma node join` / `luma update` on that node. The Nomad node UUID is stable, so pinned services stay valid. |
| Remove service A | Run `luma service remove app` after it has been deployed through Luma Control. It removes the matching single-service or Compose deployment, including DNS, the Nomad job, and generated route files; use `--dry-run` to preview or `--skip-dns` to keep DNS. |
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
| [docs/concepts.md](docs/concepts.md) | node / region / exposure / egress / service concepts. |
| [docs/deployment-yaml.md](docs/deployment-yaml.md) | service manifest fields, secrets, resources, and exposure examples. |
| [docs/exposure-model.md](docs/exposure-model.md) | `cn-edge`, `external-edge`, Tailscale relay, and Cloudflare Tunnel traffic models. |
| [docs/bootstrap.md](docs/bootstrap.md) | manager bootstrap details and profiles. |
| [docs/node-labels.md](docs/node-labels.md) | node labels, regions, and ingress labels. |
| [docs/operations.md](docs/operations.md) | daily operations and troubleshooting commands. |
| [docs/secrets.md](docs/secrets.md) | secret and environment variable handling. |
| [docs/troubleshooting.md](docs/troubleshooting.md) | common failures and fixes. |
| [docs/release.md](docs/release.md) | publishing tags, installer, and control image releases. |
| [docs/agent-skill.md](docs/agent-skill.md) | Installation and usage guide for the AI Agent Skill. |
| [docs/compose-storage.md](docs/compose-storage.md) | Multi-service Docker Compose deployment and NFS/local storage class setup and migration. |

## Agent Skill

Agents can use the installable skill in [skills/luma-deployment-yaml](skills/luma-deployment-yaml) to generate or review deployment YAML. See [docs/agent-skill.md](docs/agent-skill.md) for detailed installation and usage guidelines.

In Codex, ask:

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

- Do not commit API tokens, management tokens, node join tokens, or proxy subscription URLs.
- Do not write registry tokens into manifests or container environment variables. Use `luma registry login` and rotate/revoke the provider token if it is exposed.
- Client machines should not need SSH/Docker/Cloudflare/Nomad credentials; distribute management tokens instead.
- The Web status panel uses the management token and stores it in browser local storage. Use it on trusted devices only.
- Do not expose node agent credentials; they are internal per-node credentials managed automatically by Luma.
- Node join tokens should only be used on servers that are joining the cluster or refreshing an existing joined node agent.
- If a token or subscription URL is pasted into chat, logs, or issues, rotate it before publishing.
