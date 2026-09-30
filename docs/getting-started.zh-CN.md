# 快速上手 {#getting-started}

本文带你从一台空的 Linux 服务器走到一个运行中的应用：安装 CLI、初始化一台 Manager、部署第一个工作负载，再加入更多节点。不需要任何 Luma 前置配置。

## 准备条件 {#what-you-need}

| 条件 | 用途 |
| --- | --- |
| 一台 Linux 服务器，推荐 Ubuntu 22.04+，起步 2 核 / 2 GB | 运行 Luma Control、Nomad 和 Traefik |
| 一个由 Cloudflare 托管 DNS 的域名 | Control API、控制台和应用域名 |
| 该域名的 Cloudflare API Token，权限为 **Zone Read** 和 **DNS Edit** | Luma 创建和更新 DNS 记录 |
| 公网开放 80 和 443 端口，以及一个用于 Let's Encrypt 的邮箱 | HTTPS 证书与入口 |
| 能访问存放 Luma Control 镜像的仓库（默认 GHCR） | Manager 从这里拉取 Control |

第一个工作负载**不需要** Tailscale、构建节点或私有镜像仓库。需要家庭节点、中继或源码构建时再添加。

## 1. 安装 CLI {#1-install-the-cli}

在 Manager 以及所有要用来部署的机器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma doctor --local
```

安装脚本需要 Python 3.9+ 以及 curl 或 wget。它把最新发布版安装到 `~/.local/share/luma`，并把 `luma` 放到 `~/.local/bin`；如果该目录还不在 `PATH` 中，请打开一个新的 shell。安装过程不会改动 Docker、Nomad 或防火墙。

其他安装方式：

```bash
# A specific release, a branch or a commit
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.2.1 sh

# CI runners and virtual environments
python -m pip install "luma-infra==0.2.1"
```

卸载 CLI 使用 `scripts/uninstall-luma.sh`（不会动 `/opt/luma` 下的服务器状态）；加 `--purge` 会同时删除本地保存的配置和登录信息。

## 2. 初始化 Manager {#2-bootstrap-the-manager}

**在 Manager 服务器上**执行：

```bash
luma bootstrap --domain luma.example.com
```

`luma bootstrap` 会询问缺少的配置并解释每一项：

- `CLOUDFLARE_API_TOKEN` 和 `TRAEFIK_ACME_EMAIL` 必填。
- 无法自动推断时，会询问 `LUMA_DNS_EDGE_TARGET`（DNS 记录指向的公网 IP）。
- `TAILSCALE_AUTHKEY` 和 `EGRESS_SUBSCRIPTION_URL` 可选，直接回车跳过。

回答会保存到 `~/.luma.config.json`（权限 0600），重跑时不再询问。也可以用环境变量提供，或写在当前目录的 `.env` 中；Luma 只会从 `./.env` 读取它自己的配置项。

如果 Manager 需要代理才能访问 GHCR（中国大陆服务器通常如此），请在初始化前设置 `EGRESS_SUBSCRIPTION_URL`；否则加 `--skip-egress`。

初始化会安装 Docker 和 Nomad server、写入节点元数据、配置防火墙、以 Nomad job 方式部署 Traefik 和 Luma Control，并在 `/opt/luma/control/control.sqlite3` 创建 Control 数据库。每一步都会打印 `[start]`、`[ok]` 或 `[fail]`，失败时附带 `Fix:` 提示。修复后可以直接重跑；也可以单独修复某一层：`luma manager egress`、`luma node tailscale`、`luma doctor`。

完成后会输出：

```text
Control URL: https://luma.example.com
Management token: ...
Node join token: ...
Dashboard: https://luma.example.com/dashboard/
```

两个令牌都要保密：

- **管理令牌**用于 CLI、CI 和控制台登录，拥有全部权限。对应的环境变量是 `LUMA_DEPLOY_TOKEN`。
- **节点加入令牌**只用于服务器加入集群。

节点 agent 的凭据由系统自动管理，你不需要接触。

如需使用自己的 Control 镜像，请先发布镜像，并在初始化前设置 `LUMA_CONTROL_IMAGE`。初始化和升级过程中，Luma 不会构建或复用陈旧的本地镜像。

## 3. 部署第一个工作负载 {#3-deploy-a-first-workload}

最快的验证方式是控制台：打开 Dashboard 地址，粘贴管理令牌，选择 **应用 → 创建应用 → hello-world 首装验证**。它会部署一个不需要 DNS 和镜像仓库的内部服务，用来证明调度正常。

在你的电脑上登录并部署一个公网服务：

```bash
luma login https://luma.example.com --token-stdin < token.txt
luma init --name status --image traefik/whoami:v1.10.3 --region cn --domain status.example.com --port 80
luma validate status.yaml
luma deploy status.yaml --dry-run
luma deploy status.yaml
luma app list
```

`luma init` 会生成一份带注释的 manifest，包含内存上限和健康检查，按需修改即可。`--dry-run` 会打印 Control 将要提交的 Nomad job。请把 `status.example.com` 换成你 Cloudflare 域名下的主机名。本地构建、仓库导入和 Compose 应用见[部署应用](deploying.zh-CN.md)。

## 4. 加入更多节点 {#4-add-nodes}

使用节点加入令牌，**在每台新服务器上**执行：

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name sg-1
```

