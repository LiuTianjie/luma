# Operations

Luma Control is the self-hosted API on the manager node that handles login tokens, node registration, DNS sync, job rendering, and Nomad deployment calls. `luma deploy` talks to Luma Control; it does not SSH into a node to deploy. The orchestrator underneath is HashiCorp Nomad, so the unit of deployment is a Nomad job.

The default path is:

```text
service.yaml -> luma deploy -> Luma Control -> render jobspec -> sync DNS -> Nomad API (/v1/jobs) -> docker driver
```

Luma deploys through the Nomad HTTP API.

## Add A Service

```bash
luma service new
luma deploy <service>.yaml --dry-run
luma login https://luma.example.com --token <management-token>
luma deploy <service>.yaml
luma doctor
```

For a hand-written manifest, keep the same fields:

```yaml
name: api
image: ghcr.io/me/api:2026-05-29-1
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
```

For multi-service applications, keep `docker-compose.yml` standard and add a Luma sidecar:

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml
```

Storage services are registered in Luma Control with `luma storage set`; compose deployments only reference them by name. Luma Control rejects deployment-side storage class definitions, and backend switches require either verified `adopted: true` or `initialize: empty`. Storage ownership, local node pins, and NFS storage classes are described in `docs/compose-storage.md`.

## Update Image Tag

Change the manifest:

```yaml
image: ghcr.io/me/api:2026-05-29-2
```

Then deploy:

```bash
luma deploy api.yaml
```

## Scale Replicas

Change the manifest:

```yaml
replicas: 3
```

Then deploy:

```bash
luma deploy api.yaml
```

Temporary scale from a manager node:

```bash
nomad job scale api 3
```

Temporary commands do not update Git. Commit the manifest change afterward if it should persist.

## Pin To One Node

Use this only when the service depends on local disk, hardware, or a specific home/worker machine:

```yaml
region: home
node: home-mac-mini
```

`node` must be the Luma node name passed to `luma node join --name`. Luma renders it as a Nomad constraint on `${node.unique.name}` (or `meta.luma_node_name`) and still keeps the `region` constraint on `${meta.region}`, so the selected node must also be in that region. The Nomad node identity is stable across rejoins, so pinned placement does not need to be refreshed.

## View Status

From a client:

```bash
luma status
luma context list
luma context use <cluster-id>
```

`luma status` shows the orchestrator (Nomad), the server leader, and nodes with `role=client`.

From a manager node:

```bash
nomad job status <service>
nomad job status -verbose <service>
nomad alloc logs -f <alloc-id>
```

## Roll Back

Nomad keeps a version history per job, so Luma exposes runtime rollback through both the dashboard and CLI.

From the dashboard, open `https://<control-domain>/dashboard/`, choose **Applications -> Versions**, inspect the job versions, then choose a previous version and confirm rollback.

From the CLI:

```bash
luma history <service>
luma rollback <service>
luma rollback <service> --to-version <N>
```

`luma history` lists prior versions of the Nomad job (`GET /v1/job/<id>/versions`). `luma rollback` reverts to the previous version, or the version chosen with `--to-version`, through Nomad job revert (`POST /v1/job/<id>/revert`). Jobspecs also render `update { auto_revert = true }`, so a new version that fails its health checks rolls back to the last healthy version automatically.

This is a running job rollback. It does not rewrite Git history, change the stored manifest/YAML in Luma Control, reverse database migrations, or restore volume contents. Compose rollback applies to the whole Compose job/stack. Use pinned image tags or digests for production; mutable tags such as `latest` can make an old Nomad job version pull newer bytes.

Git-first path, when you want the manifest and the running job to stay in sync:

```bash
git revert <deploy-commit>
luma deploy <service>.yaml
```

## Restart A Service

Restart a running deployment without pulling a new image or changing its stored manifest. Restart is a delivery reconcile, not merely a process signal: Control waits for replacement allocations, refreshes Nomad CNI host-port state, reconstructs HTTP/TCP route files from the stored deployment record and actual allocation node, synchronizes DNS, and verifies every public HTTP endpoint before returning success.

```bash
luma service restart <stack>
luma service restart <stack> --service <task>
luma service restart <stack> --mode task
```

There are two restart modes:

- `recreate` — stops the allocation so Nomad reschedules a fresh one (picks up placement/rescheduling). This is the default for a whole stack.
- `task` — restarts the task in place inside the existing allocation. This is the default when `--service` targets one task.

Omitting `--mode` uses `recreate` for the whole stack and `task` when `--service` is set; pass `--mode` explicitly to override. For a Compose application, `luma service restart <app> --service <svc>` restarts one service's task in place, while `luma service restart <app>` recreates every allocation in the stack. Use `--timeout <seconds>` (default `120`) to bound the control-plane response wait.

