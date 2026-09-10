# Managed installation and local diagnosis

> Introduced in v0.1.317: the initial managed-installation hardening batch.
> Candidate preparation is isolated. A later change adds an independent
> node-agent cutover supervisor with shim rollback when the new process does
> not prove it is the target runtime. Manager updates take a Control
> maintenance lease that rejects concurrent deploys/builds and requires a
> cached Control image. Do not treat this document as a completed fleet
> migration procedure.

## Installation identity

Managed installations record their actual runtime, source directory, installation
root, command directory, owner UID, version and dependency policy in
`<runtime>/luma-installation.json`. CLI updates and node-agent updates read the
running Python environment (`sys.prefix`) first, not an unrelated command found
in root's PATH. Ordinary updates reject conflicting explicit installation paths. Directory aliases are canonicalized without resolving the venv Python executable into the system interpreter. After preparing an update, managed CLI re-execution uses the stable command shim, not the preserved old Python/source.

Historical user installations (`~/.local/share/luma/venv`) retain their original
user home and command directory. Other existing venv installations (for example,
`/opt/luma-cli`) retain their own root and `bin` directory. Repository `.venv`
environments remain development installations; they are not silently adopted.
This is runtime-based compatibility, not a complete arbitrary-path/service
adoption wizard. Ambiguous installations still need the planned explicit adoption
and service-path reconciliation flow.

New managed runtimes are prepared at their final path under
`<install-root>/releases/candidate.*/{src,venv}`. The installer does not overwrite
the active source/venv. Dependency installation and runtime validation must pass
before the command shim is atomically replaced. Candidate preparation removes group/world write permissions from the runtime directory, independent of the caller's umask, and reads the installation record back through the agent's identity validator before publication. Existing runtime diagnosis remains read-only and fails closed on unsafe permissions. Managed package installation
failures do not fall back to source code plus residual dependencies. An
installation-scoped lock rejects simultaneous prepares. This is a prepare lock,
not a durable distributed maintenance lease or a service-health guarantee.

Old environments are retained. Do not move venv directories (entrypoints can
contain absolute paths), delete previous installations, or manually repoint a
running service as part of a normal update. Automatic candidate retention/cleanup
is not included in this first implementation.

## Dependency policy

The managed installer ignores host/global/user/site pip configuration and
inherited `PIP_*` settings. It also prevents an ambient Python module path from
providing missing candidate dependencies. It does not edit system `pip.conf`.

Supported explicit inputs:

| Input | Behavior |
| --- | --- |
| `LUMA_PIP_INDEX_URL` | Single HTTPS package index; default `https://pypi.org/simple` |
| `LUMA_PIP_WHEELHOUSE` | Absolute existing local directory; disables index access for pip |
| `LUMA_PIP_CA_BUNDLE` | Absolute existing CA bundle file; normal certificate verification remains enabled |

The effective policy is saved with a successful managed installation and reused
by subsequent CLI/agent updates. Explicit Luma inputs override the saved policy.
There is no implicit extra public index or `trusted-host` fallback. Credentialed
URLs, query strings, non-HTTPS indexes and missing local paths are rejected.
Authenticated package-index workflows are not yet supported by this policy.
Process proxy settings remain separate from package-index selection.

For an installed CLI, choose an approved dependency source for the next update:

```sh
LUMA_PIP_INDEX_URL=https://packages.example.com/simple luma update --install-ref <release-tag>
```

For a bootstrap script already downloaded from the **same** intended release:

```sh
LUMA_INSTALL_REF=<release-tag> \
LUMA_PIP_INDEX_URL=https://packages.example.com/simple \
sh ./install-luma.sh
```

`LUMA_PIP_WHEELHOUSE` only makes **pip** offline: bootstrap/source archive downloads
still require connectivity or separately supplied source. A wheelhouse must
contain the build backend and all transitive dependencies for the target
Python/platform, not just the Luma wheel. Unavailable wheels/build dependencies
fail candidate preparation; they do not trigger a different index.

To switch away from a saved wheelhouse or custom CA, explicitly pass an empty
`LUMA_PIP_WHEELHOUSE` or `LUMA_PIP_CA_BUNDLE` for that update. The active process
retains its existing environment until a replacement is installed and started.

## Read-only diagnosis

```sh
luma doctor --local
luma doctor --local --format json
```

Reports the actual runtime, detected installation, effective dependency source,
ignored pip environment **key names only**, isolated dependency imports and
`pip check`. No network calls, credentials, service changes or state writes are
part of these checks. An import failure returns a nonzero status even if old
package metadata lets `pip check` pass. A severely broken runtime that cannot
start the CLI cannot use this entrypoint; standalone recovery remains a separate
planned feature.

For Control and node health, use the existing `luma doctor` / `luma doctor --deep`.
A healthy local diagnostic is not proof of a running agent, working service
manager, or successful deployment.

## Node-join acceptance

`luma node join` no longer reports completion when Control omits agent
credentials. After local service installation it waits up to 60 seconds for
Control to confirm a fresh heartbeat/lease authenticated with the newly issued
agent credential. The read-only `/v1/node-agent/readiness` endpoint is scoped to
that node credential; polling cannot make a node ready. Reissuing credentials
invalidates join verification even when an old heartbeat remains recent.

On missing credentials, readiness timeout or an unsupported old Control endpoint,
the CLI returns failure with an actionable reason and does not remove the
registered node. Resolve connectivity / upgrade Control, then rerun the normal
join command. Roll out the compatible Control endpoint **before** new joining
clients. This is join verification, not the planned persistent join operation,
full role-capability preflight or update nonce verification.

## Node-agent cutover

After a managed installer publishes a new command shim it writes
`<install-root>/lifecycle/target.json` and copies the previous shim aside.
`update-luma` then starts an independent systemd/launchd supervisor
(`python -m luma.node_lifecycle switch`) instead of restarting the agent from
inside the running task. The supervisor restarts the node-agent service, waits
until the new process is the target runtime (or the new agent records the
cutover nonce), and restores the previous shim if that proof never appears.

This is not yet a durable distributed maintenance lease, and a host without
systemd-run/launchd cannot use the supervised path. Mixed-version agents that
do not find `target.json` keep the previous in-process service refresh.

## Not yet covered

Crash recovery of a supervisor that died mid-switch, public adoption/repair,
Dashboard lifecycle UI, stable-release default resolution, builder/registry
validation, manager maintenance gates and observe/backup standardization remain
tracked in `standard-path-remediation-plan-2026-09-09.md`. No live upgrade should
be justified by candidate-preparation tests alone.