- `--region` 决定哪些服务可以运行在这里：`cn`、`global`、`home`，或通过 `luma region create` 创建的区域。
- `--name` 是节点名，manifest 用 `node:` 固定服务时填写它。

家庭节点和其他私网节点需要 Tailscale：先设置 `TAILSCALE_AUTHKEY` 或执行 `luma node tailscale`。macOS 节点还需要运行中的 Docker（Docker Desktop 或 OrbStack）。

`luma node list` 显示已注册节点及其 agent 状态。`luma node exit` 会停止本机的 Nomad 并删除本机的 Luma 状态；加上 `--endpoint` 和 `--token` 会同时从集群注销。

### 所需端口 {#required-ports}

Luma 会在 Linux 节点上配置 UFW。云安全组和 Tailscale ACL 请保持一致：

| 端口 | 方向 | 用途 |
| --- | --- | --- |
| `80/tcp`、`443/tcp` | 公网 → Manager | HTTP(S) 入口和 Let's Encrypt |
| `tcp-relay` 端口 | 公网 → Manager | 公网 TCP 中继，例如 `3306/tcp` |
| `4646/tcp` | 客户端和 Traefik → Nomad server | Nomad HTTP API |
| `4647/tcp` | Nomad client → server | Nomad RPC |
| `4648/tcp`、`4648/udp` | 所有 Nomad agent 之间 | Nomad gossip |

Manager 有 Tailscale 地址时，Luma 只在 `tailscale0` 上开放 Nomad 端口。出站代理端口 `7890` 永远不会对公网开放。

## 5. 保持更新 {#5-keep-luma-up-to-date}

```bash
luma update            # on the manager: CLI and Control; on a node: CLI and agent; elsewhere: CLI
luma update fleet      # from any client: every node with a ready agent (the manager is skipped)
```

未指定 `--install-ref` 时都会安装最新发布版。控制台的 **节点 → 升级中心** 提供带进度和路由检查的同等操作，是升级 Manager 的首选方式。`luma update` 不会重启 Docker 或 Nomad，也不会重新部署应用。

## 6. 检查健康状态 {#6-check-health}

```bash
luma status
luma doctor
luma doctor --deep
```

`doctor` 会检查登录、Control、DNS 配置、节点 agent 和 Nomad；`--deep` 还会评估每个节点上报的 Docker 和 Nomad 诊断信息。

## 下一步 {#next-steps}

- [核心概念](concepts.zh-CN.md)：区域、暴露方式和出站代理。
- [部署应用](deploying.zh-CN.md)：镜像、本地构建、仓库导入、Compose 与 CI。
- [日常运维](operations.zh-CN.md)：日志、回滚、重启、删除和节点维护。
- [故障排查](troubleshooting.zh-CN.md)：某一步失败时怎么办。
