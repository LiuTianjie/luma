# LAE CLI contract

## Output modes

- `--format text`: concise human output.
- `--format json`: one canonical JSON document on stdout.
- `--format ndjson`: one canonical event object per stdout line for long operations.

Progress and diagnostics go to stderr in text/JSON mode. stdout must never mix protocol JSON with prose. Secret values never appear on either stream.

## Core commands

```text
lae login --token-stdin
lae whoami
lae inspect --app <id> --repo <https-repository> --ref <ref> [--connection-id <id>] --idempotency-key <key> --format json
lae inspect-file --app <id> --file <artifact.html|artifact.zip> --idempotency-prefix <prefix> [--no-wait] --format ndjson
lae source-connections list
lae source-connections create --provider <github|gitea|generic> --name <name> --base-url <https-base> [--username <name>] --secret-stdin --idempotency-key <key>
lae source-connections rotate <id> [--username <name>] --secret-stdin --idempotency-key <key>
lae source-connections revoke <id> --idempotency-key <key>
lae uploads create --app <id> --file <artifact.html|artifact.zip> --idempotency-key <key>
lae uploads show <upload-id>
lae uploads complete <upload-id> --idempotency-key <key>
lae uploads delete <upload-id> --idempotency-key <key>
lae deploy --app <id> --analysis <id> --environment-version <version> [--confirm-change <stable-code> ...] --idempotency-key <key> [--wait] --format ndjson
lae operation show <operation-id> --format json
lae operation watch <operation-id> [--after <cursor>] --format ndjson
lae operation cancel <operation-id> --format json
lae apps list|show
lae apps create --name <name> --slug <slug> --idempotency-key <key>
lae apps deployments <app> [--limit <1-100>]
lae apps check-update|suspend|resume|restart <app> --idempotency-key <key>
lae apps rollback <app> [--deployment <deployment-id>] --idempotency-key <key>
lae apps delete <app> --yes --idempotency-key <key>
lae apps logs <app> [--service <key>] [--tail <lines>]
lae apps metrics <app> [--service <key>] [--window <seconds>]
lae config show --app <id> --analysis <id>
lae env list <app>
lae env set <app> <name> --service <service-or-*> --expected-version <version> --value-stdin --idempotency-key <key>
lae env unset <app> <name> --service <service-or-*> --expected-version <version> --idempotency-key <key>
lae plans list
lae billing checkout --plan <lite|pro|ultra> --interval <month|year> --idempotency-key <key>
```

The CLI exposes pending-app creation, public/private Git inspection, static
upload inspection, environment management, deployment and lifecycle
operations, bounded deployment history/logs/metrics, plan discovery, and
checkout-session creation. Token management remains a Web/API session flow;
if `lae --help` does not advertise a flow, stop instead of substituting raw
Luma calls.

`billing checkout` keeps the human-facing `month|year` interval spelling and
maps it to the API's `monthly|yearly` values. The active payment provider is a
server-side capability and is never selected or sent by the CLI.

`apps show` is the current CLI detail boundary for application services,
routes, volumes, and environment metadata. `apps deployments` returns bounded,
server-owned deployment history for status inspection and rollback target
selection. Deploy-token management remains a Web/API session flow. `apps logs`
and `apps metrics` return one bounded service view per request.

Agent execution must provide every required flag. Missing input fails instead of opening a browser. Supply environment values with `--value-stdin` and private Git credentials with `--secret-stdin`; never put them in an argument or environment variable. When stdin carries either secret, load the deploy token from `LAE_DEPLOY_TOKEN` so the two inputs cannot collide.

When inspection returns public verdict `needs_input`, its terminal output
includes a whitelisted `configuration` schema. The underlying stored analysis
status may be `needs_configuration`, but agents must branch on the public
verdict. Resume schema discovery with `config show`;
it returns service keys and environment names/required/sensitive flags only.
Fetch `env list` for the current compare-and-set version, then use `env set
--value-stdin`. Reuse that same analysis after configuration; inspect again only
after a source or deployment-plan input changes. Never infer or pass a value in
argv. A wildcard `--service '*'` satisfies all listed services only when the
same value is intended for each one.

An update-check terminal Operation may include `changes` with per-section
`added/removed/changed` keys and a sorted `confirmations` list. The API rejects
a destructive candidate unless `deploy` supplies that exact list through
repeated `--confirm-change` flags. Show the complete diff and obtain human
approval first. A legacy candidate with `deploymentPlanChanged=true` and no
`changes` must be checked again; it cannot be authorized by guessing codes.

## Static upload behavior

- `uploads create` accepts one regular non-symlink `.html` or `.zip`, computes SHA-256 and size with bounded memory, reserves it, and performs the one-time PUT. It never prints the transfer URL, signature, or server-supplied headers.
- `uploads complete` starts verification/scanning. Use `uploads show` to poll `ready` or `failed`; retain the upload ID on a timeout or transfer failure. `uploads delete` is the explicit cleanup path.
- `inspect-file` composes create, PUT, complete, ready polling, and upload analysis. Its idempotency prefix deterministically separates those three mutations. Do not reuse that prefix for a different file or application.
- The PUT client carries no Bearer token or cookie, ignores environment proxies, and rejects redirects. Do not reproduce the transfer with curl or expose its signed URL.
- `--no-wait` skips only the analysis-operation watch. Server-side upload validation still completes first so the returned analysis is valid. Resume a queued analysis with `lae operation watch <id> --after <cursor>`.

## Private Git behavior

- A connection records provider, display name, credential-free HTTPS base URL, optional username, and an encrypted secret. List output is metadata-only.
- Create and rotate read the secret only from stdin. They cannot be combined with `--token-stdin`; configure `LAE_DEPLOY_TOKEN` locally for those commands.
- Pass the returned connection ID to `inspect`; keep the repository URL credential-free and on the connection's allowed host.
- Rotate and revoke use new explicit idempotency keys. Obtain human confirmation before revocation because it invalidates related short-lived leases.

## Operation events

Every event has a monotonic cursor and stable operation ID. Keep at least:

```json
{
  "operationId": "op_...",
  "cursor": 17,
  "type": "builder.analyze.progress",
  "phase": "source.analyze",
  "status": "running",
  "message": "Source analysis updated"
}
```

Do not depend on unknown extra fields. A cursor-expired response requires fetching the validated operation/task snapshot and resuming from its advertised cursor; do not replay raw upstream logs.

## Exit codes

| Code | Meaning | Agent behavior |
| ---: | --- | --- |
| 0 | Success | Continue |
| 2 | Invalid local input/arguments | Correct input; do not retry unchanged |
| 3 | Unauthenticated/forbidden | Ask user to configure auth locally |
| 4 | User configuration required | Collect named non-secret/secret inputs safely |
| 5 | Unsupported/policy denied | Report blocker; stop |
| 6 | Quota/plan limit | Explain entitlement; never upgrade automatically |
| 7 | Source/build failure | Report stable code and safe evidence |
| 8 | Deploy/runtime verification failure | Keep operation/revision IDs for recovery |
| 9 | Retryable platform failure | Resume/retry with the same idempotency identity |

## Stable retry behavior

- Every source-connection and upload mutation requires an explicit idempotency key; `inspect-file` requires an explicit prefix. Reuse an identity only for an identical method, route, principal, and request body, and persist it beside the operation cursor.
- Retry a disconnected watch with its last durable cursor.
- Retry a failed deployment by creating a new operation linked to the previous one; never rewrite terminal history.
- Treat `canceled`, `succeeded`, and `failed` as terminal. A user cancellation observed by LAE wins over a late builder success.
