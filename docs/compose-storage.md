# Compose Deployments And Local Storage

Persistent data belongs to the node running the application. New dashboard templates use local storage; no NFS server or storage class is needed. Control chooses a ready node in the deployment region (or uses the requested node), records that owner before submitting the job, and pins subsequent deployments to it. An unavailable owner blocks placement rather than starting an empty database elsewhere.

A multi-service Compose stack runs together on one node, with one allocation. A mount-free single-service stack can run multiple allocations in its region. Read-only configuration mounts and Docker sockets do not create persistent-data ownership. Writable local mounts require stable node placement. Native services with persistent mounts require `replicas: 1`; persistent workloads do not use canary promotion that could start two writers against one directory.

## Replicas

Luma resolves `services.<name>.deploy.replicas` in Docker Compose and
`services.<name>.replicas` in the Luma sidecar to Nomad group `Count`.
Either declaration is sufficient; if both are present they must agree.
Compose `scale` is also accepted and must agree with any other declaration.
The default is `1`. Counts must be positive integers; zero, booleans, floats,
null and conflicting declarations fail validation, including import validation.
Only `deploy.mode: replicated` is supported.

Multiple replicas require exactly one service, no Compose volume mounts and no
sidecar volume declarations. This includes read-only, anonymous and legacy NFS
mounts: replicated mount semantics are not supported yet. A multi-service stack
requires every service to use one replica, even when all requested counts are
equal. Siblings share loopback inside one allocation; scaling its group would
also duplicate databases and schedulers. Put independently scaled workers in
separate deployments using externally reachable dependencies.

```yaml
# docker-compose.yml
services:
  worker:
    image: registry.example.com/worker:1
    deploy:
      replicas: 6
      resources:
        reservations: {memory: 2048M}
        limits: {memory: 2048M}
```

```yaml
# luma.compose.yml
name: worker-pool
region: render
services:
  worker:
    exposure: none
    replicas: 6
```

Create the custom region before deploying. With no `node` pin, Nomad can place
each allocation on any eligible node in that region; an even split is not
guaranteed. An explicit pin constrains all replicas to that node. Resource
reservations and container process counts apply **per replica**. Six allocations
with a 2 GiB reservation require 12 GiB of schedulable memory; spare physical
memory alone does not prove scheduling capacity.

Replicated services support bridge networking with `exposure: none`, or dynamic
`cn-edge` / `external-edge` ports. Host networking, relay exposures, `publishPort`,
Compose `ports` and `container_name` are rejected. `deploy.placement`,
`deploy.update_config` and `deploy.rollback_config` are rejected for multiple
replicas: use Luma region/node placement and its managed rolling update policy.
Updates retain `MaxParallel: 1`, health gating, auto-revert and `stop_grace_period`;
multiple replicas do not add a canary. Rollout success requires all requested
allocations of the submitted job version to be healthy.

This fix requires upgrading **Control**; installing only a newer CLI cannot
change the jobspec rendered by an older server. An upgrade does not resubmit
existing jobs. After upgrading, preview the saved deployment, check `Count`,
then deliberately redeploy through the application's existing workflow to apply
the requested count. Existing applications remain untouched until that action.

## Compose Example

Keep the application file standard:

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pg-data:/var/lib/postgresql/data
volumes:
  pg-data: {}
```

Use a local path in the Luma sidecar:

```yaml
# luma.compose.yml
name: app-stack
compose: docker-compose.yml
region: cn
services:
  postgres:
    exposure: none
volumes:
  pg-data:
    local:
      path: /srv/luma/data/app-stack/pg-data
