# Luma

Luma is a self-hosted deployment control plane for Docker Swarm with Portainer built in.

It turns scattered servers into named deployment regions, then lets any logged-in client deploy a service from a small YAML manifest.

## Core Concepts

Luma keeps the user-facing model small:

```text
node / region / exposure / egress / service
```

The runtime stack is:

```text
Luma CLI        install, login, join nodes, render, diagnose, deploy
Luma Control    self-hosted API on the manager node for auth and orchestration
Portainer       required dashboard and deployment runner
Docker Swarm    runtime and scheduler
Traefik         public HTTP/HTTPS ingress
Cloudflare      DNS and optional Tunnel
Egress Gateway  outbound proxy for image pulls and selected services
```

## 5 Minute Quickstart

Install the CLI without cloning the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
luma preflight
```

The installer downloads a release archive, creates a private venv under `~/.local/share/luma/venv`, writes a `luma` command shim to `~/.local/bin/luma`, and adds `~/.local/bin` to your shell profile when needed. Open a new shell or run `exec $SHELL -l` after installation.

For development from a checkout, the same script still works:

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

Use the same installer on every machine:

- **manager server**: installs the CLI, then `luma bootstrap manager ...` installs Docker, Tailscale, Swarm, Traefik, Portainer, Luma Control, and egress.
- **worker server**: installs the CLI, then `luma node join ...` installs/checks Docker, connects Tailscale, joins Swarm, and applies node labels.
- **client machine**: installs only the CLI, then `luma login ...` and `luma deploy ...`; Docker, SSH, Cloudflare credentials, and Portainer credentials are not required locally.

The installer loads `.env` if present and fixes Linux DNS before creating the virtualenv, so Python package installation is less likely to fail on fresh cloud servers. If `python3` is missing, it prints the OS-specific package command and exits. To install a tagged release, set `LUMA_INSTALL_REF`:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.0 sh
```

Uninstall the local CLI without touching user secrets or login contexts:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

Remove the local CLI, `~/.luma.config.json`, and `~/.config/luma`:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

The uninstall script only removes the local Luma CLI install. It does not remove Docker, Swarm, Portainer, Traefik, Luma Control, deployed services, or server-side `/opt/luma` state.

If your shell says `permission denied: luma`, it is resolving the repository's `luma/` package directory instead of the venv command. Use either:

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

On the first full manager node, create a local `.env` for secrets:

```bash
cp .env.example .env
$EDITOR .env
```

Luma loads `.env` automatically. Values already exported in your shell take priority over `.env`.

Create or edit `luma.yaml`:

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN

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

The default control API image is published as `ghcr.io/liutianjie/luma-control:latest`. For source-checkout development, set `defaults.images.lumaControl: luma-control:local` or export `LUMA_CONTROL_IMAGE=luma-control:local` before bootstrap.

Bootstrap the first full manager node directly on that server:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
```

Bootstrap is the default all-in-one setup path. It streams each step as `[start]`, `[ok]`, or `[fail]`. For `single-node`, it installs Docker, sets up Tailscale when configured, initializes Swarm, creates overlay networks, deploys Traefik, Portainer, and Luma Control, configures the firewall, and sets up egress. Portainer is initialized automatically and bound to Luma Control; you do not need to create a Portainer webhook by hand. Set `EGRESS_SUBSCRIPTION_URL` first, or use `--skip-egress` and repair egress later.

If one layer fails, re-run bootstrap or repair only that layer:

```bash
luma portainer setup
luma egress setup
luma tailscale connect
```

The bootstrap output includes a deploy token and a join token. On any client machine, login with the deploy token:

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
```

On each additional server, join from that server itself:

```bash
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

## Node Regions

Luma uses `region` as the scheduling boundary for both nodes and services.

These fields mean different things:

- `--name`: the human/node identifier registered with Luma and Docker. It can be any unique name, but use a clear name such as `global-sg-1` or `home-mac-mini`.
- `--region`: the scheduling region label used by service manifests. This is what `region: cn`, `region: global`, or `region: home` matches during deploy.
- `--egress`: optional node capability. Use it for a node that can run proxy/egress workloads.

For example, `--name m3max --region home` means the node is called `m3max` and receives `region=home`. The name does not affect scheduling.

Manager bootstrap profiles:

- `single-node`: the first all-in-one manager. Runs Swarm manager, Traefik, Portainer, Luma Control, and egress.
- `cn-edge`: a public domestic edge/manager profile without the all-in-one egress setup.

Use `luma bootstrap manager` for these profiles on the manager server:

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
luma bootstrap manager --domain luma.example.com --profile cn-edge
```

Run `luma node join` on the machine being added:

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
luma node join https://luma.example.com --token <join-token> --region cn --name cn-egress-1 --egress
```

Services that need a runtime proxy declare `proxy: true` in their service manifest. They are scheduled onto nodes with `egress=true` and receive proxy environment variables automatically.

For macOS home nodes, install and start Docker Desktop and Tailscale first; Luma does not use apt on macOS.
For non-apt Linux distributions, install Docker manually before `luma node join`.

## Update An Existing Manager

After new Luma code is merged and the control image is published, run this on the manager:

```bash
luma update manager --domain luma.example.com --profile single-node
```

`luma update manager` updates the local CLI, then runs an idempotent manager bootstrap. It refreshes `/opt/luma/luma.yaml`, `/opt/luma/control/control.json`, pulls the current `ghcr.io/liutianjie/luma-control:latest`, and redeploys the Luma Control service. Existing Portainer data, deploy tokens, join tokens, Swarm nodes, and service stacks are kept unless you explicitly purge or reset them.

If the installed CLI is too old to recognize `luma update`, run the installer once and then retry:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
luma update manager --domain luma.example.com --profile single-node
```

Deploy a service from any logged-in client. The client does not need Docker, SSH keys, Cloudflare credentials, or Portainer webhooks:

```bash
luma deploy examples/public-cn-service.yaml
```

Image pulls after bootstrap use the Docker daemon proxy configured by egress. Luma deploys the original image from the manifest first; if that pull fails on the manager, it rewrites the generated stack to a configured mirror such as `docker.1panel.live/<image>` and reports the fallback in CLI output.

Run diagnostics:

```bash
luma doctor
luma doctor --legacy-ssh --deep  # optional legacy node checks
```

Release notes for publishing the installer and tagged versions live in [docs/release.md](docs/release.md).

If Tailscale was not connected during bootstrap:

```bash
luma tailscale connect
```

## Daily Workflow

Create a service manifest:

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
public: true
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

Default deploy through Portainer:

```bash
luma deploy app.yaml
```

For multiple GitOps stacks in one private repo, give each service its own webhook env:

```yaml
name: api
portainer:
  webhookUrlEnv: PORTAINER_WEBHOOK_API
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
