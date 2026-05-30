# Release

Luma can be distributed without asking users to clone the repository.

## Recommended First Release

1. Push the repo to GitHub.
2. Build and publish the control API image:

```bash
docker build -f Dockerfile.control -t ghcr.io/liutianjie/luma-control:v0.1.0 .
docker tag ghcr.io/liutianjie/luma-control:v0.1.0 ghcr.io/liutianjie/luma-control:latest
docker push ghcr.io/liutianjie/luma-control:v0.1.0
docker push ghcr.io/liutianjie/luma-control:latest
```

3. Create a tag:

```bash
git tag v0.1.0
git push origin main --tags
```

4. Users install with:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.0 sh
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
    lumaControl: ghcr.io/liutianjie/luma-control:v0.1.0
```

## Latest Channel

For early testing, users can install `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

This is convenient but less reproducible than a tag. For real users, prefer a version tag.

## Custom Host Or Fork

Use these environment variables when the code is hosted somewhere else:

```bash
curl -fsSL https://example.com/install-luma.sh | \
  LUMA_REPO_URL=https://github.com/acme/luma \
  LUMA_INSTALL_REF=v0.1.0 \
  sh
```

Use `LUMA_ARCHIVE_URL` to bypass GitHub archive URL conventions completely.

## PyPI Later

The package already has a `pyproject.toml` and includes runtime stack templates as package data, so it can later be published to PyPI. The one-line installer is still useful because it can also do system preflight, Linux DNS repair, venv creation, and PATH setup.
