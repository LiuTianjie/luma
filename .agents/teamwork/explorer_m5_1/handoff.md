# Milestone 5 Investigation Report: 0-to-1 Developer Experience & Setup Compatibility

**Author**: Explorer M5-1  
**Date**: 2026-10-15  
**Target Repository**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_1`

---

## 1. Observation

### 1.1 Fresh Installation & 0-to-1 Bootstrap Flow
We inspected the implementation of the core developer bootstrap workflow across the following files:
- **`luma/cli/manager.py`**:
  - Line 102 (`cmd_bootstrap`): Loads cluster config, infers default manager node (`_local_node(args.profile)`), prompts interactively for missing keys (`ensure_interactive_config("manager", keys=keys)`), auto-infers Cloudflare DNS zone from `--domain` via `_ensure_cloudflare_dns_from_local_config`, captures secrets via `_attach_control_secrets`, and runs `bootstrap_manager_local()`.
  - Line 850 (`_manager_cloudflare`): Queries Cloudflare API via `find_zone(config, args.zone)`, saves `providers.dns` (`type: cloudflare`, `zone`, `zoneId`, `apiTokenEnv`) to `luma.yaml`.
  - Line 890 (`_manager_egress`): Reads `EGRESS_SUBSCRIPTION_URL`, sets up egress gateway proxy via `setup_egress()`.
- **`luma/cli/session.py`**:
  - Line 225 (`cmd_login`): Accepts `--token`, `--token-stdin`, environment variable `LUMA_DEPLOY_TOKEN`, or interactive prompt; validates credentials via `client.verify_login()`; writes context to `~/.luma.config.json` via `save_context()`.
  - Line 544 (`cmd_init`): Creates a clean starter service manifest (`app.yaml`) specifying `name`, `image`, `region`, `exposure`, `domain`, `port`, `replicas`, memory limits, and TCP/HTTP health checks. Pre-validates YAML syntax via `load_service()` before saving.
- **`luma/cli/nodes.py`**:
  - Line 50 (`cmd_node_join`): Configures local system DNS (`configure_dns`), installs Docker (`install_docker`), registers worker node with Control API (`client.register_node`), and auto-provisions Tailscale for `home` region or Tailscale-routed managers.
- **`luma/bootstrap.py`**:
  - Lines 124–136: Installs Cloudflare DNS-01 API token secret for Traefik ACME.
  - Lines 760–775 (`install_control_state`): Seeds Control SQLite database with manager state and encrypted secrets (`_manager_secret_names(config)`: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`).
  - Lines 1424–1430: Calls `sync_control_dns(config, domain)` to register the control plane domain directly in Cloudflare.

### 1.2 Environment Variable Loading & Propagation Disconnect
We inspected `luma/envfile.py`, `luma/userconfig.py`, and `luma/cli/main.py`:
- In `luma/envfile.py` (lines 54–62):
  ```python
  LUMA_SETTING_NAMES = frozenset(
      {"CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL", "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL"}
  )
  LUMA_CREDENTIAL_NAMES = frozenset({"LUMA_CONTROL_URL", "LUMA_DEPLOY_TOKEN", "LUMA_CONTROL_CONTEXT"})

  def is_luma_setting(name: str) -> bool:
      return name in LUMA_SETTING_NAMES or (name.startswith("LUMA_") and name not in LUMA_CREDENTIAL_NAMES)
  ```
- In `luma/cli/main.py` (lines 76–78):
  ```python
  else:
      for warning in load_luma_settings(Path(".env")):
          print(f"luma: warning: {warning}", file=sys.stderr)
  ```
- **Direct Empirical Observation**:
  Executing `./.venv/bin/python -c 'from luma.envfile import is_luma_setting; print(is_luma_setting("CLOUDFLARE_ZONE_ID"))'` returns `False`.
  When an operator defines `CLOUDFLARE_ZONE_ID` in their `./.env` file alongside `CLOUDFLARE_API_TOKEN`, `load_luma_settings` silently ignores `CLOUDFLARE_ZONE_ID` because it is missing from `LUMA_SETTING_NAMES`!
  However, `CLOUDFLARE_ZONE_ID` is actively expected and read in:
  - `luma/bootstrap.py:764, 779`: `["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"]`
  - `luma/cli/manager.py:740`: `names.update(key for key in os.environ if key in {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"})`
  - `luma/cloudflare.py:80, 138, 171`: `os.environ.get(str(dns_config.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))`
  - `luma/control/server.py:1978, 2083, 2304`: `os.environ.get(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))`

