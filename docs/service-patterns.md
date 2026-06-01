# Service Patterns

本仓库固定五种 exposure 模式。新增服务时优先复制 `examples/` 中的 service manifest，再修改镜像、域名、端口、region、exposure 和 replicas。只有当服务必须固定在某台机器上时才加 `node`，它会追加 `node.hostname == <hostname>`，但不会替代 `region`。

## 1. cn-edge

国内公开服务。

- 有公网域名。
- 接入国内 Traefik。
- 跑在 `region=cn`。
- 适合主 Web/API 和核心业务服务。
- 对应 `exposure: cn-edge`。

典型约束：

```yaml
placement:
  constraints:
    - node.labels.region == cn
```

公开域名通过 Traefik labels 声明。

## 2. tailscale-relay

家里服务通过国内入口和 Tailscale 暴露。

- 有公网域名。
- 用户访问备案域名。
- 国内 Traefik 作为入口。
- Traefik 通过 Tailscale 访问 home 节点。
- 适合低频工具、预览环境、家里管理面板。
- 不适合核心高频 API、大文件下载、登录或支付。
- 对应 `exposure: tailscale-relay`。

Luma 会生成 `routes/<service>.yml`，由 Traefik file provider 加载。

## 3. cloudflare-tunnel

家里或私有服务通过 Cloudflare Tunnel 暴露。

- 有公网域名。
- 不经过国内 Traefik。
- 不经过 Tailscale 数据面。
- 适合家里无公网 IP 或希望 Cloudflare 直接接入的工具服务。
- 对应 `exposure: cloudflare-tunnel`。

第一版 Luma 会生成 app + `cloudflared` stack。Cloudflare Tunnel public hostname 仍需要在 Cloudflare 侧配置。

## 4. external-edge

海外公开服务。

- 有公网域名。
- 服务容器跑在 `region=global`。
- 入口是海外/global edge。
- 适合 AI 网关、外网代理、低频外网服务。
- 不适合核心高频 API。
- 对应 `exposure: external-edge`。

这个模式会引入海外链路。只有当服务必须访问外网，且请求频率或延迟要求可接受时才使用。

## 5. none / global worker

内部服务或海外 worker。

- 无公网域名。
- 跑在 `region=global`。
- 默认只按 `region=global` 调度。
- 如果运行时必须走 Luma egress proxy，在服务 manifest 声明 `proxy: true`；不要手写默认 egress network 和代理 env。
- 通过队列消费任务。
- 访问外网后写回结果。
- 对应 `exposure: none`。

推荐链路：

```text
cn Web/API -> Queue -> global worker -> 外网 API -> 写回结果
```

这种模式比国内 API 实时调用海外 HTTP 更稳，也更容易重试、限流和降级。

## home-internal-service

家庭节点内部服务。

- 跑在 `region=home`。
- 非核心。
- 默认不暴露公网域名。
- 优先通过 Tailscale 或内网访问。
- 对应 `exposure: none`。

适合备份、低频任务、内部工具和测试服务。不要把核心公网服务调度到 home。

如果它依赖某台 home 节点的本地磁盘或硬件，可以钉到具体 Swarm hostname：

```yaml
name: home-db
image: postgres:16
region: home
node: orbstack
exposure: none
```

用 `luma status` 查看真实 hostname；不要使用 display name。
