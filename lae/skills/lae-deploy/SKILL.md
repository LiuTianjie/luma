---
name: lae-deploy
description: Onboard and authenticate users, then inspect, deploy, update, and operate applications through the Luma Application Engine CLI, including safe Web registration and checkout handoffs. Use when an agent needs to onboard a user, determine whether a local static artifact or Git repository is deployable, supply required environment variables safely, watch or resume a deployment, inspect app status, perform suspend/resume/restart/update-check/rollback actions, or explain plan/checkout flow without using a Luma management token or completing payment directly.
---

# Deploy with LAE

Use the `lae` CLI as the only deployment boundary. Never call Luma management APIs or generate Luma credentials.

## Guardrails

- Keep deploy tokens, Git credentials, OTPs, payment data, and environment values out of prompts, command arguments, repositories, and logs.
- Ask the user to enter secrets in their own terminal through stdin, an environment variable, or an OS credential store. Do not echo them. A private Git credential must use `--secret-stdin`; set `LAE_DEPLOY_TOKEN` locally because one stdin stream cannot carry both values.
- Run `inspect` before every new deployment or source update. Use `inspect-file` for `.html`/`.zip` sources. Do not bypass a blocker or edit generated LAE/Luma deployment files by hand.
- Never add a platform registry host to repository source. In particular, do
  not write the retired `registry.itool.tech`, a Builder IP,
  `localhost:5000`, `registryHost`, or `pushHost` into Compose or generated
  manifests. LAE/Luma owns internal image coordinates.
- Treat only the public analysis verdict `deployable` as ready to deploy. For
  `needs_input`, collect only the named values through safe terminal input and
  reuse the same stored analysis after configuration. For `unsupported`,
  report every stable blocker and stop. For `diagnostic_failed`, preserve the
  operation/request ID and report a platform diagnostic failure; never label
  the user's code unsupported. Internal analysis statuses such as
  `needs_configuration`/`not_deployable` and policy words `allow`/`deny` are
  not CLI verdicts.
- Do not attempt public TCP/UDP, host ports, privileged containers, host namespaces, devices, sockets, bind mounts, or custom domains.
- Require explicit user confirmation before rollback, application deletion, source-connection revocation, subscription changes, or opening a payment checkout. Never complete payment for the user.
- Preserve the returned operation ID and cursor until the operation is terminal.

Read [references/policy.md](references/policy.md) when selecting a source type or explaining a blocker. Read [references/cli-contract.md](references/cli-contract.md) when invoking commands, parsing events, handling exit codes, or resuming work.

## Workflow

### 1. Establish capability and authentication

1. Run `command -v lae` and `lae --format json version`.
2. Run `lae --format json doctor`.
3. If authentication is missing, ask the user to set `LAE_DEPLOY_TOKEN` locally or pass `--token-stdin` in their terminal. `lae login --token-stdin` verifies a token but deliberately does not persist its plaintext. Never request the token in chat.
4. Run `lae --format json whoami`. Bind all later actions to the returned tenant; never accept a tenant ID from source files.

If the CLI or required command is unavailable, stop with a concise installation/capability requirement. Do not substitute raw HTTP calls unless the user explicitly asks for API-level debugging.

### 2. Select and inspect the source

- For one HTML file or an already-built static `.zip`, use `inspect-file`. Give it exactly one regular, non-symlink file; the CLI streams SHA-256 and size without unpacking locally.
- For Dockerfile, Compose, frameworks requiring a build, private source, or multiple services, use a Git connection/repository flow.
- For a template, use its immutable template/version reference; templates still require inspection.

For a Compose stack that builds one image and runs it with multiple commands,
give one service the `build:` block and let every consumer use the exact same
logical `image:` tag. Prefer a YAML anchor and a repository-local tag such as
`app:local`:

```yaml
x-app-image: &app-image app:local
services:
  api:
    image: *app-image
    build: {context: ., dockerfile: Dockerfile}
  worker:
    image: *app-image
```

LAE maps the image-only consumer to the unique build owner and injects the
immutable Builder Registry result. Do not duplicate `build:` across consumers.
An image-only service that does not match a repository build is an explicit
external image; preserve the user's versioned non-`latest` reference rather
than rewriting it.
Luma resolves allowlisted public image references through its Builder resolver
and pins their digests. Do not add proxy variables, mirror hosts, or Builder
addresses to user Compose; resolver egress is platform infrastructure.

Create a pending application first for any new source:

```text
lae apps create --name <display-name> --slug <slug> \
  --idempotency-key <stable-create-key> --format json
```

Then choose exactly one inspection path.

Public HTTPS Git:

```text
lae inspect --app <app-id> --repo <https-repository> --ref <full-ref> \
  --idempotency-key <stable-key> --format json
```

Private Git (the user enters the secret in their own terminal):

