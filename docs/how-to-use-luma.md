# How To Use Luma

This is the operating manual for the first public version of Luma.

Luma keeps five concepts visible:

```text
node / region / exposure / egress / service
```

Portainer is a required operations console and deployment runner. Luma Control runs on the manager node and owns login tokens, node registration, DNS sync, stack rendering, and Portainer deployment calls. After `luma login`, `luma deploy` can be run from a client that does not have Docker, SSH access, Cloudflare credentials, or Portainer credentials. Tailscale is a control-plane network and a relay option for home services. Cloudflare is the DNS provider and optional tunnel provider. Egress Gateway is only for outbound traffic such as pulling images, installing dependencies, or running services that need external network access.

## 1. Install The CLI

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

This creates a private venv at `~/.local/share/luma/venv`, writes a `luma` command at `~/.local/bin/luma`, and adds `~/.local/bin` to your shell profile when needed. Use `~/.local/bin/luma` immediately, or open a new shell / run `exec $SHELL -l` before using the shorter `luma` command.

Install a specific tag:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.79 sh
```

For local development from a checkout:

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

To uninstall the local CLI:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

The default uninstall keeps `~/.luma.config.json` and `~/.config/luma`. To remove local config and login contexts too:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

This does not remove Docker, Swarm, Portainer, Traefik, Luma Control, deployed services, or `/opt/luma` from a server.

If `python3` is missing, the installer prints the package command for macOS or Ubuntu/Debian. Local Docker is optional; it is only used to validate rendered stack files before deployment.

Create `.env`:

```bash
cp .env.example .env
$EDITOR .env
```

Luma loads `.env` automatically. Shell exports win over `.env`, so CI or one-off commands can override local values.

## 2. Configure `luma.yaml`

`luma.yaml` is the only project config file Luma needs.

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    edgeTarget: 203.0.113.10
    recordType: A
    ttl: 1
    proxied: false
  portainer: {}

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/liutianjie
  stackRoot: stacks
  routesRoot: routes
  publicNetwork: public
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
  images:
    egressGateway: docker.1panel.live/metacubex/mihomo:latest
```

Run the command you actually need:

```bash
luma bootstrap manager --domain luma.example.com
```

If local values are missing, Luma asks for them first, writes `~/.luma.config.json`, then continues. On worker nodes, the same happens during `luma node join ...`. `.env` and exported environment variables still work for local overrides. If `CLOUDFLARE_API_TOKEN` is configured but `providers.dns` is missing, bootstrap and `luma update manager` infer the Cloudflare zone from the control domain and write the provider config before installing `/opt/luma/luma.yaml`. If no edge DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET`; non-interactive update uses the configured edge node public IP or an existing `LUMA_DNS_EDGE_TARGET`.

The relevant keys are explained in [secrets.md](secrets.md). The common manager values are:

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Cloudflare DNS token used to create/update control and service records. |
| `LUMA_DNS_EDGE_TARGET` | Public IP or DNS name that Cloudflare records should point to when no edge target is already configured. |
| `TRAEFIK_ACME_EMAIL` | Let's Encrypt account email used by Traefik for HTTPS certificates. |
| `EGRESS_SUBSCRIPTION_URL` | Optional proxy subscription URL for image-pull proxying and `proxy: true` services. |
| `TAILSCALE_AUTHKEY` | Optional auth key for private worker joins, home nodes, or tailscale-relay services. |
| `LUMA_SUDO_PASSWORD` | Optional fallback when sudo requires a password. |
| `LUMA_CONTROL_IMAGE` | Optional development/pinned control API image. |

Do not commit secrets.

## 3. Bootstrap The First Node

For a single public server that runs Swarm manager, Traefik, Portainer, and egress:

```bash
luma bootstrap manager --domain luma.example.com
```

This does:

- installs Docker and Compose;
- installs Tailscale and logs in when `TAILSCALE_AUTHKEY` is set;
- initializes Docker Swarm if needed;
- creates `public` and `egress` overlay networks;
- applies node labels;
- creates `/opt/luma/stacks`, `/opt/luma/routes`, `/opt/luma/control`, and `/opt/luma/egress-gateway`;
- deploys Traefik;
- deploys Portainer;
- deploys Luma Control;
- deploys egress when the profile has the `egress` role;
- configures UFW for SSH, 80, 443, 9443, and blocks inbound 7890.

Set `EGRESS_SUBSCRIPTION_URL` before running an egress profile when the manager needs a proxy to pull the configured control image. Mainland managers using the default GHCR control image should not use `--skip-egress`. Bootstrap prints live step logs with `[start]`, `[ok]`, and `[fail]` markers. If one step fails, either re-run bootstrap after fixing the issue or run the focused repair command for that layer.

If only Portainer needs repair:

```bash
luma portainer setup
```

If Tailscale login was skipped during bootstrap:

```bash
luma tailscale connect
```

If egress was skipped or needs repair:

```bash
luma egress setup
```

To intentionally skip egress during first bootstrap, only do this when the control image registry is directly reachable, or when `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` points at a registry the manager can pull:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

The bootstrap output includes a management token and a node join token. Use the management token on client machines:

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

Use the node join token on additional servers:

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

The manager applies scheduling labels automatically after the node joins Swarm. `--name` is the Luma node name used in service manifests. Luma also stores the real Swarm NodeID in `luma.node.id` and uses that NodeID for pinned scheduling, so generic Docker hostnames such as OrbStack's `orbstack` do not collide.

For `--region home`, the node must be connected to Tailscale before it can join a manager address on the tailnet. If the node is not connected yet, `luma node join` treats `TAILSCALE_AUTHKEY` as required and asks for it before registering the node. You can also run `luma tailscale connect` first to fill the key and connect Tailscale without attempting a Swarm join.

## 4. Connect Cloudflare

```bash
luma cloudflare connect --zone example.com
```

The command verifies the token, finds the zone, and writes `providers.dns.zoneId` back to `luma.yaml`.
Run this before `luma bootstrap manager` when possible. If you connect Cloudflare afterward, rerun manager bootstrap so `/opt/luma/luma.yaml` and `/opt/luma/control/control.json` are refreshed.

For `cn-edge` services, DNS defaults to the public IP of the configured edge node. A service can override this with:

```yaml
dns:
  target: 203.0.113.10