### 1.3 Cloudflare DNS & Tunnel Integration
We inspected `luma/cloudflare.py` and `luma/paas/release/helm_compiler.py`:
- In `luma/cloudflare.py`:
  - `CloudflareClient` makes direct HTTPS REST API calls to `https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records`.
  - `sync_dns` and `delete_dns` upsert and delete DNS A/CNAME records directly using `CLOUDFLARE_API_TOKEN`.
  - Lines 66–67 & 124–125: Explicitly skips DNS for Cloudflare Tunnel:
    ```python
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"
    ```
  - **Zero Kubernetes CRDs**: Luma's Cloudflare DNS engine operates independently of the cluster runtime, requiring NO `external-dns` controller or CRDs.
- In `luma/paas/release/helm_compiler.py`:
  - Line 331 (`_compile_exposure_ingress` for `external-edge`):
    ```python
    elif exposure == "external-edge":
        annotations["luma.io/edge-gateway"] = "global-edge"
        annotations["external-dns.alpha.kubernetes.io/hostname"] = domain
    ```
    `external-dns.alpha.kubernetes.io/hostname` is an annotation for the third-party Kubernetes ExternalDNS controller. In Luma, external DNS is managed directly by Luma Control/CLI calling `sync_dns()`, not by in-cluster ExternalDNS CRD controllers.
  - Lines 336–340 (`_compile_exposure_ingress` for `cloudflare-tunnel`):
    ```python
    elif exposure == "cloudflare-tunnel":
        annotations["luma.io/edge-gateway"] = "cloudflare-tunnel"
        annotations["luma.io/tunnel-hostname"] = domain
        annotations["luma.io/tunnel-service"] = f"http://{name}.{namespace}.svc.cluster.local:{port}"
    ```
    While `annotations` are generated on the Ingress, `compile_service_release` and `compile_compose_release` currently do NOT inject a `cloudflared` container sidecar into `pod_spec["containers"]`.
    In legacy Nomad (`luma/nomad_render.py:1246–1259`), `_cloudflared_task` automatically attached `cloudflare/cloudflared:latest` with `args: ["tunnel", "--no-autoupdate", "run"]` and `TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN}` into the task group.

### 1.4 Public CLI Surface Hygiene
- We ran `./.venv/bin/python -m luma.cli --help` and inspected `luma/cli/parser.py`:
  - Lines 22–54: `COMMAND_GROUPS` categorizes commands into:
    - **Get started**: `init`, `login`, `context`, `doctor`, `version`
    - **Deploy**: `validate`, `deploy`, `compose`, `import`, `build`, `artifact`, `workflow`
    - **Operate**: `status`, `app`, `secret`
    - **Cluster administration**: `bootstrap`, `update`, `node`, `region`, `storage`, `registry`, `git-provider`, `manager`
  - Line 467: `migrate` command is registered with `help=argparse.SUPPRESS`.
  - In `luma --help`, `migrate` is completely absent from the help output.
  - When explicitly invoked, `./.venv/bin/python -m luma.cli migrate --help` works cleanly, exposing `batches`, `plan`, `shadow`, `cutover`, `abort`, `finalize`.
  - All core commands (`bootstrap`, `app`, `node`, `manager`, `login`, `init`) respond with exit code 0 to `--help`.
  - Standalone script `scripts/migrate-nomad-to-k3s.py` is present and runs preflight, dry-run, planning, cutover, and Nomad decommission (`systemctl disable --now nomad`).

### 1.5 Automated Test Suites and Site Gate Results
- `test_bootstrap*.py`: Ran 32 tests in 1.816s -> **OK**
- `test_cli*.py`: Ran 146 tests in 4.743s -> **OK**
- `test_first_install_scenarios.py`: Ran 5 tests in 0.000s -> **OK**
- `test_paas_engine.py`: Ran 120 tests in 0.123s -> **OK**
- `test_challenger_m*.py`: Ran 82 tests in 0.069s -> **OK**
- `scripts/check-luma.sh` (full gate including TypeScript/dashboard and Python tests): **PASSED** (exit code 0).
- `scripts/build-site.py` + `scripts/check-site.py`: **PASSED** (exit code 0; 48 HTML pages validated, 1:1 bilingual parity).

---

