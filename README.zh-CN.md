# Luma

[English](README.md) | [中文](README.zh-CN.md)

Luma 是一个自托管部署控制面，基于 Docker Swarm，并内置 Portainer 作为运维与部署执行入口。

它把分散的服务器组织成命名部署区域，让任意已登录客户端都可以通过一份小型 YAML manifest 部署服务。

## 核心概念

Luma 把用户需要理解的模型压缩为：

```text
node / region / exposure / egress / service
```

服务 manifest 见 [docs/deployment-yaml.md](docs/deployment-yaml.md)。Agent 可以使用 [skills/luma-deployment-yaml](skills/luma-deployment-yaml) 里的可安装 skill 来生成或审阅部署 YAML。

运行时组件是：

```text
Luma CLI        安装、登录、加入节点、渲染、诊断、部署
Luma Control    manager 节点上的自托管 API，负责认证与编排
Portainer       必需的运维控制台和部署执行器
Docker Swarm    容器运行时与调度器
Traefik         公开 HTTP/HTTPS 入口
Cloudflare      DNS 和可选 Tunnel
Egress Gateway  镜像拉取和指定服务的出站代理
```

## 5 分钟上手

无需 clone 仓库即可安装 CLI：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

安装脚本会下载 release archive，在 `~/.local/share/luma/venv` 下创建独立 venv，把 `luma` 命令 shim 写到 `~/.local/bin/luma`，并在需要时把 `~/.local/bin` 加入 shell profile。安装后可以立即使用 `~/.local/bin/luma`，或打开新 shell / 执行 `exec $SHELL -l` 后使用更短的 `luma` 命令。

如果从源码 checkout 开发，同一个脚本仍然可用：

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

每台机器都使用同一个安装器：

- **manager server**：安装 CLI，然后 `luma bootstrap manager ...` 安装 Docker、Tailscale、Swarm、Traefik、Portainer、Luma Control 和 egress。
- **worker server**：安装 CLI，然后 `luma node join ...` 安装/检查 Docker，连接 Tailscale，加入 Swarm，并应用节点标签。
- **client machine**：只安装 CLI，然后 `luma login ...` 和 `luma deploy ...`；本机不需要 Docker、SSH、Cloudflare 凭据或 Portainer 凭据。

安装器会在存在 `.env` 时加载它，并在创建 virtualenv 前修复 Linux DNS，降低新云服务器上 Python 包安装失败的概率。如果缺少 `python3`，脚本会打印对应系统的安装命令并退出。安装指定 tag 时设置 `LUMA_INSTALL_REF`：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.8 sh
```

只卸载本地 CLI，不删除用户 secret 或登录 context：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

删除本地 CLI、`~/.luma.config.json` 和 `~/.config/luma`：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

卸载脚本只移除本地 Luma CLI 安装，不会移除 Docker、Swarm、Portainer、Traefik、Luma Control、已部署服务或服务器端 `/opt/luma` 状态。

如果 shell 报 `permission denied: luma`，通常是因为解析到了仓库里的 `luma/` Python package 目录，而不是 venv 命令。可以使用：

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

在第一台完整 manager 节点上，先创建本地 `.env` 保存 secret：

```bash
cp .env.example .env
luma bootstrap manager --domain luma.example.com --profile single-node
```

如果缺少必需的本地配置，命令会先交互式询问并保存到 `~/.luma.config.json`。`.env` 仍可作为项目本地覆盖，shell 里已经 export 的值优先级最高。如果配置了 `CLOUDFLARE_API_TOKEN` 但没有 `providers.dns`，manager bootstrap 会根据控制面域名推断 Cloudflare zone，并在安装 `/opt/luma/luma.yaml` 前写入 DNS provider 配置。如果没有 edge DNS target，交互式 bootstrap 会询问 `LUMA_DNS_EDGE_TARGET` 并写入 `providers.dns.edgeTarget`。

创建或编辑 `luma.yaml`：

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    edgeTarget: 203.0.113.10

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - swarm-manager
      - edge
      - egress
```

默认控制 API 镜像发布为 `ghcr.io/liutianjie/luma-control:latest`。如果从源码 checkout 开发，可以在 bootstrap 前设置 `defaults.images.lumaControl: luma-control:local`，或 export `LUMA_CONTROL_IMAGE=luma-control:local`。

在第一台完整 manager 服务器本机执行 bootstrap：

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
```

Bootstrap 是默认的一体化安装路径，会以 `[start]`、`[ok]`、`[fail]` 输出每一步状态。`single-node` 会安装 Docker，按配置连接 Tailscale，初始化 Swarm，创建 overlay network，部署 Traefik、Portainer 和 Luma Control，配置防火墙，并设置 egress。Portainer 会自动初始化并绑定到 Luma Control；默认用户不需要手动创建 Portainer webhook。运行带 egress 的 profile 前先设置 `EGRESS_SUBSCRIPTION_URL`，也可以先用 `--skip-egress` 跳过，后续再修复。

如果某一层失败，可以重跑 bootstrap，或只修复对应层：

```bash
luma portainer setup
luma egress setup
luma tailscale connect
```

Bootstrap 输出里包含 deploy token 和 join token。任意客户端机器可以用 deploy token 登录：

```bash
luma login https://luma.example.com --token <deploy-token>
luma context list
```

每台新增服务器都在该服务器本机 join：

```bash
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

