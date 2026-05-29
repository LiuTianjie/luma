# Node Labels

Docker Swarm 使用节点标签控制服务调度。所有生产 stack 都应该显式声明 placement constraints，避免服务被调度到错误区域。

## 推荐标签

```bash
docker node update --label-add region=cn cn-manager-1
docker node update --label-add region=cn cn-worker-1
docker node update --label-add region=global global-worker-1
docker node update --label-add region=home home-1
docker node update --label-add ingress=true cn-manager-1
docker node update --label-add external_net=true global-worker-1
docker node update --label-add egress=true cn-manager-1
```

## 标签含义

### `region=cn`

国内主服务区域。用于公开 Web/API、数据库、Redis、Traefik、Portainer 和核心业务服务。

### `region=global`

海外或可访问外网的能力区域。用于 AI 网关、外网 API 调用服务、爬虫、代理和 worker。

### `region=home`

家庭服务器、NAS 或家里电脑。默认只运行备份、内部工具、低频任务和测试服务，不参与核心公网服务调度。

### `ingress=true`

公网入口节点。Traefik 应该部署到带有该标签的国内节点，负责接收备案域名流量。

### `external_net=true`

具备稳定访问外网能力的节点。需要访问 OpenAI、GitHub 或其他海外资源的 worker 可以同时约束 `region=global` 和 `external_net=true`。

### `egress=true`

出站代理网关节点。`stacks/core/egress-gateway/stack.yml` 会调度到带有该标签的节点，用于 Docker 拉镜像、拉依赖和选定服务访问外网。

## 检查标签

```bash
docker node inspect <node-name> --format '{{ json .Spec.Labels }}'
```

## 修改标签

添加标签：

```bash
docker node update --label-add region=global global-worker-1
```

删除标签：

```bash
docker node update --label-rm region global-worker-1
```
