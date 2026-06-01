# Luma

[English](README.md) | [中文](README.zh-CN.md)

Luma 是一个面向小型自托管集群的部署控制面：用 Docker Swarm 运行服务，用 Portainer 执行部署，用 Traefik 暴露 HTTP/HTTPS，用 Cloudflare 管理 DNS，并提供一个可以在任意客户端使用的 `luma` CLI。

它的目标不是替代 Kubernetes，而是让几台分散的服务器变成可以按区域部署的运行环境：

```text
client laptop -> Luma Control -> Portainer -> Docker Swarm -> service tasks
```

## 适合谁

| 适合 | 不适合 |
| --- | --- |
| 你有 1 台或几台 VPS，希望快速部署自己的 Web/API/worker。 | 你已经需要 Kubernetes 级别的多租户、网络策略和复杂编排。 |
| 你希望 client 机器只拿 deploy token，不持有 SSH、Docker、Cloudflare 或 Portainer 凭据。 | 你完全不想使用公网域名或 Cloudflare DNS。 |
| 你需要按 `cn`、`global`、`home` 这类区域放置服务。 | 你只是在单机上跑几个 compose 服务，且不需要跨机器调度。 |
| 你想把部分 home/private 服务通过 Tailscale 或 Cloudflare Tunnel 暴露出来。 | 你不能接受 manager 上安装 Docker/Swarm/Traefik/Portainer。 |

## 你需要先准备什么

| 前置条件 | 是否必需 | 用途 |
| --- | --- | --- |
| 一个你能控制的域名 | 必需 | 控制面域名和公开服务域名，例如 `luma.example.com`、`api.example.com`。 |
| Cloudflare DNS API token | 必需 | Luma 用它创建/更新控制面和服务 DNS 记录。需要 zone read + DNS edit 权限。 |
| 一台 Linux manager | 必需 | 运行 Docker Swarm manager、Traefik、Portainer、Luma Control。评估阶段 2c2g 可以用。 |
| 公网 80/443 入站 | 公开服务必需 | Traefik 需要接收 HTTP/HTTPS 流量。 |
| Tailscale | 按需 | 私有多节点加入、`home` 节点、`exposure: tailscale-relay` 需要。普通单公网 manager 不强制需要。 |
| egress 订阅 | 按需 | 镜像拉取代理和 `proxy: true` 服务的运行时代理。可以先 `--skip-egress`。 |

client 机器只需要安装 CLI，并能访问控制面域名。它不需要 Docker、SSH key、Cloudflare token、Portainer password 或 Portainer webhook。

## 核心模型

Luma 的用户模型只有 5 个词：

| 概念 | 含义 |
| --- | --- |
| `node` | 加入 Swarm 的机器。可以是 manager、worker、home 节点。 |
| `region` | 调度边界。服务写 `region: cn` 就只会放到带 `region=cn` 标签的节点。 |
| `exposure` | 服务如何被访问，例如 `cn-edge`、`external-edge`、`tailscale-relay`、`cloudflare-tunnel`、`none`。 |
| `egress` | 出站代理能力。影响镜像拉取和 `proxy: true` 服务的运行时 HTTP/HTTPS 代理。 |
| `service` | 一份 Luma YAML manifest 描述的部署单元。 |

`region` 决定服务运行在哪类节点上，`exposure` 决定流量如何进来。它们相关但不等价。

例如：

| manifest | 调度位置 | 入口路径 |
| --- | --- | --- |
| `region: cn` + `exposure: cn-edge` | `region=cn` 节点 | Cloudflare DNS -> 国内 edge Traefik -> Swarm task |
| `region: global` + `exposure: external-edge` | `region=global` 节点 | Cloudflare DNS -> global edge Traefik -> Swarm task |
| `region: home` + `exposure: tailscale-relay` | `region=home` 节点 | 公网 Traefik -> Tailscale -> home service |
| `region: cn` + `exposure: none` | `region=cn` 节点 | 无公网入口，适合 worker/job |

国内公开域名不会“绕过服务器直接到容器”。`cn-edge` 的 DNS 会指向你配置的国内 edge target，流量先进入该节点上的 Traefik，再通过 Swarm overlay 转发到具体 task。即使你有多台国内节点，公开入口仍然是当前选定的 edge Traefik；服务副本可以分布到其他 `cn` 节点。

## 安装 CLI

无需 clone 仓库：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

安装脚本会创建独立 venv，把命令 shim 写到 `~/.local/bin/luma`。安装后可以立即用 `~/.local/bin/luma`，或打开新 shell / 执行 `exec $SHELL -l` 后使用 `luma`。

