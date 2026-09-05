# Local Storage and Migration

Use this reference for persistent deployments, NFS retirement, and recovery of older storage configurations. These rules describe Control 0.1.305 and later; check the running Control and relevant node Agent versions before relying on them. A checked-out source version is not deployment proof.

## Placement and Identity

- Data belongs to the application's deployment node. Control chooses or validates the first owner and records it before submitting the job. Subsequent updates preserve it, including when the owner is offline. Restore that node or perform an explicit data migration instead of launching an empty replacement elsewhere.
- Compose local paths use `volumes.<key>.local.path`; `local.node` may be omitted for Control to inherit placement. A persistent Compose stack stays on one node in one region. Direct offline rendering needs an explicit node.
- Native services use `volumes` with exact bind paths or Docker volume names. Writable persistence requires `replicas: 1`; avoid canaries that would start concurrent database writers against the same data.
- New dashboard defaults isolate data by deployment. Existing sources must retain their identities. Compare actual Nomad mount `type`, `source`, and `target`, Docker volume options, and host mount state; changing a path/name can create an empty database without deleting the old one.
- Legacy native `storage` metadata may differ from its rendered mount. A `storageClass` reference alone does not prove NFS was used. Conversely, a Docker volume with driver `local` can still mount NFS through its driver options.
- Local storage requires independent backups. It does not provide cross-node failover. Removing a deployment can remove its saved owner, so retain the pinned configuration for recovery.

## Existing-Data Migration

1. Recheck the current deployment immediately before acting. Another update may already have migrated it. If actual mounts are local and correct, skip the migration.
2. Capture the running job/version, saved deployment configuration, owning node, mount source, numeric UID/GID, permissions, and data size. Check destination free space and prepare a rollback backup. Keep credentials and raw job/environment data in private files.
3. Stop every writer for the final copy and verify termination on the owning host. A lost/unknown allocation is not proof that a process stopped. Use database-native backup/restore where appropriate; do not treat a copy of an active database directory as consistent.
4. If NFS exports the data from the application's own host, reuse that verified directory as a bind mount. For cross-host data, copy to the application node while preserving numeric ownership and permissions. Existing PostgreSQL directories may require mode 0700; do not recursively chmod them to 0777 during path preparation.
5. Verify archive/copy checksums and contents before startup. Set the destination local path with `adopted: true` and remove old `initialize: empty` acknowledgements. These flags do not override an established local-owner lock or copy data.
6. Update both the saved deployment and its source manifest so a later release cannot restore the old backend. Preserve unrelated uncommitted changes. Keep the agreed deployment/build workflow; an authorized storage-maintenance cutover does not imply a new build lane for future releases.
7. Verify actual container mounts, database/queue read-back, public health and a representative operation. Report which checks ran. Keep original data and configuration for rollback; account for writes made after cutover before reverting.

`luma storage migrate` only prints a manual plan. For a local destination, supply an explicit deployment/local node. It does not copy data or stop applications. `--delete-storage` is not part of a migration.

## LAE Runtime Boundary

For new LAE applications, unset `LUMA_LAE_RUNTIME_STORAGE_CLASS` or set it to `local`. Local host paths are derived from authenticated tenant/application identities under `/srv/luma/data/lae/tenants/`; do not accept arbitrary client host paths. Durable volume bindings retain the selected node ID before directory preparation and submission.

Existing volume references retain their recorded backend when the global default changes. A default change is not migration. Inspect the current application, runtime revision, volumeRefs, job metadata and actual source before changing bindings.

An application with no running allocations can still own data. For user-authorized deletion, use the supported lifecycle operation with the intended retain/delete policy. If an orphan's original storage class or platform row is already missing, first establish ownership and absence of other consumers, then perform narrowly scoped administrative cleanup with a backup and read-back. Do not recreate NFS just to satisfy an obsolete configuration or treat one authorized app deletion as permission to delete all historical applications.

## Retiring NFS

- Inventory current jobs, stopped applications, LAE volume bindings, Docker NFS options and host mounts. Historical references may need preservation for recovery even after current workloads stop using NFS.
- Remove unused classes only after checking dependencies. Class removal is not proof of host shutdown; a node-agent operation may be skipped while the class still disappears from Control.
- Stop and disable the NFS service on reachable hosts after client mounts are gone. Verify both inactive/disabled status and empty exports. Retain source data and configuration backups separately from live exports.
- Do not call an offline host cleaned. Report its remaining host-level work separately from the verified online state. Never relocate its database into a new empty volume to make the inventory look healthy.

For the dated production execution record, see [2026-09-05 local-storage migration](https://github.com/LiuTianjie/luma/blob/main/docs/local-storage-migration-2026-09-05.md). Treat that record as historical evidence and recheck live state before another operation.
