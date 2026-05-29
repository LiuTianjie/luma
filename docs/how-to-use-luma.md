# Luma 使用手册

这份文档说明日常怎么用 Luma 部署服务，以及 Cloudflare、Tailscale、Traefik 分别承担什么角色。

## 1. 核心原则

Luma 不让所有业务请求默认走 Tailscale。

默认分工是：

```text
Cloudflare: DNS / 可选代理 / 可选 Tunnel
Traefik: 国内主公网入口
Tailscale: 控制面网络 + 显式 tailscale-relay
Portainer: 部署控制台
Docker: 服务运行环境
```

请求是否经过 Tailscale，由服务 manifest 里的 `exposure` 决定。

## 2. 安装 CLI

在仓库根目录执行：

```bash
cd ~/infra-stacks
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

验证：

```bash
luma --help
```

## 3. 配置平台

编辑 `luma.yaml`。

关键字段：

```yaml
defaults:
  stackRoot: stacks
  routesRoot: routes
  publicNetwork: public
  entrypoint: websecure
  certResolver: letsencrypt

dns:
  provider: cloudflare
  edgeTarget: 203.0.113.10
  apiTokenEnv: CLOUDFLARE_API_TOKEN
  zoneIdEnv: CLOUDFLARE_ZONE_ID

portainer:
  webhookUrlEnv: PORTAINER_WEBHOOK_URL
```

真实使用前设置环境变量：

```bash
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ZONE_ID=...
export PORTAINER_WEBHOOK_URL=...
```

如果只是本地生成 stack，不需要这些环境变量：

```bash
luma deploy examples/public-cn-service.yaml --skip-dns --skip-webhook
```

## 4. 选择 exposure

### 国内主服务：`cn-edge`

链路：

```text
用户 -> Cloudflare DNS -> 国内 Traefik -> cn 服务
```

适合主站、核心 API、管理后台。

```yaml
name: app
image: ghcr.io/your-org/app:latest
region: cn
public: true
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

部署：

```bash
luma deploy app.yaml --commit --push
```

### Home 服务走 Tailscale：`tailscale-relay`

链路：

```text
用户 -> Cloudflare DNS -> 国内 Traefik -> Tailscale -> home 服务
```

适合低频工具、预览环境、家里管理面板。

```yaml
name: home-panel
image: ghcr.io/your-org/home-panel:latest
region: home
public: true
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
relay:
  host: home-1.your-tailnet.ts.net
```

Luma 会生成两个文件：

```text
stacks/home/home-panel/stack.yml
routes/home-panel.yml
```

要求：

- 国内 Traefik 节点挂载 `/opt/luma/routes`。
- 仓库 `routes/` 内容要同步到国内 Traefik 节点 `/opt/luma/routes`。
- home 节点防火墙只允许 Tailscale 网络访问 `publishPort`。

部署：

```bash
luma deploy home-panel.yaml --commit --push
```

### Home 服务走 Cloudflare Tunnel：`cloudflare-tunnel`

链路：

```text
用户 -> Cloudflare -> cloudflared -> home 服务
```

适合没有公网 IP 的 home 服务，或者你不希望经过国内 Traefik 的工具服务。

```yaml
name: home-tool
image: ghcr.io/your-org/home-tool:latest
region: home
public: true
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

部署前设置：

```bash
export CLOUDFLARE_TUNNEL_TOKEN=...
```

第一版 Luma 会生成 app + `cloudflared` stack。Cloudflare Tunnel public hostname 仍在 Cloudflare 控制台或后续 provider 自动化里配置。

### 海外公开能力：`external-edge`

链路：

```text
用户 -> Cloudflare DNS -> 海外/global edge -> global 服务
```

适合 AI 网关、外网代理、必须在海外执行的低频公开服务。

```yaml
name: ai-gateway
image: ghcr.io/your-org/ai-gateway:latest
region: global
public: true
exposure: external-edge
domain: ai.example.com
port: 3000
replicas: 1
dns:
  target: 198.51.100.10
```

### 内部服务 / worker：`none`

无公网入口。

```yaml
name: fetch-worker
image: ghcr.io/your-org/fetch-worker:latest
region: global
public: false
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
```

## 5. 日常命令

预览生成结果：

```bash
luma deploy service.yaml --dry-run
```

只生成 stack，不同步 DNS，不触发 Portainer：

```bash
luma deploy service.yaml --skip-dns --skip-webhook
```

真实发布：

```bash
luma deploy service.yaml --commit --push
```

单独同步 DNS：

```bash
luma dns-sync service.yaml
```

全仓库校验：

```bash
python -m unittest discover -s tests
./scripts/validate-stacks.sh
```

## 6. 第一次上线建议

按这个顺序验证：

1. 用 `examples/public-cn-service.yaml` 跑通 `cn-edge`。
2. 用 `examples/global-worker.yaml` 验证 `region=global` 调度。
3. 用 `examples/home-tailscale-relay.yaml` 验证 home 经 Tailscale relay。
4. 再考虑 `cloudflare-tunnel`。

不要第一步就上核心业务。先用 `traefik/whoami` 或简单测试镜像验证整条链路。

## 7. 安全边界

- Portainer 不直接暴露公网。
- Tailscale 默认只做控制面。
- `tailscale-relay` 只用于低频 home 服务。
- 主业务走 `cn-edge`。
- 需要外网能力但不需要同步响应的任务优先做 worker，用队列连接。
- Cloudflare token、Portainer webhook、Tunnel token 都只放环境变量，不提交到 Git。
