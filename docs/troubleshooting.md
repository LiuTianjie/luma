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

## Manager update prints `tmpfs: Unknown parameter 'noswap'`

This message comes from the manager's Nomad client when it prepares an
allocation secrets directory, not from the Luma Control image. Nomad tries to
mount the per-task `secrets/` directory as tmpfs with `noswap`; older Linux
kernels do not support that mount option, so the kernel may print:

```text
tmpfs: Unknown parameter 'noswap'
```

First check whether the update actually failed or only printed the kernel
warning:

```bash
nomad job status luma-control
nomad job allocs luma-control
```

If an allocation is running, no repair is needed; rerun `luma update manager`
only if the command itself exited non-zero.

If the allocation failed with a task-dir or tmpfs mount error, check the manager
kernel and Nomad version:

```bash
uname -r
nomad version
journalctl -u nomad -n 120 --no-pager
```

The durable fix is to run a Nomad release that falls back when `noswap` is not
supported, or upgrade the manager kernel to one with `tmpfs noswap` support.
After repairing Nomad, restart the manager agent and rerun the control-plane
refresh:

```bash
sudo systemctl restart nomad
luma update manager
```

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

## Nomad client is disconnected but containers still run

This is expected behavior, not a failure. Each Luma job renders `max_client_disconnect = 1h`, so when a home node such as a Mac mini loses its tailnet path to the Nomad server, the client is marked `disconnected` but its local allocations keep running and reconnect cleanly when the link recovers. This is the whole point of running on Nomad: a transient WAN/DERP blip no longer kills and reschedules tasks.

Check the node and the server RPC path before blaming the application:

```bash
nomad node status
nomad node status -self
tailscale ping <node-tailscale-ip>
nc -vz <server-tailscale-ip> 4647
nc -vz <server-tailscale-ip> 4648
```

If `tailscale ping` works but `4647` (RPC) times out, the tailnet data path can be wedged while Tailscale still appears online. Restart Tailscale on the side that cannot reach peer TCP:

```bash
sudo systemctl restart tailscaled
# macOS:
sudo launchctl kickstart -k system/W5364U7YZB.io.tailscale.ipn.macsys.network-extension
```

Manager and node updates install Tailscale watchdogs that perform these checks and restart local Tailscale only after consecutive failures. If a client stays `disconnected` past its `max_client_disconnect` window, Nomad reschedules the allocations elsewhere (subject to the job's region/node constraints).

## Not logged in

If deploy prints `not logged in`, authenticate against the manager's control API:

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

## Nomad deploy fails

Rerun manager bootstrap so the Nomad server and `/opt/luma/control/control.json` are refreshed:

```bash
luma bootstrap manager --domain luma.example.com
```

If deploy fails before any allocation is placed, check that the Nomad server is up and has a leader:

```bash
nomad server members
nomad status               # leader + jobs
nomad node status          # clients ready, meta.region correct
```

If a job stays `pending` with `Placement Failures`, the constraints did not match a ready client. Inspect the failed evaluation:

```bash
nomad job status <service>
nomad eval status -verbose <eval-id>
```

Common causes are a `region` constraint that no ready client satisfies, a node pinned by `meta.luma_node_name` that is `disconnected`, an image whose platform does not match the target node, or exhausted CPU/memory on the only eligible client. On Apple Silicon clients, a misread CPU fingerprint can report near-zero `cpu.totalcompute` and block placement; the client needs `cpu_total_compute` set explicitly (Luma's node config handles this).

If the Nomad server itself is unreachable from Luma Control, confirm the RPC path between clients and the server:

```bash
nc -vz <server-tailscale-ip> 4647
nomad server members        # all servers alive, one leader
```

A wedged `4647/tcp` path makes clients drop to `disconnected` even though `docker info` on the node still works.

## Public route unhealthy / Traefik router not found

A deploy of a `cn-edge` or `external-edge` service can finish placing the Nomad allocation but then fail the public-route probe with:

```text
Public route unhealthy: https://myapp.example.com/ -> HTTP 404 (Traefik router not found)
```

This means the probe reached the edge but Traefik returned its own default `404 page not found` — Traefik has no router matching that host yet, as opposed to your application returning a `404` from a real route (an application `404` is reported as reachable, not as a failed route). It usually appears when Traefik has not yet picked up the freshly published file-provider route, or the route file's host/labels do not match the requested domain.

Luma writes the generated route file atomically: it validates the rendered Traefik file-provider route, stages it outside the watched routes directory, then publishes the final file in one move, so Traefik never observes a half-written route. On an unhealthy public route (Traefik router not found, or a transient `502`/`503`/`504`), Control runs a **Recover public route** step once — it recreates the service's allocation and re-probes — before failing the deploy. If it still fails after that automatic retry, check:

```bash
# on the manager
ls /opt/luma/routes/                      # the <service>.yml route file exists
cat /opt/luma/routes/<service>.yml        # host rule matches the requested domain
nomad job status <service>                # allocation is running/healthy
```

Confirm the manifest's `region` and `exposure` actually produce an edge route (only `cn-edge`/`external-edge` get a public Traefik router), that the `domain` matches the DNS record, and that Traefik itself is running. Re-running `luma deploy` republishes the route file.

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

## Nomad server is not reachable

Check:

```bash
nomad server members
ufw status
```

The Nomad HTTP API listens on `4646`, RPC on `4647`, and Serf gossip on `4648`, all bound to `0.0.0.0` but only opened on the `tailscale0` interface by UFW. If `ufw status` does not show those ports allowed on `tailscale0`, or `nomad server members` is empty, rerun bootstrap to repair the agent config:

```bash
luma bootstrap manager --domain luma.example.com
```
