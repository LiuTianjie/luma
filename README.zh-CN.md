# Luma

[English](README.md) | [中文](README.zh-CN.md)

Luma 是一个面向小型自托管集群的部署控制面：底层用 HashiCorp Nomad 运行服务，通过 Nomad HTTP API 执行部署，用 Traefik 暴露 HTTP/HTTPS，用 Cloudflare 管理 DNS，并提供一个可以在任意客户端使用的 `luma` CLI。

它的目标不是替代 Kubernetes，而是让几台分散的服务器变成可以按区域部署的运行环境：

```text
client laptop -> Luma Control -> Nomad API -> Nomad client -> docker driver -> container
```

## 适合谁

| 适合 | 不适合 |
| --- | --- |
| 你有 1 台或几台 VPS，希望快速部署自己的 Web/API/worker。 | 你已经需要 Kubernetes 级别的多租户、网络策略和复杂编排。 |
| 你希望 client 机器只拿管理 Token，不持有 SSH、Docker、Cloudflare 或 Nomad 凭据。 | 你完全不想使用公网域名或 Cloudflare DNS。 |
| 你需要按 `cn`、`global`、`home` 这类区域放置服务。 | 你只是在单机上跑几个 compose 服务，且不需要跨机器调度。 |
| 你想把部分 home/private 服务通过 Tailscale 或 Cloudflare Tunnel 暴露出来。 | 你不能接受 manager 上安装 Docker/Nomad/Traefik。 |

## 你需要先准备什么

| 前置条件 | 是否必需 | 用途 |
| --- | --- | --- |
| 一个你能控制的域名 | 必需 | 控制面域名和公开服务域名，例如 `luma.example.com`、`api.example.com`。 |
| Cloudflare DNS API token | 必需 | Luma 用它创建/更新控制面和服务 DNS 记录。需要 zone read + DNS edit 权限。 |
| 一台 Linux manager | 必需 | 运行 Nomad server、Traefik、Luma Control。评估阶段 2c2g 可以用。 |
| 公网 80/443 入站 | 公开服务必需 | Traefik 需要接收 HTTP/HTTPS 流量。 |
| Tailscale | 按需 | 私有多节点加入、`home` 节点、`exposure: tailscale-relay` 需要。普通单公网 manager 不强制需要。 |
| egress 订阅 | 按需 | 镜像拉取代理和 `proxy: true` 服务的运行时代理。国内 manager 使用默认 GHCR control 镜像时，建议 bootstrap 前配置好。 |

client 机器只需要安装 CLI，并能访问控制面域名。它不需要 Docker、SSH key、Cloudflare token 或 Nomad 凭据。

## Token 模型

用户侧只需要理解两个 Luma Token：

| Token | 用在哪 | 用途 |
| --- | --- | --- |
| 管理 Token | 可信的 CLI client、Dashboard、CI | 登录控制面，部署应用，管理 secret、registry、storage 和 node。历史兼容原因，CLI 环境变量仍叫 `LUMA_DEPLOY_TOKEN`。 |
| 节点加入 Token | 要加入集群或刷新本机节点 agent 的服务器 | `luma node join ... --token <node-join-token>`，以及老节点补装/刷新 agent 时的 `luma update --control-url ... --token <node-join-token>`。 |

节点 agent 凭据是内部机制。Luma 会为每个 joined node 自动签发并写入该节点本机 agent 配置，Control 只保存 hash；用户只需要看 agent 是否在线，不需要复制或管理 agent 凭据。

## 核心模型

Luma 的用户模型只有 5 个词：

