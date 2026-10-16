# Build Pipeline, BuildKit Pool & Zot OCI Artifacts

Luma Next decouples **Image Building (`Artifact`)** from **Workload Deployment (`Release`)** using **Tekton Pipelines**, a **Remote BuildKit Pool (`amd64` + `arm64`)**, and **Zot OCI Registry**, while preserving all of Luma's signature topology and exposure features.

## Core Architecture

1. **Tekton `PipelineRun` Execution**:
   - Every build runs inside an isolated Kubernetes Pod managed by Tekton.
   - Cancelling a build (`luma build cancel <ID>` or via Web Console) patches `spec.status: CancelledRunFinally`, terminating active `buildctl` and `git` processes across the cluster in `< 3s`.
2. **Remote BuildKit Pool & Persistent Cache**:
   - `linux/amd64` builds route to the in-cluster `buildkitd` StatefulSet with a dedicated NVMe cache volume (`max-used-space = "30GB"`).
   - `linux/arm64` builds route over Tailscale mTLS to the dedicated Apple Silicon BuildKit daemon (`macbook-pro`).
   - Every build exports and imports OCI layer cache manifests (`--export-cache type=registry,mode=max` / `--import-cache type=registry`) stored in Zot Registry.
3. **APT / APK HTTPS Mirror Acceleration**:
   - Luma's `accelerate_dockerfile_mirrors()` automatically rewrites `deb.debian.org`, `archive.ubuntu.com`, and `dl-cdn.alpinelinux.org` to HTTPS mirrors (`mirrors.ustc.edu.cn`) to eliminate `apt-get` proxy TLS handshake failures and `502 Bad Gateway` errors.
4. **Multi-Repo Compose Build Contexts (`x-luma-git-contexts`)**:
   - Multi-repo applications (such as `serverx` + `xui`) declare sibling Git repositories in `luma.compose.yml` under `x-luma-git-contexts` instead of relying on fragile `../` host path assumptions.

```yaml
x-luma-git-contexts:
  xui:
    repo: https://gcode.gaojiua.com/Signal/xui.git
    ref: main
```

## Zot OCI Registry & Retention Pinning

Zot replaces `registry:2` as a lightweight, single-binary OCI v1.1 registry:
- **Zero-Downtime Online Garbage Collection**: Zot continuously reclaims unreferenced blobs and untagged manifests online (`gc: true`, `delay: 24h`) without stopping the registry or blocking image pushes/pulls.
- **Active & Rollback Guard Pins (`luma-pin-*`)**:
  - Whenever a `Release` is deployed, Luma's `FluxReconciler` automatically tags the active image digests (`luma-pin-active-<project>-<env>`) and the last $N = 5$ rollback digests (`luma-pin-rollback-<project>-<env>-v<gen>`).
  - Because Zot retains tagged manifests (`keepTags`), **rolling back to any of the last 5 releases is guaranteed to succeed in `< 5s` without `manifest unknown` errors**.

## Luma Signature Features in Kubernetes

Luma's thin developer contract (`luma.yaml` and `luma.compose.yml`) compiles deterministically into Kubernetes and Flux primitives without losing any of Luma's multi-region or hybrid-network capabilities:

1. **Region Scheduling (`cn`, `global`, `home`, Custom Regions) & Node Pinning**:
   - `region` compiles into Kubernetes `nodeSelector` labels (`luma.io/region: <region>` and `topology.kubernetes.io/region: <region>`).
   - Optional `node` pinning adds `kubernetes.io/hostname: <node>`.
2. **All 6 Exposure Modes (`exposure`)**:
   - `cn-edge` (`region: cn`): Compiles to a Traefik `Ingress` bound to the CN Edge gateway with automatic HTTPS and Flux post-upgrade route probing.
   - `external-edge` (`region: global`): Compiles to a Global Edge `Ingress` with `external-dns` Cloudflare hostname synchronization.
   - `tailscale-relay` (`region: home`): Runs the Pod on a private `home` K3s node behind NAT (`macbook-pro` or home server) while Edge Traefik relays public HTTPS traffic over the Tailscale WireGuard mesh (`luma.io/relay-transport: tailscale-mesh`).
   - `tcp-relay`: Validates `publishPort` (forbidding reserved ports `80`/`443`) and renders a Traefik `IngressRouteTCP` (`traefik.io/v1alpha1`) on entrypoint `tcp-<port>` for raw L4 TCP forwarding.
   - `cloudflare-tunnel`: Annotates the workload `Ingress` with `luma.io/edge-gateway: cloudflare-tunnel` so `cloudflared` exposes internal ClusterIP services without opening public node ports.
   - `none`: Renders an internal `ClusterIP` `Service` only.
3. **Region-Aware Egress & `proxy: true`**:
   - Setting `proxy: true` injects `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and a comprehensive `NO_PROXY` list that strictly bypasses `localhost`, Kubernetes cluster CIDRs (`10.0.0.0/8`, `.svc.cluster.local`), Zot (`100.66.177.70:5000`), and Gitea (`gcode.gaojiua.com`).

## CLI & Web Console Workflows

```bash
# List immutable build artifacts
luma artifact list --project serverx

# Inspect per-service sha256 digests and BuildKit stage waterfall timings
luma artifact show art-serverx-9f8e7d6c

# Deploy an existing artifact without rebuilding
luma deploy luma.yaml --artifact art-serverx-9f8e7d6c

# Promote a verified artifact from staging to prod (zero rebuild)
luma artifact promote art-serverx-9f8e7d6c --from staging --to prod
```
