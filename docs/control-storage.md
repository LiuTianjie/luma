# Control storage and recovery

Luma Control uses a local SQLite database on one Manager. The database is the
authoritative store for Control state and its indexed operational records.
The database directory must be on a local filesystem that supports SQLite
locking and WAL; an NFS share is not a supported database location. This setup
does not provide multiple active Managers.

A new `luma bootstrap manager` initializes SQLite directly and automatically.
There is no intermediate `control.json` state, separate database service,
connection string or migration command to configure. Luma uses Python's
standard-library `sqlite3` module; the CLI installer creates a Python virtual
environment, and the Control image runs on a Python base image.

The default database is `/opt/luma/control/control.sqlite3`; set
`LUMA_CONTROL_STATE_DIR` in the Control process environment to use another
state directory. The `control.sqlite3-wal` and `control.sqlite3-shm` files are
SQLite runtime sidecars, not independent backup artifacts.

## Local maintenance commands

Run these on the Manager using the same installed Luma version and state
directory as Control. They operate on local files and do not require a remote
management token:

```bash
python -m luma.control.maintenance status
python -m luma.control.maintenance check
python -m luma.control.maintenance backup /secure-backups/control-2026-09-05.tar.gz
python -m luma.control.maintenance restore /secure-backups/control-2026-09-05.tar.gz --destination /srv/luma-control-recovery
```

The backup destination must be a new path outside the Control state directory.
Restore requires a destination that does not yet exist and does not overwrite
the live Control. `status` reports database integrity, schema and row counts;
`check` verifies the active database, and `check --database PATH` verifies a
specific database copy. Review command exit status as well as the reported
integrity result. A successful archive records a recent-backup receipt for the
storage inventory; its timestamp does not prove an off-Manager copy or a
completed recovery drill. Keep the archive and verification metadata private.

## What grows, and what retention means

A page containing 300 builds is a query result, not a storage policy. Build
metadata, event text, source archives, images and application data have different
lifetimes and must be managed separately.

| Data | Location and lifecycle |
| --- | --- |
| Control configuration and operational records | Manager SQLite database; query pagination does not delete data |
| Resource history | Separate bounded `metrics-history.json` inside the Control state directory; the API reports the available interval and retention |
| Generated Nomad jobs and Traefik routes | Manager files keyed by application/service name; updating one application replaces its current generated files |
| Builder source snapshots and analysis/build evidence | Builder content-addressed store, normally `/var/lib/luma/builder/snapshots`; identical content can share a digest |
| Builder temporary checkouts and credentials | Task work directory; normal task completion removes it, while a process crash can leave remnants |
| BuildKit and Trivy caches | Builder-local cache storage, separate from Control history and Registry blobs |
| Registry images | Registry storage; protected deletion and garbage collection follow the Registry policy |
| Application volumes and databases | Application storage; neither Control history retention nor a Control database backup backs them up |

Removing a build record does not itself delete its image, source archive or
application volume. Conversely, deleting an image can remove the ability to
redeploy an old build even while its history remains searchable.

## History queries and reviewed retention

The Dashboard history page and `luma service history` query indexed build and
deployment attempts with cursor pagination. List pages contain summaries;
record details and execution steps are fetched separately. `luma history NAME`
continues to read Nomad job versions for rollback and is a different history.
Build retries create a new attempt ID linked by `retryOf`/`retryRootId`; the
failed attempt and its events are preserved. Former global 100-build,
200-deployment and 300-build-event trimming no longer governs these records.
Agent progress and LAE Builder task retention remain separate lifecycles.

Manager inventory snapshots refresh about hourly even when the Dashboard is
closed; the page shows each measurement timestamp and its coverage.

In Dashboard storage governance, inspect the measured inventory, adjust the
history policy, then create a preview. The default policy is 90 days for
summaries, 14 days for detailed events and a 24-hour review grace period.
Changing the policy does not start automatic deletion. A history cleanup plan
stores identifiers and fingerprints, not copies of the deleted payloads; keep
a separate verified backup if you need recovery after applying it.

