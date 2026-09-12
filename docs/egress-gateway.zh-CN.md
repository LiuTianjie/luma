# 出网网关 {#egress-gateway}

Egress Gateway 是 Luma 的出站代理层。

它解决两个实际问题：

- 中国大陆服务器可能无法直连拉取新镜像或依赖；
- 某些服务需要访问外网，同时公共入口仍留在中国大陆。

出网不是入站路径。用户流量仍由选择的 `exposure` 模式接入。

## 设置 {#setup}

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup
```

Luma 会：

- 下载订阅；
- 将 YAML 或 base64 订阅转换成最小 Mihomo 配置；
- 以 `600` 权限写入 `/opt/luma/egress-gateway/config.yaml`；
- 配置稳定系统 DNS，保障镜像仓库和初始化可靠性；
- 首次初始化出网时临时禁用 Docker daemon 代理；
- 使用内置出网镜像部署核心 `egress-mihomo` Nomad job；
- 将网关主机 Nomad client meta 标为 `egress=true`，供内部 `egress_mihomo` 服务使用；
- 安装适配 Docker 的公网端口防护；
- 将 Docker daemon 代理配置为 `127.0.0.1:7890`；
- 重启 Docker。

默认出网镜像来自已验证可用于首次初始化的中国大陆镜像源：

```yaml
defaults:
  images:
    egressGateway: docker.1panel.live/metacubex/mihomo:latest
```

多数用户无需配置镜像。自行维护镜像源时，可在 `luma.yaml` 覆盖 `defaults.images.egressGateway`。

后续刷新：

```bash
luma egress refresh
```

## 运行方式 {#runtime}

网关监听：

```text
127.0.0.1:7890
```

本地 Docker 和显式设置 `proxy: true` 的服务可以访问出网代理。Luma 安装主机防火墙、raw `PREROUTING` 和 Docker `DOCKER-USER` 防护，禁止 Docker 发布的 `7890/tcp` 和 `7890/udp` 从默认公网接口进入，同时允许本地 Docker 和内部出网流量使用。

Docker daemon 代理：

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

## 验证 {#verify}

```bash
nomad job status egress-mihomo
sudo docker pull hello-world:latest
```

服务需要运行时代理时，在清单声明 `proxy: true`：

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
```

Luma 自动注入 `HTTP_PROXY=http://egress_mihomo:7890` 和 `HTTPS_PROXY=http://egress_mihomo:7890`，调度仍按 `region`。清单已显式设置对应变量时，保留原值。

## 安全 {#security}

- 不要提交 `EGRESS_SUBSCRIPTION_URL`。
- 订阅 URL 出现在聊天、日志或截图中时应轮换。
- 公网接口保持禁止 `7890` 入站。初始化和出网设置会安装防护；手动重置防火墙后可重跑 `luma egress setup`。内置出网服务有保守的 Nomad 资源限制，为 2 核 2 GiB Manager 的控制面及应用保留余量。
- 优先使用一个网关，只有调度或吞吐需要时再增加。