```

`local.node` is optional for Control deployments. The path follows the deployment node; there is no separate storage-node choice. To select the first node explicitly, set `services.postgres.node`. Conflicting node pins are rejected. Control saves the resolved pins and local volume node in the deployment configuration. Existing directories keep their ownership and permissions, including PostgreSQL's restrictive data-directory mode.

The dashboard generates `/srv/luma/data/<deployment-slug>/<volume-name>` for new Compose volumes. Use distinct paths for distinct data sets. Bare named Docker volumes are also supported and pin the deployment to their owning node; their existing names are preserved on updates.

## Native Service Example

```yaml
name: app-postgres
image: postgres:17
region: cn
exposure: none
replicas: 1
volumes:
  - app-postgres-data:/var/lib/postgresql/data
```

Control records the selected node. The dashboard scopes new native volume names by deployment. Existing volume names and bind paths retain their identities: replacing a source at the same mount target is blocked until the data is explicitly migrated. A node rename or lost registration must be reconciled with the existing data owner before deployment.


## Host networking

Ordinary Compose groups share a Nomad bridge network namespace so sibling
service names resolve on `127.0.0.1`. A stack that must scrape host-loopback
listeners (Traefik metrics, local Nomad HTTP) may set `network_mode: host` on
**every** service. Mixed host/bridge in one group is rejected. Host-network
Compose requires `exposure: none` and cannot set `publishPort`; it is for
internal collectors such as [`luma-observe`](../observe/), not public apps.

## Deploy And Update

```bash
luma login https://luma.example.com --token <management-token>
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --format ndjson
```

Reusing the same deployment name updates its existing job and storage owner. Changing a deployment name creates another deployment. The Control preview resolves saved ownership and current node candidates; deployment additionally inspects the existing Nomad job and allocations to verify legacy ownership. Direct offline `compose render` needs an explicit local node because it cannot persist a placement decision.

Application settings remain in the standard Compose file. The sidecar carries region, exposure, routing and storage. See [deployment-yaml.md](deployment-yaml.md) for routing fields.

## Migrate Existing NFS Data

Backend changes do not copy data automatically. First inspect the actual running mount: old configuration may differ from what an earlier renderer mounted. Record the application node, source directory or Docker volume, numeric ownership, permissions, data size and current job/configuration.

1. Prepare the destination on the existing application node and check free space.
2. Capture a recoverable backup. Stop every writer before the final copy. Use database-native backup/restore when appropriate; copying an active database directory is not a consistent backup.
3. Copy while preserving numeric ownership and permissions, or reuse the same host directory when NFS exports it from the application node itself.
4. Verify data and permissions; configure the local destination and set `adopted: true` on that volume. Remove any old `initialize: empty` acknowledgement.
5. Redeploy, check application/database health and read back representative data. Keep the original data and saved job/configuration for rollback. After new writes, rollback also requires reconciling those writes.

Example destination:

```yaml
volumes:
  pg-data:
    local:
      node: manager
      path: /srv/luma/data/app-stack/pg-data
    adopted: true
```

Print a manual migration plan:

```bash
luma storage migrate luma.compose.yml \
  --volume pg-data --from-node old-storage-node --from-volume old-docker-volume
```

This command does not stop services or copy data. For a local destination it requires an explicit deployment/local node. `adopted: true` acknowledges verified data; it does not allow moving an established local owner to another node. Moving that owner requires an operator-managed data migration and ownership reconciliation.

## Legacy NFS Compatibility

Existing `storageClass` references remain usable while data is migrated. Registered classes remain manager-owned; non-empty `storageClasses` in submitted sidecars are rejected. `luma storage list`, `check`, `apply`, and `remove` continue to operate on those legacy classes. No class is required for local storage.

Do not remove a class, unmount volumes or disable the NFS server until every active and stopped deployment has been checked. An application with no running allocations can still depend on its old data. Keep offline-node data in place until that node can be inspected.

## Removal And Recovery

`luma app remove <name>` removes the application job and routes but retains data by default. Avoid `--delete-storage` during migrations. Removing a deployment can also remove its saved owner, so save its pinned configuration before removal and use it when restoring the application.

Local storage does not provide cross-node failover. Recover the original node or restore a verified backup and explicitly reconcile placement. Maintain backups independently of the application's disk.
