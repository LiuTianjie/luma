# 核心概念 {#concepts}

Luma 对外提供五个核心概念。

## 节点（Node） {#node}

在本机运行 Luma 的服务器，角色可以是 Manager 或工作节点。新安装不要求客户端通过 SSH 进入节点；每台服务器在自身执行 `luma bootstrap manager` 或 `luma node join`。

```yaml
nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles: [nomad-server, edge, egress]
```

## 区域（Region） {#region}

区域决定服务运行的位置。每个节点只属于一个区域；`replicas` 会分布在该区域的就绪节点上。只有服务必须固定在某台机器时，才设置 `node`。

内置区域也定义了入口拓扑与默认出网方式：

- `cn`：中国大陆公共服务和核心工作负载。加入节点和拉取镜像时使用 Manager 出网代理，允许 `cn-edge`。
- `global`：海外或外网工作节点与服务。直连出网，允许 `external-edge`。
- `home`：家庭或私网节点。加入节点和拉取镜像时使用 Manager 出网代理，允许 `tailscale-relay`。

使用 `luma region create <name>`（或节点页）创建更多调度池。自定义区域默认使用 `exposure: none`，并显式设置 `egress: proxy|direct`。通过 `--region <name>` 加入节点，部署时使用相同的 `region` 和所需副本数。

## 入口（Exposure） {#exposure}

入口决定公共流量如何到达服务：

- `cn-edge`：Cloudflare DNS → 中国大陆 Traefik → 中国大陆服务。
- `tailscale-relay`：Cloudflare DNS → 中国大陆 Traefik → Tailscale → 家庭服务。
- `cloudflare-tunnel`：Cloudflare Tunnel → 私网服务。
- `external-edge`：Cloudflare DNS → global 边缘节点 → global 服务。
- `none`：不提供公共入口。

## 出网（Egress） {#egress}

用于镜像拉取、依赖下载和指定服务的出站代理。

它不是公共流量入口。

镜像拉取使用 `luma egress setup` 配置的 Docker daemon 代理。服务运行时代理需要显式启用：在服务清单设置 `proxy: true`。Luma 会把出网代理附加到服务，并注入默认 `HTTP_PROXY` / `HTTPS_PROXY`。调度仍遵循服务的 `region`。

## 服务（Service） {#service}

服务通过简短的 YAML 清单描述，Luma 将其转换为 Nomad job：

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

只有服务必须运行在特定机器时，才设置 `node: <luma-node-name>`。该值是 `luma node join --name` 指定的名称。Luma 仍会添加 `region` 放置约束，再把节点名渲染为 `${node.unique.name}`（或 `meta.luma_node_name`）上的 Nomad 约束。