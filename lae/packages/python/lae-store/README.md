# LAE Store

`lae-store` is the PostgreSQL-only persistence boundary for LAE. It provides:

- tenant-scoped repositories with no caller-supplied SQL escape hatch;
- keyed-hash deploy-token material (plaintext is never persisted);
- durable operations, monotonic operation events and an outbox;
- PostgreSQL `FOR UPDATE SKIP LOCKED` worker claims, leases, heartbeats,
  cancellation and expired-lease reclaim;
- transaction-level advisory locking plus a partial unique index for one active
  application mutation at a time.
- a tenant-scoped application catalog with managed HTTP routes, service and
  volume metadata, encrypted environment metadata with optimistic CAS, and
  immutable revision/deployment facts.
- versioned Lite/Pro/Ultra plan reads plus tenant/principal-scoped mock checkout
  orders whose price, currency and interval are copied from server configuration;
  signed provider events are keyed-hash idempotent and transactionally replace
  the active subscription without deleting subscription history.

SQLite is deliberately unsupported: its locking, partial-index and queue
semantics do not prove the PostgreSQL guarantees used here.

The payment implementation is deliberately a dev/validation mock. Production
defaults to `LAE_BILLING_DRIVER=disabled`; billing endpoints fail closed while
identity, Lite usage and deployment remain available. WeChat Pay and Alipay
have only a provider port, not adapters. No commercial prices are bundled.

## Application catalog boundary

`ApplicationCatalogStore` creates, lists and reads applications only inside a
`TenantScope`. Application creation serializes on a tenant advisory lock,
loads the active plan version inside the same transaction, and enforces the
application, per-app service, public HTTP route and volume-byte limits before
inserting any catalog rows. This prevents concurrent requests from exceeding
the application quota.

The first-deploy flow creates a quota-counted `kind=pending` application draft
with no fabricated service rows. Source analysis binds to that application;
`materialize_topology` requires that application's stored, deployable analysis,
then atomically changes it to `service` or `compose` and inserts the analyzed
services/routes/volumes after enforcing topology limits.
Materialization is one-way. An existing materialized topology cannot be
rewritten; later changes are represented by revisions.

Public routes have server-generated 128-bit lowercase labels under
`*.itool.tech`; callers cannot supply a hostname. A Compose application may
have multiple public HTTP routes. There is deliberately no public TCP/UDP,
tcp-relay, host-port or bind-mount representation in the catalog schema.

Environment values are persisted only in `app_environment` as envelope
ciphertext, a 32-byte checksum and encryption-key version. The application row
owns the global environment version used for compare-and-swap updates. Public
repository records expose configured/key metadata only; they have no value,
ciphertext or checksum field. A future internal runtime-only decryptor must be
separately capability-scoped and is not part of this public repository.

Revision and deployment rows capture immutable source/analysis/plan binding
and deployment lineage. Runtime reconciliation and activation must update
deployment/revision status and application current pointers transactionally;
that worker transition API is intentionally a later slice.

## Workspace wiring

The workspace owner must add `packages/python/lae-store` to the root `uv`
members and regenerate `uv.lock`. Runtime services should depend on
`lae-store`; migration jobs additionally install `lae-store[migrations]`.

Run migrations from `lae/` with an asyncpg URL:

```sh
LAE_DATABASE_URL='postgresql+asyncpg://...' \
  uv run --package lae-store --extra migrations \
  alembic -c migrations/alembic.ini upgrade head
```

The migration is an expand migration. A later contract migration must only be
introduced after all deployed API and worker versions no longer need the old
shape.

## Tests

The default `test_store_*` suite uses pure state-machine tests and compiles SQL
against SQLAlchemy's PostgreSQL dialect. It never falls back to SQLite.

For real PostgreSQL verification, point the integration entry at a disposable,
empty database. The explicit allow flag exists to prevent accidental DDL on a
non-test database:

```sh
LAE_TEST_POSTGRES_DSN='postgresql+asyncpg://...' \
LAE_TEST_POSTGRES_ALLOW_DDL=1 \
  python -m unittest discover -s tests -p 'test_store_postgres_integration.py'
```

CI should run that entry against an ephemeral PostgreSQL service. It upgrades
the Alembic migration, exercises concurrent claims/events/idempotency, and
downgrades the disposable database on completion.
