# Operations

Resource history, resumable logs, disk samples, alerts and authenticated Prometheus scraping are described in [Observability](./observability.md). For the single Manager SQLite store, history retention, backups and Manager recovery, see [Control storage and recovery](./control-storage.md).

Day-2 tasks for running applications and nodes. Deploying is covered in [Deploying applications](deploying.md).

## Inspect Applications

```bash
luma status                                   # cluster, nodes and application health
luma app list --region cn                     # services and replica health
luma app show api                             # one application or service
luma app events api                           # recent runtime events of the latest allocation
luma app logs api --tail 100
luma app logs api --allocation <alloc-id> --previous
luma app logs api --follow --format ndjson
```

`--tail` is a 1-500 line budget shared by all selected sources. Text output labels every line with `[allocation/task/stream]`; `[partial]` and `[continued]` mark fragments. Use JSON or NDJSON to keep the source and cursor metadata. `--follow` resumes from the last cursor after a dropped connection (backoff up to 15 seconds, at most eight reconnects without progress); press Ctrl-C to stop.

Control also keeps a searchable history of build and deployment attempts, separate from application logs:

```bash
luma app history api --kind deployment --status failed
luma app history --source dashboard --since 2026-09-01T00:00:00+08:00 --format json
luma app history --id <record-id> --kind deployment
luma build logs <build-id>
```

Pages hold 50 records by default (maximum 100); pass `nextCursor` as `--cursor` with the same filters. Retention is described in [Control storage](control-storage.md).

On the manager, Nomad's own tools work too: `nomad job status <app>`, `nomad alloc logs -f <alloc-id>`.

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

## Roll Back

Nomad keeps a version history per job, so Luma exposes runtime rollback through both the dashboard and CLI.

From the dashboard, open `https://<control-domain>/dashboard/`, choose **Applications -> Versions**, inspect the job versions, then choose a previous version and confirm rollback.

From the CLI:

```bash
luma app versions <app>
luma app rollback <app>
luma app rollback <app> --to-version <N>
```

`luma app versions` lists prior versions of the Nomad job (`GET /v1/job/<id>/versions`). `luma app rollback` reverts to the previous version, or the version chosen with `--to-version`, through Nomad job revert (`POST /v1/job/<id>/revert`). Jobspecs also render `update { auto_revert = true }`, so a new version that fails its health checks rolls back to the last healthy version automatically.

This is a running job rollback. It does not rewrite Git history, change the stored manifest/YAML in Luma Control, reverse database migrations, or restore volume contents. Compose rollback applies to the whole Compose job/stack. Use pinned image tags or digests for production; mutable tags such as `latest` can make an old Nomad job version pull newer bytes.

Git-first path, when you want the manifest and the running job to stay in sync:

```bash
git revert <deploy-commit>
luma deploy <app>.yaml
```

## Restart A Service

Restart a running deployment without pulling a new image or changing its stored manifest. Restart is a delivery reconcile, not merely a process signal: Control waits for replacement allocations, refreshes Nomad CNI host-port state, reconstructs HTTP/TCP route files from the stored deployment record and actual allocation node, synchronizes DNS, and verifies every public HTTP endpoint before returning success.

```bash
luma app restart <app>
luma app restart <app> --service <task>
luma app restart <app> --mode task
```

There are two restart modes:

- `recreate` — stops the allocation so Nomad reschedules a fresh one (picks up placement/rescheduling). This is the default for a whole stack.
- `task` — restarts the task in place inside the existing allocation. This is the default when `--service` targets one task.

Omitting `--mode` uses `recreate` for the whole stack and `task` when `--service` is set; pass `--mode` explicitly to override. For a Compose application, `luma app restart <app> --service <svc>` restarts one service's task in place, while `luma app restart <app>` recreates every allocation in the stack. Use `--timeout <seconds>` (default `120`) to bound the control-plane response wait.


From Dashboard → Applications → application → Services, each running service has a **Shell** action. Nodes have their own Shell action under Infrastructure → Nodes. Both open dedicated terminal pages; leaving the page ends that browser session. That opens an interactive terminal in the service container through the node agent (`docker exec`), using the same terminal supervisor as node shells. It is for live diagnosis, not a substitute for logs or a restart. System stacks are blocked. Node agents need the `container-terminal` capability; update them if the action reports that the agent does not support container terminal.

The response includes `replacementAllocations` and a structured `delivery` result (`routes`, `dns`, and `probes`). A platform-managed deployment with no saved record reports delivery reconciliation as skipped; a managed public deployment does not report `delivery.status=ready` until its public probe succeeds. Reconciled file-provider HTTP routes use explicit priority so they safely override stale legacy Nomad-provider routes that advertise an unreachable provider-private node address.

Restart refuses the system stacks `traefik`, `egress`, and `luma-control` (Control runs inside the `luma-control` allocation, so cycling it from application management would kill Control itself).

## Remove A Deployment

Use the deployed service or Compose application name:

```bash
luma app remove <app>
```

The control plane uses the manifest recorded during the last successful deploy, deletes the Luma-managed Cloudflare DNS record for public services, deregisters and purges the Nomad job (`DELETE /v1/job/<id>?purge=true`), and deletes generated manager files. The same command removes single-service and Compose deployments. Because the control plane stores the manifest, this also works for deployments created through the web UI when the client no longer has a local YAML file. For `tailscale-relay`, it also deletes `/opt/luma/routes/<service>.yml`. For `cloudflare-tunnel`, Cloudflare Tunnel public hostname cleanup is skipped because that hostname is still managed in Cloudflare Zero Trust.

Storage data is preserved by default. To intentionally delete removable storage referenced by the recorded deployment, preview and then run:

