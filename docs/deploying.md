# Deploying applications

Every deployment goes through Luma Control: the CLI sends a manifest, Control renders a Nomad job, updates DNS and routes, submits the job and streams progress back.

```text
luma deploy app.yaml -> Luma Control -> Nomad job -> Nomad client -> Docker container
```

## Choose a workflow

| You have | Command | What happens |
| --- | --- | --- |
| A published container image | `luma deploy app.yaml` | Deploys the image named in the manifest |
| A local checkout | `luma build local .` | Builds with your Docker Buildx, pushes to the cluster registry, deploys |
| A GitHub or Gitea repository | `luma builder submit owner/repo` | An idle compatible Runner clones, builds and pushes; Builder then requests deployment from Control |
| A `docker-compose.yml` | `luma compose deploy luma.compose.yml` | Deploys all services together on one node |

`luma deploy` never builds source code. Source builds use the independent Builder, a Runner and its configured registry; see [Builds](#builds).

## Deploy an image

Create a manifest with `luma init` or write one by hand:

```yaml
name: api
image: ghcr.io/acme/api:1.4.2
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
resources:
  limits:
    memory: 512M
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
```

Then:

```bash
luma validate api.yaml            # schema, region/exposure rules; no network
luma deploy api.yaml --dry-run    # render a local Nomad preview; no deployment
luma deploy api.yaml
```

Deploy is an upsert: redeploying the same `name` updates the existing Nomad job, and Nomad keeps the previous version for [rollback](operations.md#roll-back). Use immutable image tags or digests in production; a mutable tag such as `latest` makes rollback unpredictable.

Control performs these steps and prints each one:

1. store the manifest and scoped secrets;
2. render the Nomad job and, for `tailscale-relay` or `tcp-relay`, a Traefik route file;
3. create or update Cloudflare DNS (skip with `--skip-dns`);
4. submit the job and wait for the rollout;
5. probe public routes and wait for provider/upstream convergence; persistent Traefik router misses can recreate the affected ingress, while persistent gateway errors can recreate the application allocation.

Concurrent deployments share an in-progress recovery of the same Traefik ingress. Control rechecks the route before starting recovery so a completed recovery does not trigger another unnecessary restart. Separate ingresses and healthy deployments continue independently.

The default wait is 3000 seconds (`--timeout`). If a step fails after earlier ones changed the cluster, the deployment is recorded as `failed_partial` so `luma app remove` can still clean it up.

Every field is described in the [manifest reference](deployment-yaml.md); placement and ingress are explained in [Concepts](concepts.md) and the [exposure model](exposure-model.md). To serve a public service through Alibaba Cloud ESA, add `cdn: esa`; see [CDN](cdn.md).

## Secrets

Reference variables in the manifest and keep their values out of Git:

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

Provide them at deploy time from a `.env` file, or store them once:

```bash
luma deploy api.yaml --env .env                         # only referenced variables are sent
luma secret set DATABASE_URL --scope api                # prompts for the value
luma secret import .env.production --scope api          # every variable in the file
```

Secrets are scoped by application name, so `api/DATABASE_URL` and `worker/DATABASE_URL` are separate. Registry credentials for private images are set with `luma registry login`. See [Secrets](secrets.md).

## Builds

Remote repository builds run on the decoupled **Luma Build** addon (`luma-build`) and token-authenticated **Build Runners** (`luma runner`). Install the addon once with `luma addon install builder` and attach one or more Linux or macOS runners with `luma runner join` (see [Pluggable addons & SSO](addons.md) and [Build runners](runners.md)).

When you are signed in to Luma Control (`luma login`), `luma builder` automatically discovers the cluster's Builder URL and reuses your Control credentials. You can also bind or override the endpoint explicitly with `luma builder use https://build.example.com` (or `--url` / `LUMA_BUILD_URL`).

### Native-architecture runners

Luma Build never falls back to slow QEMU emulation for ARM builds. Register native AMD64 and ARM64 runners to match your target cluster architecture:

```bash
luma builder token create x86-builder --weight 200 --cache-mode read-write
luma builder token create arm64-builder --weight 100 --cache-mode read-only
```

Then join each machine with `luma runner join --url https://build.example.com --token <runner-token>` (Linux or macOS with OrbStack / Docker Desktop). Each runner reports its actual native Docker platform and keeps a persistent local BuildKit cache (L1) backed by shared registry layer caching (L2).

When a build targets ARM64—including mixed AMD64/ARM64 requests—the scheduler routes it to a native ARM64 runner and verifies the Docker engine's native architecture before building. If no matching native runner is online, the build stays queued or fails fast rather than silently emulating.

### Submit a repository build

Submit a GitHub or Gitea repository directly from the CLI or from the Dashboard **Create application** Git form:

```bash
luma builder submit acme/app --ref main --env .env                      # GitHub owner/repo
luma builder submit https://gitea.example.com/acme/app.git --ref v1.4.0
```

For private repositories, save a Git provider token in Control once (shared with Builder via scoped secrets / Control SSO) and reference it:

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin
luma git-provider repos github:personal
luma builder submit --provider-id github:personal --repository acme/app --ref main
```

Builder looks for a single-service manifest (`.luma.yml`, `luma.yml`, `*.luma.yml`) or a Compose sidecar (`luma.compose.yml`, `.luma.compose.yml`, `*.luma.compose.yml`, `docker-compose.luma.yml`). Select one explicitly with `--compose-sidecar path/in/repo.yml`, or pass `--manifest local.yml` when the repository has none. For Compose repositories, services with `build:` are built and rewritten to the pushed `image:`. Check that locally with `luma compose validate --import-mode luma.compose.yml`.

### Dockerfile targets and pinned Git inputs

Use `build.target` to select a literal Dockerfile stage. A remote repository build can also supply named source contexts from other repositories:

```yaml
# docker-compose.yml; the same build fields work in a single-service manifest
services:
  worker:
    build:
      context: .
      dockerfile: Dockerfile
      target: release
      x-luma-git-contexts:
        library:
          lock: deploy/library.lock.json
```

`lock` is a path relative to the primary repository root, not an inline object or a path relative to `build.context`. Its JSON contains exactly `repository`, `commit` and `archive_sha256`:

```json
{
  "repository": "https://gitea.example.com/acme/library.git",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "archive_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Replace these example hashes with the full lowercase commit and SHA256 of `git archive --format=tar.gz <commit>`. The runner verifies both before supplying the exported files as the BuildKit context `library`, usable with `COPY --from=library`. Hosted archive endpoints may produce different bytes and are not interchangeable with this archive.

Additional repositories must use the same HTTPS origin (host and port) as the primary repository, without embedded credentials, query or fragment. They use the selected primary Git provider credential; that credential must have access to every locked repository. Fetching and verification happen outside Docker. Only verified exported source enters BuildKit; Git credentials, helpers and `.git` metadata do not enter Docker contexts or image layers.

`luma build local` does not support `x-luma-git-contexts`, including in Compose builds. Use remote `luma builder submit` for these inputs; do not move provider credentials into a Dockerfile to bypass the restriction. `build.target` alone does not require an additional Git input.

### Build runs and the queue

Submitted builds enter the persistent `luma-build` SQLite queue and move through two decoupled stages:

- **Build.** Compatible idle runners claim queued leases based on native platform, cache affinity (preferring the repository's previous runner during a short affinity window so L1 BuildKit cache hits stay warm), and runner weight. Registry push credentials and Git tokens are scoped to `luma-build`, leased in memory only for the active build, and cleared when the job finishes.
- **Deploy.** Once the runner pushes the immutable image, it is immediately released for the next build while Builder's deploy worker submits the rendered rollout to Luma Control. On Control, up to eight independent applications can deploy concurrently while the same application (`name`) and conflicting domains or TCP relay ports remain serialized. Within a single branch/ref, newer submissions supersede older pending builds so stale revisions never overwrite newer rollouts.

```bash
luma builder list --app api --status failed
luma builder logs <build-id>
luma builder retry <build-id>
luma builder cancel <build-id>
```

Closing the CLI after a build is accepted does not cancel it. Queued requests survive restarts; see [Build runners](runners.md) for runner management and cache tuning.

### Build a local checkout and managed registry

When you prefer to build directly on your workstation with local Docker Buildx instead of queuing on remote runners, configure a cluster registry (an external OCI registry such as GHCR/ACR, or a self-hosted registry via `luma registry serve`) and set Control's build target with `luma build config`:

```bash
luma registry serve --node builder
luma build config --registry-host 100.64.0.70:5000 --push-host 100.64.0.70:5000
```

Then build and deploy from your local checkout:

```bash
cd app
luma build local . --env .env
```

The project identity comes from the `origin` remote (`--repo-url` otherwise). Luma builds with your local Docker Buildx, verifies the Docker engine's native architecture for ARM builds, pushes to the configured registry, and submits the resulting immutable image to Control. Your machine must reach the configured registry. Options: `--platform`, `--context`, `--dockerfile`, `--compose-sidecar`, `--builder` (reuse a Buildx builder), and `--proxy` (HTTP proxy for base images and `RUN` steps).

## Compose applications

Keep `docker-compose.yml` standard and add a Luma sidecar that says where and how it runs:

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --env .env
```

All services of one application run on one node and share a network namespace. Persistent volumes use local storage on that node by default. `cloudflare-tunnel` is not supported for Compose. See [Compose and storage](compose-storage.md).

## Recorded workflows

Control remembers how each application was last built and deployed successfully (local build, builder submission, image deploy or Compose, plus ref, platform, region and similar options). The next `deploy`, `builder submit`, `build local`, or `compose deploy` compares itself with that record:

- no record, or a match: proceed and refresh the record;
- a difference: show it and stop until you confirm. In a terminal you are asked; in JSON, quiet or non-interactive mode the command exits nonzero. Rerun with `--accept-workflow-change` only after reviewing the difference.

```bash
luma workflow show api
luma workflow record api --note "Built on Luma Build runners" -- builder-submit acme/api --ref main
luma workflow run api --path ~/src/api
```

Records never contain tokens or `.env` contents. Notes are free text; do not put secrets in them.

## CI

CI can use the CLI without a saved login:

```bash
python -m pip install "luma-infra==0.2.93"
export LUMA_CONTROL_URL=https://luma.example.com
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
luma deploy deploy/app.yaml --format ndjson --timeout 3000
```

Connection settings are resolved from command-line flags, then environment variables (`LUMA_CONTROL_URL`, `LUMA_DEPLOY_TOKEN`, `LUMA_CONTROL_CONTEXT`, `LUMA_INSECURE`, `LUMA_RESOLVE_IP`), then the saved login. A `--control-url` that differs from the saved login never borrows its token. Commands with `--format` support `text`, `json` and `ndjson`; always check the exit status too.

On a workstation, keep several clusters as saved logins and pick one per command:

```bash
luma login https://staging.example.com --token-stdin < staging.token
luma context list
luma deploy app.yaml --control-context staging
```
