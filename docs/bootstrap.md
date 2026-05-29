# Bootstrap

从零启动这套部署控制面时，按下面顺序执行。

## 1. 安装 Docker

在所有服务器安装 Docker Engine，并确认当前用户或部署用户可以执行 Docker 命令。

```bash
docker version
```

## 2. 安装并登录 Tailscale

在所有服务器安装 Tailscale，并加入同一个 tailnet。

```bash
tailscale status
```

确认国内、海外、家庭节点之间可以通过 Tailscale IP 互相访问。

## 3. 初始化 Docker Swarm

在国内主入口服务器上初始化 Swarm。该节点通常也是第一台 manager。

```bash
docker swarm init --advertise-addr <cn-manager-tailscale-or-private-ip>
```

记录输出中的 worker join token。

## 4. 加入 worker 节点

在国内 worker、海外 worker、家庭节点上执行 join 命令。

```bash
docker swarm join --token <worker-token> <manager-ip>:2377
```

在 manager 上确认节点列表：

```bash
docker node ls
```

## 5. 创建 overlay network

创建供 Traefik 和公开服务共用的 external overlay network。

```bash
docker network create --driver=overlay --attachable public
```

## 6. 给节点打 labels

按 `docs/node-labels.md` 给节点打 region 和能力标签。

```bash
docker node update --label-add region=cn cn-manager-1
docker node update --label-add ingress=true cn-manager-1
```

## 7. 部署 Traefik

如果计划使用 `exposure: tailscale-relay`，先在国内入口节点准备 Traefik 动态路由目录：

```bash
sudo mkdir -p /opt/luma/routes
```

后续把仓库中的 `routes/*.yml` 同步到这个目录。Traefik stack 已经把 `/opt/luma/routes` 挂载为 file provider 动态配置目录。

部署 `stacks/core/traefik/stack.yml`。

```bash
docker stack deploy -c stacks/core/traefik/stack.yml traefik
```

确认 Traefik service 运行在 `ingress=true` 的国内节点。

## 8. 部署 Portainer

部署 `stacks/core/portainer/stack.yml`。

```bash
docker stack deploy -c stacks/core/portainer/stack.yml portainer
```

Portainer 管理端口不要直接暴露公网。推荐通过 Tailscale IP 或内网访问。

## 9. 配置 Portainer

在 Portainer 中完成：

- 连接当前 Docker Swarm environment。
- 配置本 Git 仓库。
- 配置镜像 Registry 和凭据。
- 为后续 stack 开启 Git 部署或手动从 Git 选择 stack 文件。

## 10. 部署 whoami 验证服务

部署 `stacks/cn/whoami/stack.yml`，并把 `whoami.example.com` 替换成真实备案域名。

```bash
docker stack deploy -c stacks/cn/whoami/stack.yml whoami
```

验证内容：

- DNS 指向国内入口服务器。
- Traefik 自动发现 whoami service。
- HTTPS 证书签发成功。
- 请求命中国内节点上的 `traefik/whoami` 容器。

## 11. 验证 tailscale-relay

当需要让 home 服务走国内入口时：

1. 在 home 节点部署 `examples/home-tailscale-relay.yaml` 生成的 stack。
2. 确认 home 节点可以通过 Tailscale hostname 被国内入口节点访问。
3. 把 Luma 生成的 `routes/<service>.yml` 同步到国内入口节点 `/opt/luma/routes/`。
4. 确认 home 节点防火墙只允许来自 Tailscale 网络或国内入口 Tailscale IP 的 published port。
5. 访问对应域名，确认链路为 `Cloudflare DNS -> CN Traefik -> Tailscale -> home service`。
