# 安装与初始化 {#bootstrap}

Luma CLI 会自动完成初始化。目前首个受支持的目标系统是 Ubuntu 22.04 及以上版本。

首次执行 `luma bootstrap manager` 会自动在 `/opt/luma/control/control.sqlite3` 创建本地 SQLite 控制数据库。新安装直接初始化 SQLite，不会先创建旧版 JSON 状态。无需独立数据库服务、连接字符串或迁移命令。SQLite 通过 Python 标准库的 `sqlite3` 模块访问；安装程序使用本地 Python 虚拟环境，Control 镜像使用自身的 Python 运行时。

旧版 JSON 导入仅用于已有安装的升级兼容。单 Manager 的存储布局与备份命令见[控制面存储与恢复](control-storage.md)。

## 1. 准备本地 CLI {#1-prepare-local-cli}

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

本地需要 Python 3.9+，以及 curl 或 wget。只有运行工作负载的服务器需要 Docker。

## 2. 准备 Manager 节点 {#2-prepare-the-manager-node}

首次初始化应直接在完整的 Manager 节点上执行。curl 安装的 CLI 即可，不必先有 `luma.yaml`。可选：把服务器写进 `luma.yaml`，以便推导 DNS 目标与标签：

```yaml
nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - nomad-server
      - edge
      - egress
```

如果 sudo 需要密码：

```bash
export LUMA_SUDO_PASSWORD='...'
```

如需无人值守登录 Tailscale：

```dotenv
TAILSCALE_AUTHKEY=...
```

如需内置出网网关：

```dotenv
EGRESS_SUBSCRIPTION_URL=...
```

把这些值写入 `.env`，Luma 会自动加载。

默认的 Manager 初始化使用 Luma 配置的已发布 Control 镜像。先发布可拉取的镜像，再通过 `defaults.images.lumaControl` 或 `LUMA_CONTROL_IMAGE` 固定该镜像。初始化和更新不会复用过时的本地镜像，也不会临时构建备用镜像；配置的镜像无法拉取时，操作会失败。

发布自定义 Control 镜像：

```bash
docker build -f Dockerfile.control -t ghcr.io/<you>/luma-control:latest .
docker push ghcr.io/<you>/luma-control:latest
export LUMA_CONTROL_IMAGE=ghcr.io/<you>/luma-control:latest
```

## 3. 初始化 Manager {#3-bootstrap-the-manager}

对于第一台一体化服务器：

```bash
luma bootstrap manager --domain luma.example.com
```

该命令具有幂等性，可以重复执行。它会安装 Docker，在 Nomad server agent 未运行时安装并启动它，应用节点 `meta`（region / luma_node_name / ingress / egress），创建运行目录，配置 UFW，并将 Traefik 和 Luma Control 部署为 Nomad job。它也会安装 Tailscale；设置 `TAILSCALE_AUTHKEY` 后，会自动将节点登录到 tailnet。包含 `egress` 角色的配置还会执行出网设置。如果 Manager 需要代理才能拉取配置的 Control 镜像，请先设置 `EGRESS_SUBSCRIPTION_URL`。中国大陆的 Manager 使用默认 GHCR Control 镜像时，不应使用 `--skip-egress`。

初始化期间，Luma 会实时输出各步骤日志：

```text
[start] Install Docker
[ok] Docker installed
[start] Deploy Traefik
[fail] Deploy Traefik
  Fix: Re-run luma bootstrap manager after fixing the error
```

如果某一步失败，先修复对应环节，然后重新初始化，或运行针对性的修复命令：

```bash
luma tailscale connect
luma egress setup
```

如果希望先初始化节点，稍后再配置出网，只有在能够直连 Control 镜像仓库，或 `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` 指向 Manager 可拉取的仓库时，才使用 `--skip-egress`：

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

输出包括：

```text
Control domain: luma.example.com
Control URL: https://luma.example.com
Orchestrator: nomad
Nomad API: http://203.0.113.10:4646
Cluster: luma-...
Management token: ...
Node join token: ...
```

请妥善保管管理令牌和节点加入令牌。

