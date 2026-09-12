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
luma bootstrap manager --domain luma.example.com --profile single-node
```

## `cn-edge` {#cn-edge}

中国大陆公共边缘节点：

- Docker
- Nomad server
- Traefik

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