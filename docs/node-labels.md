# Node Labels

Docker Swarm 使用节点标签控制服务调度。所有生产 stack 都应该显式声明 placement constraints，避免服务被调度到错误区域。

## 推荐标签

```bash
docker node update --label-add region=cn cn-manager-1
docker node update --label-add region=cn cn-worker-1
docker node update --label-add region=global global-sg-1
docker node update --label-add region=home home-1
docker node update --label-add ingress=true cn-manager-1
```

## 标签含义

### `region=cn`

国内主服务区域。用于公开 Web/API、数据库、Redis、Traefik、Portainer 和核心业务服务。

### `region=global`

海外区域。用于 AI 网关、外网 API 调用服务、爬虫和 worker。调度按 region 选择区域；是否走 Luma egress proxy 由服务 manifest 的 `proxy: true` 决定。

### `region=home`

家庭服务器、NAS 或家里电脑。默认只运行备份、内部工具、低频任务和测试服务，不参与核心公网服务调度。

### `ingress=true`

公网入口节点。Traefik 应该部署到带有该标签的国内节点，负责接收备案域名流量。

### `egress=true`

内部网关标签，不是普通节点加入模型。`luma egress setup` 会在承载 egress gateway 的机器上维护这个标签，确保 `egress_mihomo` 调度到正确位置。业务服务声明 `proxy: true` 后会加入 `egress` 网络并注入代理环境变量，但仍按服务 `region` 调度。

如果老版本曾经在普通 worker 上执行过包含 `--egress` 的 join 命令，它可能仍残留 `egress=true`。升级到 region-first 模型后，普通业务服务不再需要这个标签。只在你确认该机器不应该承载内部 egress gateway 时移除：

```bash
docker node update --label-rm egress <node-name>
```

## 检查标签

```bash
docker node inspect <node-name> --format '{{ json .Spec.Labels }}'
```

## 修改标签

添加标签：

```bash
docker node update --label-add region=global global-sg-1
```

删除标签：

```bash
docker node update --label-rm region global-sg-1
```
