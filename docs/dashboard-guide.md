# Luma console guide

The console is available at `https://<control-domain>/dashboard/`. Sign in with a passkey, or with the management token on a trusted device. It shares the same Control API as the CLI. See [Sign-in and passkeys](#sign-in-and-passkeys).

## First hour after bootstrap

1. Open `/dashboard/` and paste the management token from bootstrap. Then add a passkey in **Settings → Sign-in** so later sign-ins do not need the token.
2. Create **hello-world first install** (Applications → Create application). It is internal (`exposure: none`) and does not need extra DNS, Tailscale or registry.
3. Use **First install** only for remaining cluster extras: Cloudflare zone details, Tailscale, egress, or a builder registry. A skipped optional check is not a failed install.
4. After hello-world is healthy, create a public service with `cn-edge` / `external-edge`.

## Sign-in and passkeys

The browser never stores the management token. Signing in, with a passkey or by pasting the token once, makes Control set a session cookie (`HttpOnly`, `Secure`, `SameSite=Strict`) for the Control domain; Control keeps only a hash of it. A session ends when you sign out, after 2 hours without activity, or 24 hours after sign-in, whichever comes first. The CLI and CI keep using the management token unchanged.

Add the first passkey in **Settings → Sign-in** after signing in with the token, or from a browser that is not signed in, through a one-time link:

```bash
luma passkey enroll --name "MacBook Touch ID"
```

The link opens `/dashboard/enroll`, works once and expires after 15 minutes. Finishing it signs that browser in, so treat the link like a password. A passkey is bound to the Control domain (the WebAuthn RP ID): open the console at `https://<control-domain>/dashboard/`, not through an IP address or another host name. Signed-in requests that change state must come from that origin; Control rejects them otherwise.

**Settings → Sign-in** lists passkeys and browser sessions. Removing a passkey ends the sessions it opened. **Require a passkey** stops browsers from signing in with the management token and ends existing token sessions. It needs at least one passkey and turns off when the last passkey is removed.

Recovery never depends on a browser:

```bash
luma passkey require off
luma passkey list
luma passkey remove <id>
luma passkey sign-out
```

`luma passkey sign-out` ends every browser session, or one with `--session <id>`. The management token stays the root credential: whoever holds it can run these commands, so a leaked token still means rotating it.

Passkey verification runs in the Control image, which installs the `control` extra (the pinned `webauthn` package, Python 3.10+). A Control without it still offers token sign-in and reports passkeys as unavailable. The embedded Grafana at `/grafana` sits behind the same sign-in: Traefik asks Control (`/v1/auth/forward`) on every request and lets it through only with a valid console session or the management token, otherwise it answers 401. Inside Grafana the access is still anonymous Viewer, so the console needs no second login. Because Control makes the decision, `/grafana` is unavailable while Control is down; on the manager, Grafana stays reachable at `127.0.0.1:3100` for debugging.

## Choose a workspace

| Workspace | What you can do |
| --- | --- |
| Overview | Review applications or nodes that need attention. |
| Applications | Inspect services, replicas, endpoints, logs, metrics, configuration and version history. |
| Deployments | Follow builds and deployment results. |
| Observability | Review alert incidents, application monitoring (requests, logs and traces), resource metrics, alert rules and notification channels. |
| Infrastructure | Manage nodes (including joining new ones and cleaning up their disks), regions, network routes, storage and registry images. |
| Settings | Manage secrets, registry credentials, Git credentials, sign-in (passkeys and browser sessions) and system maintenance (Control and agent upgrades). |
| First install | Configure cluster dependencies and verify them. |

Settings and First install sit in the sidebar's Platform group. Creating an application and building from Git belong to Applications. The top bar shows your current location. Appearance, language and sign-out controls live in the sidebar. When the desktop sidebar is collapsed to icons, workspaces with secondary pages open a flyout menu. Choosing an appearance or language closes the preference menu. On desktop, the sidebar header contains the collapse control; narrow screens use the top-bar navigation opener.

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
