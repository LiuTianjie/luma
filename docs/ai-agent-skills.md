# AI Agent Skills

Luma includes three skills for AI coding assistants. They give the assistant repository-specific deployment and observability workflows, reference material and validation rules. They are installed in the assistant, not in the running cluster.

## Deployment workflows

[`luma-deployment-yaml`](../skills/luma-deployment-yaml/) helps generate and review single-service manifests and Compose sidecars, choose region and exposure, configure local storage, and troubleshoot builds or deployments.

For an existing application, it follows the recorded build and deployment workflow. A change of workflow must be explained and confirmed; a skill installation alone does not update the CLI or Control. Secrets remain references such as `${DATABASE_PASSWORD}`, not plaintext in YAML.

Example request:

> Review this Compose project, generate its Luma sidecar, and validate it. Keep the existing deployment workflow and use local persistent storage.

## Application observability

[`luma-observe`](../skills/luma-observe/) helps deploy and operate the optional, independent observability stack: Traefik request metrics, Nomad allocation failures, OTLP collection and notification delivery.

The skill distinguishes observe's vmalert/Alertmanager rules from Control's dashboard-managed application alert presets, and covers authenticated Grafana access, telemetry retention and notification setup.

Installing the skill does not install the stack. The stack requires its own deployment; application tracing also requires the appropriate OpenTelemetry runtime instrumentation.

Example request:

> Set up luma-observe for this cluster, verify application metrics and failed-allocation alerts, and check notification delivery.

## Log investigation

[`luma-logs`](../skills/luma-logs/) uses `luma app alerts`, `luma app log-query`,
runtime events and deployment history to investigate application failures. It
queries authenticated Control APIs, without exposing storage ports or extracting
credentials. See [the log query API](log-investigation.md).

Example request:

> Analyze this application's errors over the last hour, distinguish retries from
> final task failures, and explain the impact with log evidence.

## Installation and automatic updates

Install the suite bundled with the currently installed CLI:

```sh
luma skills install --agent codex
luma skills install --agent claude
# Other assistants, with a remembered custom destination:
luma skills install --path /absolute/path/to/assistant/skills
luma skills status --format json
```

`luma update` and the installer refresh existing suites from the **same release**.
For pip/uv upgrades, the next ordinary Luma command also synchronizes the packaged
skills. No skills are downloaded from a separate `main` branch. Existing manual
copies in Codex/Claude directories are adopted, and newly added Luma skills join
that suite. Assistants with no existing Luma suite are left alone until `install`.
`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and registered custom paths are supported. A
privileged installer updates the target user's default directories as that user;
non-default assistant paths should first be registered with `skills install`.

Each skill records the paired version and content hashes in `.luma-managed.json`.
On upgrades, changed local files are backed up to `luma-skill-backups` beside the
assistant's skills directory before replacement. The backup path is printed.
Unchanged managed files are replaced without accumulating backups; obsolete files
inside a skill disappear with the directory replacement. A symlinked skill is
left untouched with an explicit warning. Permission failures do not fail the CLI
upgrade, but are reported as incomplete synchronization. Resolve them and run:

```sh
luma skills sync
# Restore the bundled content even after editing an already-current version:
luma skills sync --force
```

`status` is read-only and reports pairing and local modifications. Same-release
local edits are left in place until a new bundle arrives or `--force` is used.
Keep reusable custom instructions separately or reapply them from the backup.
Source checkouts do not auto-sync on ordinary command execution; use `skills sync`
explicitly while developing. All skill references are included in wheel and source
packages. Installing a skill does not upgrade Control or deploy luma-observe.
Reload the skill or start a new assistant conversation after an update.

## References

- [Deployment YAML](deployment-yaml.md)
- [Compose and local storage](compose-storage.md)
- [Observability](observability.md)
- [Deployment workflow records](../skills/luma-deployment-yaml/references/deployment-workflow.md)
