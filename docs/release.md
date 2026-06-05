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

4. Configure PyPI Trusted Publishing once for the `luma-infra` project:

- owner: `LiuTianjie`
- repository: `luma`
- workflow: `pypi.yml`
- environment: `pypi`

The package distribution name is `luma-infra`; the installed console command remains `luma`.

5. Create a tag to publish a versioned image, GitHub archive, and PyPI package:

```bash
git tag v0.1.61
git push origin main --tags
```

The `Publish Python Package` workflow builds wheel and sdist, runs `twine check`, and publishes with `pypa/gh-action-pypi-publish@release/v1` through OIDC. Do not store a long-lived `PYPI_API_TOKEN` secret.

6. CI users install with:

```bash
python -m pip install "luma-infra==0.1.61"
```

Interactive users can still install with:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.61 sh
```

The installer downloads the GitHub archive for that tag, creates `~/.local/share/luma/venv`, installs the Python package, writes `~/.local/bin/luma`, and adds `~/.local/bin` to the user's shell profile when needed.

Users can uninstall the local CLI with:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

Use `--purge` to also remove local config and login contexts:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

The uninstall script is intentionally local-only. It does not remove server runtime components such as Docker, Swarm, Portainer, Traefik, Luma Control, deployed services, or `/opt/luma`.

The default control image is `ghcr.io/liutianjie/luma-control:latest`. If you want a fully pinned bootstrap, set this in `luma.yaml` before running manager bootstrap:

```yaml
defaults:
  images:
    lumaControl: ghcr.io/liutianjie/luma-control:v0.1.61
```

## Latest Channel

For early testing, users can install `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

This is convenient but less reproducible than a tag. For real users, prefer a version tag.

For CI, prefer the pinned PyPI package:

```bash
python -m pip install "luma-infra==0.1.61"
```

## Custom Host Or Fork

Use these environment variables when the code is hosted somewhere else:

```bash
curl -fsSL https://example.com/install-luma.sh | \
  LUMA_REPO_URL=https://github.com/acme/luma \
  LUMA_INSTALL_REF=v0.1.61 \
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
