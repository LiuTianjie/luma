# Build runners

`luma builder` manages the independent `luma-build` queue. `luma runner` joins and controls the build process on the current machine. A runner needs Docker with Buildx and Git; it does not need Nomad or `luma node join`. Linux uses systemd; macOS uses a system launchd service with the invoking user's Docker environment.

Control’s **Create application → Git repository** form submits to this same queue and follows build logs in place. It does not select a cluster build node or configure an internal registry. Control discovers the public domain of the `luma-build` application (`server` service for Compose); for a separately hosted Builder, configure `LUMA_BUILD_URL` on Control. Builder still needs its destination registry (such as ACR), Git credentials and Control deployment target configured.

## Join and start

On an operator machine signed in to Luma Control (`luma login`), `luma builder` auto-discovers the deployed Builder URL (run `luma builder use https://build.example.com` only if you want to override it or target a standalone Builder), then create a runner token:

```bash
luma builder use https://build.example.com
luma builder token create my-runner --cache-mode read-write
```

On the machine that will build images:

```bash
luma runner join --url https://build.example.com --token lbr_xxx
luma runner status
luma runner logs --follow
```

Join installs and verifies build tools, checks the Docker engine, verifies the token and warms BuildKit without taking a queued build. Only after those checks pass does it save the configuration and start the background service. The process survives closing SSH/the terminal, starts automatically at boot and restarts after an unexpected failure. The operating system may request sudo to install/manage the service. macOS still needs its Docker engine (OrbStack or Docker Desktop) to be running, which can require user login.

Use `--token-stdin` to read a token from stdin, or `LUMA_RUNNER_TOKEN` to supply it from the environment. The service stores it in `/opt/luma/runner/config.json`, readable only by the runner's OS user and root. The system service command contains the config path, not the token. The first join saves the invoking user's home, Docker context and proxy settings. Joining again as that user refreshes the environment; start/restart reuse the saved environment. BuildKit metadata stays in `~/.luma-runner` unless `--state-dir` is set.

## Build prerequisites and network preflight

Both `luma runner join` and `luma node join` install missing Git/Buildx on apt-based Linux and verify them in the same private Docker configuration used for builds. A new Linux runner can install the Docker engine as well; an existing engine is not replaced or restarted to add Buildx. Docker CE uses `docker-buildx-plugin`; Ubuntu/Debian Docker uses `docker-buildx`.

On macOS, start OrbStack or Docker Desktop first. Missing Git/Buildx are installed with native Homebrew: `/opt/homebrew` on Apple Silicon (including a Rosetta shell), `/usr/local` on Intel. Run join as the user who owns Docker/Homebrew. Build platforms come from the actual Docker engine; native ARM requirements still apply.

Join bootstraps the actual persistent BuildKit instance and performs a cache-only `FROM scratch` preflight with `# syntax=docker/dockerfile:1`. This prepares the BuildKit image and Dockerfile frontend before reporting success. It does not build or publish an application image. The foreground runner repeats the tool and BuildKit preflight before it can lease tasks. A failed preflight preserves existing runner service configuration; node enrollment rolls back if BuildKit preparation fails.

Docker has two separate network paths: the Docker daemon pulls the BuildKit container image, while BuildKit resolves the Dockerfile frontend and base images itself. A daemon proxy does not configure BuildKit. On restricted networks, use a reachable HTTP proxy with `luma runner join --proxy http://proxy-host:7890`; existing token/options are reused. Wait for active work to finish before rejoining. Node join uses the region's returned egress proxy for BuildKit. Configure daemon image-pull access separately if bootstrapping the BuildKit container fails; join surfaces that failure immediately. Application-specific images and dependencies are still resolved when their builds run.

BuildKit also supports opt-in `docker.io` registry mirrors without any hardcoded third-party mirror defaults:

- Set `LUMA_RUNNER_DOCKER_MIRRORS` (comma-separated mirror hosts or URLs, for example `LUMA_RUNNER_DOCKER_MIRRORS="mirror.example.com"`) in the runner environment or before `luma runner join`;
- Or configure `registry-mirrors` in the host's `/etc/docker/daemon.json` — the runner automatically reads those mirrors when rendering `buildkitd.toml` (`[registry."docker.io"]`).

Every configured mirror host is automatically appended to BuildKit's `NO_PROXY` list so mirror pulls connect directly even when a runner HTTP proxy is configured.

## Daily commands

```bash
luma runner stop
luma runner start
luma runner restart
luma runner status --format json
luma runner logs --lines 100
luma builder token list
```

