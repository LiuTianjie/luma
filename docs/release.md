# Release

Luma can be distributed without asking users to clone the repository.

## Recommended Release

1. Bump the package version before committing code that should be distinguishable by `luma version`:

```bash
python scripts/bump-version.py
```

Use `--minor`, `--major`, or `--set 0.2.0` when patch bumping is not appropriate. Verify without changing files:

```bash
python scripts/bump-version.py --check
```

2. Push the repo to GitHub.
3. Build and publish the control API image with GitHub Actions:

```bash
git push origin main
```

The `Build Control Image` workflow publishes:

- `ghcr.io/liutianjie/luma-control:latest` from `main`
- `ghcr.io/liutianjie/luma-control:main-<sha>` from `main`
- `ghcr.io/liutianjie/luma-control:<tag>` from `v*` tags
- `ghcr.io/liutianjie/luma-control:sha-<7-char-sha>` from an authorized
  `workflow_dispatch` run on any branch or tag

The manual tag is commit-scoped. It does not add `latest` to a topic branch or
change the existing `main-*`/release-tag channels. Run the workflow only after
the candidate ref is pushed, and verify that the completed run's `headSha`
matches the commit you intend to install:

```bash
BRANCH=codex/lae-foundation
git fetch origin "$BRANCH"
FULL_SHA="$(git rev-parse "origin/$BRANCH^{commit}")"
SHORT_SHA="$(printf '%s' "$FULL_SHA" | cut -c1-7)"

gh workflow run control-image.yml --ref "$BRANCH"
gh run list --workflow control-image.yml --branch "$BRANCH" \
  --event workflow_dispatch --limit 5 \
  --json databaseId,headSha,status,conclusion

RUN_ID=<database-id-for-the-matching-head-sha>
gh run watch "$RUN_ID" --exit-status
test "$(gh run view "$RUN_ID" --json headSha --jq .headSha)" = "$FULL_SHA"
test "$(gh run view "$RUN_ID" --json conclusion --jq .conclusion)" = success

CONTROL_IMAGE="ghcr.io/liutianjie/luma-control:sha-$SHORT_SHA"
docker buildx imagetools inspect "$CONTROL_IMAGE"
```

Do not select a run only because it is the newest one; branch concurrency and
re-runs can make that assumption wrong. A `workflow_dispatch` run can target a
non-default branch with `--ref`, but the workflow itself must already be
available to GitHub Actions from the repository's default branch.

## Candidate Manager Upgrade And Rollback

Use the same full commit for the CLI source archive and the commit-scoped
Control image. `install-luma.sh` accepts a full 40-character Git commit as
`LUMA_INSTALL_REF`, so manager and fleet updates do not need a mutable branch
name. Before changing the manager, record the current Nomad job version and
image in the change record:

```bash
PREVIOUS_JOB_VERSION="$(nomad job inspect -json luma-control | jq -er .Version)"
PREVIOUS_CONTROL_IMAGE="$(nomad job inspect -json luma-control | \
  jq -er '.TaskGroups[] | select(.Name == "luma-control") | .Tasks[] | select(.Name == "luma-control") | .Config.image')"
nomad job history -p luma-control
```

Also record a known-good Git install ref, normally the current release tag, as
`PREVIOUS_INSTALL_REF`. Then run the candidate update on the manager. Keep the
LAE Control environment exported in the same shell when this is an LAE-aware
Control rollout:

```bash
FULL_SHA=<verified-40-character-commit>
SHORT_SHA="$(printf '%s' "$FULL_SHA" | cut -c1-7)"
CONTROL_IMAGE="ghcr.io/liutianjie/luma-control:sha-$SHORT_SHA"

export LUMA_CONTROL_IMAGE="$CONTROL_IMAGE"
luma update manager --install-ref "$FULL_SHA" --domain luma.itool.tech

luma version --control-url https://luma.itool.tech
curl --fail --silent --show-error https://luma.itool.tech/v1/health
nomad job status luma-control
```

The current manager update path preserves Control state and user jobs, but it
does reconcile firewall TCP relay ports, Traefik when the manager has the
`edge` role, the watchdog, installed config/state, and the `luma-control` Nomad
job. It does not restart Docker or Nomad, run egress setup, or redeploy user
applications. Treat it as a control-plane maintenance change and keep the
pre-change values above until post-update checks pass.

The Control job has Nomad `AutoRevert`, but operators must still verify the
public health endpoint and the running image. If the new Control allocation is
unhealthy, restore the prior Nomad job immediately:

```bash
nomad job revert luma-control "$PREVIOUS_JOB_VERSION"
nomad job status luma-control
curl --fail --silent --show-error https://luma.itool.tech/v1/health
```

