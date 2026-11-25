# Local Storage and Migration

Use this reference for persistent deployments and migrating local volume data. Check the running Control and relevant node Agent versions: current Control reports storage backend, mount and explicit owner changes as warnings, while older versions may reject them. Neither behavior copies data. A checked-out source version is not deployment proof.

## Placement and Identity

- Data belongs to the application's deployment node. Control chooses or validates the first owner and records it before submitting the job. Subsequent updates preserve it, including when the owner is offline. Restore that node or perform an explicit data migration instead of launching an empty replacement elsewhere.
- Compose local paths use `volumes.<key>.local.path`; `local.node` may be omitted for Control to inherit placement. A persistent Compose stack stays on one node in one region. Direct offline rendering needs an explicit node.
- Native services use `volumes` with exact bind paths or Docker volume names. Writable persistence requires `replicas: 1`; avoid canaries that would start concurrent database writers against the same data.
- New dashboard defaults isolate data by deployment. Existing sources must retain their identities. Compare actual Nomad mount `type`, `source`, and `target`, Docker volume options, and host mount state; changing a path/name can create an empty database without deleting the old one.
- Local storage requires independent backups. It does not provide cross-node failover. Removing a deployment can remove its saved owner, so retain the pinned configuration for recovery.

## Host access for storage owners

Ordinary workers join as `isolated`. When Control enforces workload admission,
an actual deployment with persistent Docker mounts must resolve to one
ready storage owner with a compatible Luma Agent. Control validates the mounts
through that Agent and promotes an isolated owner to `trusted` when needed.
Promotion restarts the owner's Nomad client before job submission. If the
deployment cannot resolve one owner, verify the saved placement and mount
sources before changing host access.

For standalone changes to a worker's host access, use the reviewed bundle procedure in the [worker host access guide](https://liutianjie.github.io/luma/docs/nomad-security.html#worker-host-access).

## Existing-Data Migration

1. Recheck the current deployment immediately before acting. Another update may already have migrated it. If actual mounts are local and correct, skip the migration.
2. Capture the running job/version, saved deployment configuration, owning node, mount source, numeric UID/GID, permissions, and data size. Check destination free space and prepare a rollback backup. Keep credentials and raw job/environment data in private files.
3. Stop every writer for the final copy and verify termination on the owning host. A lost/unknown allocation is not proof that a process stopped. Use database-native backup/restore where appropriate; do not treat a copy of an active database directory as consistent.
4. Copy to the destination application node while preserving numeric ownership and permissions. Existing PostgreSQL directories may require mode 0700; do not recursively chmod them to 0777 during path preparation.
5. Verify archive/copy checksums and contents before startup. Set the destination local path with `adopted: true` and remove old `initialize: empty` acknowledgements. These flags do not copy data. Changing the owner node only produces a `Storage placement` warning, so copy data before redeploying elsewhere.
6. Update both the saved deployment and its source manifest so a later release cannot restore the old path. Preserve unrelated uncommitted changes. Keep the agreed deployment/build workflow; an authorized storage-maintenance cutover does not imply a new build lane for future releases.
7. Verify actual container mounts, database/queue read-back, public health and a representative operation. Report which checks ran. Keep original data and configuration for rollback; account for writes made after cutover before reverting.

`luma storage migrate` only prints a manual plan. For a local destination, supply an explicit deployment/local node. It does not copy data or stop applications. `--delete-storage` is not part of a migration.
