# Luma Application Engine

This directory is the product boundary for LAE. It is intentionally isolated
from the root Luma Python package and dashboard workspace so it can be moved to
an independent repository without changing package names or service contracts.

The current foundation is executable and testable. It establishes versioned
contracts, a deterministic analyzer, PostgreSQL durable operations/outbox, a
typed Luma adapter with an in-memory fake, component health entry points, a
machine-readable CLI, public email/deploy-token authentication, an atomic
public HTTPS-Git analysis enqueue path, a quota-counted application catalog,
encrypted environment metadata, and a dev/staging-only signed mock billing
slice. The private Git connection catalog now stores only versioned AES-GCM
ciphertext and issues consumer/host-bound one-time leases; a real mutually
authenticated LAE-to-Luma redemption endpoint is still required before private
Git is production-usable. Real payment providers, the deployment controller,
and end-to-end Luma staging are not yet claimed as complete.

## Boundaries

- Browser, CLI, and user agents consume the public LAE `/v1` API.
- `lae-worker` is the only LAE component that will call Luma through the
  versioned adapter boundary.
- `lae-agent-runner` deterministically analyzes a read-only source snapshot and
  runs only inside a Luma builder sandbox. It never receives a Luma management
  token or source credential.
- PostgreSQL is the durable operation/event truth. Valkey may only be used
  for wakeups, rate limits, cache, and event fan-out.
- Production infrastructure and tenant applications are deployed by Luma.

## Toolchains

- Python 3.12 with an independent `uv` workspace.
- Node.js 22 with an independent `pnpm` workspace.
- Hand-written JSON Schema and event catalog under
  `packages/contracts/src/lae_contracts/specs/` are the only protocol source.
- Generated Python/TypeScript clients will consume those sources later; no
  generated protocol copy is checked in during this first scaffold.

## Validate

The contract validator and tests use only the Python standard library:

```bash
make contracts
make test
make smoke
make check
```

The workspace package managers can also verify their own metadata:

```bash
uv lock --check
pnpm install --frozen-lockfile
pnpm check
```

Useful component checks:

```bash
uv run --package lae-api lae-api --health
uv run --package lae-worker lae-worker --once
uv run --package lae-agent-controller lae-agent-controller --health
uv run --package lae-agent-runner lae-agent-runner --health
uv run --package lae-cli lae --format json version
```

The default suite compiles PostgreSQL-specific queue statements without using
SQLite. A real integration run requires an explicitly disposable database:

```bash
LAE_TEST_POSTGRES_DSN='postgresql+asyncpg://...' \
LAE_TEST_POSTGRES_ALLOW_DDL=1 \
  uv run --package lae-store --extra migrations python -m unittest discover \
  -s tests -p 'test_*_postgres_integration.py'
```

This pattern intentionally runs every disposable-PostgreSQL suite: queue,
authentication, public analysis, artifact ingest, application catalog,
environment encryption, billing, private Git connections and static uploads.
CI uses the same pattern so a new real-DB slice cannot be silently omitted by
an older single-file command.

`packages/python/lae-luma-adapter` is the only worker-facing Builder Task
boundary. Its HTTP implementation filters internal Luma fields, while
`FakeLuma` provides the same idempotency, ownership, cursor and cancellation
semantics for worker/API integration tests.

The builder invokes the analyzer with an immutable source snapshot and trusted
metadata:

```bash
lae-agent-runner analyze \
  --source /workspace/source \
  --metadata /workspace/metadata.json \
  --output-dir /workspace/output
```

The runner atomically writes canonical `evidence.json`,
`deployment-plan.json`, internal `build-plan-proposal.json`, and finally
`result.json`. It does not clone, access source credentials, resolve image tags,
build, push, or deploy. The Luma analyze executor resolves proposal image refs
under its registry policy and is the only component that creates the persisted
`lae.build-plan-candidate/v1`; every external image in that candidate is bound
to `resolvedDigest`. Its artifact digest is the SHA-256 of the actual canonical
file bytes. The trusted controller validates the candidate, changes its schema
version to `lae.build-plan/v1`, and adds its HMAC signature before creating a
`build-plan` task. The legacy `--run` command only validates a
`luma.builder-task/v1` envelope and remains available as a protocol smoke
check.

Image-only Compose services are represented explicitly as signed
`externalImages`; they are not treated as an empty build. Luma resolves each
allowed public reference anonymously with `crane` during analysis, signs the
expected platform digest, and verifies the same resolution again at build time
before producing CycloneDX, offline Trivy, and LAE-owned external-resolution
evidence. This is implemented and covered by isolated tests, but remains behind
the public launch gate until network-level redirect/DNS egress enforcement and
real Luma staging validation are complete.
