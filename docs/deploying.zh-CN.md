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
| GitHub 或 Gitea 仓库 | `luma import owner/repo` | 由构建节点克隆、构建、推送并部署 |
| `docker-compose.yml` | `luma compose deploy luma.compose.yml` | 把所有服务一起部署到同一个节点 |

`luma deploy` 从不构建源码。构建需要构建节点和镜像仓库，见[构建](#builds)。

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
luma deploy api.yaml --dry-run    # print the Nomad job Control will submit
luma deploy api.yaml
```

部署是 upsert：用同一个 `name` 重新部署会更新已有的 Nomad job，Nomad 会保留上一个版本用于[回滚](operations.zh-CN.md#roll-back)。生产环境请使用不可变的镜像 tag 或 digest；`latest` 这类可变 tag 会让回滚结果不可预测。

Control 按以下步骤执行并逐步输出：

1. 保存 manifest 和作用域密钥；
2. 渲染 Nomad job；`tailscale-relay` 和 `tcp-relay` 还会渲染 Traefik 路由文件；
3. 创建或更新 Cloudflare DNS（`--skip-dns` 跳过）；
4. 提交 job 并等待滚动发布完成；
5. 探测 `cn-edge` 和 `external-edge` 服务的公网路由；如果 Traefik 尚未识别，会重建一次 allocation。

默认等待 3000 秒（`--timeout`）。如果某一步在前面步骤已经修改集群后失败，部署会记录为 `failed_partial`，之后仍可用 `luma app remove` 清理。

所有字段见 [部署 YAML 参考](deployment-yaml.zh-CN.md)；调度与入口的含义见[核心概念](concepts.zh-CN.md)和[暴露模型](exposure-model.zh-CN.md)。

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

源码构建需要一个装有 Docker Buildx 的构建节点（Linux，或使用 OrbStack / Docker Desktop 的 macOS）和一个集群能拉取的镜像仓库：

```bash
luma registry serve --node builder          # deploy the managed registry on a node
luma build config --node builder --default-node builder
```

Luma 会根据目标区域的节点决定镜像架构：ARM 节点得到 `linux/arm64`，混合架构的区域得到多平台镜像。只有覆盖全部目标架构时才接受 `--platform`。

### 原生架构构建节点 {#native-builders}

先升级 Control 和构建节点 agent，再同时声明 AMD64 与 ARM64 构建节点：

```bash
luma build config --node builder --node air3 --default-node builder \
  --registry-host 100.64.0.70:5000 --push-host 100.64.0.70:5000
```

所有仓库构建节点必须上报 `native-arm-build-v1` 能力，自动选择还需要 `repository-build-plan-v1`。先升级 Control 和全部构建 agent。macOS 的 OrbStack 和 Docker Desktop 均受支持；`luma status` 会显示原生容器平台，macOS ARM 节点对应 `linux/arm64`。

当声明的构建节点具有不同架构时，Control 会先解析仓库 manifest（包括指定的 Compose sidecar 和服务级部署位置），锁定 Git commit，再选择原生构建节点。只要包含 ARM64，即使同时构建 AMD64，也必须使用原生 ARM64 builder。原生节点忙碌或离线时继续排队；没有对应 ARM 原生节点则报错，不能回退到模拟构建。运行时密钥只在部署阶段使用。

显式 `--build-node` 只能固定兼容节点，不能绕过 ARM 要求。每次 ARM 构建前都会核实所有 Buildx worker 的实际 Docker 引擎架构，不能仅依据包含 QEMU 模拟架构的支持列表。该校验覆盖仓库导入、Compose、重试和 `luma build local`，也包括自定义 Buildx builder。本地旧 CLI 需要先升级；无法验证原生架构的 remote/Kubernetes Buildx driver 不允许执行 ARM 构建。

混合 AMD64/ARM64 请求整体交给 ARM builder，其中 AMD64 部分可以使用模拟执行，不拆分到多台机器。不同 ARM 架构（例如 ARMv7 与 ARM64）需要分开提交。不含 ARM 的构建保留原有回退行为。`pushHost` 必须能被所有 builder 访问，不能使用指向各自机器的 `localhost`。

### 构建本地代码 {#build-a-local-checkout}

```bash
cd app
luma build local . --env .env
```

项目身份取自 `origin` 远程地址（没有时用 `--repo-url`）。Luma 用本机 Docker Buildx 构建，按与 `luma import` 相同的仓库路径推送到集群镜像仓库，然后部署。本机必须能访问该镜像仓库。可选参数：`--platform`、`--context`、`--dockerfile`、`--compose-sidecar`、`--builder`（复用已有 Buildx builder）和 `--proxy`（基础镜像和 `RUN` 步骤使用的 HTTP 代理）。

### 导入仓库 {#import-a-repository}

```bash
luma import acme/app --ref main --env .env                      # GitHub owner/repo
luma import https://gitea.example.com/acme/app.git --ref v1.4.0
```

私有仓库先保存一次令牌，之后引用它：

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin
luma git-provider repos github:personal
luma import --provider-id github:personal --repository acme/app --ref main
```

导入会查找单服务 manifest（`.luma.yml`、`luma.yml`、`*.luma.yml`）或 Compose sidecar（`luma.compose.yml`、`.luma.compose.yml`、`*.luma.compose.yml`、`docker-compose.luma.yml`）。可以用 `--compose-sidecar 仓库内路径` 明确指定；仓库里没有部署文件时用 `--manifest 本地文件`。对 Compose 仓库，带 `build:` 的服务会被构建并改写为推送后的 `image:`。本地用 `luma compose validate --import-mode luma.compose.yml` 检查这一路径。

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

将示例摘要替换为完整小写 commit 和 `git archive --format=tar.gz <commit>` 的 SHA256。Builder 在校验两者后才把导出的文件作为 BuildKit 具名上下文 `library`，Dockerfile 可用 `COPY --from=library` 引用。Git 托管平台的 archive 接口可能产生不同字节，不能代替该归档。

额外仓库必须与主仓库具有相同 HTTPS origin（主机和端口），URL 不得包含凭据、query 或 fragment。读取使用主仓库选定的 Git provider 凭据，该凭据必须能访问每个锁定仓库。拉取和校验都发生在 Docker 之外；进入 BuildKit 的只有已验证源码，不包含 Git 凭据、凭据辅助文件或 `.git` 元数据。

导入时显式要求协议能力：

```bash
luma import --provider-id gitea:team --repository acme/app --ref "$APP_COMMIT" \
  --compose-sidecar deploy/luma.compose.yml \
  --require-build-capability repository-build-inputs-v1
```

CLI 必须支持该参数，Control 必须声明 `required-build-capability-v1`，选中的就绪 Builder 必须声明 `repository-build-inputs-v1`。Control 在执行时再次检查实时 Builder，并在部署前要求构建结果确认能力；重试保留要求。缺能力会明确停止，不会静默忽略额外输入。源码实现不代表已发布或线上已加载：使用前需要发布相应 Luma 版本并升级 Control 与 Builder。

外部 `--manifest` 会自动要求同一能力，无需重复传该参数。仅当仓库没有清单时，Builder 才使用外部清单中通过校验的构建和调度字段；仓库自带清单在构建与部署阶段都优先。外部清单的运行环境和密钥不会作为构建输入传递。使用新版 CLI 的外部清单入口前，必须升级 Control 与 Builder。

`luma build local` 不支持 `x-luma-git-contexts`，包括 Compose 构建。额外 Git 输入必须走远程 `luma import`，不能把 provider 凭据塞进 Dockerfile 绕过。仅使用 `build.target` 不代表必须有额外 Git 输入。

### 构建记录与队列 {#build-runs-and-the-queue}

导入和重试会进入按仓库、ref 和部署目标划分的持久队列：同一目标的新提交会取消更早的排队请求，而 `main` 和 `dev` 互不阻塞。忙碌的构建节点只会让自己的请求排队，不会影响其他构建节点。

```bash
luma build list --app api --status failed
luma build logs <build-id>
luma build retry <build-id>
luma build cancel <build-id>
```

构建被接受后关闭 CLI 不会取消构建。排队中的请求在 Control 重启后保留；被中断的进行中构建会标记为失败，而不是自动重放。

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

Control 会记住每个应用上一次成功的构建部署方式（本地构建、导入、镜像部署或 Compose，以及 ref、构建节点、平台、区域等参数）。之后的 `deploy`、`import`、`build local`、`build retry` 或 `compose deploy` 会与记录对比：

- 没有记录或完全一致：继续执行并刷新记录；
- 存在差异：显示差异并停下等待确认。终端中会直接询问；JSON、quiet 或非交互模式会以非零状态退出。确认差异后才用 `--accept-workflow-change` 重试。

```bash
luma workflow show api
luma workflow record api --note "Built on the builder node" -- import acme/api --ref main
luma workflow run api --path ~/src/api
```

记录中不会包含令牌或 `.env` 内容。备注是自由文本，不要写入密钥。

## CI {#ci}

CI 可以在不保存登录的情况下使用 CLI：

```bash
python -m pip install "luma-infra==0.2.13"
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