同一段输出会打印 Dashboard 地址。打开后粘贴管理令牌，在 **应用 → 创建应用** 部署 **hello-world 首装验证**。该冒烟服务不需要额外 DNS、Tailscale、Registry 或 LAE。若某步打印了 `[fail]`，按 `Fix:` 修好后重跑同一条 bootstrap；`luma egress setup`、`luma tailscale connect`、`luma doctor` 可修单层。

Luma 只向用户提供两类令牌：

- **管理令牌**：供可信 CLI 客户端和控制台使用，用于部署、配置存储、管理密钥与镜像仓库，以及操作节点。为兼容旧版本，CLI 环境变量仍叫 `LUMA_DEPLOY_TOKEN`。
- **节点加入令牌**：仅供加入集群或刷新本地节点 agent 的服务器使用。

每个节点的 agent 凭据由内部管理。`luma node join` 和 `luma update` 会自动申请并安装，Control 只保存其哈希值。

`--domain` 指定 Luma Control 的 URL。Nomad HTTP API 不通过此域名路由；它监听 Manager 的 Tailscale 地址上的 `4646` 端口，仅能通过 tailnet 访问。

## 4. 从客户端登录 {#4-login-from-a-client}

在需要获得部署权限的机器上运行：

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

客户端把端点、集群 ID 和管理令牌存储在 `~/.config/luma/contexts/`。客户端不需要 Docker、SSH 权限、Cloudflare 凭据或 Nomad 凭据。

在浏览器中打开控制台：

```text
https://luma.example.com/dashboard/
```

输入同一个管理令牌，即可查看控制面就绪状态、节点、服务及推导的流量路径。浏览器会把令牌保存在本地存储中，因此仅应在可信设备上使用。

## 5. 加入工作节点 {#5-join-worker-nodes}

直接在每台新增服务器上运行：

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

节点向 Manager 获取 Nomad gossip key 和 server retry-join 地址，然后在本地注册为 Nomad client。本地加入成功后，节点会回调 Luma Control，让 Manager 自动记录区域与 Luma 节点 `meta`。`--name` 是状态输出和服务清单使用的 Luma 节点名，会写入客户端的 `meta.luma_node_name`，供固定节点调度使用。`--region` 定义调度边界，写入 `meta.region`。

可以添加 `--engine nomad` 显式选择 Nomad client agent 流程；它本身就是默认值，通常无需指定。

Luma 会为每个 job 渲染 Nomad 的 `max_client_disconnect`，让通过 Tailscale 连接 server 的家庭工作节点能容忍短暂断线：客户端短暂失去 RPC 连接时，本地 allocation 会继续运行，而不是被终止并重新调度。

### 节点所需端口 {#required-node-ports}

Luma 在 Linux 节点初始化或加入时会配置 UFW。如果还使用了云安全组、主机防火墙或 Tailscale ACL，也需要放行以下路径：

| 端口 | 方向 | 用途 |
| --- | --- | --- |
| `80/tcp` | 互联网到 Manager/边缘节点 | HTTP 入口及 Let's Encrypt HTTP-01 验证。 |
| `443/tcp` | 互联网到 Manager/边缘节点 | 公共服务和 Luma Control 的 HTTPS 入口。 |
| `tcp-relay` 发布端口 | 互联网到 Manager/边缘节点 | 公共 TCP 中继端口，例如 MySQL 的 `3306/tcp`。Luma 从 Control 状态恢复 Traefik 监听器；云防火墙和安全组须放行相同端口。 |
| `4646/tcp` | 客户端/Traefik 到 Nomad server | Nomad HTTP API，用于部署、状态与服务发现。 |
| `4647/tcp` | Nomad client 到 server | Nomad RPC。 |
| `4648/tcp` 和 `4648/udp` | 节点之间 | Nomad Serf gossip，用于 server 成员通信。 |

`7890/tcp` 和 `7890/udp` 明确禁止公网入站访问，出网代理只服务于本地 Docker 和服务的出站流量。为此，Luma 同时安装主机防火墙、raw `PREROUTING` 和 Docker `DOCKER-USER` 防护，因为 Docker 发布端口及其 userland proxy 可能绕过普通 UFW 拒绝规则。

