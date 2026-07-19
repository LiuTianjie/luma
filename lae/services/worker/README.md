# LAE worker: source analysis slice

`lae_worker.analyze` is the first durable `source.analyze` orchestration slice.
It claims the existing PostgreSQL operation lease, resolves tenant/application/
source facts through `TenantRepository`, creates one idempotent Luma
`analyze-source` task, mirrors only allowlisted structured events, forwards
cancellation, and completes the operation with digest and artifact descriptor
references. It never fetches or clones source code in the worker.
The current executor is HTTPS-only, so SSH and shorthand Git references fail
closed until credential-broker plus executor support is implemented end to end.

Migration `20260711_0003` adds tenant-fenced `analyses`, `builder_tasks`,
`source_credential_leases`, `artifacts`, and `analysis_artifacts` records.
`PostgresAnalyzeStateStore` uses a monotonic CAS `checkpoint_version`; the
in-memory implementation remains test-only. `PostgresAnalysisRecorder` first
stores the three validated descriptors and their links. The artifact-ingest
slice then advances each descriptor through `pending -> uploading -> verified`
and changes the analysis from `descriptor-only` to `stored` only after all
three managed objects have passed exact media type, byte count, and SHA-256
verification.

## Secure artifact ingest boundary

`ArtifactIngestingAnalysisRecorder` defines the Builder-to-LAE byte-transfer
boundary without accepting a Builder filesystem path or object URL. A broker
issues a short-lived, single-use handle bound to tenant, application,
operation, Luma task, artifact name, digest, media type, and size. Downloads
are streamed in bounded chunks with timeout, cancellation checkpoints, a 16
MiB per-analysis-artifact cap, and a fresh lease for each bounded retry.

`S3CompatibleObjectStore.put_verified` is an atomic port: implementations must
upload to private validation, verify the descriptor, then publish at a key derived
only from the LAE tenant, closed artifact kind, and digest. The upstream cannot
choose the key. Existing exact objects make crash retries idempotent; partial
or mismatched objects never cause `planStored=true`. The in-memory broker,
catalog, and object store are test-only and cover success, retry, cancellation,
binding isolation, single use, and integrity failures.

Luma now exposes a service-principal-only lease endpoint plus a one-shot byte
stream. Control validates the successful analyze task and exact descriptor,
queues an export only on that task's builder node, keeps the redemption-token
digest and rendezvous file in process memory, and never persists the token,
artifact bytes, node path, or an internal URL in `control.json`. The node opens
only the content-addressed analyzer artifact and streams it back with an exact
size/digest binding. Expiry, replay, cross-principal/task access, redirect,
late task-state changes, cancellation, and descriptor mismatch fail closed.

`LAE_ARTIFACT_DRIVER=s3` wires the production recorder to the real SigV4
adapter, HTTP lease broker, PostgreSQL catalog, and operation heartbeat guard.
The S3 endpoint/bucket are fixed runtime configuration; caller URLs never
participate. Production requires HTTPS and an exact
`LAE_ARTIFACT_S3_ALLOWED_HOSTS` entry. validation may explicitly use HTTP for an
internal MinIO drill. The bucket must already exist and its lifecycle/backup
policy remains an operator responsibility. Required configuration is:

- `LAE_ARTIFACT_S3_ENDPOINT`, `LAE_ARTIFACT_S3_ALLOWED_HOSTS`
- `LAE_ARTIFACT_S3_BUCKET`, `LAE_ARTIFACT_S3_REGION`
- `LAE_ARTIFACT_S3_ACCESS_KEY`, `LAE_ARTIFACT_S3_SECRET_KEY`
- optional `LAE_ARTIFACT_S3_PATH_STYLE` and bounded timeout/retry settings

Production still fails closed when this complete verified recorder cannot be
constructed. Development may use the descriptor-only recorder, which
truthfully returns `artifactState=descriptor-only` and `planStored=false`.
Internal Luma URLs/tokens, image references, and artifact bytes are never
copied into operation results or events.

## Persisted schema

The source-analysis slice uses these tenant-fenced records:

- `analyses`: `id`, `tenant_id`, `application_id`, `source_revision_id`,
  `operation_id`, `status`, `policy_version`, `agent_image_digest`,
  `resolved_commit_full`, `source_tree_digest`, `source_snapshot_id`,
  `source_snapshot_digest`, `deployment_plan_digest`, `build_plan_digest`,
  `evidence_digest`, `artifact_state`, timestamps. Require unique
  `(tenant_id, operation_id)`, composite FKs from tenant/application and
  tenant/source, immutable source/policy/digest binding, and closed analysis
  statuses including `needs_configuration` and `not_deployable`.
- `builder_tasks` (the durable `AnalyzeStateStore`): `id`, `tenant_id`,
  `application_id`, `source_revision_id`, `operation_id`, `luma_cluster_id`,
  `luma_principal_id`, `luma_task_id`, `action`, `idempotency_key_hash`,
  `request_digest`, `event_cursor`, `upstream_status`, `cancel_forwarded_at`,
  `checkpoint_version`, `result_descriptor_json`, timestamps. Require unique
  `(luma_cluster_id,luma_principal_id,luma_task_id)`, unique
  `(luma_cluster_id,luma_principal_id,tenant_id,application_id,idempotency_key_hash)`,
  unique active task per `(operation_id,action)`, nonnegative cursor/version,
  and composite tenant FKs. Store the stable idempotency key as a keyed digest,
  not reusable credential material.
- `source_credential_leases`: `id`, `tenant_id`, `source_connection_id`,
  `operation_id`, `builder_task_id`, `allowed_action`, `allowed_host`, `status`,
  `expires_at`, `consumed_at`, `revoked_at`. Require one task/action/host-bound
  consumption, tenant-composite FKs, and never store the exchanged PAT/SSH key.
  `allowed_host` is a canonical HTTPS `host[:non-default-port]` binding, so a
  lease for one origin cannot be redeemed against another port.
- `artifacts`: `id`, `tenant_id`, `kind`, `digest`, `media_type`, `size_bytes`,
  `storage_key`, `upload_status`, `verified_at`, timestamps. Require unique
  `(tenant_id,kind,digest)`, closed `pending|uploading|verified|failed` status,
  and only mark a plan stored after object size/media type/digest verification.
  Analysis artifact link rows bind all three descriptor names (`evidence`,
  `deploymentPlan`, `buildPlan`) to artifact IDs. The byte-transfer catalog
  must verify object size/media type/digest before changing them to `verified`;
  only a final all-three-verified transaction may set `planStored=true`.

## Public creation transaction

`POST /v1/analyses` now calls `PostgresAnalysisRequestStore`, which creates the
source revision, queued analysis identity, `source.analyze` operation, builder
checkpoint, anonymous credential lease metadata, first event/outbox, and
principal-scoped idempotency response in one PostgreSQL transaction. A worker
can therefore never claim a publicly-created operation without its checkpoint.
The queued analysis row reserves the public `analysisId`; the recorder fills
that same row with immutable commit/snapshot/plan facts after Luma succeeds.

This slice requires an existing tenant-owned application and a credential-free
HTTPS Git URL. Tenant-scoped source connections now use a separate versioned
AES-256-GCM keyring, exact HTTPS host allowlist, atomic connection-backed lease,
HMAC consumer binding, strict TTL and single-use PostgreSQL redemption. The
broker returns an in-memory credential whose repr excludes the secret, but is
not exposed as a public endpoint. Production private Git therefore remains
fail-closed until a mutually authenticated internal endpoint lets Luma Builder
redeem only the opaque `credentialLeaseId`; credentials are never added to the
Luma task. Calling `OperationStore.create_operation()` and then `initialize()`
as two public request transactions remains forbidden; `initialize()` is
retained only for internal seams and legacy integration tests.

Checkpoint update and operation-event append should share one PostgreSQL
transaction. Until then the runner deliberately uses at-least-once event
mirroring, and includes the monotonic `builderCursor` so duplicate safe events
after a crash are identifiable.