安装指定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.10 sh
```

从源码开发：

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

安装器只安装本地 CLI。它不会修改系统 DNS、Docker、Swarm、Tailscale 或防火墙；这些主机级操作只会发生在 `luma bootstrap manager` 或 `luma node join` 阶段。

如果在仓库目录下执行 `luma` 报 `permission denied: luma`，通常是 shell 命中了仓库里的 `luma/` Python package 目录。改用：

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

卸载本地 CLI：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

同时删除本地登录上下文和配置：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

卸载脚本不会删除服务器上的 Docker、Swarm、Portainer、Traefik、Luma Control、已部署服务或 `/opt/luma` 状态。

## 第一台 Manager

在 manager 服务器本机执行：

```bash
cp .env.example .env
$EDITOR .env
luma bootstrap manager --domain luma.example.com
```

常见需要放进 `.env` 或通过交互式 CLI 填写的值：

```bash
CLOUDFLARE_API_TOKEN=...
LUMA_DNS_EDGE_TARGET=203.0.113.10
TRAEFIK_ACME_EMAIL=ops@example.com
EGRESS_SUBSCRIPTION_URL=...
```

| 变量 | 什么时候需要 | 用途 |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | manager 必需 | Cloudflare DNS token。Luma 用它创建/更新控制面域名和公开服务域名记录。需要 Zone Read + DNS Edit 权限。 |
| `LUMA_DNS_EDGE_TARGET` | 通常需要 | Cloudflare A/CNAME 记录要指向的公网 IP 或 DNS 名称。没有在 `luma.yaml` 里配置 edge target / edge node public IP 时，bootstrap 会询问它。 |
| `TRAEFIK_ACME_EMAIL` | manager 必需 | Traefik/Let's Encrypt 申请 HTTPS 证书时使用的邮箱，也用于证书过期通知。 |
| `EGRESS_SUBSCRIPTION_URL` | 需要 egress 时 | 代理订阅 URL。Luma 用它生成 Mihomo 配置，服务镜像拉取代理和 `proxy: true` 的运行时代理会用到。 |
| `TAILSCALE_AUTHKEY` | 私有节点/home/tailscale-relay 按需 | 让服务器加入你的 tailnet。普通单公网 manager 和普通公开服务不强制需要。 |
| `LUMA_SUDO_PASSWORD` | sudo 需要密码时按需 | 本机执行 sudo 命令的兜底密码。只保存在本机用户配置中，不会分发给 client。 |

如果不想提前编辑 `.env`，也可以直接运行 `luma bootstrap manager --domain ...`。缺少本地配置时，CLI 会逐项说明用途并交互式询问。

`EGRESS_SUBSCRIPTION_URL` 可选。如果暂时没有，先用：

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Bootstrap 会安装/检查 Docker，初始化 Swarm，创建 overlay network，部署 Traefik、Portainer、Luma Control，配置防火墙，并按需设置 egress。它会输出 deploy token 和 join token。

如果某一层失败，可以重跑 bootstrap，或只修复对应层：

```bash
luma portainer setup
luma egress setup
luma tailscale connect
```

默认 control API 镜像是 `ghcr.io/liutianjie/luma-control:latest`。源码开发时可以在 bootstrap 前设置 `LUMA_CONTROL_IMAGE=luma-control:local`，或在 `luma.yaml` 中设置 `defaults.images.lumaControl`。

## 角色和命令速查

| 你在哪台机器上 | 想做什么 | 命令 |
| --- | --- | --- |
| manager | 首次安装控制面 | `luma bootstrap manager --domain luma.example.com` |
| manager | 更新 CLI 和控制面 | `luma update` |
| worker/home 节点 | 加入集群 | `luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1` |
| client laptop | 登录控制面 | `luma login https://luma.example.com --token <deploy-token>` |
| client laptop | 部署服务 | `luma deploy app.yaml` |
| 任意已登录 client | 管理部署 secret | `luma secret set DATABASE_URL` |
| 任意机器 | 看本地版本 | `luma version` |
| 任意机器 | 诊断本地环境 | `luma doctor` |

每台机器都可以用同一个安装器。区别在于后续命令：

- manager 跑 `bootstrap manager` 和 `update`。
- worker/home 节点跑 `node join`、`node exit`。
- client 只跑 `login`、`deploy`、`secret`、`context`。

## 添加节点

Bootstrap 输出中会给出 join token。每台新服务器都在该服务器本机执行：

```bash
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
```

`--name` 是 Luma 中的人类可读显示名；Docker join 后仍可能使用主机真实 Docker node name，Luma 会把真实 Docker node name 作为 Swarm 身份，并保存你的显示名。

`--region` 是调度标签。服务 manifest 中的 `region` 匹配它：

```bash
luma node join https://luma.example.com --token <join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <join-token> --region home --name home-mac-mini
```