```text
lae source-connections create --provider <github|gitea|generic> \
  --name <connection-name> --base-url <credential-free-https-base> \
  [--username <username>] --secret-stdin \
  --idempotency-key <stable-connection-key> --format json
lae inspect --app <app-id> --repo <credential-free-https-repository> \
  --ref <full-ref> --connection-id <connection-id> \
  --idempotency-key <stable-analysis-key> --format json
```

Local static artifact:

```text
lae inspect-file --app <app-id> --file <artifact.html|artifact.zip> \
  --idempotency-prefix <stable-prefix> --format ndjson
```

`inspect-file` reserves storage, performs the one-time transfer without deploy-token headers or redirects, waits for server-side validation, and creates the upload analysis. It never prints the signed transfer URL or headers. With `--no-wait`, it still waits for the upload to become `ready`, then returns the newly queued analysis operation.

Create a pending application only for a new deployment; an update must reuse the exact existing app after `apps show`. Repository and base URLs must not contain credentials. Keep the same idempotency key only while retrying the exact same request body. If the installed CLI does not advertise the selected command, stop at the capability gate rather than substituting a raw Luma call.

Record the analysis and operation IDs. Summarize only structured topology, public HTTP routes, volumes, warnings, blockers, and environment variable names. Do not expose builder nodes, internal registry addresses, credential lease IDs, or raw builder output.

### 3. Resolve configuration

For every required variable:

1. Read the `configuration` object returned with public verdict `needs_input`. If resuming later, run `lae config show --app <app> --analysis <analysis> --format json`. Trust only names, `serviceKeys`, `required`, and `sensitive`; no value is returned.
2. Run `lae env list <app> --format json` and retain its current version for compare-and-set.
3. Explain why each required name is needed and which services consume it. Use `--service '*'` when one value is shared by every listed service, or set each listed service separately.
4. Direct the user to `lae env set <app> <name> --service <service-or-*> --expected-version <version> --value-stdin --idempotency-key <stable-key>` or the Web form. The deploy token must come from `LAE_DEPLOY_TOKEN` when stdin carries the value. Do not ask them to paste the value into the conversation, including for non-sensitive configuration.
5. Re-fetch `config show` and `env list`, then deploy the same stored analysis.
   Environment values are versioned separately and do not invalidate that
   analysis. Re-run inspection only when source, topology, routes, volumes, or
   build requirements changed.

If a Compose diff adds/removes a volume, changes a public route, or is destructive, show the structured diff and wait for explicit confirmation.

### 4. Deploy and watch

Use NDJSON for agent-driven long operations:

```text
lae deploy --app <app-id> --analysis <analysis-id> \
  --environment-version <version> --idempotency-key <stable-key> \
  --wait --format ndjson
```

Deploy only a completed analysis into its existing application. Reuse a caller-provided idempotency key for an identical retry and never reuse it with a changed body.

Consume events line by line. Persist the latest `operationId` and `cursor`. Report fixed phases and safe messages only. On disconnect, resume instead of creating another deployment:

```text
lae operation watch <operation-id> --after <cursor> --format ndjson
```

Do not call a deployment successful until the operation is terminal `succeeded` and every declared public HTTP route passes verification. Return the stable `*.itool.tech` URL(s) from the result; do not construct a domain locally.

### 5. Operate an application

Start with `lae apps show <app> --format json`. Then use the narrow action the user requested:

For a curated starter, inspect `lae templates list --format json`, then run `lae templates launch <template-id> --name <name> --slug <slug> --wait --idempotency-key <key>`. A template launch still goes through the same LAE Agent diagnosis and may return public verdict `needs_input`.

- `check-update`: inspect upstream changes; never deploy automatically unless the user explicitly enabled that policy.
- `suspend`/`resume`/`restart`: watch the returned operation to terminal state.
- `rollback`: show target revision, route/volume/env diff, then obtain explicit confirmation.
- logs: use `lae apps logs <app> [--service <key>] [--tail 120]`; request one bounded service tail and never dump suspected secrets into chat.
- metrics: use `lae apps metrics <app> [--service <key>] [--window 3600]`; keep the window between 60 seconds and 7 days.

If a cancel request races with a successful builder response, LAE operation state is authoritative. Watch until LAE reports a terminal state.

## Registration and billing boundary

The current CLI authenticates with an existing deploy token; email
registration/login and deploy-token creation are session-only Web flows. If the
user has no token, send them to the LAE Web login/account pages and stop until
they configure the resulting token locally. Do not substitute raw auth APIs,
read their mailbox, capture an OTP, or ask them to paste a token into chat.

The CLI may create a checkout session only when the configured deploy token has
the explicit `billing:checkout` scope. Show the plan, interval, price, provider,
and checkout URL, then require the user to open and approve it. Never complete
payment, change a plan automatically, or submit payment data. After the user
returns, re-run `whoami` and `lae plans list`; do not infer success from a
browser redirect.