| 概念 | 含义 |
| --- | --- |
| `node` | 加入集群的机器。manager 跑 Nomad server，其余机器作为 Nomad client（worker 或 home）。 |
| `region` | 调度边界。内置 `cn` / `global` / `home`，也可以 `luma region create` 建自定义池。服务写 `region: batch-a` 就只会放到该 Region 的节点。 |
| `exposure` | 服务如何被访问，例如 `cn-edge`、`external-edge`、`tailscale-relay`、`cloudflare-tunnel`、`none`。 |
| `egress` | 出站代理能力。影响镜像拉取和 `proxy: true` 服务的运行时 HTTP/HTTPS 代理。 |
| `service` | 一份 Luma YAML manifest 描述的部署单元。 |

`region` 决定服务运行在哪类节点上，`exposure` 决定流量如何进来。它们相关但不等价。
只有当服务必须固定在某个 Luma 节点名上时，才在 manifest 里设置 `node`；Luma 仍然会保留 `region` 调度约束，并把节点名渲染成 Nomad 的 `${node.unique.name}`（或 `meta.luma_node_name`）约束。Nomad 节点身份是稳定的 UUID，固定节点服务在重新 join 后仍然指向同一台机器。

例如：

| manifest | 调度位置 | 入口路径 |
| --- | --- | --- |
| `region: cn` + `exposure: cn-edge` | `region=cn` 节点 | Cloudflare DNS -> 国内 edge Traefik -> Nomad allocation |
| `region: global` + `exposure: external-edge` | `region=global` 节点 | Cloudflare DNS -> global edge Traefik -> Nomad allocation |
| `region: home` + `exposure: tailscale-relay` | `region=home` 节点 | 公网 Traefik -> Tailscale -> home service |
| `region: cn` + `exposure: none` | `region=cn` 节点 | 无公网入口，适合 worker/job |
| `region: batch-a` + `exposure: none` | 自定义 Region 节点 | 无公网入口；副本数在该池内调度 |

国内公开域名不会“绕过服务器直接到容器”。`cn-edge` 的 DNS 会指向你配置的国内 edge target，流量先进入该节点上的 Traefik，再由 Traefik 通过 Nomad provider 发现的 allocation 转发。即使你有多台国内节点，公开入口仍然是当前选定的 edge Traefik；服务 allocation 可以分布到其他 `cn` 节点。

## 安装 CLI

无需 clone 仓库：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

安装脚本会创建独立 venv，把命令 shim 写到 `~/.local/bin/luma`。安装后可以立即用 `~/.local/bin/luma`，或打开新 shell / 执行 `exec $SHELL -l` 后使用 `luma`。

安装指定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.302 sh
```

从源码开发：

```bash
git clone https://github.com/LiuTianjie/luma.git
cd luma
./scripts/install-luma.sh
. .venv/bin/activate
```

安装器只安装本地 CLI。它不会修改系统 DNS、Docker、Nomad、Tailscale 或防火墙；这些主机级操作只会发生在 `luma bootstrap manager` 或 `luma node join` 阶段。

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

卸载脚本不会删除服务器上的 Docker、Nomad、Traefik、Luma Control、已部署服务或 `/opt/luma` 状态。

## 第一台 Manager

在 manager 服务器本机执行：

```bash
cp .env.example .env
$EDITOR .env
luma bootstrap manager --domain luma.example.com
```

Bootstrap 会自动在 Manager 本地创建 `/opt/luma/control/control.sqlite3`。
全新安装直接使用 SQLite，无需单独安装数据库服务、配置连接串或执行迁移命令。
只有已有安装的旧 JSON 状态才会在升级时自动导入。详见[控制面存储说明](docs/control-storage.md)。

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

### Manager 端 LAE Control 文件

Luma Control 为 LAE 提供服务时，Builder 与 Runtime 身份必须使用两套独立配置。Nomad job 已挂载 `/opt/luma/control`，所以传给 Control 的 principal、broker 和 admin 文件都必须放在这个目录下，并使用非软链接的私有常规文件（推荐 `0600`）。Token 内容只由 Control 在运行时读取，不会写进 Nomad Job 或 Control 状态数据库。

Builder principal 文件 `/opt/luma/control/lae-builder-principals.json`：

```json
{
  "lae-builder": {
    "tokenFile": "lae-builder.token",
    "tenantRefs": ["*"],
    "applicationRefs": ["*"]
  }
}
```

Runtime principal 文件 `/opt/luma/control/lae-runtime-principals.json`：

```json
{
  "lae-runtime": {
    "tokenFile": "lae-runtime.token",
    "tenantRefs": ["*"],
    "applicationRefs": ["*"],
    "builderPrincipalRefs": ["lae-builder"],
    "scopes": [
      "runtime:volumes:prepare",
      "runtime:deployments:write",
      "runtime:deployments:read",
      "runtime:logs",
      "runtime:metrics",
      "runtime:secrets:issue"
    ]
  }
}
```

`tokenFile` 必须和对应 principal 文件在同一目录。准备好两个 token 文件后执行：

```bash
sudo install -d -m 700 /opt/luma/control
sudo chmod 600 \
  /opt/luma/control/lae-builder.token \
  /opt/luma/control/lae-runtime.token \
  /opt/luma/control/lae-builder-principals.json \
  /opt/luma/control/lae-runtime-principals.json

