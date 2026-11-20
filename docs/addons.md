# Pluggable addons & unified SSO

Luma Control ships as a focused, self-contained deployment control plane: node orchestration, image/Compose/YAML deployments, scoped secrets, deployment history, live container logs, Agent-backed resource metrics, and core alerting work out of the box with **zero addons installed**. [简体中文](addons.zh-CN.md)

When your cluster needs remote multi-runner Git builds, full-stack observability, or frontend product analytics and crash tracking, enable the official pluggable addons from **Platform → Addons** or with `luma addon`. Every addon shares **one Luma Control identity** (Passkey browser SSO and `luma login` CLI tokens) and auto-manages its internal secrets.

## Official addons

| Addon | Stack | Recommended RAM & placement | Auth mode |
| --- | --- | --- | --- |
| **Luma Build** (`builder`) | `luma-build` | >= 0.5 GiB RAM (SQLite queue; builds run on enrolled Runners; pin node recommended) | Control SSO (`control-sso`) |
| **Luma Observe** (`observe`) | `luma-observe` | >= 8 GiB RAM on Manager (13 observability components; pinned to Manager) | Control `forwardAuth` (`/grafana`) |
| **Luma Eye** (`eye`) | `luma-eye` | >= 1.5 GiB (`node-edge`) / 2 GiB (`all-in-one`); **pinned node required** for ClickHouse & SQLite volumes | Control SSO (`control-sso`) |

## Core vs addon UI capabilities

Luma's console adapts automatically to the addons running in your cluster (`luma.addon/v1` `uiCapabilities`). Uninstalled addons never clutter the sidebar or expose broken panels; installing an addon unlocks its contextual extension slots immediately.

| Surface | Control Core (0 addons) | Unlocked by `Luma Build` | Unlocked by `Luma Observe` | Unlocked by `Luma Eye` |
| --- | --- | --- | --- | --- |
| **Sidebar monitoring** | **Monitoring** (4 native views): Incidents, Metrics, Rules, Channels | — | Expands to **Observability**: adds Platform health, App monitoring (Grafana/logs/traces), Dashboard sharing | Adds **Frontend RUM** (`/observe/eye`) portfolio summary |
| **Deployments & Create** | Deployment timeline, image/YAML/template creation | Unlocks Git repository import (`/builds`), remote build retries, and Builder console link | — | — |
| **Application Detail** | Overview, Services, Alerts, Live Logs, Metrics, Config, Secrets, Versions | — | Adds overview pipeline status, **Dashboards** tab (Grafana + task board), historical log search & Tempo traces in **Logs**, Alertmanager alerts | Adds **Frontend** (`eye`) tab auto-matched by app slug or domain |
| **Alert rule presets** | 7 native Agent/Control presets (`node.*`, `app.memory`, `task.queue_age`) | Adds `build.failed` | Adds 11 HTTP, Nomad, log-match, and Taskiq/Celery presets | — |

## Unified Control authentication

Luma Control acts as the single Identity Provider (IdP) for all addons:

1. **Browser SSO (`control-sso`, used by Luma Build and Luma Eye)**: opening an addon console redirects to `https://<control-domain>/dashboard/addon-login?addon=<id>&challenge=...`, reuses your existing Passkey/dashboard session, issues a one-use PKCE ticket, and establishes an audience-bound opaque addon session (`las_...`). Signing out of Control or uninstalling the addon revokes all addon sessions immediately.
2. **ForwardAuth (`forward-auth`, used by Luma Observe)**: Traefik verifies `/grafana` requests against Control's `/v1/auth/forward` endpoint using the same dashboard session or management token.
3. **Zero-extra-token CLI & automatic URL discovery**: both `luma builder` and `luma eye` automatically reuse your active `luma login` token and discover the deployed addon URL directly from Control (no manual endpoint setup required). When targeting a separately hosted Builder or overriding the discovered endpoint, pass `--url`, set `LUMA_BUILD_URL`, or bind once with `luma builder use <url>`.