A preview records up to 1000 candidates and estimated serialized payload bytes;
when its result says `truncated`, repeat the preview after applying the reviewed
batch to inspect further candidates.
After its grace period, apply that specific plan explicitly. Control rechecks
terminal status, retry/current-deployment references and each record's
fingerprint before deleting. Active and referenced records remain protected.
Plans expire seven days after they become eligible. A policy change or expired
plan requires a new preview; individual records changed since preview are
skipped and reported in the apply result. Depending on age, a candidate loses
only old detailed events or the summary together with its events.

The same reviewed plan includes resolved/closed alert incidents and final
delivery records older than `summaryDays` (90 by default). Active incidents and
outstanding deliveries remain protected, and the apply result reports alert
history cleanup separately. This is not an automatic background deletion.

The API entry points require the management token:

| Endpoint | Operation |
| --- | --- |
| `GET /v1/governance/inventory` | Measured Manager files, record counts, unknown external stores and recent plans |
| `GET /v1/governance/policy` | Read the current retention policy |
| `POST /v1/governance/policy` | Set integer `summaryDays`, `detailDays`, `graceHours` |
| `POST /v1/governance/history/preview` | Persist a reviewable history cleanup plan |
| `GET /v1/governance/history/plans/PLAN_ID` | Reload its candidates, dates and result |
| `POST /v1/governance/history/apply` | Apply `{ "planId": "PLAN_ID", "confirmed": true }` after the grace period |

Reclaimed payload estimates are not promised filesystem savings. SQLite
normally reuses freed pages; the database file may remain the same size after
record removal. This flow does not automatically VACUUM the live database.

## Builder and Registry capacity

Builder inventory and cleanup run on the selected Builder through its agent.
`POST /v1/governance/builder` accepts `node`, `operation`, and when required
`planId` plus `confirmed: true`; poll the returned task through
`GET /v1/governance/builder/TASK_ID`. Supported operations are `inventory`,
`preview`, `quarantine`, `restore` and `purge`. The Dashboard presents the same
sequence and its eligibility times.

The scope is its configured content-addressed snapshot store, including source
archives, analysis artifacts and build/external-image evidence. Manager
inventory does not guess remote disk usage; unknown Builder, Registry,
BuildKit, Trivy and application-volume usage remains unknown until measured
through the appropriate owner.

Builder cleanup requires a fresh complete Manager reference inventory and no
active build tasks. All known snapshot references remain protected; only old,
unreferenced content can become a candidate. An explicit preview records file
identities. After at least 24 hours, explicit quarantine moves the unchanged
candidates into a recoverable area; final purge requires another 24 hours and
fresh reference checks. Restore never overwrites an existing file. The plan's
seven-day expiry and the exact eligible time are returned with the plan.

Builder plan metadata (`.governance.sqlite3`) and quarantine (`.trash`) live
inside the Builder snapshot root. They are remote Builder data and are not
included in a Manager Control backup.

Quarantine moves bytes on the same filesystem and does not free disk space.
Final purge does. Symlinks, unrecognized paths, changed content, stale reference
coverage or active tasks block cleanup rather than broadening the deletion
scope. This flow does not clean temporary workspaces, BuildKit/Trivy caches,
Registry blobs, secrets or application volumes.

Registry has its own retention policy and protected deletion/GC flow. Its
default mode is `recommend`, with 20 recent images, a 30-day age policy and a
seven-day recovery window. Automatic deletion requires explicit `enforce`
configuration; changing Control history retention does not enable it. Registry
manifest deletion and physical blob garbage collection are separate steps,
and shared layers can remain in use by retained images.

## Existing installations: legacy state import

This compatibility path applies only when upgrading an existing installation
with legacy JSON state. It is not part of a new Manager setup and requires no
manual migration command. Use `luma update manager` for the first upgrade and
allow a short Control maintenance window. Its configuration reads and image
prefetch do not initialize SQLite. Immediately before import, the installer
stops only the `luma-control` Nomad job, confirms that no allocation can still
write JSON, and saves a private `control-pre-sqlite-*` checkpoint beside the
state directory. Failure to confirm termination aborts without importing.
The first database initialization imports the
available legacy `control.json`
state, retains a private `control.json.pre-sqlite.bak`, and records the import
checksum and counts in `control-sqlite-migration.json`.
After that import, the database is the sole source of truth. Do not edit the
legacy JSON file expecting to update the running Control, and do not run old
JSON-writing and new database-writing Control versions against the same state
directory.