The response includes `replacementAllocations` and a structured `delivery` result (`routes`, `dns`, and `probes`). A platform-managed deployment with no saved record reports delivery reconciliation as skipped; a managed public deployment does not report `delivery.status=ready` until its public probe succeeds. Reconciled file-provider HTTP routes use explicit priority so they safely override stale legacy Nomad-provider routes that advertise an unreachable provider-private node address.

Restart refuses the system stacks `traefik`, `egress`, and `luma-control` (Control runs inside the `luma-control` allocation, so cycling it from application management would kill Control itself).

## Remove A Deployment

Use the deployed service or Compose application name:

```bash
luma service remove <service>
```

The control plane uses the manifest recorded during the last successful deploy, deletes the Luma-managed Cloudflare DNS record for public services, deregisters and purges the Nomad job (`DELETE /v1/job/<id>?purge=true`), and deletes generated manager files. The same command removes single-service and Compose deployments. Because the control plane stores the manifest, this also works for deployments created through the web UI when the client no longer has a local YAML file. For `tailscale-relay`, it also deletes `/opt/luma/routes/<service>.yml`. For `cloudflare-tunnel`, Cloudflare Tunnel public hostname cleanup is skipped because that hostname is still managed in Cloudflare Zero Trust.

Storage data is preserved by default. To intentionally delete removable storage referenced by the recorded deployment, preview and then run:

```bash
luma service remove <service> --dry-run --delete-storage
luma service remove <service> --delete-storage
```

For single-service deployments, this deletes managed storage paths referenced by `storage.<volume>.path` and removes named Docker volume objects declared in the manifest; bind mounts are skipped. For Compose deployments, this deletes managed volume subdirectories referenced by the sidecar, not the storage class itself. It cannot be combined with `--skip-orchestrator`.

Preview the cleanup without changing the manager:

```bash
luma service remove <service> --dry-run
```

Keep DNS or the running Nomad job when you are doing a partial cleanup. `--skip-orchestrator` leaves the Nomad job in place:

```bash
luma service remove <service> --skip-dns
luma service remove <service> --skip-orchestrator
```

If the control plane is unavailable, remove the Nomad job directly on the manager, then remove generated files:

```bash
nomad job stop -purge <service>
sudo rm -rf /opt/luma/stacks/<region>/<service>
sudo rm -f /opt/luma/routes/<service>.yml
```

## Remove A Node

From any logged-in client:

```bash
luma node remove <node-name>
```

The request is handled by Luma Control on the manager. It deletes the Luma node registration and drains the matching Nomad client (`PUT /v1/node/<id>/drain`); a dead client is then garbage-collected by Nomad automatically. Use this for stale nodes that already left locally, failed joins, or decommissioned worker/home machines. Manager nodes (Nomad servers) are protected and must not be removed through this command.

Because the Nomad node identity is a stable UUID, a worker/home machine that leaves and rejoins with the same Luma node name keeps the same `meta.luma_node_name`, so services pinned by Luma node name do not need a NodeID refresh. Keep manifests pinned by Luma node name; do not replace them with Docker hostnames.

## Drain A Node

```bash
nomad node drain -enable <node-id>
```

Restore it:

```bash
nomad node drain -disable <node-id>
```

## Refresh Egress

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress refresh
```

Verify image pulls on the target node:

```bash
sudo docker pull hello-world:latest
```

## Repair Control Plane

```bash
luma bootstrap manager --domain luma.example.com
luma doctor
```

## Required Network Ports

For Linux nodes, Luma configures UFW during bootstrap/join. Mirror the same access in cloud security groups or Tailscale ACLs:

| Port | Required between | Purpose |
| --- | --- | --- |
| `80/tcp` | public clients -> edge manager | HTTP redirect and Let's Encrypt challenge. |
| `443/tcp` | public clients -> edge manager | HTTPS ingress for Luma Control and public services. |
| `tcp-relay` published ports | public clients -> edge manager | Public TCP relay ports, for example `3306/tcp` for MySQL. Luma restores Traefik listeners from Control state; cloud firewalls/security groups must allow the same ports. |
| `4646/tcp` | clients/Traefik -> Nomad server | Nomad HTTP API (deploy, status, service discovery). |
| `4647/tcp` | Nomad clients -> Nomad server | Nomad RPC. |
| `4648/tcp`, `4648/udp` | all Nomad agents | Nomad Serf gossip (server membership). |

Nomad agents bind to `0.0.0.0` but advertise their Tailscale address, and UFW only opens `4646/4647/4648` on the `tailscale0` interface, so the control plane is not exposed on public IPs.

## Tailscale Relay

`tailscale-relay` is explicit per service. It is suitable for home tools, previews, or low-frequency internal panels that need a public domain.

It is not the default path for normal public traffic.
