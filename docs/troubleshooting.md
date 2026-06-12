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
luma doctor --deep
```

For private registries, separate reachability, Docker daemon proxying, and auth:

```bash
# run these on the node that receives the service task
docker info | grep -i proxy -A3
curl -vk https://<registry-host>/v2/
docker pull <registry-host>/<org>/<image>:<tag>
```

If `curl /v2/` returns `401` with `docker-distribution-api-version`, the registry is reachable from that node. If `docker pull` still fails with EOF/timeout before auth, check Docker daemon `HTTPProxy`/`HTTPSProxy` and ensure the private registry host is in daemon `NO_PROXY` on the same node. This is separate from manifest `proxy: true`, which only affects runtime outbound traffic from the container.

For services pinned to `home` or ARM nodes, make sure the image has the target platform:

```bash
docker buildx imagetools inspect <image>
```

Luma validates pulls for the target node platform and deploys the digest returned by that target-platform pull.

## Fleet update fails

If `luma update fleet` reports `HOME: parameter not set` or `HOME: unbound variable`, the node is running an older installer path from a service environment without `HOME`. Update the manager/CLI to a version with the installer HOME fallback, then rerun fleet update.

If a node reports `unsupported node agent task action: update-luma` or `node agent does not support fleet update`, that node's agent is too old to update itself through fleet tasks. Run once on that node:

```bash
luma update
```

If the node has no saved local agent metadata:

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

After that, future `luma update fleet` runs can update it remotely.

## Dashboard terminal disconnects

`terminal agent disconnected` means the browser session was connected, but the node-side terminal agent WebSocket disappeared. Common causes:

- the node agent process restarted because it lost the control API lease;
- multiple `node-agent terminal-supervisor` processes are running for the same node and replacing each other at the control plane;
- the node's agent token is stale, causing repeated `401 unauthorized` in `/var/log/luma-node-agent.err`.

Check on the node:

```bash
pgrep -af 'luma.*node-agent|terminal-supervisor'
tail -n 120 /var/log/luma-node-agent.err
```

A healthy node should have one `node-agent run` process and one `node-agent terminal-supervisor` child. If old orphan supervisors remain, clear them and restart the node agent:

```bash
sudo pkill -f 'node-agent terminal-supervisor'
sudo systemctl restart luma-node-agent.service
# macOS:
sudo launchctl kickstart -k system/io.luma.node-agent
```

Current Luma keeps the node agent alive across transient lease failures and uses a per-node lock so only one terminal supervisor runs.

## Swarm node is down but containers still run

If a home node such as a Mac mini shows `down` in Swarm but local containers are still running, check the manager-to-node tailnet TCP path before blaming the application:

```bash
docker node ls
docker node inspect <node-id> --format '{{.Status.State}} {{.Status.Message}} {{.Status.Addr}}'
tailscale ping <node-tailscale-ip>
nc -vz <node-tailscale-ip> 2377
nc -vz <node-tailscale-ip> 7946
```

If `tailscale ping` works but `2377` or `7946` times out, the tailnet data path can be wedged while Tailscale still appears online. Restart Tailscale on the side that cannot reach peer TCP:

```bash
sudo systemctl restart tailscaled
# macOS:
sudo launchctl kickstart -k system/W5364U7YZB.io.tailscale.ipn.macsys.network-extension
```

Manager and node updates install Tailscale watchdogs that perform these checks and restart local Tailscale only after consecutive failures.

## Not logged in

If deploy prints `not logged in`, authenticate against the manager's control API:

```bash
luma login https://luma.example.com --token <management-token>
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

## macOS node join fails at Docker

macOS workers and home nodes must have Docker Desktop installed and running before `luma node join`.
Luma cannot install Docker Desktop automatically.

Verify locally before joining:

```bash
command -v docker
docker info
```

If Docker Desktop is missing or still starting, `luma node join` stops before registering the node with Luma Control. Start Docker Desktop, wait until `docker info` succeeds, then rerun the same join command.

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
