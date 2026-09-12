# AI Agent Skills

Luma includes two skills for AI coding assistants. They give the assistant repository-specific deployment and observability workflows, reference material and validation rules. They are installed in the assistant, not in the running cluster.

## Deployment workflows

[`luma-deployment-yaml`](../skills/luma-deployment-yaml/) helps generate and review single-service manifests and Compose sidecars, choose region and exposure, configure local storage, and troubleshoot builds or deployments.

For an existing application, it follows the recorded build and deployment workflow. A change of workflow must be explained and confirmed; a skill installation alone does not update the CLI or Control. Secrets remain references such as `${DATABASE_PASSWORD}`, not plaintext in YAML.

Example request:

> Review this Compose project, generate its Luma sidecar, and validate it. Keep the existing deployment workflow and use local persistent storage.

## Application observability

[`luma-observe`](../skills/luma-observe/) helps deploy and operate the optional, independent observability stack: Traefik request metrics, Nomad allocation failures, OTLP collection and notification delivery.

Installing the skill does not install the stack. The stack requires its own deployment; application tracing also requires the appropriate OpenTelemetry runtime instrumentation.

Example request:

> Set up luma-observe for this cluster, verify application metrics and failed-allocation alerts, and check notification delivery.

## Installation

From a local checkout, copy both skill folders to the assistant's user-level skills directory. For Codex:

```bash
mkdir -p ~/.codex/skills/luma-deployment-yaml ~/.codex/skills/luma-observe
cp -R skills/luma-deployment-yaml/. ~/.codex/skills/luma-deployment-yaml/
cp -R skills/luma-observe/. ~/.codex/skills/luma-observe/
```

For Claude Code:

```bash
mkdir -p ~/.claude/skills/luma-deployment-yaml ~/.claude/skills/luma-observe
cp -R skills/luma-deployment-yaml/. ~/.claude/skills/luma-deployment-yaml/
cp -R skills/luma-observe/. ~/.claude/skills/luma-observe/
```

For other assistants, use their supported skill installation mechanism with the linked source directories. Start a new conversation after installation. When updating Luma, refresh the skills from the matching checkout so their instructions stay aligned with the installed version.

## References

- [Deployment YAML](deployment-yaml.md)
- [Compose and local storage](compose-storage.md)
- [Observability](observability.md)
- [Deployment workflow records](../skills/luma-deployment-yaml/references/deployment-workflow.md)