The first migrated Control job disables automatic rollback to the old JSON
image. A failed rollout keeps the pending cutover marker so a retry remains
protected. Ordinary later SQLite updates retain their existing automatic
rollback behavior. Do not directly run a new database/backup command against
the live legacy directory before this upgrade sequence; those commands can
initialize the database without fencing the legacy process.

An old JSON-writing image cannot consume the migrated database. Rolling back
to that image requires stopping the new Control and restoring the final legacy
checkpoint plus its matching configuration and old job spec. Keep the migrated
SQLite directory separately. This rolls back Control state to the checkpoint
time; it does not undo application-side operations that happened afterward.
See the [release cutover and rollback procedure](release.md#first-json-to-sqlite-upgrade).

Import preserves records still present in the legacy files. It cannot recover
builds, deployment history or metrics already removed by earlier count limits.
An existing database is not repeatedly overwritten from a legacy JSON snapshot.
The committed `control-sqlite-authority.json` records the database identity. A
missing, truncated or mismatched database after cutover fails closed, even if
legacy JSON still exists. Restore a verified backup; deleting the database does
not switch the Manager back to JSON.

## Backup scope

A consistent SQLite backup must include transactions still present in WAL. Use
the supported database backup operation; copying only the active database file
can omit recent commits. Do not concatenate, edit or restore WAL/SHM files from
another database snapshot.

The database contains credentials and private configuration. Backup artifacts
must remain private, and should be encrypted when transferred or stored off the
Manager. Commands should report paths, counts and integrity results; there is
no need to print token or key contents for verification.

The Control archive includes a consistent database snapshot and supported
files inside the Control state directory, including private token files and
fleet/prepare/Registry recovery artifacts. It excludes live WAL/SHM, lock and
temporary files. Legacy import files are retained as recovery evidence. Keep
configuration/token rotation quiescent while creating the archive: the SQLite snapshot is transactional, while external
files are copied separately. Files configured outside the state directory are
not automatically included.

A complete Manager recovery set also needs the files outside that archive:

- Control service environment and any secret files configured outside the
  state directory, such as external LAE principal/signing-key files or a custom
  metrics token path. Notification channel App Secrets stored in SQLite are
  already part of the database snapshot; cached tenant access tokens are not
  persisted and are obtained again after restart.
- The Manager's Luma configuration, generated jobs/routes, Traefik configuration
  and ACME certificate state, plus the desired deployment manifests from Git.
- Nomad recovery material appropriate to the cluster, and independent backups
  of application volumes, databases and any Registry/Builder data that must be
  retained.

Keep a dated inventory of paths and permissions alongside each recovery set.
A successful database backup is evidence for Control database consistency; it
does not establish that the other recovery items are complete.

## Restoring or moving the Manager

1. Stop writes from the old Control before switching the authoritative Manager.
   Preserve its database and configuration as the rollback checkpoint.
2. Verify the backup's integrity, then restore it onto local storage on the
   target Manager with the `restore` command into a new, nonexistent directory.
   Point the target Control at that restored directory, and restore external configuration and
   private files with their original permissions.
3. Start a compatible Control version with the restored state directory. Check
   authentication, expected cluster/node identities, retained history and
   database health before redirecting clients and node agents.
4. Check Nomad leadership/connectivity, agent heartbeats, generated ingress
   routes and actual application endpoints. A database integrity check alone
   does not prove runtime availability.
5. Keep the original checkpoint until the target has passed these checks. Do
   not restart the old Manager as another writer for the same cluster.

Test this procedure with an isolated recovery copy before relying on it for a
production incident. This guide describes the implementation and runbook; it
is not a claim that a live backup, migration or recovery drill has been run.