Manager 有 Tailscale 地址时，Luma 仅在 `tailscale0` 接口开放 `4646/tcp`、`4647/tcp`、`4648/tcp` 和 `4648/udp`；该拓扑下，Nomad 控制面流量应走 Tailscale。Agent 绑定 `0.0.0.0`，但广播其 Tailscale IP，接口级 UFW 规则会限制访问。如果有意不使用 Tailscale，而让 Nomad 通过公网 IP 通信，请用云安全组或可信来源 ACL 限制这些端口。

节点重新加入前，如果需要退出故障或重建后的 Manager，请在该节点运行：

```bash
luma node exit
```

默认会排空本地 Nomad client 并删除 `/opt/luma`，保留 Tailscale 登录状态与 Docker 缓存。添加 `--endpoint <control-url> --token <management-token-or-node-join-token>`，可在退出时从控制面注销 Luma 节点名。只有还需退出 tailnet 时才添加 `--tailscale`；只有需要删除未使用的 Docker 缓存和卷时才添加 `--prune-docker`。

## 6. 配置或刷新服务提供方 {#6-configure-or-refresh-providers}

执行 `luma bootstrap manager` 时，服务提供方配置和密钥会复制到 Manager 的 Control 状态中。初始化后修改了 `luma.yaml` 或 Cloudflare 设置，请重新运行：

如果 Manager 的本地配置有 `CLOUDFLARE_API_TOKEN`，却没有 `providers.dns`，初始化和 `luma update manager` 会根据 Control 域名推导 Cloudflare zone，查询 zone ID，并在安装 `/opt/luma/luma.yaml` 前写入 `providers.dns`。未配置 DNS 目标时，交互式初始化会询问 `LUMA_DNS_EDGE_TARGET`；非交互更新使用已配置边缘节点的公网 IP，或已有的 `LUMA_DNS_EDGE_TARGET`。

```bash
luma bootstrap manager --domain luma.example.com
```

升级 Luma 本身后，使用 `luma update`：

```bash
luma update
```

更新命令总是先刷新本地 CLI。在 Manager 上，默认的 `luma update` 会检测 Manager Control 状态（SQLite，或尚未迁移的旧版 JSON），保留已有令牌、节点及用户 job，并协调 Manager 控制面：恢复防火墙 TCP 中继端口；当 Manager 具有 `edge` 角色时协调 Traefik；更新 Tailscale 看门狗、控制配置与状态元数据；本地有 Cloudflare 凭据时推导 DNS 提供方配置；更新 `luma-control` Nomad job。它会拉取配置的 Control 镜像，以 Nomad 自动回滚模式提交 job，并尽可能刷新 Manager 的本地节点 agent。它不会重启 Docker 或 Nomad agent，不会执行出网设置，也不会重新部署用户服务。

对于已加入集群的工作/家庭节点，`luma update` 更新 CLI 并刷新本地节点 agent。旧节点没有保存 agent 元数据时，请先提供一次 Control URL 和节点加入令牌：

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

从任意已登录客户端更新就绪的非 Manager 节点 agent：

```bash
luma update fleet
```

批量更新要求节点 agent 已支持 `luma-update` 任务。如果节点因不支持批量更新而被跳过，请先在该节点运行一次 `luma update`，后续即可远程刷新。批量更新也会刷新 Tailscale 看门狗等节点侧辅助服务。

批量更新默认跳过 Nomad server（Manager）节点，以免客户端批量操作影响活动控制面。请在 Manager 主机上单独执行 `luma update manager`。

普通客户端上的 `luma update` 只更新 CLI。在 Manager 上需要传入 `--domain` 时，可执行 `luma update manager`，强制运行同样的 Manager 控制面协调流程。

首次安装或明确修复基础设施时，使用 `luma bootstrap manager --domain ...`。完整初始化可能操作 Docker、防火墙、Traefik、Nomad agent 和出网配置，应安排在维护窗口。如果旧版 CLI 仍采用之前的更新行为，先通过安装程序或包管理器更新 CLI，再运行新版 `luma update manager`。

