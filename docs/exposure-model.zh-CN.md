# 入口模型 {#exposure-model}

Luma 将控制面和数据面分开。

## 各组件职责 {#roles}

- Cloudflare：DNS 自动化、可选代理和 Tunnel 公共主机名。
- Traefik：`cn` 边缘的公共 HTTP/HTTPS 入口，以及 `tcp-relay` 发布端口。
- Tailscale：管理网络，以及所选 `home` 服务的显式中继路径。
- Luma Control：驱动 Nomad HTTP API 的部署控制面。
- Nomad / Docker：运行时执行层。

Tailscale 不是默认业务数据面。只有服务明确选择 `exposure: tailscale-relay` 时才承载该中继数据流量。

## 入口模式 {#exposure-modes}

### `cn-edge` {#cn-edge}

主要的中国大陆公共服务。

```text
User -> Cloudflare DNS -> CN Traefik -> CN service
```

适用于：

- 主站；
- 公共 Web/API；
- 登录、支付、控制台及常规产品流量。

清单：

```yaml
name: app
image: ghcr.io/your-org/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

Luma 生成：

- `stacks/cn/app/app.nomad.json`；
- Nomad provider 使用的 Traefik 服务标签；
- 指向已配置中国大陆边缘目标的 Cloudflare DNS 记录。

默认通过约束为 `${meta.region}=cn` 且 `${meta.ingress}=true` 的单个 Traefik 实例接入。存在多个 CN 节点时，DNS 仍指向配置的边缘目标，由该处 Traefik 接收请求，并转发给 Nomad provider 发现的匹配 allocation。

### `tailscale-relay` {#tailscale-relay}

通过中国大陆边缘节点，经 Tailscale 暴露所选家庭服务。

```text
User -> Cloudflare DNS -> CN Traefik -> Tailscale -> home service
```

适用于：

- 低流量工具；
- 预览环境；
- 家庭管理面板；
- 可以接受依赖家庭网络的服务。

不适用于：

- 核心 API；
- 登录或支付；
- 大文件下载；
- 高频产品流量。

清单：

```yaml
name: home-panel
image: ghcr.io/your-org/home-panel:latest
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
relay:
  host: home-1.your-tailnet.ts.net
```

Luma 生成：

- `stacks/home/home-panel/home-panel.nomad.json`，在家庭节点发布服务端口；
- `routes/home-panel.yml`，由 Traefik file provider 加载；
- 指向已配置中国大陆边缘目标的 Cloudflare DNS 记录。

运维要求：

- 家庭主机防火墙仅允许从 Tailscale 接口或 CN 边缘 Tailscale IP 访问发布端口；
- CN Traefik 节点的 `/opt/luma/routes` 必须能读取 `routes/`。

### `tcp-relay` {#tcp-relay}

通过专用 Traefik TCP 入口提供原生公共 TCP 服务。

```text
Client -> Cloudflare DNS -> edge Traefik TCP entrypoint -> task host port
```

适用于：

- 必须从公网访问的 MySQL 或其他非 HTTP 协议；
- 一个公共端口对应一个后端服务的场景。

不适用于：

- 按主机名在同一个端口复用多个普通 MySQL 服务；
- 未配置数据库凭据、IP 白名单或防火墙控制就直接暴露公网。

清单：

```yaml
name: granary-db
image: mysql:8.4.9
region: home
node: lab
exposure: tcp-relay
domain: granary-db.itool.tech
port: 3306
publishPort: 3306
```

Luma 生成：

- `stacks/home/granary-db/granary-db.nomad.json`，以 host 模式发布服务端口；
- 更新 Traefik 服务，增加推导的 `tcp-3306` 入口和 host 模式发布端口；
- `routes/granary-db.yml`，使用 Traefik `tcp.routers` 与 `HostSNI("*")` / `HostSNI(\`*\`)`；
- 指向已配置边缘目标的 Cloudflare DNS 记录。

普通 MySQL 客户端不会以 HTTP Host 请求头开始通信，也不能假定在服务端握手前提供可靠 TLS SNI。`tcp-relay` 应视为端口独占：一个发布端口路由到一个 TCP 服务。

### `cloudflare-tunnel` {#cloudflare-tunnel}

直接通过 Cloudflare Tunnel 暴露家庭或私网服务。

```text
User -> Cloudflare -> cloudflared -> service
```

适用于：

- 没有公网 IP 的家庭服务；
- 希望由 Cloudflare 承接公共路径的低频工具；
- 不应经过 CN Traefik 节点的服务。

清单：

```yaml
name: home-tool
image: ghcr.io/your-org/home-tool:latest
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

Luma 生成：

- 包含应用的服务 stack；
- 使用 `${CLOUDFLARE_TUNNEL_TOKEN}` 的 `cloudflared` sidecar。

Tunnel 公共主机名仍在 Cloudflare 管理。此模式跳过普通 DNS A 记录同步。

### `external-edge` {#external-edge}

拥有独立海外/公共边缘入口的 global 服务。

```text
User -> Cloudflare DNS -> external/global edge -> global service
```

适用于：

- AI 网关；
- 海外 API 网关；
- 代理服务；
- 必须在 CN 网络之外执行的低频服务。

清单：

```yaml
name: ai-gateway
image: ghcr.io/your-org/ai-gateway:latest
region: global
exposure: external-edge
domain: ai.example.com
port: 3000
replicas: 1
dns:
  target: 198.51.100.10
```

Luma 生成：

- `stacks/global/ai-gateway/ai-gateway.nomad.json`；
- global 边缘环境使用的 Traefik 服务标签；
- 指向 `dns.target` 的 Cloudflare DNS 记录。

通过 `dns.target` 选择此服务的公共/global 边缘 IP。没有独立 global 边缘目标时，工作负载仍可调度到 `region: global`，但公共 HTTP 流量不会自动从所有 global 节点进入。

### `none` {#none}

内部服务与工作任务。

```text
No public route
```

适用于：

- 工作任务；
- 队列消费者；
- 内部 job；
- 备份任务。

清单：

```yaml
name: fetch-worker
image: ghcr.io/your-org/fetch-worker:latest
region: global
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
```

Luma 生成：

- 包含放置约束的 stack；
- 无 Traefik 标签；
- 无 DNS 记录。

## 选择规则 {#decision-rule}

- 默认公共产品流量：`cn-edge`。
- 通过中国大陆边缘访问家庭服务：`tailscale-relay`。
- 公共原生 TCP 服务：`tcp-relay`。
- 经 Cloudflare 访问家庭/私网服务：`cloudflare-tunnel`。
- 海外公共服务：`external-edge`。
- 工作任务和内部服务：`none`。