```bash
luma app remove <app> --dry-run --delete-storage
luma app remove <app> --delete-storage
```

For single-service deployments, this deletes managed storage paths referenced by `storage.<volume>.path` and removes named Docker volume objects declared in the manifest; bind mounts are skipped. For Compose deployments, this deletes managed volume subdirectories referenced by the sidecar, not the storage class itself. It cannot be combined with `--skip-orchestrator`.

Preview the cleanup without changing the manager:

```bash
luma app remove <app> --dry-run
```

Keep DNS or the running Nomad job when you are doing a partial cleanup. `--skip-orchestrator` leaves the Nomad job in place:

```bash
luma app remove <app> --skip-dns
luma app remove <app> --skip-orchestrator
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

The request is handled by Luma Control on the manager. It deletes the Luma node registration and drains the matching Nomad client (`PUT /v1/node/<id>/drain`); a dead client is then garbage-collected by Nomad automatically. If Nomad returns 404 or `node not found` for that ID, Control skips drain (`nomadDrainSkipped: nomad_node_not_found`) and still deletes the Luma registration. Network, auth, and Nomad-unavailable errors still fail the remove. Use this for stale nodes that already left locally, failed joins, or decommissioned worker/home machines. Manager nodes (Nomad servers) are protected and must not be removed through this command.

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
luma manager egress
```

Verify image pulls on the target node:

```bash
sudo docker pull hello-world:latest
```

## Update Luma

```bash
luma update                                  # manager: CLI + Control; node: CLI + agent; client: CLI
luma update --install-ref v0.1.366           # a specific release, branch or full commit
luma update fleet                            # every non-manager node with a ready agent
luma update fleet --include-manager          # explicit repair only
```

Updates install the latest release unless `--install-ref` is given; use a full commit for a coordinated candidate rollout. The dashboard's **Nodes → Update center** is the preferred manager path: it mirrors the Control image into the internal registry first, then rolls out with progress and route checks. A direct `luma update` on the manager pulls the image itself; where GHCR is unreachable, set `LUMA_CONTROL_IMAGE` to a pullable image.

On the manager, `luma update` refreshes the firewall, Traefik, the Tailscale watchdog, Control configuration and the `luma-control` job (with Nomad auto-revert), then refreshes the local node agent. It does not restart Docker or the Nomad agent, run egress setup, or redeploy applications. Manager state under `/opt/luma/control` is root-only; without passwordless sudo run `sudo ~/.local/bin/luma update`.

On a joined node, `luma update` refreshes the CLI and the node agent. A node whose agent is too old for fleet updates is reported as skipped; run `luma update` on it once. For an old node without saved agent metadata, pass `--control-url https://luma.example.com --token <node-join-token>`.

Managers and nodes run a small Tailscale watchdog that restarts Tailscale after consecutive peer failures, without touching Docker, Nomad or applications.

## Repair The Control Plane

```bash
luma bootstrap --domain luma.example.com
luma doctor
```

Bootstrap is idempotent but touches Docker, the firewall, Traefik, Nomad and egress; treat a full rerun as a maintenance-window operation.

## Manager IP Change

Run on the manager, preview first:

```bash
luma manager ip-change --old 203.0.113.10 --new 203.0.113.20 --domain luma.example.com --dry-run
luma manager ip-change --old 203.0.113.10 --new 203.0.113.20 --domain luma.example.com
```

It verifies the new address over HTTPS, then updates only the manager's `publicIp`, `providers.dns.edgeTarget` and the Cloudflare A records that exactly match the old address. It backs up `luma.yaml`, reuses the running Control image and does not redeploy applications. Rerunning finishes any records left by a partial failure.

## Private Registries On New Nodes

`luma node join` and `luma node nomad-join` configure the cluster's managed HTTP registries after Docker and Tailscale are set up and before Nomad starts. On Linux, the endpoints are merged into Docker's `insecure-registries` and `NO_PROXY`, unrelated settings are preserved, a changed `daemon.json` is backed up, and Docker restarts only if needed. The join fails rather than leaving a node that cannot pull, and a node with running containers is refused (drain it first). macOS nodes are not changed.

`luma registry serve` records whether the managed registry uses HTTP or HTTPS. Public registries and private TLS endpoints are never downgraded to HTTP.

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

Nomad agents bind to `0.0.0.0` and advertise their Tailscale address. Luma-managed UFW rules only open `4646/4647/4648` on `tailscale0`; public isolation depends on those firewall rules, not on the advertise address. On hosts without UFW, configure equivalent host firewall rules and cloud security groups, and verify the ports are unreachable through the public interface.

## Tailscale Relay

`tailscale-relay` is explicit per service. It is suitable for home tools, previews, or low-frequency internal panels that need a public domain.

It is not the default path for normal public traffic.

## Node agent fails after an installer path change

A Nomad node can remain `ready` while its Luma agent is offline. Before installing
missing Python packages, inspect the service's actual `ExecStart`, its launcher,
and the Python environment it selects. Hosts may have a working installation
under `/opt/luma-cli` alongside an incomplete user-local installation.

The installer validates the target runtime before publishing its command shim or
refreshing the node agent service: dependency imports, CLI/agent command loading,
and `pip check` must succeed. A failed package installation can use source fallback
only when these checks pass. The shim runs the validated Python with `-m luma.cli`,
not an existing console script whose shebang may refer to a different environment.
Validation failure returns nonzero and does not publish an installation-success
message, replace the shim, or refresh/restart the service.

This publication gate is not a transactional rollback of source or package changes
already made inside the target install directory. For an affected existing host,
restore the service to a verified working environment, then check both systemd
restart counts and fresh Control heartbeats; a running process alone does not prove
that the agent has reconnected.