```

## 5. Repair Or Refresh Egress

```bash
luma egress setup
```

Bootstrap already runs this for `single-node` unless `--skip-egress` is used. Run it directly when egress was skipped, failed, or the subscription needs repair. It downloads the subscription, strips it into a minimal Mihomo config, writes it to `/opt/luma/egress-gateway/config.yaml`, deploys `egress_mihomo`, and configures Docker daemon proxy:

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

Service runtime proxy is opt-in per service. Declare `proxy: true` in the service manifest; do not hand-write the egress network or default proxy env unless you need to override them:

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
```

Luma attaches the service to the `egress` overlay network and injects `HTTP_PROXY=http://egress_mihomo:7890` plus `HTTPS_PROXY=http://egress_mihomo:7890` when those env vars are not already set. Scheduling still follows the service `region`.

Refresh subscription output later:

```bash
luma egress refresh
```

## 6. Create A Service

Interactive mode:

```bash
luma service new
```

Manual manifest:

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

To pin a service to one machine, add `node` with the Luma node name passed to `luma node join --name`:

```yaml
region: home
node: home-mac-mini
```

## 7. Deploy

Default deploy path:

```bash
luma deploy app.yaml
```

This submits the manifest to the logged-in Luma Control endpoint. The manager renders generated files under `/opt/luma`, syncs DNS, creates or updates the stack through the Portainer API, and probes the public route for `cn-edge` and `external-edge` services.

`luma deploy` prints client-side progress and each control-plane step. A public route probe reports the HTTP status from `/`; `404` means the route reached the application but the app may not serve a root page. The client waits up to 1800 seconds by default because first deploys may pull large images through the manager. Override it when needed:

```bash
luma deploy app.yaml --timeout 3600
```

Repeated deploys are updates. The same service `name` maps to the same Portainer stack; running deploy again rewrites the generated stack file and updates that stack. Changing `name` creates a different stack.

Preview without side effects:

```bash
luma deploy app.yaml --dry-run
```

Submit to the control plane, render/write files on the manager, but skip DNS sync and Portainer deployment:

```bash
luma deploy app.yaml --skip-dns --skip-portainer
```

Remove a service or Compose application by its deployed name:

```bash
luma service remove app
```

Luma Control uses the manifest recorded during the last successful deploy. This deletes the Cloudflare DNS record for public services, removes the Portainer stack, and deletes generated manager files such as `/opt/luma/stacks/<region>/<service>` and `routes/<service>.yml` for `tailscale-relay`. The same command removes single-service and Compose deployments. Preview first or keep DNS when needed:

```bash
luma service remove app --dry-run
luma service remove app --skip-dns
```

## 8. Exposure Modes

`cn-edge`:

```text
user -> Cloudflare DNS -> CN Traefik -> cn service
```

Use this for domestic public services.

`tailscale-relay`:

```text
user -> Cloudflare DNS -> CN Traefik -> Tailscale -> home service
```

Use this for low-frequency home services that should still share the same public domain experience.

`cloudflare-tunnel`:

```text
user -> Cloudflare -> cloudflared -> service
```

Use this for home services that should not depend on the CN edge.

`external-edge`:

```text
user -> Cloudflare DNS -> global edge -> global service
```

Use this for overseas services that need external network access and a public endpoint.

`none`:

No public entrypoint. Use it for workers and internal services.

## 9. Diagnose

```bash
luma doctor
```

Each failed check includes a concrete fix command or environment variable.

## 10. First Real Smoke Test

Use the reference node first:

```bash
luma doctor
luma bootstrap manager --domain luma.example.com
luma egress setup
luma deploy examples/public-cn-service.yaml
```

Then check:

```bash
docker service ls
curl -I https://whoami.example.com
```

Rotate any token or subscription URL that has been pasted into chat or logs before open-sourcing the repository.