## 节点区域

Luma 使用 `region` 作为节点和服务的调度边界。

这些字段含义不同：

- `--name`：Luma 和 Docker 中的人类可读节点标识。它可以是任意唯一名称，但建议使用 `global-sg-1` 或 `home-mac-mini` 这类清晰名称。
- `--region`：服务 manifest 用于调度匹配的区域标签。`region: cn`、`region: global`、`region: home` 会匹配这个值。

例如，`--name m3max --region home` 表示节点叫 `m3max`，并获得 `region=home`。节点名不影响调度。

Manager bootstrap profile：

- `single-node`：第一台一体化 manager。运行 Swarm manager、Traefik、Portainer、Luma Control 和 egress。
- `cn-edge`：国内公开 edge/manager profile，不包含一体化 egress setup。

在 manager 服务器上使用 `luma bootstrap manager`：

```bash
luma bootstrap manager --domain luma.example.com --profile single-node
luma bootstrap manager --domain luma.example.com --profile cn-edge
```

在被加入的机器本机运行 `luma node join`：

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

需要运行时代理的服务在 manifest 中声明 `proxy: true`。它们仍然按 `region` 调度，会加入 `egress` overlay network，并自动获得 `HTTP_PROXY` / `HTTPS_PROXY`。这是容器运行时代理，不是镜像拉取代理。

macOS home 节点需要先安装并启动 Docker Desktop 和 Tailscale；Luma 不会在 macOS 上使用 apt。
非 apt Linux 发行版需要在 `luma node join` 前手动安装 Docker。

如果需要在 manager 重建前让节点退出，先在该节点上运行：

```bash
luma node exit
```

这会离开 Docker Swarm，并删除 `/opt/luma` 下的本地 Luma runtime 状态。默认保留 Tailscale 以及 Docker image/volume cache。只有明确想清理未使用 Docker 缓存和 volume 时才使用 `--prune-docker`，需要同时退出 Tailscale 时使用 `--tailscale`。

## 更新已有 Manager

新 Luma 代码合并并发布 control image 后，在 manager 上运行：

```bash
luma update
```

`luma update` 会先更新本地 CLI，然后运行幂等的 manager bootstrap。它会从 `/opt/luma/control/control.json` 推断控制面域名；只有这个状态缺失，或你明确要更换控制面域名时，才需要传 `--domain luma.example.com`。它会刷新 `/opt/luma/luma.yaml`、`/opt/luma/control/control.json`，拉取当前 `ghcr.io/liutianjie/luma-control:latest`，并重新部署 Luma Control 服务。已有 Portainer 数据、deploy token、join token、Swarm 节点和服务 stack 都会保留，除非你明确执行 purge 或 reset。

如果已安装 CLI 太旧，不认识 `luma update`，先运行安装器再重试：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
luma update
```

检查本地 CLI 和 manager control API 版本：

```bash
luma version
luma version --control-url https://luma.example.com
```

更新后的 control API 会报告 `Node join model: region-first`。如果没有，说明 manager 仍在运行旧的 `luma-control` task。

任意已登录客户端都可以部署服务。客户端不需要 Docker、SSH key、Cloudflare 凭据或 Portainer webhook：

```bash
luma deploy examples/public-cn-service.yaml
```

Bootstrap 后的镜像拉取会使用 egress 配置的 Docker daemon proxy。Luma 会先按 manifest 中的原始镜像部署；如果 manager 拉取失败，它会把生成的 stack 改写为配置的镜像源 fallback，例如 `docker.1panel.live/<image>`，并在 CLI 输出里报告 fallback。

运行诊断：

```bash
luma doctor
luma doctor --legacy-ssh --deep  # 可选 legacy 节点检查
```

发布安装器和 tag 版本的 release notes 见 [docs/release.md](docs/release.md)。

如果 bootstrap 时 Tailscale 没有连接：

```bash
luma tailscale connect
```

## 日常工作流

创建服务 manifest：

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

在小规格 manager 节点上，应为 app manifest 显式声明资源边界。Luma 内置的 Traefik、Portainer、Luma Control 和 egress stack 已经带保守 Swarm resource limits；业务服务和 manager 共用机器时，也应设置 `resources.limits` 和 `resources.reservations`。

默认通过 Portainer 部署：

```bash
luma deploy app.yaml
```

公开 `cn-edge` 和 `external-edge` 服务部署后，Luma 还会探测公开路由。应用根路径 `/` 返回 `HTTP 404` 仍然表示路由已经到达应用；如果应用没有首页，请用实际路径如 `/admin/` 验证。

如果一个私有 repo 中有多个 GitOps stack，为每个服务配置独立 webhook env：

```yaml
name: api
portainer:
  webhookUrlEnv: PORTAINER_WEBHOOK_API
```

## 文档

- `docs/concepts.md`：node / region / exposure / egress / service。
- `docs/profiles.md`：内置 bootstrap profiles。
- `docs/secrets.md`：环境变量和 secret 处理。
- `docs/troubleshooting.md`：常见失败和修复。
- `docs/exposure-model.md`：流量暴露模式。
- `docs/egress-gateway.md`：出站代理网关。

## 安全

不要提交 API token、Portainer webhook 或代理订阅 URL。

如果 token 或订阅 URL 已经贴进聊天或日志，在发布仓库前先轮换。