如果 CLI 太旧，无法识别 `luma update`，请先运行一次安装程序，再重试更新命令。

当 Tailscale 和 systemd 可用时，Manager 初始化/更新会安装 systemd Tailscale 看门狗。已加入节点的 agent 安装/更新也会在 Linux 使用 systemd，在 macOS 使用 LaunchDaemon。看门狗只在连续多次对端 TCP 检查失败后重启本地 Tailscale，因此应用部署不应导致 Docker 或 Nomad agent 停机。

验证 Manager 确实在运行新版 Control API：

```bash
luma version --control-url https://luma.example.com
```

预期输出应包含 `Node join model: region-first`。

Cloudflare 配置：

```bash
export CLOUDFLARE_API_TOKEN='...'
export LUMA_DNS_EDGE_TARGET='203.0.113.10'
luma cloudflare connect --zone example.com
```

初始化会自动配置 Nomad。Luma 通过 Nomad HTTP API 执行部署。

初始化把相关 Cloudflare 配置保存到 Manager Control 状态。权威存储是 `/opt/luma/control/control.sqlite3`，新 Manager 直接创建该数据库；已有安装才会一次性导入旧 JSON。详见[控制面存储](control-storage.md)。客户端不需要这些配置值。

## 7. 验证 {#7-verify}

```bash
luma doctor
```

预期存在的核心 Nomad job：

```text
traefik
luma-control
egress-mihomo
```

## 8. 第一个公共服务 {#8-first-public-service}

```bash
luma deploy examples/public-cn-service.yaml
```

检查 DNS 和 Traefik：

```bash
curl -I https://whoami.example.com
```

## 工作节点加入时的托管镜像仓库访问 {#managed-registry-access-when-joining-workers}

`luma node join` 和 agent 驱动的 `luma node nomad-join` 都会在 **Docker/Tailscale 设置完成后、启动 Nomad 之前**配置 Luma 托管的 HTTP 镜像仓库。Control 注册响应或 agent 任务会携带 `insecureRegistries`。

在 Linux 上，该步骤将端点合并到 Docker 的 `insecure-registries`，保留其他 daemon 设置，并将端点添加到 Docker 实际生效的 `NO_PROXY`（包括已有的 daemon JSON 代理设置）。写入前会验证候选配置，采用原子写入，对有变更的现有 `daemon.json` 备份，并仅在配置或激活需要时重启 Docker。配置已生效时重复执行不会重启 Docker。它会验证 Docker 当前的传输与代理配置，以及直接请求 `/v2/` 的响应（200，或要求认证的 401）。这只能证明仓库传输方式和可达性，不能证明有权拉取特定仓库。

配置格式错误、仓库不可达或 Docker 设置未生效都会使加入失败，避免节点就绪后接收无法拉取镜像的部署。节点上有运行中的容器时，会拒绝必须修改 daemon 的操作；请先排空或维护节点。Agent 驱动的设置也会拒绝 Manager 节点，避免 Control 重启承载自身的 Docker daemon。macOS Nomad exec 节点不使用 Linux 的 Docker task driver，也不会应用 Linux daemon 变更。

`luma registry serve` 会记录仓库使用 HTTP 还是 HTTPS，供后续节点加入使用。对于已有安装，只有当 `build.registryHost` 是已注册节点的 Tailscale IP、端口为 5000，且既无显式 HTTPS 策略也无仓库凭据记录时，才识别为旧版内部 HTTP 仓库。`localhost:5000` 等仅供构建机使用的 `pushHost` 不会下发到工作节点。公共仓库和任意私有 TLS 端点都不会自动降级为 HTTP。

升级 Control 以及加入节点使用的 CLI/agent 后，才能使用该流程。带托管 HTTP 端点的 agent 驱动加入要求 `nomad-join-registry-v1`；旧 agent 必须升级，不能静默跳过仓库配置。此变更不会自动重启已加入的节点。