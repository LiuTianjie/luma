# 部署应用 {#deploying-applications}

所有部署都经过 Luma Control：CLI 发送 manifest，Control 渲染 Nomad job、更新 DNS 和路由、提交 job，并把进度实时返回。

```text
luma deploy app.yaml -> Luma Control -> Nomad job -> Nomad client -> Docker container
```

## 选择部署方式 {#choose-a-workflow}

| 你手上有 | 命令 | 过程 |
| --- | --- | --- |
| 已发布的容器镜像 | `luma deploy app.yaml` | 部署 manifest 中写明的镜像 |
| 本地代码目录 | `luma build local .` | 用本机 Docker Buildx 构建，推送到集群镜像仓库，然后部署 |
| GitHub 或 Gitea 仓库 | `luma builder submit owner/repo` | 空闲且架构匹配的 Runner 克隆、构建、推送，再由 Builder 请求 Control 部署 |
| `docker-compose.yml` | `luma compose deploy luma.compose.yml` | 把所有服务一起部署到同一个节点 |

`luma deploy` 从不构建源码。源码构建使用独立 Builder、Runner 和已配置的镜像仓库，见[构建](#builds)。

## 部署镜像 {#deploy-an-image}

用 `luma init` 生成 manifest，或手写一份：

```yaml
name: api
image: ghcr.io/acme/api:1.4.2
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
resources:
  limits:
    memory: 512M
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
```

然后：

```bash
luma validate api.yaml            # schema, region/exposure rules; no network
luma deploy api.yaml --dry-run    # render a local Nomad preview; no deployment
luma deploy api.yaml
```

部署是 upsert：用同一个 `name` 重新部署会更新已有的 Nomad job，Nomad 会保留上一个版本用于[回滚](operations.zh-CN.md#roll-back)。生产环境请使用不可变的镜像 tag 或 digest；`latest` 这类可变 tag 会让回滚结果不可预测。

Control 按以下步骤执行并逐步输出：

1. 保存 manifest 和作用域密钥；
2. 渲染 Nomad job；`tailscale-relay` 和 `tcp-relay` 还会渲染 Traefik 路由文件；
3. 创建或更新 Cloudflare DNS（`--skip-dns` 跳过）；
4. 提交 job 并等待滚动发布完成；
5. 探测公网路由并等待 provider 和上游收敛；Traefik 路由持续缺失时可以重建对应入口，网关错误持续存在时可以重建应用 allocation。

并发部署共享同一 Traefik 入口正在执行的恢复操作。Control 在开始恢复前重新探测路由，避免已经恢复后再触发不必要的重启。不同入口和正常部署仍独立执行。

默认等待 3000 秒（`--timeout`）。如果某一步在前面步骤已经修改集群后失败，部署会记录为 `failed_partial`，之后仍可用 `luma app remove` 清理。

所有字段见 [部署 YAML 参考](deployment-yaml.zh-CN.md)；调度与入口的含义见[核心概念](concepts.zh-CN.md)和[暴露模型](exposure-model.zh-CN.md)。要通过阿里云 ESA 提供公网服务，加上 `cdn: esa`，见 [CDN](cdn.zh-CN.md)。

## 密钥 {#secrets}

在 manifest 中引用变量，不要把值写进 Git：

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

部署时从 `.env` 提供，或提前保存：

```bash
luma deploy api.yaml --env .env                         # only referenced variables are sent
luma secret set DATABASE_URL --scope api                # prompts for the value
luma secret import .env.production --scope api          # every variable in the file
```

密钥按应用名隔离，`api/DATABASE_URL` 和 `worker/DATABASE_URL` 互不影响。私有镜像的仓库凭据用 `luma registry login` 设置。详见[密钥与凭据](secrets.zh-CN.md)。

## 构建 {#builds}

远程仓库构建运行在独立解耦的 **Luma Build** 插件（`luma-build`）和凭据认证的 **Build Runner**（`luma runner`）之上。通过 `luma addon install builder` 安装一次插件，再用 `luma runner join` 接入一台或多台 Linux 或 macOS Runner（详见[插件与统一登录](addons.zh-CN.md)和[构建 Runner](runners.zh-CN.md)）。

完成 `luma login` 登录 Luma Control 后，`luma builder` 会自动发现当前集群的 Builder 地址并复用 Control 凭据；如需显式指定或覆盖，也可以执行 `luma builder use https://build.example.com`（或传入 `--url` / 设置 `LUMA_BUILD_URL`）。

### 原生架构 Runner {#native-architecture-runners}

Luma Build 对 ARM 构建从不回退到慢速 QEMU 模拟。为目标集群架构分别注册原生 AMD64 和 ARM64 Runner：

```bash
luma builder token create x86-builder --weight 200 --cache-mode read-write
luma builder token create arm64-builder --weight 100 --cache-mode read-only
```

随后在各构建机器上运行 `luma runner join --url https://build.example.com --token <runner-token>`（支持 Linux 及运行 OrbStack / Docker Desktop 的 macOS）。每台 Runner 会上报真实原生 Docker 平台，并维护持久化本地 BuildKit 缓存（L1）与共享镜像仓库层缓存（L2）。

凡包含 ARM64 目标的构建（包括混合 AMD64/ARM64 构建），调度器都会将其派发给原生 ARM64 Runner，并在构建前核验 Docker 引擎的真实原生架构。没有匹配的原生 Runner 在线时，任务会继续排队或快速报错，而不会静默模拟。

### 提交仓库构建 {#submit-a-repository-build}

通过 CLI 或控制台 **创建应用** 中的 Git 表单提交 GitHub 或 Gitea 仓库构建：

```bash
luma builder submit acme/app --ref main --env .env                      # GitHub owner/repo
luma builder submit https://gitea.example.com/acme/app.git --ref v1.4.0
```

私有仓库只需在 Control 保存一次 Git provider 令牌（通过作用域密钥与 Control SSO 供 Builder 复用），之后直接引用：

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin
luma git-provider repos github:personal
luma builder submit --provider-id github:personal --repository acme/app --ref main
```

Builder 会自动查找单服务 manifest（`.luma.yml`、`luma.yml`、`*.luma.yml`）或 Compose sidecar（`luma.compose.yml`、`.luma.compose.yml`、`*.luma.compose.yml`、`docker-compose.luma.yml`）。也可以用 `--compose-sidecar 仓库内路径` 显式指定，或在仓库没有部署清单时用 `--manifest 本地文件`。对 Compose 仓库，带 `build:` 的服务会被构建并改写为推送后的 `image:`；本地可用 `luma compose validate --import-mode luma.compose.yml` 校验。

### Dockerfile 阶段与固定 Git 输入 {#dockerfile-targets-and-pinned-git-inputs}

`build.target` 选择一个字面量 Dockerfile 阶段名。远程仓库构建还可以把其他仓库作为具名源码上下文：

```yaml
# docker-compose.yml; the same build fields work in a single-service manifest
services:
  worker:
    build:
      context: .
      dockerfile: Dockerfile
      target: release
      x-luma-git-contexts:
        library:
          lock: deploy/library.lock.json
```

`lock` 是相对主仓库根目录的文件路径，不是内联对象，也不相对 `build.context`。该 JSON 文件必须且只能包含 `repository`、`commit`、`archive_sha256`：

```json
{
  "repository": "https://gitea.example.com/acme/library.git",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "archive_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

将示例摘要替换为完整小写 commit 和 `git archive --format=tar.gz <commit>` 的 SHA256。Runner 在校验两者后才把导出的文件作为 BuildKit 具名上下文 `library`，Dockerfile 可用 `COPY --from=library` 引用。Git 托管平台的 archive 接口可能产生不同字节，不能代替该归档。

额外仓库必须与主仓库具有相同 HTTPS origin（主机和端口），URL 不得包含凭据、query 或 fragment。读取使用主仓库选定的 Git provider 凭据，该凭据必须能访问每个锁定仓库。拉取和校验都发生在 Docker 之外；进入 BuildKit 的只有已验证源码，不包含 Git 凭据、凭据辅助文件或 `.git` 元数据。

`luma build local` 不支持 `x-luma-git-contexts`，包括 Compose 构建。额外 Git 输入必须走远程 `luma builder submit`，不能把 provider 凭据塞进 Dockerfile 绕过。仅使用 `build.target` 不代表必须有额外 Git 输入。

### 构建记录与队列 {#build-runs-and-the-queue}

提交后的构建进入 `luma-build` 持久化 SQLite 队列，按两个解耦阶段执行：

- **构建**：匹配原生平台的空闲 Runner 按缓存亲和（短亲和窗口内优先派发给上次编译该仓库的 Runner，保持 L1 本地 BuildKit 热缓存）与权重领取租约。镜像推送凭据和 Git 令牌保存在 `luma-build` 作用域密钥中，仅在执行租约期间从内存下发给 Runner，任务结束后立即清理。
- **部署**：镜像推送完成后 Runner 立即释放以承接下一次构建，同时 Builder 的部署工作线程向 Luma Control 提交发布。Control 上最多八个互不冲突的应用可以并行部署，同一应用（`name`）、冲突域名或 TCP 中继端口仍串行处理。同一分支/ref 内，新提交会自动取代旧的排队任务，避免旧构建覆盖新版本。

```bash
luma builder list --app api --status failed
luma builder logs <build-id>
luma builder retry <build-id>
luma builder cancel <build-id>
```

构建被接受后关闭 CLI 不会取消构建。排队中的请求在重启后保留；Runner 管理与缓存调优见[构建 Runner](runners.zh-CN.md)。

### 构建本地代码与托管镜像仓库 {#build-a-local-checkout-and-managed-registry}

如果希望直接在本机通过 Docker Buildx 构建而不经过远程 Runner 队列，可配置集群镜像仓库（如 GHCR / ACR 等外部 OCI 仓库，或通过 `luma registry serve` 部署的自托管仓库），并用 `luma build config` 配置 Control 的默认构建目标：

```bash
luma registry serve --node builder
luma build config --registry-host 100.64.0.70:5000 --push-host 100.64.0.70:5000
```

随后在本地代码目录执行构建与部署：

```bash
cd app
luma build local . --env .env
```

项目身份取自 `origin` 远程地址（没有时用 `--repo-url`）。Luma 用本机 Docker Buildx 构建，在 ARM 构建前核验 Docker 引擎的原生架构，推送到已配置的镜像仓库，再将不可变镜像提交给 Control 部署。本机必须能访问目标镜像仓库。可选参数：`--platform`、`--context`、`--dockerfile`、`--compose-sidecar`、`--builder`（复用已有 Buildx builder）和 `--proxy`（基础镜像和 `RUN` 步骤使用的 HTTP 代理）。

## Compose 应用 {#compose-applications}

保持标准的 `docker-compose.yml`，再加一个说明部署位置和方式的 Luma sidecar：

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --env .env
```

同一个应用的所有服务运行在同一个节点上，共享网络命名空间。持久卷默认使用该节点的本地存储。Compose 不支持 `cloudflare-tunnel`。详见 [Compose 与存储](compose-storage.zh-CN.md)。

## 部署方式记录 {#recorded-workflows}

Control 会记住每个应用上一次成功的构建部署方式（本地构建、Builder 提交、镜像部署或 Compose，以及 ref、平台、区域等参数）。之后的 `deploy`、`builder submit`、`build local` 或 `compose deploy` 会与记录对比：

- 没有记录或完全一致：继续执行并刷新记录；
- 存在差异：显示差异并停下等待确认。终端中会直接询问；JSON、quiet 或非交互模式会以非零状态退出。确认差异后才用 `--accept-workflow-change` 重试。

```bash
luma workflow show api
luma workflow record api --note "Built on Luma Build runners" -- builder-submit acme/api --ref main
luma workflow run api --path ~/src/api
```

记录中不会包含令牌或 `.env` 内容。备注是自由文本，不要写入密钥。

## CI {#ci}

CI 可以在不保存登录的情况下使用 CLI：

```bash
python -m pip install "luma-infra==0.2.92"
export LUMA_CONTROL_URL=https://luma.example.com
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
luma deploy deploy/app.yaml --format ndjson --timeout 3000
```

连接参数的优先级为：命令行参数 > 环境变量（`LUMA_CONTROL_URL`、`LUMA_DEPLOY_TOKEN`、`LUMA_CONTROL_CONTEXT`、`LUMA_INSECURE`、`LUMA_RESOLVE_IP`）> 已保存的登录。与已保存登录不同的 `--control-url` 不会借用其令牌。支持 `--format` 的命令可输出 `text`、`json` 和 `ndjson`，请同时检查退出状态。

在工作站上，可以把多个集群保存为登录，并按命令选择：

```bash
luma login https://staging.example.com --token-stdin < staging.token
luma context list
luma deploy app.yaml --control-context staging
```