LUMA_LAE_SERVICE_PRINCIPALS_FILE=/opt/luma/control/lae-builder-principals.json
LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE=/opt/luma/control/lae-runtime-principals.json
```

可选的 credential/object broker 与 LAE 超级管理员代理也使用服务端 token 文件：

```bash
LUMA_CREDENTIAL_BROKER_URL=https://lae-api.internal/v1/internal/credential-leases/redeem
LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS=5
LUMA_CREDENTIAL_BROKER_TOKEN_FILE=/opt/luma/control/lae-broker.token

LUMA_OBJECT_SOURCE_BROKER_URL=https://lae-api.internal/v1/internal/object-source-leases/redeem
LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS=5
# 有意复用 credential broker token 时可以省略下一项：
LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE=/opt/luma/control/lae-object-broker.token

LUMA_LAE_ADMIN_API_URL=https://lae-api.internal
LUMA_LAE_ADMIN_TIMEOUT_SECONDS=8
LUMA_LAE_ADMIN_TOKEN_FILE=/opt/luma/control/lae-admin.token
```

把这些路径和 URL 写进 manager 的 `.env`，然后执行 `luma bootstrap manager` 或 `luma update manager`。Manager bootstrap 只会把上述 HTTPS URL、受限 timeout 和 `/opt/luma/control` 内的文件路径传进 Nomad job。旧的 `LUMA_LAE_SERVICE_TOKEN`、`LUMA_LAE_*_PRINCIPALS_JSON` 仍可供直接运行 Control 或本地测试兼容使用，但不会被 manager bootstrap 转发；生产 Nomad manager 应使用文件模式。

如果不想提前编辑 `.env`，也可以直接运行 `luma bootstrap manager --domain ...`。缺少本地配置时，CLI 会逐项说明用途并交互式询问。

只有当 manager 能直接拉取配置的 control 镜像时，`EGRESS_SUBSCRIPTION_URL` 才可以先不配置。国内机器使用默认 GHCR control 镜像时，应先配置它，不建议用 `--skip-egress`。

只有在 control 镜像 registry 可直连，或你已经把 `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` 固定到 manager 可拉取的 registry 时，才用：

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

Bootstrap 会安装/检查 Docker，安装并启动 Nomad server，把 Traefik、Luma Control 作为 Nomad job 部署，配置防火墙，并按需设置 egress。它会输出管理 Token 和节点加入 Token。

如果某一层失败，可以重跑 bootstrap，或只修复对应层：

```bash
luma egress setup
luma tailscale connect
```

默认 control API 镜像是 `ghcr.io/liutianjie/luma-control:latest`。为了让升级可预测，建议发布不可变 tag，并在 bootstrap/update 前设置 `LUMA_CONTROL_IMAGE=ghcr.io/<you>/luma-control:<tag>`，或在 `luma.yaml` 中设置 `defaults.images.lumaControl`。如果配置的 control 镜像拉取失败，Luma 会直接失败。启用 egress 时，Luma 会先配置 Docker daemon 代理，再拉取默认 GHCR control 镜像。

## 角色和命令速查

| 你在哪台机器上 | 想做什么 | 命令 |
| --- | --- | --- |
| manager | 首次安装控制面 | `luma bootstrap manager --domain luma.example.com` |
| manager | 更新 CLI 和控制面 | `luma update` |
| 已登录 client | 更新 ready 的非 manager 节点 Luma | `luma update fleet` |
| 可信设备上的浏览器 | 升级 Control、节点并检查全部公网路由 | `https://luma.example.com/dashboard/fleet` |
| 已登录 client | 创建自定义 Region | `luma region create batch-a --egress proxy` |
| worker/home 节点 | 加入集群 | `luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1` |
| client laptop | 登录控制面 | `luma login https://luma.example.com --token <management-token>` |
| client laptop | 部署服务 | `luma deploy app.yaml` |
| 任意已登录 client | 管理部署 secret | `luma secret set DATABASE_URL` |
| 任意已登录 client | 管理私有镜像仓库凭证 | `printf '%s' "$GHCR_TOKEN" \| luma registry login ghcr.io --username <user> --password-stdin` |
| 任意机器 | 看本地版本 | `luma version` |
| 任意机器 | 检查认证、远端 Control 与节点健康 | `luma doctor` |

