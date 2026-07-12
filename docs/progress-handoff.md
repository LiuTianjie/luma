# Luma Progress Handoff

Date: 2026-07-12

This is a short handoff for the current control-plane implementation. Public docs such as `README.md`, `docs/how-to-use-luma.md`, and `docs/bootstrap.md` are authoritative for end-user commands.

## Current Live Snapshot

- Luma CLI, Control, the manager agent, and all six online non-manager agents
  are live on `0.1.196`; 761/761 tests passed before release. Offline `blg`
  remains on `0.1.175` and must be upgraded when it reconnects.
- `manager` is the only control plane. `aly` is a stale historical name. The
  LAE platform is a 9-task group on `lab`; tenant runtime admission is limited
  to `manager + tecent`, while tenant-facing APIs never reveal placement.
- LAE staging Job version 21 runs images built from exact commit
  `7c1212c037e356c3e6af39829bbed0615bea234d`; all nine platform tasks and
  Web/API/Agent/artifact probes are healthy.
- Two real FastAPI tenant applications run on `tecent`. A fresh template launch
  completed Agent diagnosis, Builder build, Runtime deployment, random-domain
  publication, and valid TLS without a user-supplied Luma manifest.
- Luma `0.1.190` scopes Nomad service and Traefik router names by deployment;
  `0.1.192` registers edge upstreams through the runtime node's
  `luma_tailscale_ip` metadata; `0.1.196` adds BuildKit timeout cancellation
  and release tag/package-version validation. Job v21, online fleet, and
  Control/manager upgrades completed without manual application restarts.
  A targeted LAE Web route sentinel passed, but longer probes still contain a
  few transient LAE 404/502/timeouts, so zero-downtime is not yet proven.
- Update-check now excludes attempt-scoped snapshot/plan identifiers from its
  semantic digest. Two independent checks produced the same candidate plan;
  after redeploying that candidate, the next check returned source, plan, and
  aggregate `changed=false`.
- Production remains blocked on dedicated runner/core pools, full source and
  lifecycle matrices, Docker/CNI/reconciliation fault injection, real SMTP,
  payment providers, isolation, backups, and recovery drills.

## Current Direction

Luma uses a self-hosted control plane instead of SSH-driven deployment:

- The first full node runs `luma bootstrap manager --domain luma.example.com` locally on the server.
- Worker nodes run `luma node join https://luma.example.com --token <node-join-token> --region cn|global|home --name <node-name>` locally on each server.
- Client machines only run `luma login` and `luma deploy`; they do not need Docker, SSH, Cloudflare credentials, or Nomad credentials.
- The orchestrator is HashiCorp Nomad; deploy goes directly through the Nomad HTTP API.
- Luma Control owns login tokens, node registration, node meta, DNS sync, jobspec rendering, image pull fallback, deployment state, and Nomad API orchestration.

## Main Flow

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
luma bootstrap manager --domain luma.example.com
luma login https://luma.example.com --token <management-token>
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma deploy app.yaml
luma doctor
```

## Important Defaults

- Control API image: `ghcr.io/liutianjie/luma-control:latest`.
- Runtime root on the manager: `/opt/luma`.
- Client context root: `~/.config/luma`.
- Default public ingress: Traefik on 80/443.
- Default Nomad control-plane ports: 4646 (HTTP API), 4647 (RPC), 4648 (Serf), opened only on the tailnet.
- Default egress proxy: Mihomo on local `127.0.0.1:7890`, blocked from public inbound access.

## Repair Commands

Run these directly on the server being repaired:

```bash
luma bootstrap manager --domain luma.example.com
luma egress setup
luma egress refresh
luma tailscale connect
```

## Release Flow

```bash
docker build -f Dockerfile.control -t ghcr.io/liutianjie/luma-control:v0.1.0 .
docker tag ghcr.io/liutianjie/luma-control:v0.1.0 ghcr.io/liutianjie/luma-control:latest
docker push ghcr.io/liutianjie/luma-control:v0.1.0
docker push ghcr.io/liutianjie/luma-control:latest
git tag v0.1.0
git push origin main --tags
```

Users can pin a release:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.0 sh
```

## Validation

Use these before publishing:

```bash
python -m unittest discover -s tests
python -m compileall luma
./scripts/validate-stacks.sh
sh -n scripts/install-luma.sh
git diff --check
```
