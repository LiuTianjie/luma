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
| A GitHub or Gitea repository | `luma import owner/repo` | A builder node clones, builds, pushes and deploys |
| A `docker-compose.yml` | `luma compose deploy luma.compose.yml` | Deploys all services together on one node |

`luma deploy` never builds source code. Building requires a builder node and a registry; see [Builds](#builds).

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
luma deploy api.yaml --dry-run    # print the Nomad job Control will submit
luma deploy api.yaml
```

Deploy is an upsert: redeploying the same `name` updates the existing Nomad job, and Nomad keeps the previous version for [rollback](operations.md#roll-back). Use immutable image tags or digests in production; a mutable tag such as `latest` makes rollback unpredictable.

Control performs these steps and prints each one:

1. store the manifest and scoped secrets;
2. render the Nomad job and, for `tailscale-relay` or `tcp-relay`, a Traefik route file;
3. create or update Cloudflare DNS (skip with `--skip-dns`);
4. submit the job and wait for the rollout;
5. probe the public route of `cn-edge` and `external-edge` services, recreating the allocation once if Traefik has not picked it up yet.

The default wait is 3000 seconds (`--timeout`). If a step fails after earlier ones changed the cluster, the deployment is recorded as `failed_partial` so `luma app remove` can still clean it up.

Every field is described in the [manifest reference](deployment-yaml.md); placement and ingress are explained in [Concepts](concepts.md) and the [exposure model](exposure-model.md).

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

Source builds need a builder node (any Linux node with Docker Buildx) and a registry the cluster can pull from:

```bash
luma registry serve --node builder          # deploy the managed registry on a node
luma build config --node builder --default-node builder
```

Luma picks the image architecture from the target region's nodes: an ARM node gets `linux/arm64`, a mixed region gets a multi-platform image. `--platform` is accepted only when it covers every target.

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

### Build runs and the queue

Imports and retries enter a persistent queue per repository, ref and deployment target: a newer submission for the same target cancels older queued ones, while `main` and `dev` never block each other. A busy builder keeps its requests queued without holding up other builders.

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
python -m pip install "luma-infra==0.1.366"
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