每台机器都可以用同一个安装器。区别在于后续命令：

- manager 跑 `bootstrap manager` 和 `update`。
- worker/home 节点跑 `node join`、`node exit`。
- client 只跑 `login`、`deploy`、`secret`、`registry`、`context`。

## 添加节点

Bootstrap 输出中会给出节点加入 Token。每台新服务器都在该服务器本机执行：

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

`--name` 是 Luma 节点名，会出现在 `luma status` 中，也用于服务 manifest 的 `node` 字段。它会写进 Nomad client 的 `meta.luma_node_name`，固定节点调度时使用它，避免 Docker hostname 重名导致串机器。
Nomad 节点身份是稳定的 UUID，某台机器执行过 `luma node exit` 后用同一个 Luma 节点名重新 join，`meta.luma_node_name` 不变，固定节点服务约束仍然有效。不要依赖 Docker hostname 做固定节点调度。

`--region` 是调度边界，会写进 `meta.region`。服务 manifest 中的 `region` 匹配它：

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

macOS home 节点需要提前安装并启动 Docker Desktop（或 OrbStack）和 Tailscale。执行 `luma node join --region home ...` 时，如果本机还没有连上 Tailscale，CLI 会把 `TAILSCALE_AUTHKEY` 当作必填项先询问，再注册节点并把本机作为 Nomad client 加入。非 apt Linux 发行版需要提前手动安装 Docker。加 `--engine nomad` 可以显式指定 Nomad client 路径，这也是默认值。

移除节点前，先在该节点本机执行：

```bash
luma node exit
```

默认会 drain 本机 Nomad client 并清理 `/opt/luma` 下的本地 Luma runtime 状态，但保留 Tailscale 和 Docker image/volume cache。需要同时从控制面注销节点名时加 `--endpoint <control-url> --token <management-token-or-node-join-token>`；需要同时退出 Tailscale 时加 `--tailscale`；明确想清理未使用 Docker 缓存和 volume 时才加 `--prune-docker`。

如果只是清理控制面中残留的 registered-only 记录，可以在已登录 client 上运行 `luma node remove <name>`。manager 会删除 Luma 注册并 drain 对应的 Nomad client；manager（Nomad server）节点受保护。

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

Luma 会自动把 egress 代理挂到该服务，并注入 `HTTP_PROXY` / `HTTPS_PROXY`。这只影响容器运行时出站请求，不等同于镜像拉取代理。

