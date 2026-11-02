# SDK distribution and releases

Publish the business-agnostic `luma-observe-sdk` distribution as permanent
GitHub Release assets. Source stays in this repository's `sdk/python/` directory,
with a version independent of `luma-infra`. Applications can install the exact
wheel URL with its SHA-256 through their normal Python dependency tools.
Public PyPI is an optional additional channel once its publisher is configured.
Application credentials, business fields and dashboard definitions remain outside
the package.

The SDK has its own `observe-sdk-v<version>` tags and
`.github/workflows/observe-sdk-pypi.yml` workflow. Do not use Luma's root `v*`
tags for SDK releases: those belong to `luma-infra` and Control images.

## Optional PyPI publisher setup

The workflow source alone does not create a PyPI project or its trust binding.
Create a PyPI Pending Publisher for the first release, or a Trusted Publisher
on the existing SDK project, with these exact GitHub details:

| Setting | Value |
| --- | --- |
| PyPI project | `luma-observe-sdk` |
| GitHub owner | `LiuTianjie` |
| GitHub repository | `luma` |
| Workflow filename | `observe-sdk-pypi.yml` |
| Environment name | `pypi-observe-sdk` |

Create the matching GitHub environment `pypi-observe-sdk` and apply the
repository's intended release access controls. The root package's existing
`pypi` environment and publisher binding remain separate. The SDK publish job
uses a short-lived GitHub OIDC identity; it requires no PyPI password or API token
in the source or workflow. Existing observation tokens and `luma secret` values
are unrelated to package publication and remain unchanged.

After that binding is ready, set the GitHub repository variable
`OBSERVE_SDK_PYPI_ENABLED` to the exact string `true` to enable PyPI publication
for subsequent SDK releases. Without it, the PyPI job is skipped and the checked
GitHub Release remains the stable distribution channel. Enabling the variable
does not automatically publish previously released versions to PyPI.

## Release flow

1. Update both `sdk/python/pyproject.toml` and
   `sdk/python/src/luma_observe/__init__.py` to the intended SDK version.
2. Review and commit the SDK source and workflow changes. Push the reviewed
   commit and an `observe-sdk-v<version>` tag that points to it.
3. The release workflow rejects mismatched tags or version fields, then calls
   the same SDK workflow used by pull requests. That workflow checks Python
   3.10 with the minimum supported OTel version, Python 3.12 with current
   dependencies, the installed wheel/source artifacts, and the isolated
   Collector/metrics/Grafana integration.
4. After all checks pass, the release job downloads the Python 3.12 distributions
   from that same workflow run and publishes the wheel, sdist and `SHA256SUMS`
   to the tag's GitHub Release. Its notes contain the wheel's exact installation
   URL and hash. `--verify-tag` requires the pushed tag and `--latest=false`
   preserves the root Luma release's latest designation. The job fails if the
   release already exists; it never overwrites existing assets.
5. Only when `OBSERVE_SDK_PYPI_ENABLED == 'true'`, the PyPI job runs after the
   GitHub Release succeeds and uploads the same checked distributions. Neither
   publishing job rebuilds the package or publishes Control artifacts.
6. Verify the assets are available and their hashes match before telling consumers
   to install them. Preserve published assets and tags; fixes require a new SDK
   version. A rerun after a successful GitHub Release will fail at release creation
   rather than replace files or silently change a consumer's dependency.

## Install a GitHub Release wheel

After `observe-sdk-v0.1.0` has been published, its wheel URL is:

```text
https://github.com/LiuTianjie/luma/releases/download/observe-sdk-v0.1.0/luma_observe_sdk-0.1.0-py3-none-any.whl
```

The release also includes `SHA256SUMS`. Copy the exact wheel hash from that file
or use the complete installation command in the release notes. This example is
a format template; replace `<wheel-sha256-from-SHA256SUMS>` before using it:

```sh
python -m pip install 'luma-observe-sdk[taskiq] @ https://github.com/LiuTianjie/luma/releases/download/observe-sdk-v0.1.0/luma_observe_sdk-0.1.0-py3-none-any.whl#sha256=<wheel-sha256-from-SHA256SUMS>'
```

Use the same direct reference in the application's dependency configuration and
update its lockfile normally. Choose only the framework extras that the process
uses. These URLs are usable only after the reviewed SDK release exists.

If that exact version has also been published to PyPI:

```sh
python -m pip install 'luma-observe-sdk[taskiq]==0.1.0'
```

## Immutable Git source alternative

Actions artifacts are review/build outputs with a retention period. Do not make
their temporary download URL a long-lived application or Docker dependency.
An accessible, immutable Git commit is an alternative stable source. This
requires the SDK implementation to have been committed and pushed first;
uncommitted local work has no usable remote commit reference.

The following is only a dependency-format example. Replace `<full-sha>` with the
reviewed, pushed commit's complete SHA, not a moving branch name:

```text
luma-observe-sdk[taskiq] @ git+https://github.com/LiuTianjie/luma.git@<full-sha>#subdirectory=sdk/python
```

For a uv project, the equivalent source declaration is:

```toml
[project]
dependencies = ["luma-observe-sdk[taskiq]"]

[tool.uv.sources]
luma-observe-sdk = { git = "https://github.com/LiuTianjie/luma.git", rev = "<full-sha>", subdirectory = "sdk/python" }
```

Merge these entries into the application's existing configuration and update its
lockfile normally. Git-based builds need Git and repository network access in
their build environment. The released wheel avoids rebuilding the SDK from Git;
once available, an exact PyPI version can also use normal index resolution.