## 2. Logic Chain

1. **Premise**: Open-source users installing Luma from scratch (0-to-1) rely on placing environment variables in `./.env` (e.g. `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `EGRESS_SUBSCRIPTION_URL`, `TRAEFIK_ACME_EMAIL`, `TAILSCALE_AUTHKEY`).
2. **Finding from Observation 1.2**: In `luma/envfile.py`, `LUMA_SETTING_NAMES` only includes `{"CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL", "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL"}`. It omits `CLOUDFLARE_ZONE_ID`.
3. **Inference**: If a user specifies `CLOUDFLARE_ZONE_ID` in `.env`, `load_luma_settings` drops it. The CLI and manager then fail to see `CLOUDFLARE_ZONE_ID` unless it is explicitly exported in the user's interactive shell.
4. **Resolution**: Adding `"CLOUDFLARE_ZONE_ID"` to `LUMA_SETTING_NAMES` in `luma/envfile.py` guarantees 100% transparent loading of all documented Cloudflare settings from `./.env`.
5. **Premise**: Luma is designed to operate on bare servers and K3s clusters without forcing users to install complex Kubernetes operators or manual CRDs (such as ExternalDNS or Cloudflare Tunnel operators).
6. **Finding from Observation 1.3**:
   - `luma/cloudflare.py` directly handles DNS records using the Cloudflare REST API. No Kubernetes CRD is needed.
   - For Cloudflare Tunnel (`exposure: cloudflare-tunnel`), legacy Nomad bundled `cloudflared:latest` as a native container sidecar in the task group. In K3s, `helm_compiler.py` only attaches annotations to an Ingress without running `cloudflared`.
7. **Inference**: For K3s PaaS workloads to achieve true zero-CRD parity with legacy Nomad, `compile_service_release` should inject a native `cloudflared:latest` sidecar container into the Pod spec whenever `exposure == "cloudflare-tunnel"` (configured via secret `TUNNEL_TOKEN`).
8. **Premise from User Feedback**: Migration is a one-time operation and must not clutter the public CLI interface for fresh open-source users.
9. **Finding from Observation 1.4**:
   - `luma --help` is completely clean of `migrate` due to `help=argparse.SUPPRESS` and omission from `COMMAND_GROUPS`.
   - Standalone migration is available via `scripts/migrate-nomad-to-k3s.py` and `luma migrate`.
10. **Documentation Gap**: While the migration mechanics are implemented, `docs/migration-guide*.md` does not yet explicitly document:
    - The zero-breaking-change guarantee for fresh 0-to-1 installations.
    - Zero manual CRD requirements for Cloudflare DNS and Tunnels.
    - The exact operational hazard protections (Traefik port coexistence without hostPort duplication, memory headroom checks, SQLite/bot single-instance isolation, and Nomad decommissioning).

---

## 3. Caveats

- **External Cloudflare API**: In offline test environments (and CI without live Cloudflare credentials), API calls to `api.cloudflare.com` are mocked or skipped via `--skip-dns`. Live DNS tests require valid `CLOUDFLARE_API_TOKEN` and zone access.
- **Root / Sudo Execution**: Full `luma bootstrap` on Linux requires root privileges (`sudo`) to install Docker, systemd services, and bind ports `80`/`443`. On macOS development workstations, `luma bootstrap` runs in local mock/profile mode.
- **No other caveats**: All observations are backed by direct codebase inspection and automated test execution.

---

## 4. Conclusion & Proposed Recommendations

### 4.1 Summary of System Health
- The 0-to-1 setup workflow (`luma bootstrap`, `luma login`, `luma init`, `luma node join`, `luma manager cloudflare`, `luma manager egress`) is structurally sound, backward compatible, and free of architectural regressions.
- The public CLI surface cleanly hides one-time migration commands (`migrate`) while keeping core developer commands prominent and intuitive.
- All 32 bootstrap unit tests, 146 CLI unit tests, 120 PaaS engine tests, and the full repository gate pass cleanly.

### 4.2 Proposed Code Fix 1: Add `CLOUDFLARE_ZONE_ID` to `LUMA_SETTING_NAMES`
In `luma/envfile.py`:
```python
# Before (line 54):
LUMA_SETTING_NAMES = frozenset(
    {"CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL", "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL"}
)

# After:
LUMA_SETTING_NAMES = frozenset(
    {
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ZONE_ID",
        "EGRESS_SUBSCRIPTION_URL",
        "TAILSCALE_AUTHKEY",
        "TRAEFIK_ACME_EMAIL",
    }
)
```
*Rationale*: Enables `.env` files containing `CLOUDFLARE_ZONE_ID=...` to load cleanly without requiring manual shell `export`.

### 4.3 Proposed Code Fix 2: Cloudflare Tunnel Sidecar in `compile_service_release`
In `luma/paas/release/helm_compiler.py`:
When `exposure == "cloudflare-tunnel"`, inject a `cloudflared` container into `pod_spec["containers"]`:
```python
if exposure == "cloudflare-tunnel":
    tunnel_cfg = parsed.get("tunnel") if isinstance(parsed.get("tunnel"), dict) else {}
    token_env = str(tunnel_cfg.get("tokenEnv") or "CLOUDFLARE_TUNNEL_TOKEN")
    cloudflared_container: Dict[str, Any] = {
        "name": "cloudflared",
        "image": "cloudflare/cloudflared:latest",
        "args": ["tunnel", "--no-autoupdate", "run"],
        "env": [
            {
                "name": "TUNNEL_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": secret_manifest["metadata"]["name"],
                        "key": token_env,
                        "optional": True,
                    }
                },
            }
        ],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "256Mi"},
        },
    }
    pod_spec["containers"].append(cloudflared_container)