私有镜像不要把 registry token 写进 manifest。先在任意已登录 client 上保存仓库凭证：

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
```

之后 manifest 仍然只写镜像名，例如 `image: ghcr.io/acme/private-api:1.0.0`。部署时 Luma 会按 image 推断 registry host，把 registry auth 注入 Nomad jobspec 的 docker `config.auth` 块，让被调度到的节点能拉取镜像。这适合 GitHub Actions 构建出来的私有 GHCR 镜像；同一个仓库即使还用 GitHub Pages 发布文档或营销页，也不需要把 GHCR token 写到 Luma manifest 里。

私有镜像拉取和运行时 `proxy: true` 是两条路径。如果 Docker daemon 配了全局代理，而私有 registry 在认证前就 EOF/timeout，先看 `docker info` 的 HTTPProxy/HTTPSProxy/NO_PROXY，并确保私有 registry host 在 Docker daemon 的 `NO_PROXY` 里；`curl https://<registry>/v2/` 返回 `401` 通常说明 registry 本身可达，下一步应查 Docker daemon 的代理绕过。

### 管理 Luma Registry 镜像

Dashboard 的「Registry」页会按 manifest digest 展示仓库、全部 tag、平台、逻辑体积、创建时间、宿主磁盘占用和近月 blob 写入趋势。Control 会联合检查当前部署、Compose、Builder 结果、LAE runtime、运行中的 agent task，以及 Nomad 保留的每一个 job version；引用扫描不完整时自动清理和 GC 会暂停，但操作者接受页面列出的引用风险后，仍可明确删除任意选中的 manifest。

进入 Registry 页面只读取上一次持久化快照，不会同步触发全量扫描。「重新扫描」、删除执行、GC 和 Enforce 自动策略会刷新快照；全新安装尚无快照时，首次扫描会在后台建立。

Dashboard 的「删除并回收」会在一次 Registry 停机窗口内删除选中的 manifest 并回收其 blob，磁盘空间立刻释放。这条路径不留队列记录、没有宽限期、也不做 manifest 备份，**删除后不可恢复**。返回结果会报告回收了多少 blob；如果 blob 被回收但磁盘占用没有下降（这些镜像的 layer 全部与保留镜像共享），会明确说明。需要可取消窗口与 tag 恢复时，用下面的 CLI 队列。

Control 在停止 Registry 之前会先记录原 Nomad job，所以即使 Control 在维护窗口中崩溃，下一次 Control 启动或自动化 tick 也会把 Registry 拉回来——即便保留策略自动化被关闭。

CLI 提供排队式的删除流程：

```bash
luma registry images --refresh
luma registry delete acme/api sha256:<digest>
luma registry deletion <deletion-id> cancel
luma registry deletion <deletion-id> restore
luma registry gc                 # 只做离线预检
luma registry gc --execute       # 恢复窗口结束后真正回收
luma registry policy --mode recommend --keep-last 20 --max-age-days 30
```

删除按 digest 排队；同一 digest 的所有 tag 会一起处理。多架构 index 只会带上未被其他保留 index 共享的平台 manifest。默认 `recommend` 只标记候选，不自动删除；显式切换到 `enforce` 后才会按队列宽限期执行删除，并在恢复窗口结束后离线 GC。Registry 在删除和 GC 期间会短暂停止，Control 无论维护成功或失败都会重新注册原 Nomad job。GC 前可从 Dashboard 或 CLI 恢复原 tag；GC 完成后不可恢复。

敏感值不要直接写进 manifest。如果项目已经有 `.env`，部署时直接传入：

```bash
luma deploy app.yaml --env .env
```

