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

The Dashboard Git form and `luma builder submit` use the independent Builder queue. Bind the CLI once with `luma builder use https://build.example.com`. No build-node or registry selection is needed: compatible idle Runners claim tasks and use Builder’s configured destination, such as Alibaba Cloud ACR. See [Runner setup](runners.md).

### Legacy Control builds

The following `luma import` / `luma build config` instructions apply to the existing Control-managed build path; the Dashboard Git form uses the independent Builder instead.

Source builds need a node with Docker Buildx (Linux, or macOS with OrbStack / Docker Desktop) and a registry the cluster can pull from:

```bash
luma registry serve --node builder          # deploy the managed registry on a node
luma build config --node builder --default-node builder
```

Luma picks the image architecture from the target region's nodes: an ARM node gets `linux/arm64`, a mixed region gets a multi-platform image. `--platform` is accepted only when it covers every target.

### Native builders

Register AMD64 and ARM64 builders to route builds by their target architecture:

```bash
luma build config --node builder --node air3 --default-node builder \
  --registry-host 100.64.0.70:5000 --push-host 100.64.0.70:5000
```

Upgrade Control and all builder agents first. Repository builders must advertise `native-arm-build-v1`; automatic selection also uses `repository-build-plan-v1`. macOS with OrbStack or Docker Desktop is supported. `luma status` reports each builder's native container platform.

When declared builders have different native architectures, Control first resolves the repository manifest (including a selected Compose sidecar and per-service placement), locks its Git commit, then queues the build on a matching native builder. Any build containing ARM64, including mixed AMD64/ARM64 builds, must use a native ARM64 builder. A busy or offline native builder remains queued; a missing native ARM builder is an error. Runtime secrets stay on Control until deployment.

`--build-node` can pin a compatible builder but cannot bypass the ARM requirement. Before every ARM build, Luma verifies the actual Docker engine of every Buildx worker; emulated platform advertisements are insufficient. This covers imports, Compose, retries and `luma build local`, including custom Buildx builders. Upgrade an older local CLI before building ARM images. Unverifiable remote/Kubernetes Buildx drivers are rejected for ARM builds.

A mixed AMD64/ARM64 request runs on the ARM builder, where its AMD64 portion may use emulation; it is not split across machines. Different ARM architectures (for example ARMv7 and ARM64) require separate requests. Builds without ARM retain their existing fallback behavior. Set `pushHost` to an address reachable from every builder, rather than `localhost`.

### Build a local checkout

```bash
cd app
luma build local . --env .env
```

The project identity comes from the `origin` remote (`--repo-url` otherwise). Luma builds with your local Docker Buildx, pushes to the cluster registry under the same repository path as `luma import`, then deploys. Your machine must reach the registry. Options: `--platform`, `--context`, `--dockerfile`, `--compose-sidecar`, `--builder` (reuse a Buildx builder) and `--proxy` (HTTP proxy for base images and `RUN` steps).

### Import a repository

```bash
luma import acme/app --ref main --env .env                      # GitHub owner/repo
luma import https://gitea.example.com/acme/app.git --ref v1.4.0
```

For private repositories, save a token once and refer to it:

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin
luma git-provider repos github:personal
luma import --provider-id github:personal --repository acme/app --ref main
```

Import looks for a single-service manifest (`.luma.yml`, `luma.yml`, `*.luma.yml`) or a Compose sidecar (`luma.compose.yml`, `.luma.compose.yml`, `*.luma.compose.yml`, `docker-compose.luma.yml`). Select one explicitly with `--compose-sidecar path/in/repo.yml`, or pass `--manifest local.yml` when the repository has none. For Compose repositories, services with `build:` are built and rewritten to the pushed `image:`. Check that locally with `luma compose validate --import-mode luma.compose.yml`.

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

Replace these example hashes with the full lowercase commit and SHA256 of `git archive --format=tar.gz <commit>`. Builder verifies both before supplying the exported files as the BuildKit context `library`, usable with `COPY --from=library`. Hosted archive endpoints may produce different bytes and are not interchangeable with this archive.

Additional repositories must use the same HTTPS origin (host and port) as the primary repository, without embedded credentials, query or fragment. They use the selected primary Git provider credential; that credential must have access to every locked repository. Fetching and verification happen outside Docker. Only verified exported source enters BuildKit; Git credentials, helpers and `.git` metadata do not enter Docker contexts or image layers.

Require the protocol explicitly when importing:

```bash
luma import --provider-id gitea:team --repository acme/app --ref "$APP_COMMIT" \
  --compose-sidecar deploy/luma.compose.yml \
  --require-build-capability repository-build-inputs-v1