```
*Rationale*: Matches Nomad `_cloudflared_task` behavior. Runs `cloudflared` directly inside the Pod network namespace with zero external CRDs required.

### 4.4 Proposed Documentation Hardening in `docs/migration-guide*.md`
Update `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md` to include:
1. **0-to-1 Fresh Installation vs. Migration Parity**: Clarify that fresh users simply use `luma bootstrap` -> `luma login` -> `luma init` -> `luma deploy` without ever needing `luma migrate`.
2. **Zero-CRD Cloudflare Architecture**: Clarify that Luma connects to Cloudflare directly via API tokens (`CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ZONE_ID`) and native `cloudflared` containers, completely eliminating the need for Kubernetes `external-dns` or Cloudflare Operator CRDs.
3. **Migration Hazards & Mitigations**:
   - Same-node Traefik dynamic routing avoiding duplicate `hostPort: 80/443`.
   - Node RAM headroom check (>= 512MB free).
   - Stateful SQLite / Bot single-instance lock safety (shadow replica 0, stop Nomad before K3s replica 1).
   - Standalone execution script `scripts/migrate-nomad-to-k3s.py` and final decommission (`systemctl disable --now nomad`).

---

## 5. Verification Method

### 5.1 Automated Unit Tests
Run the following test commands from the project root:
```bash
# 1. Bootstrap flow and manager configuration
./.venv/bin/python -m unittest discover -s tests -p "test_bootstrap*.py" -v

# 2. CLI commands, options, and .env isolation
./.venv/bin/python -m unittest discover -s tests -p "test_cli*.py" -v

# 3. First install dependency verification
./.venv/bin/python -m unittest tests/test_first_install_scenarios.py -v

# 4. PaaS engine, exposure modes, and migration safety
./.venv/bin/python -m unittest tests/test_paas_engine.py -v

# 5. Challenger stress tests for exposure modes and sidecars
./.venv/bin/python -m unittest tests/test_challenger_m*.py -v
```

### 5.2 CLI Surface Verification
Verify that `migrate` is absent from `luma --help` but responsive when invoked directly:
```bash
# Must NOT contain "migrate" in command listings
./.venv/bin/python -m luma.cli --help | grep -w "migrate"  # Expect: no output

# Subcommands must respond correctly
./.venv/bin/python -m luma.cli migrate --help
./.venv/bin/python -m luma.cli migrate batches
```

### 5.3 Standalone Migration Script Verification
Test preflight checks and dry-run execution:
```bash
./.venv/bin/python scripts/migrate-nomad-to-k3s.py --help
./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run
```

### 5.4 Full Repository Gate & Documentation Parity
```bash
# Full test, lint, and build gate
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# Bilingual documentation anchor and codeblock verification
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
```

### 5.5 Invalidation Conditions
- Any failure in `check-luma.sh` or `check-site.py`.
- Any reappearance of `migrate` in the top-level `luma --help` output.
- Failure of `load_luma_settings` to read `CLOUDFLARE_ZONE_ID` when defined in `.env`.
