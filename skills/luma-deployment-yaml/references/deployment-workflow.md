# Deployment Workflow Records

Luma Control stores one workflow per application, shared across agents and machines.
The CLI automatically checks it before `import`, `build local`, `build retry`,
`deploy`, or `compose deploy` starts build/deploy work. There is no repository
workflow file to create and no `--remember` flag to add.

## Choosing And Confirming A Path

| Check result | Agent action |
| --- | --- |
| No record | Use the user's requested path or the repository's deployment setup. Deploy normally; success creates the record. Do not backfill historical deployments or require setup confirmation. |
| Matching record | Continue without another confirmation. |
| Different workflow | Explain the recorded and requested values. Continue only after the user explicitly approves that change. |
| Check unavailable or unsupported | Report the actual error. Do not treat it as no record or bypass the check. |

The comparison covers build lane and relevant explicit CLI parameters: repository,
Git ref, builder, platform, context/Dockerfile, sidecar, region, exposure/domain/port,
deployment env-file path and network options. Timeout, output format, credentials
and notes do not trigger confirmation. Source-code changes, image tags and manifest
contents are not compared by this guard; ordinary deployment validation still applies.

Interactive text mode asks `Confirm this workflow change and deploy? [y/N]`.
Non-interactive, JSON/NDJSON and quiet calls stop with a nonzero exit code and show
the differences. Present those differences to the user before retrying with
`--accept-workflow-change`. Reuse an existing explicit approval of these same
differences; do not ask again merely because a retry uses another output format.
Do not insert the flag automatically as an error-recovery step or blanket CI default.

Do not switch the build lane to work around infrastructure failure without the
user's approval. In particular, do not commit/push a checkout to force Repository
Import when the agreed path is local build/upload, or silently use local Docker
when the application is configured to build on Builder.

## Inspect, Reuse, And Explain

```bash
luma workflow list --format json
luma workflow show app --format json
luma workflow run app --path /path/to/checkout
```

`show` returns the recorded argument array, method, optional note, and last recorded
success. `run` executes the saved Luma command from the project root with the current
Control credentials and still performs the normal check. It does not commit/push Git
or execute an arbitrary shell script. Repository-relative paths move with a checkout;
external absolute config/env paths need to exist on the next machine.

Normal deployment records automatically. Add a rationale without changing that flow:

```bash
luma import acme/app --ref main --build-node builder \
  --workflow-note 'Use Builder because the build depends on its network access'
```

For an intentional manual setup/edit, without deploying:

```bash
luma workflow record app --note 'Release from main on Builder' -- \
  import acme/app --ref main --build-node builder
```

Use `record` only when setting/editing the workflow is part of the authorized task;
never use it to make a pending mismatch disappear. A manual edit is not deployment
proof: any prior `lastSuccess` retains its original recipe.

Service/Compose manifests identify the application. Imports match by repository,
with the selected Compose sidecar disambiguating monorepos. If several applications
still match, use `--workflow-app APP` for the intended application, not a new name
chosen to evade its record. A retry retains `build retry ID` and the original build
parameters; replay requires that build record to remain available.

## Recording And Availability

- Only a successful CLI deployment refreshes the success record. Failed builds,
  interrupted streams, dry runs and `--skip-orchestrator` preserve the previous one.
- Deployment may succeed while recording fails. Report `workflow.saved: false` and
  its warning separately; inspect the actual deployment before deciding any next step.
- Tokens, URL credentials and env-file contents are not stored in the recipe. Keep
  free-text notes free of secrets too.
- The installed CLI must expose `workflow` and `--accept-workflow-change`; Control's
  authenticated-context health information must include `deployment-workflow-v1`.
  Check CLI help and `/v1/health` capabilities when compatibility is uncertain.
  A package version pinned elsewhere in the skill is not proof of this capability.
- When an upgrade is needed, update Control before using the new CLI for deployment,
  within the user's authorized release scope. Do not announce availability based
  only on edited source or updated skill files.
- These checks apply to the updated Luma CLI. Older clients, raw API and Dashboard
  deployments do not participate; do not use them to bypass a CLI mismatch. This
  workflow does not add LAE tenant functionality.
