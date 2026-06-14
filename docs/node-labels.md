# Node Labels

Nomad 用 client 的 `meta` 控制服务调度。所有生产 job 都应该显式声明 placement constraint，避免服务被调度到错误区域。

当前 Luma 的正常路径是通过 `luma node join --name <luma-node-name> --region <region>` 自动写入 client `meta`。Luma 会写入：

- `region=<cn|global|home>`：服务 `region` 调度使用。
- `luma_node_name=<luma-node-name>`：人类可读的 Luma 节点名，固定节点调度使用。
- `ingress`/`egress`：入口和出站网关角色标记。

服务 manifest 的 `node` 字段应使用 Luma 节点名，不要使用 Docker hostname。控制面部署时会把它渲染成 `constraint { attribute = "${meta.luma_node_name}"; value = ... }`。Nomad 节点身份是稳定的 UUID，节点离开集群后用同一个 Luma 节点名重新 join，`meta.luma_node_name` 不变，固定节点服务约束仍然有效。

## meta 写入方式

`meta` 在装机时写进 Nomad client 的 HCL 配置，由 `luma node join` 维护，不需要运行时手改：

```hcl
client {
  enabled = true
  meta {
    region         = "cn"
    luma_node_name = "cn-worker-1"
    ingress        = "true"
    egress         = "true"
  }
}
```

## meta 含义

### `region=cn`

国内主服务区域。用于公开 Web/API、数据库、Redis、Traefik、Luma Control 和核心业务服务。

### `region=global`

海外区域。用于 AI 网关、外网 API 调用服务、爬虫和 worker。调度按 region 选择区域；是否走 Luma egress proxy 由服务 manifest 的 `proxy: true` 决定。

### `region=home`

家庭服务器、NAS 或家里电脑。默认只运行备份、内部工具、低频任务和测试服务，不参与核心公网服务调度。

### `ingress=true`

公网入口节点。Traefik 应该部署到带有该标记的国内节点，负责接收备案域名流量。

### `egress=true`

内部网关标记，不是普通节点加入模型。`luma egress setup` 会在承载 egress gateway 的机器上维护这个标记，确保 `egress_mihomo` 调度到正确位置。业务服务声明 `proxy: true` 后会挂上 egress 代理并注入代理环境变量，但仍按服务 `region` 调度。

## 检查 meta

```bash
nomad node status -self
nomad node status <node-id>      # 查看 Meta 段
```

## 修改 meta

`meta` 由 `luma node join` 在装机时写入。需要调整时改 client 的 HCL 配置后重启 Nomad agent；正常情况下不应运行时手改，以免和控制面记录不一致。
