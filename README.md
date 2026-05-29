# infra-stacks

多服务器统一部署控制仓库，用 `Portainer + Docker Swarm + Traefik + Tailscale` 管理国内、海外和家庭节点上的服务。

## 技术栈

- Portainer：统一管理 Docker Swarm、Stack、Registry 和 Git 部署。
- Docker Swarm：提供多节点调度、overlay network、service replicas 和节点标签约束。
- Traefik：作为公开入口，通过 Swarm labels 自动绑定域名、HTTPS 和反向代理。
- Tailscale：打通国内、海外、家里服务器之间的私网通信。

暂时不使用 Kubernetes、k3s 或 Rancher。当前目标是轻量、可维护、能统一部署，而不是一开始就引入完整云原生平台。

## 日常使用姿势

1. 业务项目构建 Docker 镜像并推送到 Registry。
2. 在本仓库新增或修改对应的 `stack.yml`。
3. Portainer 从 Git 拉取并部署 stack。
4. Traefik 通过 labels 自动绑定域名和 HTTPS。
5. Docker Swarm 根据 node labels 把服务调度到 `cn`、`global` 或 `home`。

不在业务服务器上 build 镜像，不手动 SSH 登录服务器执行 `docker run`。

## 目录结构

```text
docs/       架构、启动、节点标签、服务模式和运维文档
stacks/     可由 Portainer 部署的真实 Swarm stacks
templates/  新服务可复制的 stack 模板
scripts/    本地校验脚本
```

核心 stack 放在 `stacks/core/`，示例业务服务按 region 放在 `stacks/cn/`、`stacks/global/` 和 `stacks/home/`。

## 服务类型选择规则

- 主 Web/API、数据库、Redis、国内公开服务：使用 `public-cn-service`，部署到 `region=cn`。
- 需要访问外网但仍使用备案域名入口的低频公开服务：使用 `public-global-service`，入口在国内 Traefik，容器跑 `region=global`。
- 爬虫、AI 调用、外网 API worker：使用 `global-worker`，不暴露公网域名，通过队列消费任务。
- 备份、内部工具、低频测试服务：使用 `home-internal-service`，部署到 `region=home`，默认只通过 Tailscale 或内网访问。

核心高频业务默认不要实时强依赖海外 HTTP；跨 region 调用优先走异步队列。

## 新增服务流程

1. 从 `templates/` 复制最接近的模板到 `stacks/<region>/<service>/stack.yml`。
2. 修改 `image`、域名、服务端口、`replicas` 和 placement constraints。
3. 确认公开服务有 Traefik labels，worker 和 home 内部服务不暴露公网域名。
4. 运行 `./scripts/validate-stacks.sh`。
5. 提交变更，让 Portainer 从 Git 部署或更新 stack。

## 基础部署前置条件

- 所有服务器已安装 Docker。
- 所有服务器已登录同一个 Tailscale tailnet。
- 国内 manager 节点已初始化 Docker Swarm。
- 海外和家庭节点已加入 Swarm。
- 已创建 external overlay network：`public`。
- 所有节点已按 `docs/node-labels.md` 打标签。
- Registry 凭据已在 Portainer 中配置。
- 备案域名 DNS 指向国内公网入口节点。

第一条真实验证链路建议部署 `stacks/cn/whoami/stack.yml`。
