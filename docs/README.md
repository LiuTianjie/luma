# Luma documentation

New to Luma? Read the first two pages in order, then pick guides as you need them. [简体中文](README.zh-CN.md)

## Start here

1. [Getting started](getting-started.md): install the CLI, bootstrap a manager, deploy a first application, add nodes.
2. [Concepts](concepts.md): nodes, regions, exposure, egress and services, and how they fit together.

## Guides

| Guide | Covers |
| --- | --- |
| [Deploying applications](deploying.md) | Images, local builds, repository imports, Compose, recorded workflows and CI |
| [Operations](operations.md) | Logs, history, rollback, restart, removal, updates and node maintenance |
| [Compose and storage](compose-storage.md) | Multi-service applications and persistent volumes |
| [Secrets](secrets.md) | Tokens, application secrets and registry credentials |
| [Exposure model](exposure-model.md) | Every ingress mode with requirements and examples |
| [Egress gateway](egress-gateway.md) | Outbound proxy for image pulls and `proxy: true` services |
| [Observability](observability.md) | Metrics, alerts and the optional observe stack |
| [Console guide](dashboard-guide.md) | The web dashboard |
| [AI agent skills](ai-agent-skills.md) | Skills that teach coding assistants to deploy with Luma |
| [Troubleshooting](troubleshooting.md) | Known failures and their fixes |

## Reference

| Reference | Covers |
| --- | --- |
| [Manifest reference](deployment-yaml.md) | Every field of a service manifest |
| [CLI reference](luma-cli-reference.md) | Every command and option, generated from the CLI |
| [Control storage and recovery](control-storage.md) | The manager's SQLite state, retention, backup and restore |
| [Installation lifecycle](installation-lifecycle.md) | How installs and updates are validated on each machine |
| [Bootstrap profiles](profiles.md) | Role sets used by `luma bootstrap --profile` |

## For maintainers

[Release process](maintainers/release.md) · [Website](maintainers/website.md) · [Dashboard design](maintainers/dashboard-design.md)