macOS home 节点需要提前安装并启动 Docker Desktop 和 Tailscale。非 apt Linux 发行版需要提前手动安装 Docker。

移除节点前，先在该节点本机执行：

```bash
luma node exit
```

默认会离开 Swarm 并清理 `/opt/luma` 下的本地 Luma runtime 状态，但保留 Tailscale 和 Docker image/volume cache。需要同时退出 Tailscale 时加 `--tailscale`；明确想清理未使用 Docker 缓存和 volume 时才加 `--prune-docker`。

## 部署服务

最小公开服务：

```yaml
name: status
image: traefik/whoami:latest
region: cn
exposure: cn-edge
domain: status.example.com
port: 80
replicas: 1
```

部署：

```bash
luma validate status.yaml
luma deploy --dry-run status.yaml
luma deploy status.yaml
```

小规格 manager 上同时跑业务服务时，建议显式声明资源限制：

```yaml
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

需要运行时代理的服务写 `proxy: true`：

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
```

Luma 会自动加入 `egress` overlay network，并注入 `HTTP_PROXY` / `HTTPS_PROXY`。这只影响容器运行时出站请求，不等同于镜像拉取代理。

敏感值不要直接写进 manifest。先存到控制面：

```bash
luma secret set DATABASE_URL
```

再在 YAML 中引用：

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

完整字段参考见 [docs/deployment-yaml.md](docs/deployment-yaml.md)，示例见 [examples](examples)。

## 常见任务

| 问题 | 做法 |
| --- | --- |
| 更新 manager | 在 manager 上运行 `luma update`。正常情况下不需要再传域名。 |
| 在 client 或 worker 上运行 `luma update` 会怎样 | 只更新本地 CLI，不刷新 manager 控制面。 |
| `luma update` 什么时候需要 `--domain` | 只有 `/opt/luma/control/control.json` 缺失，或你确实要切换控制面域名时。 |
| 服务 A 从一个 region 迁到另一个 region | 改 manifest 的 `region`，必要时同步修改 `exposure`，然后重新 `luma deploy app.yaml`。 |
| 服务从公开变内部 | 把 `exposure` 改为 `none`，移除不再需要的 `domain`/公开入口配置，重新 deploy。 |
| 服务从内部变公开 | 设置匹配的 `region` + `exposure`，补 `domain` 和 `port`，重新 deploy。 |
| 新增国内 worker | 在新机器执行 `luma node join ... --region cn --name ...`。 |
| 新增海外 worker | 在新机器执行 `luma node join ... --region global --name ...`。 |
| 新增家里节点 | 先准备 Docker Desktop/Tailscale，再执行 `luma node join ... --region home --name ...`。 |
| manager 只有 2c2g | 给业务 manifest 设置 `resources.limits` 和 `resources.reservations`，避免业务服务挤占控制面。 |
| Tailscale bootstrap 时没连上 | 在对应机器运行 `luma tailscale connect`。 |
| egress 失败或后补订阅 | 设置 `EGRESS_SUBSCRIPTION_URL` 后运行 `luma egress setup`。 |
| 检查控制面版本 | `luma version --control-url https://luma.example.com`。 |
| 公开服务 `/` 返回 404 | 这通常说明路由已经打到应用了；用真实路径如 `/admin/` 验证。 |

## 文档地图

| 文档 | 内容 |
| --- | --- |
| [docs/concepts.md](docs/concepts.md) | node / region / exposure / egress / service 的概念。 |
| [docs/deployment-yaml.md](docs/deployment-yaml.md) | service manifest 字段、secret、resources、exposure 示例。 |
| [docs/exposure-model.md](docs/exposure-model.md) | `cn-edge`、`external-edge`、Tailscale relay、Cloudflare Tunnel 的流量模型。 |
| [docs/bootstrap.md](docs/bootstrap.md) | manager bootstrap 细节和 profile。 |
| [docs/node-labels.md](docs/node-labels.md) | 节点标签、region、ingress 标签。 |
| [docs/operations.md](docs/operations.md) | 日常运维和排障命令。 |
| [docs/secrets.md](docs/secrets.md) | secret 和环境变量处理。 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 常见失败和修复。 |
| [docs/release.md](docs/release.md) | 发布 tag、安装器和 control image 的流程。 |

Agent 可以使用 [skills/luma-deployment-yaml](skills/luma-deployment-yaml) 里的可安装 skill 来生成或审阅部署 YAML。

## 安全边界

- 不要提交 API token、Portainer webhook、deploy token、join token 或代理订阅 URL。
- client 机器不需要 SSH/Docker/Cloudflare/Portainer 凭据，尽量只分发 deploy token。
- join token 只给要加入集群的服务器使用。
- 如果 token 或订阅 URL 已经贴进聊天、日志或 issue，发布前先轮换。
