# Troubleshooting

Start with:

```bash
luma preflight
luma doctor
```

If env checks fail, create or edit `.env`:

```bash
cp .env.example .env
$EDITOR .env
```

## Local CLI cannot be installed

Run:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

If `python3` is missing, install it first:

```bash
# macOS
brew install python

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl
```

Local Docker is optional on client machines. Install Docker only on servers that will run manager or worker workloads.

## `zsh: permission denied: luma`

Your shell is resolving the repository's `luma/` package directory instead of the installed CLI command.

Fix:

```bash
./scripts/install-luma.sh
. .venv/bin/activate
hash -r
which luma
luma preflight
```

Fallback:

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

## Tailscale is not logged in

Create an ephemeral or reusable auth key in Tailscale, then:

```bash
luma tailscale connect
```

## Docker image pulls fail

Fix:

```bash
luma egress setup
luma doctor --legacy-ssh --deep
```

## Not logged in

If deploy prints `not logged in`, authenticate against the manager's control API:

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
```

## Portainer deploy fails

Rerun manager bootstrap so Portainer is initialized and `/opt/luma/control/control.json` is refreshed:

```bash
luma bootstrap manager --domain luma.example.com
```

If deploy fails with `Unable to check for name collision` or `The agent was unable to contact any other agent located on a manager node`, inspect the Portainer agent placement first:

```bash
docker service inspect portainer_agent --format '{{json .Spec.TaskTemplate.ContainerSpec.Env}} {{json .Spec.TaskTemplate.Placement.Constraints}}'
docker service ps portainer_agent --no-trunc
```

Current Luma installs keep `tcp://tasks.agent:9001` compatible with existing Portainer endpoints, but constrain `portainer_agent` to Swarm manager nodes. If an older install still has worker agent tasks, rerun:

```bash
luma portainer setup
```

If worker nodes still need to run workloads, their Swarm networking must allow node-to-node `7946/tcp`, `7946/udp`, and `4789/udp`. A one-way `7946/tcp` timeout can make Portainer worker agents report that no manager agent exists, even when `docker node ls` shows the manager as ready.

If bootstrap fails with `Portainer authentication failed: HTTP 422 Invalid credentials`, Portainer already has
an admin password that does not match Luma's saved state. If you know the current password, bind it explicitly
and rerun:

```bash
export LUMA_PORTAINER_ADMIN_PASSWORD='...'
luma bootstrap manager --domain luma.example.com
```

If you do not know the current Portainer admin password, reset the Portainer admin password first:

```bash
docker service scale portainer_portainer=0
docker pull portainer/helper-reset-password
docker run --rm -v portainer_portainer_data:/data portainer/helper-reset-password
docker service scale portainer_portainer=1
```

Then rerun bootstrap with `LUMA_PORTAINER_ADMIN_PASSWORD` set to the new password printed by the helper.

If you intentionally use legacy Portainer webhooks, configure them on the manager:

```dotenv
PORTAINER_WEBHOOK_URL=...
PORTAINER_WEBHOOK_API=...
```

## Cloudflare DNS fails

Use a Zone-scoped API token:

```text
Zone / DNS / Edit
Zone / Zone / Read
Specific zone: your domain
```

Then:

```bash
luma cloudflare connect --zone example.com
```

## Sudo fails

Run bootstrap with sudo, configure passwordless sudo, or set:

```bash
LUMA_SUDO_PASSWORD=...
```

## Portainer is not reachable

Check:

```bash
docker stack services portainer
ufw status
```

The default Portainer HTTPS port is `9443`.

Repair:

```bash
luma portainer setup
```