That first rollback restores the Control job spec; it does not restore the
locally installed CLI. After service recovery, return both CLI and image to the
recorded release:

```bash
PREVIOUS_INSTALL_REF=<known-good-tag-or-40-character-commit>
export LUMA_CONTROL_IMAGE="$PREVIOUS_CONTROL_IMAGE"
luma update manager --install-ref "$PREVIOUS_INSTALL_REF" --domain luma.itool.tech
```

If the candidate CLI itself cannot run the rollback, reinstall the known-good
CLI first, then repeat the manager refresh:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | \
  LUMA_INSTALL_REF="$PREVIOUS_INSTALL_REF" sh
export LUMA_CONTROL_IMAGE="$PREVIOUS_CONTROL_IMAGE"
~/.local/bin/luma update manager --install-ref "$PREVIOUS_INSTALL_REF" \
  --domain luma.itool.tech
```

4. Configure PyPI Trusted Publishing once for the `luma-infra` project:

- owner: `LiuTianjie`
- repository: `luma`
- workflow: `pypi.yml`
- environment: `pypi`

The package distribution name is `luma-infra`; the installed console command remains `luma`.

5. Create a tag to publish a versioned image, GitHub archive, and PyPI package:

```bash
git tag v0.1.275
git push origin main --tags
```

The `Publish Python Package` workflow builds wheel and sdist, runs `twine check`, and publishes with `pypa/gh-action-pypi-publish@release/v1` through OIDC. Do not store a long-lived `PYPI_API_TOKEN` secret.

6. CI users install with:

```bash
python -m pip install "luma-infra==0.1.275"
```

Interactive users can still install with:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.275 sh
```

The installer downloads the GitHub archive for that tag, creates `~/.local/share/luma/venv`, installs the Python package, writes `~/.local/bin/luma`, and adds `~/.local/bin` to the user's shell profile when needed.

### Roll Out A Published Release From Dashboard

After both package and Control-image workflows succeed, open Dashboard → Nodes → Update center. Use the immutable tag as the release ref and the same-tag Control image. The supported order is:

1. capture the public-route baseline;
2. confirm the Control update; the declared Builder first copies the external image into the internal registry through its managed egress proxy and verifies the digest;
3. let the page start the manager rollout only after that preparation succeeds and reconnect automatically;
4. verify the automatic post-update route sentinel;
5. update only non-manager nodes whose reported agent version differs from the release;
6. retry any failed or interrupted image, manager, or node operation from the same page.

Control-image preparation and fleet operations are persisted by Control. The image preparation uses the Builder's `control-image-mirror-v1` capability and the configured `registryHost` / `pushHost`; its proxy is never exposed to the browser. Manager updates run in an independent transient systemd unit, so refreshing the manager node agent cannot terminate the rollout that started it. A fleet node is successful only after the installer finishes and a new heartbeat reports the target release version. CLI update commands remain break-glass and first-adoption fallbacks; they are not required for normal releases.

Users can uninstall the local CLI with:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

Use `--purge` to also remove local config and login contexts:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

The uninstall script is intentionally local-only. It does not remove server runtime components such as Docker, Nomad, Traefik, Luma Control, deployed services, or `/opt/luma`.

The default control image is `ghcr.io/liutianjie/luma-control:latest`. If you want a fully pinned bootstrap, set this in `luma.yaml` before running manager bootstrap:

```yaml
defaults:
  images:
    lumaControl: ghcr.io/liutianjie/luma-control:v0.1.275
```

## Latest Channel

For early testing, users can install `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

This is convenient but less reproducible than a tag. For real users, prefer a version tag.

For CI, prefer the pinned PyPI package:

```bash
python -m pip install "luma-infra==0.1.275"
```

## Custom Host Or Fork

Use these environment variables when the code is hosted somewhere else:

```bash
curl -fsSL https://example.com/install-luma.sh | \
  LUMA_REPO_URL=https://github.com/acme/luma \
  LUMA_INSTALL_REF=v0.1.275 \
  sh
```

Use `LUMA_ARCHIVE_URL` to bypass GitHub archive URL conventions completely.

## PyPI Package Checks

Before tagging, verify the package locally:

```bash
rm -rf dist
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m venv /tmp/luma-package-test
. /tmp/luma-package-test/bin/activate
python -m pip install dist/*.whl
luma version --local
```

The package includes runtime stack templates and dashboard assets as package data. The one-line installer remains useful for local preflight, venv creation, and PATH setup. Host-level changes such as Linux DNS repair are handled by manager bootstrap or node join, not by a CLI-only install.