`stop` stops the local service and disables automatic startup while retaining its configuration. `start` reuses that configuration and enables automatic startup again. `restart` replaces the running process. Stop/restart interrupts an idle HTTP long poll instead of waiting for the server's 15-second response. These commands control one runner service per machine; repeated joins update it instead of adding another service. A stop/restart during a build cancels the local work; wait for builds to finish when interruption is undesirable.

macOS service logs use `/opt/luma/runner/logs/runner.log`, rotating at 10 MiB with 5 backups. Linux uses journald. `luma runner logs --follow` follows across rotations.

`runner status` reports the **local service**. `builder token list` reports **server-observed heartbeats**; the online threshold is 90 seconds. A running service can still have a network or authentication problem; inspect its logs and the remote list. `luma runner run` is explicitly foreground execution for debugging and containers. The old `luma builder runner` command remains a foreground compatibility entry point.

## Recover a failed Linux service

Luma 0.2.81 fixes the quoted `WorkingDirectory` generated by earlier runner service commands. If systemd reports `WorkingDirectory= path is not absolute`, upgrade the CLI on that runner machine, then run `luma runner start`. It rewrites the service definition while keeping the saved token and options. The directory from which you invoke the command is irrelevant. Linux logs come from journald (`luma runner logs`), not the macOS log-file path. Service-command failures now show the failed operation and sanitized diagnostics.

## Migrate existing runners

Existing registrations and names remain valid. Upgrade the Luma CLI on the runner machine and deploy the new builder service before joining. Stop its old foreground command with Ctrl-C (or stop its old Docker container), then run `luma runner join` with the **existing token**. Do not create another runner name. An existing manually installed `luma-build-runner.service` is replaced by the managed service after successful validation.

If the token was lost, rotate it under the same registered name on the operator machine:

```bash
luma builder token create my-runner --rotate
```

Use the new token in `luma runner join` on that runner machine. Rotation keeps the runner name and ID and invalidates the previous token. Reapply nondefault weight, concurrency and cache flags when rotating. The previous process must be upgraded to this runner release to exit automatically when its old token is rejected.

## Delete a runner

Use the builder dashboard's **Delete** action or:

```bash
luma builder token delete my-runner
```

Deletion removes the registration and token from the list. It cancels builds currently assigned to that runner and queued builds explicitly pinned to it. Completed build history remains, and already-built images waiting for deployment continue through the deploy queue. Old revoked entries are removed when the updated builder starts. The previous `token revoke` command/API is retained as a deletion compatibility alias; it no longer creates a revoked row.

Updated runners treat HTTP 401/403 as terminal authentication rejection: they cancel local work and exit successfully so systemd, launchd and the documented Docker restart policy do not relaunch them repeatedly. Network timeouts and HTTP 5xx remain retryable. To reconnect a deleted runner, create a new token and join again. Deletion does not remove the machine's local BuildKit cache or uninstall Luma.

## Cache and containers

For a slow-upload home Mac, join with a read-only registry cache:

```bash
luma runner join --url https://build.example.com --token lbr_xxx --cache-mode read-only --cache-keep-gb 25
```

The independent builder's cache-affinity wait is 3 seconds by default (`LUMA_BUILD_AFFINITY_SECONDS`), separate from Control's builder scheduling. Local BuildKit cache defaults to 20 GiB. `--workers` sets local worker slots; the token's `--concurrency` caps server assignments.

Linux containers can use the independent runner image instead of installing a host service:

```bash
docker run -d --name luma-runner --restart=on-failure --network=host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v luma-runner-state:/var/lib/luma-runner \
  -e LUMA_BUILD_URL=https://build.example.com \
  -e LUMA_RUNNER_TOKEN=lbr_xxx \
  ghcr.io/liutianjie/luma-build-runner:v0.2.76
```

Docker's `on-failure` policy does not start a container after a Docker-daemon/host restart. Use `luma runner join` for host boot startup. Do not use `--restart=always` if deletion must leave the process stopped.

## Upgrade order

Luma/Control and the independent builder have separate release versions. Upgrade Control and the Luma CLI first, then deploy the matching builder release, then migrate/restart runner processes. `runner join` requires the builder's `/v1/runner/join` endpoint and gives an upgrade error with older servers. Updating Control alone does not deploy builder or restart standalone runner processes. See the [release guide](maintainers/release.md#independent-builder-release) and [builder deployment files](../builder/README.md).
