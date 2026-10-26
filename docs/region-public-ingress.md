# Public ingress for a custom region

A region such as `render` can keep its existing nodes and scheduling identity while allowing selected HTTP services to use `tailscale-relay`. The public path is:

```text
Client -> public HTTPS / Traefik -> Tailscale -> render node -> application
```

Enable ingress on the region, then select it on the application. Workers and databases with `exposure: none` stay unpublished. Changing ingress does not change the region's egress policy or redeploy existing jobs. This uses the existing Nomad architecture.

## Upgrade the cluster

Use one published release for the CLI, Control and node agents. Wait for that release's Python package and Control image workflows to succeed before installation. A CLI-only upgrade cannot change the server's validation or jobspec renderer. Before upgrading, ensure the Manager can accommodate Control's 4 GiB reservation and other system services. Verify the resulting job as described in [Control memory](operations.md#control-memory).

1. In Dashboard → Settings → System maintenance, capture the current public-route baseline. Record the current release ref and Control image, and take the private backup described in [Control storage](control-storage.md#backup-scope).
2. Install the CLI release locally and in CI:

```bash
luma update --install-ref v0.2.18
python -m pip install --upgrade "luma-infra==0.2.18"
```

The first command updates the local Luma installation (and its agent on a joined node). The second is the equivalent package pin for a Python CI environment; do not install into an unrelated system Python just to run both.

3. In System maintenance, use the same release ref and image `ghcr.io/liutianjie/luma-control:v0.2.18`. Prepare the image through the configured Builder, then update Control. Keep the verified internal registry digest and the operation record. The manager updater refreshes its CLI and agent as well. Verify the automatic route sentinel after reconnecting.
4. Update the remaining nodes from the same page, or run:

```bash
luma update fleet --install-ref v0.2.18 --all
luma node list
luma version
```

Success requires a fresh heartbeat reporting the release on every node; a queued task or installer exit alone is insufficient. Control retries a failed node up to five times; nodes still offline or failed afterwards need repair before calling the fleet complete. The manager was handled in step 3; `--include-manager` is for explicit repair.

The new Control advertises `region-exposures-v1` and `macos-compose-network-v1` in `luma version` / `/v1/health`. No new node-agent protocol is required for Compose networking: Control delivers the trusted lifecycle tasks with the jobspec. Existing jobs keep their previous network until intentionally redeployed. For CLI-only manager recovery and version-compatible rollback, use the [release guide](maintainers/release.md#candidate-manager-upgrade-and-rollback).

## Enable render ingress

For an existing region:

```bash
luma region update render --exposure tailscale-relay
luma region list
```

The result must include `none,tailscale-relay`. `--exposure` replaces the allowed ingress list; repeat it to retain other modes. `none` is always retained. Omitting `--egress` preserves the current outbound policy. Built-in regions remain read-only. Dashboard → Nodes → Regions also supports enabling HTTP relay.

For a new region, create it before joining nodes:

```bash
luma region create render --egress direct --exposure tailscale-relay
```

Region configuration allows an ingress mode; the target node still needs working Tailscale, Docker, a ready Nomad client and access to the managed registry. Traefik and DNS keep using the existing public edge. Application ports need only be reachable from that edge through the tailnet; no router port forwarding to the Mac is needed.

## Native API manifest

A native single-service API on a Mac retains Docker host networking. Set the application's actual listening port and omit `publishPort`. Give test and production different listening ports if they share a node.

```yaml
name: api-test
image: registry.example.com/api:release
region: render
node: mac-worker-1
exposure: tailscale-relay
domain: app-test.example.com
port: 18080
replicas: 1
env:
  PORT: "18080"
healthcheck:
  test: [CMD, curl, -f, http://127.0.0.1:18080/healthz]
  interval: 15s
  timeout: 5s
```

Replace the image, domain, command/environment and health path with the application's real contract. `PORT` is only an example; it does not reconfigure applications that ignore that variable. Keep application authentication in place. The route resolves from the stable node name and its recorded Tailscale address.

```bash
luma validate luma.api.test.yml
luma deploy luma.api.test.yml --dry-run
```

After reviewing the preview, deploy through the application's recorded image/local-build/import workflow. Do not deploy a source placeholder image instead of building the intended revision.

## Mac Compose networking

For a multi-service or public Compose stack on a Mac, use one node pin for the group. A local-storage owner chosen by Control can provide that pin. Luma creates a private Docker namespace for each allocation inside OrbStack. Sibling service names resolve on its private loopback; only explicitly exposed ports are published. Application containers do not receive the Docker socket. Linux keeps its existing Nomad CNI networking.

```yaml
# compose.yml
services:
  api:
    image: registry.example.com/api:release
    environment:
      PORT: "8080"
      DATABASE_URL: postgres://app:${DB_PASSWORD}@database:5432/app
  database:
    image: postgres:17
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: app
    volumes:
      - app-test-db:/var/lib/postgresql/data
volumes:
  app-test-db: {}
```

```yaml
# luma.compose.yml
name: app-test
compose: compose.yml
region: render
services:
  api:
    node: mac-worker-1
    exposure: tailscale-relay
    domain: app-test.example.com
    port: 8080
    publishPort: 18080
  database:
    exposure: none
```

This forwards `mac-worker-1:18080` to the API's container port `8080`. A second deployment can keep `port: 8080` and use `publishPort: 18081`; it must have a different deployment name and a different volume identity for independent data. Bare Docker volume names are preserved, not automatically scoped. Databases at `database:5432` are reachable only by siblings in that allocation. Workers deployed separately need a deliberately reachable private database endpoint; a Compose service name does not cross allocation boundaries.

Published Compose stacks require one replica on the pinned node. Internal single-service worker pools can still use region scheduling and multiple replicas. The new Mac path supports `none`, `tailscale-relay` and `tcp-relay`; explicit all-service `network_mode: host` remains an internal-only opt-in. Do not add Compose `ports` or host networking to the public example.

The helper defaults to `python:3.13-alpine`, cached separately through the Builder into the internal registry. To pin another compatible Python image, use `defaults.images.composeNetwork` in the manager configuration. Docker publishes declared ports on the VM wildcard address so OrbStack can forward them to macOS; host firewall rules are responsible for restricting direct LAN access. Automatic cleanup removes only the namespace owned by that allocation. Restarts and replacements recreate it through Nomad lifecycle hooks.

```bash
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
```

## Verify and roll back

Check the target allocation, edge-to-node connectivity, DNS and public HTTPS in that order. Verify the real health endpoint, authenticated APIs, and any SSE streams or HTTP Range downloads supported by the application. Test from outside the tailnet and from the edge; a healthy allocation does not prove public reachability. Files served directly by object storage keep their existing URLs.

```bash
curl --fail --show-error https://app-test.example.com/healthz
curl --no-buffer --max-time 15 https://app-test.example.com/events
curl --fail --show-error -D - -H 'Range: bytes=0-1023' \
  https://app-test.example.com/files/example.bin -o /dev/null
```

Adapt the paths and authentication to the real application. A finite SSE timeout is expected for a persistent stream. Recreate a disposable test allocation and confirm its route recovers and another deployment remains reachable before migrating stateful applications.

If the application rollout fails, restore its recorded prior revision first. To disable region relay, remove or redeploy all users of that exposure before updating the region:

```bash
luma region update render --exposure none
```

Control rejects this change while saved native or Compose deployments still use relay, including partial failures. A Control rollback does not rewrite existing jobs or application data; preserve the matching job artifacts and previous install ref/image.

Maintainers can reproduce the local Mac checks with an isolated Nomad binary and an already available Python image:

```bash
python scripts/check-macos-compose.py --nomad /path/to/nomad --image python:3.13-alpine
```

It verifies Luma's rollout health gate, remapped ports, sibling resolution, two-stack isolation, SSE, Range, cleanup and recreation on OrbStack, and removes its own jobs, containers and temporary agent data. It does not contact the configured live cluster or prove that cluster's public DNS/TLS path.
