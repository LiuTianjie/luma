# 初始化配置预设 {#profiles}

Profile 是 Manager 初始化预设。普通工作节点加入不使用 profile，应在各节点运行 `luma node join ... --region <cn|global|home|custom> --name <node-name>`。自定义区域用 `luma region create` 创建。

## `single-node` {#single-node}

在一台公共服务器运行：

- Docker
- Nomad server（同时作为 `region=cn` 的 client）
- Traefik
- Luma Control API
- 出网网关

用法：

```bash
luma bootstrap --domain luma.example.com --profile single-node
```

## `cn-edge` {#cn-edge}

中国大陆公共边缘节点（角色：`nomad-manager`、`edge`；标签：`region=cn`、`ingress=true`）：

- Docker
- Nomad server
- Traefik
- Luma Control API（不含出网网关）

## `egress-gateway` {#egress-gateway}

面向镜像拉取与 `proxy: true` 服务的出站代理网关（角色：`egress`；标签：`egress=true`）。

## `home-node` {#home-node}

用于内部工具、`tailscale-relay` 或 `cloudflare-tunnel` 服务的家庭/私网节点（角色：`home`；标签：`region=home`）。

## `global-worker` {#global-worker}

海外/直连外网工作节点（角色：`global-worker`；标签：`region=global`）。

## 工作节点区域 {#worker-regions}

工作节点采用区域优先的加入方式：

```bash
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
```

- `region` 是调度边界，写入 Nomad client 的 `meta.region`。
- `name` 是状态输出和服务清单使用的 Luma 节点名，固定调度使用 `meta.luma_node_name`。
- 需要代理的服务声明 `proxy: true`。