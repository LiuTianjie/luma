# infra-stacks

多服务器统一部署控制仓库，用 `Portainer + Docker Swarm + Traefik + Tailscale` 管理国内、海外和家庭节点上的服务。

本仓库配套 CLI 名为 **Luma**。日常新增服务时优先写一个小的 service manifest，再执行 `luma deploy <service>.yaml`，由 Luma 生成 Swarm stack、同步 DNS、触发 Portainer 部署。

Luma 使用显式 `exposure` 模型决定请求怎么进来。Tailscale 默认只做控制面网络；只有 `exposure: tailscale-relay` 的 home 服务才会把业务请求经由 Tailscale 转发。

## 技术栈

- Portainer：统一管理 Docker Swarm、Stack、Registry 和 Git 部署。
- Docker Swarm：提供多节点调度、overlay network、service replicas 和节点标签约束。
- Traefik：作为公开入口，通过 Swarm labels 自动绑定域名、HTTPS 和反向代理。
- Tailscale：打通国内、海外、家里服务器之间的私网通信。

暂时不使用 Kubernetes、k3s 或 Rancher。当前目标是轻量、可维护、能统一部署，而不是一开始就引入完整云原生平台。

## 日常使用姿势

1. 业务项目构建 Docker 镜像并推送到 Registry。
2. 写一个 service manifest，例如 `examples/public-cn-service.yaml`，并选择 `exposure`。
3. 执行 `luma deploy <service>.yaml`。
4. Luma 生成或更新 `stacks/<region>/<service>/stack.yml`。
5. Luma 可选同步 Cloudflare DNS、提交 Git、触发 Portainer webhook。
6. Traefik 通过 labels 自动绑定域名和 HTTPS。
7. Docker Swarm 根据 node labels 把服务调度到 `cn`、`global` 或 `home`。

不在业务服务器上 build 镜像，不手动 SSH 登录服务器执行 `docker run`。

## Luma CLI

本地安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

常用命令：

```bash
luma render examples/public-cn-service.yaml
luma deploy examples/public-cn-service.yaml --skip-dns --skip-webhook
luma deploy examples/public-cn-service.yaml --commit --push
```

本地验证：

```bash
python -m unittest discover -s tests
./scripts/validate-stacks.sh
```

外部 provider 凭据通过环境变量提供：

```bash
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ZONE_ID=...
export PORTAINER_WEBHOOK_URL=...
```

详细使用手册见 `docs/how-to-use-luma.md`，CLI 字段说明见 `docs/luma-cli.md`，暴露模式说明见 `docs/exposure-model.md`。

## 目录结构

```text
docs/       架构、启动、节点标签、服务模式、Luma CLI 和运维文档
examples/   Luma service manifest 示例
luma/       Luma CLI 源码
stacks/     可由 Portainer 部署的真实 Swarm stacks
templates/  新服务可复制的 stack 模板
scripts/    本地校验脚本
```

核心 stack 放在 `stacks/core/`，示例业务服务按 region 放在 `stacks/cn/`、`stacks/global/` 和 `stacks/home/`。

## 服务类型选择规则

- 主 Web/API、数据库、Redis、国内公开服务：使用 `public-cn-service`，部署到 `region=cn`。
- 需要访问外网并自带海外入口的低频公开服务：使用 `public-global-service`，部署到 `region=global`，`exposure=external-edge`。
- 家里服务需要通过国内入口访问：使用 `exposure=tailscale-relay`，入口在国内 Traefik，后端经 Tailscale 到 home。
- 家里服务需要直接走 Cloudflare：使用 `exposure=cloudflare-tunnel`。
- 爬虫、AI 调用、外网 API worker：使用 `global-worker`，不暴露公网域名，通过队列消费任务。
- 备份、内部工具、低频测试服务：使用 `home-internal-service`，部署到 `region=home`，默认只通过 Tailscale 或内网访问。

核心高频业务默认不要实时强依赖海外 HTTP；跨 region 调用优先走异步队列。

## 新增服务流程

1. 从 `examples/` 复制最接近的 service manifest。
2. 修改 `name`、`image`、`exposure`、`domain`、`port`、`region` 和 `replicas`。
3. 执行 `luma deploy <service>.yaml --skip-dns --skip-webhook` 做本地生成验证。
4. 准备真实发布时，配置 Cloudflare 和 Portainer 环境变量后执行 `luma deploy <service>.yaml --commit --push`。
5. 运行 `./scripts/validate-stacks.sh` 做全仓库 stack 校验。

如果需要完全手写 stack，也可以继续复制 `templates/`，但优先使用 Luma，避免 Traefik labels 和 placement constraints 写散。

## 基础部署前置条件

- 所有服务器已安装 Docker。
- 所有服务器已登录同一个 Tailscale tailnet。
- 国内 manager 节点已初始化 Docker Swarm。
- 海外和家庭节点已加入 Swarm。
- 已创建 external overlay network：`public`。
- 如果使用 `tailscale-relay`，国内 Traefik 节点上 `/opt/luma/routes` 可用，并和仓库 `routes/` 保持同步。
- 所有节点已按 `docs/node-labels.md` 打标签。
- Registry 凭据已在 Portainer 中配置。
- 备案域名 DNS 指向国内公网入口节点。

第一条真实验证链路建议部署 `stacks/cn/whoami/stack.yml`。
