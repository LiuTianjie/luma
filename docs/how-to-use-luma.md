# How To Use Luma

This is the operating manual for the first public version of Luma.

Luma keeps five concepts visible:

```text
node / region / exposure / egress / service
```

Portainer is the default deployment control plane. Tailscale is a control-plane network and a relay option for home services. Cloudflare is the DNS provider and optional tunnel provider. Egress Gateway is only for outbound traffic such as pulling images, installing dependencies, or running services that need external network access.

## 1. Install The CLI

```bash
cd ~/infra-stacks
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
luma --help
```

## 2. Configure `luma.yaml`

`luma.yaml` is the only project config file Luma needs.

```yaml
project: itool

providers:
  dns:
    type: cloudflare
    zone: itool.tech
    zoneId: ac7105f330b0107c778ea8769bdfdc00
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    recordType: A
    ttl: 1
    proxied: false
  portainer:
    webhookUrlEnv: PORTAINER_WEBHOOK_URL

nodes:
  aly:
    host: aly
    publicIp: 8.130.148.30
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/turning4th
  stackRoot: stacks
  routesRoot: routes
  publicNetwork: public
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
```

Secrets are environment variables:

```bash
export CLOUDFLARE_API_TOKEN='...'
export PORTAINER_WEBHOOK_URL='...'
export EGRESS_SUBSCRIPTION_URL='...'
export LUMA_SUDO_PASSWORD='...'
```

Do not commit secrets.

## 3. Bootstrap The First Node

For a single public server that runs Swarm manager, Traefik, Portainer, and egress:

```bash
luma node bootstrap aly --profile single-node
```

This does:

- installs Docker and Compose;
- initializes Docker Swarm if needed;
- creates `public` and `egress` overlay networks;
- applies node labels;
- creates `/opt/luma/routes` and `/opt/luma/egress-gateway`;
- deploys Traefik;
- deploys Portainer;
- configures UFW for SSH, 80, 443, 9443, and blocks inbound 7890.

If only Portainer needs repair:

```bash
luma portainer setup aly
```

## 4. Connect Cloudflare

```bash
export CLOUDFLARE_API_TOKEN='...'
luma cloudflare connect --zone itool.tech
```

The command verifies the token, finds the zone, and writes `providers.dns.zoneId` back to `luma.yaml`.

For `cn-edge` services, DNS defaults to the public IP of the configured edge node. A service can override this with:

```yaml
dns:
  target: 203.0.113.10
```

## 5. Set Up Egress

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup aly
```

This downloads the subscription, strips it into a minimal Mihomo config, writes it to `/opt/luma/egress-gateway/config.yaml`, deploys `egress_mihomo`, and configures Docker daemon proxy:

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

Refresh subscription output later:

```bash
luma egress refresh aly
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
public: true
exposure: cn-edge
domain: app.itool.tech
port: 3000
replicas: 2
```

## 7. Deploy

Default production path:

```bash
luma deploy app.yaml --commit --push
```

This renders the stack, syncs DNS, commits, pushes, and triggers Portainer.

Preview without side effects:

```bash
luma deploy app.yaml --dry-run
```

Generate local files without DNS or Portainer:

```bash
luma deploy app.yaml --skip-dns --skip-webhook
```

Emergency deploy when Portainer is unavailable:

```bash
luma deploy app.yaml --direct --node aly
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
luma doctor --deep
```

Each failed check includes a concrete fix command or environment variable.

## 10. First Real Smoke Test

Use the reference node first:

```bash
luma doctor
luma node bootstrap aly --profile single-node
luma egress setup aly
luma deploy examples/public-cn-service.yaml --commit --push
```

Then check:

```bash
docker service ls
curl -I https://whoami.itool.tech
```

Rotate any token or subscription URL that has been pasted into chat or logs before open-sourcing the repository.
