# Compose Deployments And Local Storage

Persistent data belongs to the node running the application. New dashboard templates use local storage; no NFS server or storage class is needed. Control chooses a ready node in the deployment region (or uses the requested node), records that owner before submitting the job, and pins subsequent deployments to it. An unavailable owner blocks placement rather than starting an empty database elsewhere.

A Compose stack runs together on one node. Read-only configuration mounts and Docker sockets do not create persistent-data ownership. Writable local mounts require stable node placement. Native services with persistent mounts require `replicas: 1`; persistent workloads do not use canary promotion that could start two writers against one directory.

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

## Deploy And Update

```bash
luma login https://luma.example.com --token <management-token>
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --format ndjson
```

Reusing the same deployment name updates its existing job and storage owner. Changing a deployment name creates another deployment. The Control preview resolves saved ownership and current node candidates; deployment additionally inspects the existing Nomad job and allocations to verify legacy ownership. Direct offline `compose render` needs an explicit local node because it cannot persist a placement decision.

Application settings remain in the standard Compose file. The sidecar carries region, exposure, routing and storage. See [deployment-yaml.md](deployment-yaml.md) for routing fields.

## LAE Runtime

LAE defaults to internal local storage when `LUMA_LAE_RUNTIME_STORAGE_CLASS` is unset or `local`. The host path is generated from the authenticated tenant and application identities under `/srv/luma/data/lae/tenants/`; API callers cannot supply arbitrary host paths. The volume binding records the owning node ID before data preparation and job submission. Retries and later releases stay on that node.

Existing LAE volume references retain their recorded backend even when the global default changes. Setting the environment variable to `local` does not migrate old NFS data. An explicit previously configured NFS class remains supported during migration.

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

Do not remove a class, unmount volumes or disable the NFS server until every active and stopped deployment and LAE binding has been checked. An application with no running allocations can still depend on its old data. Keep offline-node data in place until that node can be inspected.

## Removal And Recovery

`luma service remove <name>` removes the application job and routes but retains data by default. Avoid `--delete-storage` during migrations. Removing a deployment can also remove its saved owner, so save its pinned configuration before removal and use it when restoring the application.

Local storage does not provide cross-node failover. Recover the original node or restore a verified backup and explicitly reconcile placement. Maintain backups independently of the application's disk.
