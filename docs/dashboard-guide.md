# Luma console guide

The console is available at `https://<control-domain>/dashboard/`. Sign in with a management token on a trusted device. It shares the same Control API as the CLI.

## First hour after bootstrap

1. Open `/dashboard/` and paste the management token from bootstrap.
2. Create **hello-world first install** (Applications → Create application). It is internal (`exposure: none`) and does not need extra DNS, Tailscale, registry, or LAE.
3. Use **First install** only for remaining cluster extras: Cloudflare zone details, Tailscale, egress, or a builder registry. A skipped optional check is not a failed install.
4. After hello-world is healthy, create a public service with `cn-edge` / `external-edge`.

LAE (the multi-tenant engine) is optional. The console hides it until Control has an LAE admin endpoint. First install does not configure it.

## Choose a workspace

| Workspace | What you can do |
| --- | --- |
| Overview | Review applications or nodes that need attention. |
| Applications | Inspect services, replicas, endpoints, logs, metrics, configuration and version history. |
| Delivery | Follow builds and deployment results. |
| Observability | Review alert incidents, application monitoring, resource metrics, logs, alert rules and notification channels. |
| Infrastructure | Manage nodes, regions, routes, storage and system maintenance. |
| First setup | Configure cluster dependencies and verify them. |
| Settings | Manage secrets, registry credentials, Git providers and storage configuration. |

The top bar shows your current location. Appearance, language and sign-out controls live in the sidebar. Choosing an appearance or language closes the preference menu. On desktop, the sidebar header contains the collapse control; narrow screens use the top-bar navigation opener.

## Create an application

Open **Applications → Create application** and choose a source:

- **Git repository**: select a connected provider or enter a repository URL, then choose a build node.
- **Container image**: configure an image, region, exposure, resources and environment variables.
- **YAML file**: edit the deployment manifest directly.
- **Application templates**: search single-service or Compose templates, then customize the configuration.

Selecting a template does not deploy it. Review the configuration and validate it before submitting. Manual YAML edits become the source used for validation and deployment; restoring the form asks before discarding those edits.

See [Deployment YAML](deployment-yaml.md) for the schema and [Compose & storage](compose-storage.md) for multi-service and persistent-volume behavior.

## Inspect an application

An application's tabs keep the same object context while switching between overview, services and instances, logs, metrics, configuration, and version history.

- **Overview** presents runtime status, placement, access addresses, storage and diagnostics.
- **Logs** offers application-filtered VictoriaLogs search and Tempo traces in Grafana, with an expand/restore button. These views require observability services. **Live logs** retains Control-backed service/instance output, filtering, pause, wrapping, copy and download; opening logs from a specific service selects this view directly. Runtime events and pull diagnostics are separate from application output.
- **Configuration** shows the registered deployment source and update time, with a copy action. A file switch appears when more than one configuration is available.
- **Versions** separates loading failures from an empty history and from rollback failures. A rollback requires confirmation and changes the running deployment.

A route configured as internal-only is not evidence that its application is unreachable from every private network. Check its exposure and node placement before changing ingress.

## Follow a route

Open **Infrastructure → Network → Routes**. Search by domain, application, node or address, or filter by ingress type. Selecting a route focuses its full configured path through shared nodes to the destination. The node-topology tab provides a separate view of cluster relationships.

The route view combines configuration and running-instance relationships. It is **not** a hop-by-hop connectivity test. DNS, certificates, routes and service health can fail independently; use the relevant diagnostic or route verification action to investigate.

## Metrics and notifications

Resource charts place values beside their history, with time axes and sample tooltips. Sampling gaps remain visible rather than being filled with invented measurements. A current snapshot and the latest historical sample can have different timestamps and values.

Alert incidents, rules and notification channels have separate pages. The alert-engine summary appears on the incident page. Notification channels show their configuration and delivery history; a queued notification is not a successful delivery. Saving a channel does not send a test message automatically.

Built-in resource monitoring and optional application-level observability have different requirements. See [Observability](observability.md) for the distinction and deployment details.

## Preview versus a live cluster

The public website's console illustrations use example names and values. Local development fixtures are useful for checking layout and interactions; neither a website illustration nor a local preview establishes production health. Verify live behavior against your own Control instance.

## Application secrets

Open Applications → select an application → Secrets to manage its scoped secrets. Add or rotate a value without leaving application details; values are write-only. Configuration references to stored global secrets are labeled separately. Use Manage global value for shared changes, or set an application override to leave other applications unchanged. If configuration cannot be loaded, only saved application secrets are listed.

Saving a secret updates Control storage. Existing instances do not receive the new value automatically: update/redeploy the application to apply it. A saved secret is not proof that an external credential has been rotated or that the running application has adopted it.