Luma 只会导入 manifest 里实际引用的变量，并按应用名隔离保存；两个服务都叫 `DATABASE_URL` 也不会互相覆盖。YAML 里照常引用：

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
```

也可以手动管理 scoped secret：

```bash
luma secret set DATABASE_URL --scope app
```

完整字段参考见 [docs/deployment-yaml.md](docs/deployment-yaml.md)，示例见 [examples](examples)。

## 常见任务

| 问题 | 做法 |
| --- | --- |
| 更新整个 Luma 集群 | 首选 Dashboard 的「节点 → 升级中心」：填写不可变 release tag，先记录全部公网路由基线。二次确认后由 Builder 先把 Control 镜像缓存到内网 registry 并校验，再启动 manager 更新；页面会自动重连并复查路由。随后只更新版本未对齐的非 manager 节点。镜像、Control 和逐节点结果都会持久化，关页或 Control 重启后仍可查看与重试。 |
| 命令行升级兜底 | 只有 Dashboard 不可用或首次接入不支持 `luma-update` 的历史 agent 时，才在 manager 使用 `luma update manager`、在 client 使用 `luma update fleet`。正常日常升级无需 SSH 到节点。 |
| 查看整个集群状态 | 任意已登录 client 运行 `luma status`，会输出控制面、DNS、编排器（Nomad）及其 leader、注册节点（role=client）。 |
| 在 client 或 worker 上运行 `luma update` 会怎样 | 只更新本地 CLI，不刷新 manager 控制面。 |
| `luma update` 什么时候需要 `--domain` | 只有 Manager Control 状态缺失（SQLite 与可导入的旧 JSON 均不存在），或你确实要切换控制面域名时。 |
| manager 公网 IP 变化 | 在 manager 先运行 `luma manager ip-change --old <旧IP> --new <新IP> --domain <控制面域名> --dry-run`，确认计划后去掉 `--dry-run`。命令只更新明确的 manager/DNS 字段和精确匹配旧 IP 的 Cloudflare A 记录，并复用当前 control 镜像完成恢复。 |
| 服务 A 从一个 region 迁到另一个 region | 改 manifest 的 `region`，必要时同步修改 `exposure`，然后重新 `luma deploy app.yaml`。 |
| 服务 A 固定到某个节点 | 把 manifest 的 `node` 设为 `luma node join --name` 使用的 Luma 节点名，保留匹配的 `region`，然后重新 deploy。控制面会渲染成 Nomad 的节点身份约束。 |
| 回滚服务 A | 运行 `luma history app` 和 `luma rollback app`（或 `--to-version <N>`），也可以在控制台的「应用 -> 版本」里操作。回滚是 Nomad job 版本的运行态回退；生产回滚请使用固定镜像 tag/digest。 |
| 节点重新 join | 保持同一个 Luma 节点名，在该节点重新 `luma node join` / `luma update`。Nomad 节点 UUID 稳定，固定节点服务仍然有效。 |
| 下掉服务 A | 运行 `luma service remove app`。它会删除 DNS、Nomad job 和生成的 route 文件；用 `--dry-run` 预览，或用 `--skip-dns` 保留 DNS。 |
| 服务从公开变内部 | 把 `exposure` 改为 `none`，移除不再需要的 `domain`/公开入口配置，重新 deploy。 |
| 服务从内部变公开 | 设置匹配的 `region` + `exposure`，补 `domain` 和 `port`，重新 deploy。 |
| 部署私有 GHCR 镜像 | 先用 `luma registry login ghcr.io --username <user> --password-stdin` 保存凭证，再部署普通 manifest。 |
| 新增国内 worker | 在新机器执行 `luma node join ... --region cn --name ...`。 |
| 新增海外 worker | 在新机器执行 `luma node join ... --region global --name ...`。 |
| 新增家里节点 | 先准备 Docker Desktop/Tailscale，再执行 `luma node join ... --region home --name ...`。如果未连接 Tailscale，CLI 会要求输入 `TAILSCALE_AUTHKEY`。 |
| manager 只有 2c2g | 给业务 manifest 设置 `resources.limits` 和 `resources.reservations`，避免业务服务挤占控制面。 |
| Tailscale bootstrap/join 时没连上 | 在对应机器运行 `luma tailscale connect`；该命令会要求输入 `TAILSCALE_AUTHKEY`。 |
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
| [docs/observability.md](docs/observability.md) | 指标、日志、告警规则、飞书通知和独立探测边界。 |
| [docs/control-storage.md](docs/control-storage.md) | 单 Manager SQLite、历史查询、容量治理、备份和恢复。 |
| [docs/luma-cli-reference.md](docs/luma-cli-reference.md) | 从当前 argparse 自动生成的命令与参数参考。 |
| [docs/secrets.md](docs/secrets.md) | secret 和环境变量处理。 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 常见失败和修复。 |
| [docs/release.md](docs/release.md) | 发布 tag、安装器和 control image 的流程。 |
| [docs/agent-skill.md](docs/agent-skill.md) | Luma / LAE Agent Skill 的安装与使用。 |
| [docs/lae/README.md](docs/lae/README.md) | LAE 产品设计、CLI/Skill 契约与 validation 状态。 |
| [docs/compose-storage.zh-CN.md](docs/compose-storage.zh-CN.md) | Docker Compose 部署与 NFS/本地共享存储的管理和迁移说明。 |

## Agent Skill

Luma 部署 YAML 用 [skills/luma-deployment-yaml](skills/luma-deployment-yaml)；租户侧 `lae` CLI 用 [lae/skills/lae-deploy](lae/skills/lae-deploy)。Claude/Cursor（`~/.claude/skills`）和 Codex（`~/.codex/skills`）的安装步骤见 [docs/agent-skill.md](docs/agent-skill.md)。

从本仓库 checkout 安装或更新 Luma 技能（LAE 独立维护）：

```bash
for dest in ~/.claude/skills ~/.codex/skills; do
  mkdir -p "$dest/luma-deployment-yaml"
  cp -R skills/luma-deployment-yaml/. "$dest/luma-deployment-yaml/"
