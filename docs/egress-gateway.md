# Egress Gateway

`egress-gateway` 是 Luma 的出站代理平面。

它和公网入口、控制面分开：

```text
公网数据面：用户 -> Cloudflare/DNS -> Traefik -> 服务
控制面：你 -> Tailscale -> SSH / Portainer / Docker
出站面：Docker / App -> egress-gateway -> 外网
```

它不承载用户入口流量，只解决国内节点访问外网的问题。

## 用途

- Docker 拉 Docker Hub / GHCR 镜像。
- apt、npm、pip、Go modules 等依赖下载。
- 选定 worker 访问 OpenAI、GitHub 或其他海外 API。
- 某些服务仍跑在 `region=cn`，但出站需要代理。

## Runtime

第一版使用 `metacubex/mihomo:latest`。

Stack 文件：

```text
stacks/core/egress-gateway/stack.yml
```

运行时配置文件放在 egress 节点：

```text
/opt/luma/egress-gateway/config.yaml
```

这个目录可能包含订阅转换后的节点信息、GeoIP 数据和运行缓存，不能提交到 Git。

订阅地址也不能提交到 Git。用环境变量传入：

```bash
export EGRESS_SUBSCRIPTION_URL='https://your-subscription-url'
sudo -E ./scripts/refresh-egress-config.sh
```

脚本会：

- 下载订阅；
- 兼容 YAML 或 base64 订阅内容；
- 把客户端订阅转换成服务端精简配置；
- 只保留可用 proxies，去掉外部 rule-provider 依赖；
- 强制设置 `mixed-port: 7890`、`allow-lan: true`、`bind-address: 0.0.0.0`；
- 使用 `MATCH,EGRESS` 规则，避免启动时依赖 GeoIP/ruleset 下载；
- 写入 `/opt/luma/egress-gateway/config.yaml`；
- 如果 `egress_mihomo` 已经存在，自动 force update 服务。

## 节点标签

给专门跑出站代理的节点打标签：

```bash
docker node update --label-add egress=true <node-name>
```

v0.1 建议只给一台节点打 `egress=true`。

## 网络

创建可复用 overlay network：

```bash
docker network create --driver=overlay --attachable egress
```

`egress-gateway` 会：

- 加入 `egress` overlay network；
- 在宿主机发布 `7890/tcp` 和 `7890/udp`；
- 供 Docker daemon 或选定业务容器使用。

## 部署

```bash
docker stack deploy -c stacks/core/egress-gateway/stack.yml egress
```

检查：

```bash
docker stack services egress
docker service logs -f egress_mihomo
```

## 给 Docker daemon 使用

如果 egress gateway 就跑在当前节点：

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:7890"
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

验证：

```bash
sudo systemctl show --property=Environment docker
docker pull traefik:v3.1
```

如果使用远程 egress gateway，把 `127.0.0.1` 换成内网/Tailscale 地址，并用安全组或防火墙限制来源。

## 给业务容器使用

只有需要出站代理的服务才加：

```yaml
env:
  HTTP_PROXY: http://egress_mihomo:7890
  HTTPS_PROXY: http://egress_mihomo:7890
  NO_PROXY: localhost,127.0.0.1,.svc,.local
networks:
  - egress
```

不要默认给所有服务加代理。

## 安全规则

- 不要把 `7890` 暴露给公网。
- 云安全组只允许自己的服务器访问。
- 订阅 URL 和生成的 `config.yaml` 不进 Git。
- 如果订阅泄漏，立即重置。
- 出站代理日志可能包含访问目标，按敏感日志处理。