```

This requires a CLI that supports the flag, Control capability `required-build-capability-v1`, and the selected ready Builder advertising `repository-build-inputs-v1`. Control checks the live Builder again and requires the build result to confirm the capability before deployment; retries preserve the requirement. Missing support stops the operation rather than ignoring the additional inputs. Source implementation alone does not prove the feature is released or running: publish the supporting Luma version and upgrade Control and Builder before using this path.

An external `--manifest` automatically requires the same capability, even without the flag. Builder uses its validated build and placement fields only when the repository has no manifest; a repository manifest takes precedence for both building and deployment. Runtime environment and secrets from the external manifest are never forwarded as build inputs. Upgrade Control and Builder before using an external manifest with this CLI.

`luma build local` does not support `x-luma-git-contexts`, including in Compose builds. Use remote `luma import` for these inputs; do not move provider credentials into a Dockerfile to bypass the restriction. `build.target` alone does not require an additional Git input.

### Build runs and the queue

Imports and retries enter a persistent queue and move through two stages:

- **Build.** Builds never wait for another build of the same application, because every attempt pushes its own immutable image. They wait only for a free compatible builder. Without `--build-node`, every declared builder with the right native platform and agent capabilities forms a pool: whichever becomes free first takes the next build. A project's previous builder holds its BuildKit cache, so it gets a short head start (`LUMA_BUILD_AFFINITY_WAIT_SECONDS` on Control, default 20) before another idle builder may take over. Pooling requires `registryHost` and a `pushHost` that every builder reaches, rather than `localhost`. `--build-node` pins one builder.
- **Deploy.** Up to eight independent applications can deploy concurrently. The same application (the manifest `name`) and conflicting domains or TCP relay ports remain serialized. Within one repository and ref, the latest submission wins: a newer finished image can deploy while an older image is still building, and that older image cannot overwrite it later. Local build order is reserved before building, not when uploading finishes. A rollout already running finishes before the next revision; `main` and `dev` are ordered for a shared target, never canceled by each other.

Canceling a build during rollout stops its exact Nomad deployment (honoring the job's automatic rollback policy), interrupts the Control wait, and releases its queue slot when the executor exits. Repeated clicks or elapsed time do not mark a still-running executor as canceled. If Nomad cannot confirm the stop, the attempt reports the cancellation error instead of claiming success.

The builder is released as soon as its image is pushed, so the next build starts while the previous one deploys. `LUMA_BUILD_QUEUE_CONCURRENCY` (default 4) limits concurrent build stages on Control; deployments have their own slot.

```bash
luma build list --app api --status failed
luma build logs <build-id>
luma build retry <build-id>
luma build cancel <build-id>
```

Closing the CLI after a build is accepted does not cancel it. Queued requests survive a Control restart; interrupted active builds are marked failed instead of being replayed.

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

Control remembers how each application was last built and deployed successfully (local build, import, image deploy or Compose, plus ref, builder, platform, region and similar options). The next `deploy`, `import`, `build local`, `build retry` or `compose deploy` compares itself with that record:

- no record, or a match: proceed and refresh the record;
- a difference: show it and stop until you confirm. In a terminal you are asked; in JSON, quiet or non-interactive mode the command exits nonzero. Rerun with `--accept-workflow-change` only after reviewing the difference.

```bash
luma workflow show api
luma workflow record api --note "Built on the builder node" -- import acme/api --ref main
luma workflow run api --path ~/src/api
```

Records never contain tokens or `.env` contents. Notes are free text; do not put secrets in them.

## CI

CI can use the CLI without a saved login:

```bash
python -m pip install "luma-infra==0.2.88"
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

## Independent build queue

`luma builder submit` uses the separate `luma-build` service and token-authenticated runners. It has its own runner pool, credentials, queue and releases. See [Build runners](runners.md) for setup and the distinction from Control-managed `luma import` / `luma build`.