```bash
luma addon list
luma builder status
luma eye status
luma eye portfolio --since 24h
```

## First-time configuration & secret automation

You never need to hand-edit YAML files or manually invent internal tokens when installing an addon:

- **Auto-managed by Control (zero setup)**:
  - `LUMA_CONTROL_URL` (and `LUMA_DEPLOY_TOKEN` for `builder`) are wired automatically.
  - Internal secrets (`LUMA_EYE_ADMIN_TOKEN`, `LUMA_EYE_RELAY_SECRET`, `LUMA_EYE_AUTO_KEY`, `LUMA_OBSERVE_READ_TOKEN`, `LUMA_OBSERVE_INGEST_TOKEN`) are generated cryptographically on first configure and preserved across upgrades unless `--rotate-secrets` is passed.
  - `LUMA_EYE_AUTO_ORIGIN_SUFFIXES` is derived automatically from your Eye domain (for example `eye.example.com` → `.example.com,localhost,127.0.0.1`).
- **Credential reuse (zero duplicate entry)**:
  - When configuring **Luma Build** in **Addons Hub** or via CLI, select an already-saved Control container registry or Git provider instead of retyping passwords or PATs.
- **Minimal required inputs & live prechecks**:
  - **Luma Eye**: 1 domain + region + **mandatory pinned node** (auto-selected when the target region has 1 node; best candidate pre-selected when multiple nodes exist).
  - **Luma Observe**: 1 Manager region (auto-detected from the registered Manager node).
  - **Luma Build**: 1 domain + registry (inherited from Control when available) + recommended pinned node.
  - Before deploying, the install dialog checks the target node's real available and total RAM against the addon's `requirements` and warns clearly if memory is tight or insufficient.
  - After installing **Luma Build**, if 0 build runners are online, both **Addons Hub** and **Build from Git** show an inline runner enrollment guide (`luma builder token create` + `luma runner join`).

## Installing, configuring and uninstalling

### One-click install from the browser

Open **Platform → Addons**, click **Install** on any official addon, verify the pre-selected pinned node and live memory precheck, fill in the domain (if not pre-filled), and click **Save & install**. Control pulls the official prebuilt GHCR images declared in the addon spec and deploys the stack in place.

### Installing via CLI

```bash
# One-click install using official prebuilt GHCR images
luma addon install eye --domain eye.example.com --region cn --node manager --yes
luma addon install observe --region cn --yes
luma addon install builder --domain build.example.com --use-registry ghcr.io --use-git-provider github:my-account --yes
```

For dedicated public-IP telemetry nodes (`node-edge` preset on Luma Eye), pass `--preset node-edge --node <edge-node>`:

```bash
luma addon install eye --preset node-edge --domain eye.example.com --region global --node edge-1 --yes
```

### Building from local source (for development or custom forks)

Pass `--source <dir>` to build and push images locally before deploying through the same addon configuration pipeline:

```bash
luma addon install eye --source ./eye --yes
luma addon install builder --source ./builder --yes
luma addon install observe --source ./observe --yes
```

### Updating settings or uninstalling safely

```bash
luma addon show eye
luma addon configure eye --set retentionDays=14 --apply
luma addon uninstall eye
```

Uninstalling stops the addon services and routes, revokes its Control SSO sessions, and immediately hides its UI extension slots in the console. **Persistent data volumes (`eye-data`, `clickhouse-data`, `luma-build-data`) are preserved by default** unless `--delete-storage` is explicitly passed.

## Declarative `luma.addon/v1` specification

Official addons live under `luma/assets/addons/*.yaml` (`builder.yaml`, `eye.yaml`, `observe.yaml`). Each manifest declares its `auth` contract, `uiCapabilities`, `requirements` (minimum/recommended RAM and stateful node-pinning policy), `defaultImages`, topology `presets`, tiered `configSchema` (`basic`, `recommended`, `advanced`), and `autoSecrets`. External extensions can adopt the exact same schema to participate in Control SSO and schema-driven configuration.
