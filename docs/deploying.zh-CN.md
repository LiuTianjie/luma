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

源码构建需要一个构建节点（任意装有 Docker Buildx 的 Linux 节点）和一个集群能拉取的镜像仓库：

```bash
luma registry serve --node builder          # deploy the managed registry on a node
luma build config --node builder --default-node builder
```

Luma 会根据目标区域的节点决定镜像架构：ARM 节点得到 `linux/arm64`，混合架构的区域得到多平台镜像。只有覆盖全部目标架构时才接受 `--platform`。

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
python -m pip install "luma-infra==0.2.0"
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
