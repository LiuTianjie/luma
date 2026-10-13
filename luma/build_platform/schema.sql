CREATE TABLE IF NOT EXISTS bp_schema(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS bp_targets (
  target_key text PRIMARY KEY, generation bigint NOT NULL DEFAULT 0,
  applied_generation bigint NOT NULL DEFAULT 0, active_release text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bp_runs (
  id text PRIMARY KEY, accepted_seq bigserial UNIQUE, idempotency_key text UNIQUE, request_hash text NOT NULL,
  request jsonb NOT NULL, private_request bytea NOT NULL,
  source text NOT NULL, project_key text NOT NULL, target_key text NOT NULL DEFAULT '',
  generation bigint NOT NULL DEFAULT 0, mode text NOT NULL DEFAULT 'builder',
  status text NOT NULL DEFAULT 'queued', build_status text NOT NULL DEFAULT 'queued',
  phase text NOT NULL DEFAULT 'queued', release_status text NOT NULL DEFAULT '',
  cancel_requested boolean NOT NULL DEFAULT false, capacity_held boolean NOT NULL DEFAULT false,
  prepared jsonb NOT NULL DEFAULT '{}', result jsonb NOT NULL DEFAULT '{}',
  artifact_id text NOT NULL DEFAULT '', release_id text NOT NULL DEFAULT '',
  retry_of text NOT NULL DEFAULT '', message text NOT NULL DEFAULT '', next_event bigint NOT NULL DEFAULT 0,
  created_at bigint NOT NULL, updated_at bigint NOT NULL, completed_at bigint
);
CREATE INDEX IF NOT EXISTS bp_runs_history ON bp_runs(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS bp_runs_queue ON bp_runs(status,created_at,id);
CREATE INDEX IF NOT EXISTS bp_runs_project ON bp_runs(project_key,capacity_held);
CREATE TABLE IF NOT EXISTS bp_attempts (
  id text PRIMARY KEY, run_id text NOT NULL REFERENCES bp_runs(id), number integer NOT NULL,
  job_id text NOT NULL UNIQUE, token_hash text NOT NULL, expires_at bigint NOT NULL,
  status text NOT NULL DEFAULT 'pending', result jsonb NOT NULL DEFAULT '{}', error text NOT NULL DEFAULT '',
  created_at bigint NOT NULL, updated_at bigint NOT NULL, UNIQUE(run_id,number)
);
CREATE TABLE IF NOT EXISTS bp_artifacts (
  id text PRIMARY KEY, run_id text NOT NULL UNIQUE REFERENCES bp_runs(id),
  source_commit text NOT NULL, recipe_hash text NOT NULL, result jsonb NOT NULL,
  cache_refs jsonb NOT NULL DEFAULT '[]', created_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS bp_artifacts_cache ON bp_artifacts(recipe_hash,created_at DESC);
CREATE TABLE IF NOT EXISTS bp_releases (
  id text PRIMARY KEY, run_id text NOT NULL REFERENCES bp_runs(id),
  artifact_id text NOT NULL REFERENCES bp_artifacts(id), target_key text NOT NULL,
  generation bigint NOT NULL, expected_index bigint, status text NOT NULL DEFAULT 'queued', result jsonb NOT NULL DEFAULT '{}',
  error text NOT NULL DEFAULT '', created_at bigint NOT NULL, updated_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS bp_releases_target ON bp_releases(target_key,generation DESC);
CREATE TABLE IF NOT EXISTS bp_outbox (
  id bigserial PRIMARY KEY, revision bigint NOT NULL DEFAULT 1, run_id text NOT NULL REFERENCES bp_runs(id),
  operation text NOT NULL, delivered boolean NOT NULL DEFAULT false,
  claimed_until bigint NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0,
  created_at bigint NOT NULL, UNIQUE(run_id,operation)
);
CREATE TABLE IF NOT EXISTS bp_log_chunks (
  run_id text NOT NULL REFERENCES bp_runs(id), batch_id text NOT NULL,
  first_seq bigint NOT NULL, last_seq bigint NOT NULL, object_key text NOT NULL,
  checksum text NOT NULL, created_at bigint NOT NULL,
  PRIMARY KEY(run_id,batch_id), UNIQUE(run_id,first_seq)
);
CREATE INDEX IF NOT EXISTS bp_log_seek ON bp_log_chunks(run_id,last_seq);
CREATE TABLE IF NOT EXISTS bp_settings (key text PRIMARY KEY, value jsonb NOT NULL);
INSERT INTO bp_schema(version) VALUES(1) ON CONFLICT DO NOTHING;

ALTER TABLE bp_runs ADD COLUMN IF NOT EXISTS active_platform text NOT NULL DEFAULT '';
ALTER TABLE bp_runs ADD COLUMN IF NOT EXISTS placement_started bigint NOT NULL DEFAULT 0;
