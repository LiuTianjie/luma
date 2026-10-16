# 镜像构建流水线、BuildKit 集群池与 Zot OCI 制品管理 {#build-pipeline-buildkit-pool-zot-oci-artifacts}

Luma Next 通过 **Tekton Pipelines**、**远程 BuildKit 守护进程池（`amd64` + `arm64`）** 与 **Zot OCI Registry**，将**镜像构建（`Artifact`）**与**应用发布（`Release`）**彻底解耦，同时完整保留 Luma 的多 Region 与混合网络暴露面特性。

## 核心架构特性 {#core-architecture}

1. **Tekton `PipelineRun` 原生隔离与秒级取消**：
   - 每次构建都是 K3s 集群中受资源配额约束的独立 Tekton `PipelineRun`。
   - 执行 `luma build cancel <ID>` 或在 Web 控制台点击取消时，Tekton 通过 `CancelledRunFinally` 在 `< 3s` 内终止整个构建进程组与远程 `buildctl` 会话，彻底根除孤儿构建与锁死。
2. **远程 BuildKit 缓存池与跨架构路由**：
   - `linux/amd64` 构建直接调度至集群内 `buildkitd` StatefulSet（挂载 50 GiB 持久化构建缓存卷，30 GiB 水位线自动 GC）。
   - `linux/arm64` 构建通过 Tailscale mTLS 路由至 Apple Silicon 原生 BuildKit 节点（`macbook-pro`）。
   - 自动启用 `--export-cache type=registry,mode=max` 与 `--import-cache type=registry`，将构建层缓存持久化至 Zot。
3. **Dockerfile APT / APK HTTPS 镜像源自动加速**：
   - `accelerate_dockerfile_mirrors()` 自动将 `deb.debian.org`、`archive.ubuntu.com`、`dl-cdn.alpinelinux.org` 重写为 HTTPS 国内镜像源（`mirrors.ustc.edu.cn`），根治 `apt-get` 经 HTTP 代理抓包导致的 `502 Bad Gateway` 与 SSL 握手错误。
4. **多仓库构建上下文声明（`x-luma-git-contexts`）**：
   - 针对 `serverx` + `xui` 等多仓库联合构建场景，在 `luma.compose.yml` 中声明 `x-luma-git-contexts`，由 Tekton `git-clone` 并行拉取并注入 BuildKit `--opt context:<name>=...`，告别脆弱的 `../` 宿主机相对路径依赖。

```yaml
x-luma-git-contexts:
  xui:
    repo: https://gcode.gaojiua.com/Signal/xui.git
    ref: main
```

## Zot OCI Registry 与回滚防删锁 {#zot-oci-registry-retention-pinning}

Luma 采用 CNCF 生态单二进制 **Zot OCI Registry** 替代传统 `registry:2`：
- **零停机在线垃圾回收（Online GC）**：Zot 内置后台在线 GC（`gc: true`），无需停机或中断推送/拉取即可自动清理无引用的孤立层与过期缓存。
- **Active 与最近 5 版回滚防删锁（`luma-pin-*`）**：
  - 每次部署或回滚时，`FluxReconciler` 自动为当前线上版本打上 `luma-pin-active-<project>-<env>` 标签，并为最近 $N = 5$ 个健康版本保留 `luma-pin-rollback-<project>-<env>-v<gen>` 标签。
  - Zot 策略配置为永不回收带 `luma-pin-*` 标签的 Manifest，**100% 保证最近 5 个历史版本随时可在 `< 5s` 内秒级回滚，绝不出现 `manifest unknown`**。

## Luma 特色能力在 Kubernetes 架构下的实现 {#luma-signature-features-in-kubernetes}

Luma 极简的开发者契约（`luma.yaml` 与 `luma.compose.yml`）在底层被确定性地编译为 Kubernetes 与 Flux 原语，所有标志性的混合云拓扑与暴露能力均被原生继承：

1. **多 Region 调度（`cn`、`global`、`home` 及自定义 Region）与节点亲和（`node`）**：
   - `region` 字段自动编译为 Pod 的 `nodeSelector`（`luma.io/region: <region>` 与 `topology.kubernetes.io/region: <region>`）。
   - 若同时指定 `node`，则追加 `kubernetes.io/hostname: <node>` 精准绑定。
2. **全部 6 种暴露模式（`exposure`）与 Region 强校验**：
   - `cn-edge`（要求 `region: cn`）：编译为绑定国内边缘网关的 Traefik `Ingress`，配合自动 HTTPS 与 Flux 升级后真路由探测。
   - `external-edge`（要求 `region: global`）：编译为海外边缘 `Ingress`，联动 `external-dns` 自动同步 Cloudflare 域名解析。
   - `tailscale-relay`（要求 `region: home`）：工作负载运行在家庭内网无公网 IP 的 `home` 节点（如 `macbook-pro`），边缘网关 Traefik 通过 Tailscale WireGuard 组网隧道（`luma.io/relay-transport: tailscale-mesh`）将公网 HTTPS 流量反代至家庭节点 Pod。
   - `tcp-relay`：校验 `publishPort`（禁止占用 `80`/`443` 保留端口）并生成 Traefik `IngressRouteTCP`（`traefik.io/v1alpha1`），通过 `tcp-<port>` EntryPoint 实现四层 TCP 直通转发。
   - `cloudflare-tunnel`：生成带 `luma.io/edge-gateway: cloudflare-tunnel` 注解的路由声明，由集群内 `cloudflared` 直接将域名流量代理至内部 `ClusterIP` Service。
   - `none`：仅生成集群内部 `ClusterIP` `Service`，不暴露任何公网路由。
3. **区域感知出口策略（`egress`）与 `proxy: true`**：
   - 当服务声明 `proxy: true` 时，自动向容器注入 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 以及完备的 `NO_PROXY` 白名单（强制直连 `localhost`、K8s 集群网段 `.svc.cluster.local`、Zot `100.66.177.70:5000` 及 Gitea `gcode.gaojiua.com`）。

## CLI 与控制台常用命令 {#cli-web-console-workflows}

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