done
```

## 安全边界

- 不要提交 API token、管理 Token、节点加入 Token 或代理订阅 URL。
- 不要把 registry token 写进 manifest 或容器环境变量。使用 `luma registry login`，凭证泄露时到 registry provider 侧轮换或吊销 token。
- client 机器不需要 SSH/Docker/Cloudflare/Nomad 凭据，尽量只分发管理 Token。
- 节点加入 Token 只给要加入集群或刷新已加入节点 agent 的服务器使用。
- 不要暴露节点 agent 凭据；它是 Luma 自动管理的内部本机凭据。
- 如果 token 或订阅 URL 已经贴进聊天、日志或 issue，发布前先轮换。

### 自动记住构建部署方式

Luma CLI 会在 `import`、`build local/retry`、`deploy` 和 `compose deploy` 执行构建、上传或部署之前，自动查询服务端保存的应用部署方式：

- **没有记录**：正常部署，成功后自动写入，不需要处理存量应用。
- **与记录一致**：直接继续。
- **与记录不同**：显示构建方式、分支、Builder、平台等差异，二次确认后才继续。交互式终端询问用户；AI/CI 等非交互调用先停止，用户同意后才可加 `--accept-workflow-change` 重试。

成功后才更新记录；失败和 dry-run 保留原来的成功记录。记录保存在 Luma Control，换 AI、电脑或仓库 checkout 后仍可查询。

```bash
luma workflow show 应用名
luma workflow list --format json
luma workflow run 应用名 --path /path/to/repo
luma workflow record 应用名 --note '固定使用 Builder 远程构建' -- import owner/repo --ref main --build-node builder
```

首次使用需升级 Control 和 CLI。完整行为与 CI/Agent 使用约定见 [CLI 文档](docs/luma-cli.md#automatic-builddeploy-workflow-checks)。
