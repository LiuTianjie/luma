# 运维操作 {#operations}

资源历史、可恢复日志、磁盘采样、告警和认证 Prometheus 抓取见[可观测性](./observability.md)。单 Manager SQLite、历史保留、备份和恢复见[控制面存储与恢复](./control-storage.md)。

Luma Control 是 Manager 上的自托管 API，管理令牌、节点注册、DNS、job 渲染和 Nomad 部署调用。`luma deploy` 与 Control 通信，不通过 SSH 部署。底层使用 HashiCorp Nomad，部署单位为 Nomad job。

默认流程：

```text
service.yaml -> luma deploy -> Luma Control -> render jobspec -> sync DNS -> Nomad API (/v1/jobs) -> docker driver
```

Luma 通过 Nomad HTTP API 部署。

## 添加服务 {#add-a-service}

```bash
luma service new
luma deploy <service>.yaml --dry-run
luma login https://luma.example.com --token <management-token>
luma deploy <service>.yaml
luma doctor
```

手写清单使用相同字段：

```yaml
name: api
image: ghcr.io/me/api:2026-05-29-1
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
```

多服务应用保持标准 `docker-compose.yml`，另加 Luma sidecar：

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml
```

存储服务通过 `luma storage set` 注册到 Control，Compose 仅按名称引用。Control 拒绝部署侧定义 storage class；切换后端必须验证 `adopted: true` 或声明 `initialize: empty`。存储所有权、本地节点固定和 NFS class 见 `docs/compose-storage.md`。

## 更新镜像标签 {#update-image-tag}

修改清单：

```yaml
image: ghcr.io/me/api:2026-05-29-2
```

然后部署：

```bash
luma deploy api.yaml
```

## 调整副本 {#scale-replicas}

修改清单：

```yaml
replicas: 3
```

然后部署：

```bash
luma deploy api.yaml
```

从 Manager 临时扩缩容：

```bash
nomad job scale api 3
```

临时命令不更新 Git。需要持久保留时，随后提交清单变更。

## 固定到一个节点 {#pin-to-one-node}

仅在服务依赖本地磁盘、硬件或特定家庭/工作机器时使用：

```yaml
region: home
node: home-mac-mini
```

`node` 必须是 `luma node join --name` 的节点名。Luma 渲染 `${node.unique.name}`（或 `meta.luma_node_name`）约束，并保留 `${meta.region}` 约束，因此目标节点必须属于该区域。Nomad 节点身份跨重新加入保持稳定，无需刷新固定放置。

## 查看状态 {#view-status}

从客户端：

```bash
luma status
luma context list
luma context use <cluster-id>
```

`luma status` 显示 Nomad、server leader 和 `role=client` 节点。

从 Manager：

```bash
nomad job status <service>
nomad job status -verbose <service>
nomad alloc logs -f <alloc-id>
```

## 回滚 {#roll-back}

Nomad 为每个 job 保留版本历史，Luma 在控制台和 CLI 都提供运行时回滚。

控制台打开 `https://<control-domain>/dashboard/`，进入 **应用 → 版本**，检查 job 版本，选择旧版本并确认回滚。

CLI 操作：

```bash
luma history <service>
luma rollback <service>
luma rollback <service> --to-version <N>
```

`luma history` 列出 Nomad job 旧版本（`GET /v1/job/<id>/versions`）；`luma rollback` 通过 `POST /v1/job/<id>/revert` 回退上一版或 `--to-version` 指定版本。Jobspec 渲染 `update { auto_revert = true }`，新版本健康检查失败时自动回退到最近健康版本。

这只回滚运行 job，不改写 Git 历史或 Control 保存的清单/YAML，不逆转数据库迁移或恢复卷。Compose 回滚作用于整个 job/stack。生产使用固定标签或 digest；`latest` 等可变标签可能让旧 job 拉取到新内容。

如需清单与运行 job 保持同步，采用 Git 优先流程：

```bash
git revert <deploy-commit>
luma deploy <service>.yaml
```

## 重启服务 {#restart-a-service}

无需拉取新镜像或改变存储清单即可重启。重启不仅是进程信号，还会协调交付：等待替换 allocation，刷新 Nomad CNI 主机端口状态，根据记录部署和实际节点重建 HTTP/TCP 路由，同步 DNS，并验证所有公共 HTTP 端点后才返回成功。

```bash
luma service restart <stack>
luma service restart <stack> --service <task>
luma service restart <stack> --mode task
```

两种重启模式：

- `recreate`：停止 allocation，让 Nomad 重新调度新实例，应用放置/重调度规则；整个 stack 默认此模式。
- `task`：在现有 allocation 内原地重启任务；`--service` 指定单任务时默认此模式。

省略 `--mode` 时按上述默认值，也可显式覆盖。Compose 的 `luma service restart <app> --service <svc>` 原地重启一个服务任务，`luma service restart <app>` 重建全部 allocation。`--timeout <seconds>` 限制 Control 响应等待，默认 `120`。


**控制台 → 应用 → 应用详情 → 服务**中，运行服务有 **Shell** 操作；节点 Shell 位于 **基础设施 → 节点**。都打开独立终端页，离开页面结束浏览器会话。通过节点 agent 的 `docker exec` 进入容器，复用节点 Shell 的终端 supervisor。用于实时诊断，不能替代日志或重启。系统 stack 被禁止。节点 agent 需声明 `container-terminal`，不支持时先更新。

响应包含 `replacementAllocations` 和结构化 `delivery`（`routes`、`dns`、`probes`）。平台管理但无保存记录的部署会报告跳过交付协调；托管公共部署只有公网探测成功才返回 `delivery.status=ready`。协调后的 HTTP 文件路由使用显式优先级，安全覆盖仍广播不可达私网地址的旧 Nomad-provider 路由。

拒绝重启 `traefik`、`egress`、`luma-control` 系统 stack；Control 本身就在 `luma-control` allocation 内，应用管理中循环重启会终止自身。

## 删除部署 {#remove-a-deployment}

使用已部署服务或 Compose 应用名：

```bash
luma service remove <service>
```

Control 使用最近成功部署记录的清单，删除 Luma 管理的公共 DNS，注销并清除 Nomad job（`DELETE /v1/job/<id>?purge=true`），删除 Manager 生成文件。单服务与 Compose 共用命令，客户端没有本地 YAML 也可删除网页创建的部署。tailscale-relay 同时删除 `/opt/luma/routes/<service>.yml`。cloudflare-tunnel 主机名仍归 Cloudflare Zero Trust 管理，跳过其清理。

默认保留存储。如需删除记录中引用的可移除存储，先预览再执行：

```bash
luma service remove <service> --dry-run --delete-storage
luma service remove <service> --delete-storage
```

单服务会删除 `storage.<volume>.path` 托管路径和清单中的 Docker 命名卷，跳过 bind mount。Compose 删除 sidecar 引用的托管卷子目录，不删除 class 本身。不能与 `--skip-orchestrator` 同用。

只预览清理，不修改 Manager：

```bash
luma service remove <service> --dry-run
```

部分清理时可保留 DNS 或运行 job。`--skip-orchestrator` 不移除 Nomad job：

```bash
luma service remove <service> --skip-dns
luma service remove <service> --skip-orchestrator
```

Control 不可用时，在 Manager 直接删除 Nomad job，再删除生成文件：

```bash
nomad job stop -purge <service>
sudo rm -rf /opt/luma/stacks/<region>/<service>
sudo rm -f /opt/luma/routes/<service>.yml
```

## 删除节点 {#remove-a-node}

从任意已登录客户端：

```bash
luma node remove <node-name>
```

Manager Control 删除 Luma 注册并排空匹配 client（`PUT /v1/node/<id>/drain`），死亡 client 随后由 Nomad 自动回收。适用于已本地退出的旧节点、失败加入或退役工作/家庭机器。Manager/Nomad server 受保护，不能用此命令删除。

Nomad 身份是稳定 UUID，同名退出并重新加入的机器保留 `meta.luma_node_name`，固定服务无需刷新 NodeID。清单保持按 Luma 节点名固定，不要换成 Docker 主机名。

## 排空节点 {#drain-a-node}

```bash
nomad node drain -enable <node-id>
```

恢复调度：

```bash
nomad node drain -disable <node-id>
```

## 刷新出网 {#refresh-egress}

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress refresh
```

在目标节点验证镜像拉取：

```bash
sudo docker pull hello-world:latest
```

## 修复控制面 {#repair-control-plane}

```bash
luma bootstrap manager --domain luma.example.com
luma doctor
```

## 所需网络端口 {#required-network-ports}

Linux 初始化/加入时会配置 UFW，云安全组或 Tailscale ACL 应允许相同访问：

| 端口 | 通信方向 | 用途 |
| --- | --- | --- |
| `80/tcp` | 公网客户端 → 边缘 Manager | HTTP 跳转及 Let's Encrypt 验证。 |
| `443/tcp` | 公网客户端 → 边缘 Manager | Control 和公共服务 HTTPS。 |
| `tcp-relay` 发布端口 | 公网客户端 → 边缘 Manager | 如 MySQL `3306/tcp`。Luma 从 Control 状态恢复 Traefik 监听器，云防火墙/安全组须放行同端口。 |
| `4646/tcp` | 客户端/Traefik → Nomad server | HTTP API：部署、状态、服务发现。 |
| `4647/tcp` | Nomad client → server | RPC。 |
| `4648/tcp`、`4648/udp` | Nomad agent 之间 | Serf gossip/server 成员通信。 |

Nomad agent 绑定 `0.0.0.0` 并广播 Tailscale 地址。UFW 仅在 `tailscale0` 放行 4646/4647/4648；公网隔离依赖防火墙，不依赖广播地址。无 UFW 主机应配置等价主机防火墙与云安全组，并验证公网接口无法访问这些端口。

## Tailscale 中继 {#tailscale-relay}

`tailscale-relay` 按服务显式选择，适合需要公网域名的家庭工具、预览或低频内部面板。

它不是常规公共流量的默认路径。

## 安装路径变化后节点 agent 失败 {#node-agent-fails-after-an-installer-path-change}

Nomad 节点可能仍 `ready`，但 Luma agent 离线。安装缺失 Python 包前，检查服务实际 `ExecStart`、启动器及其 Python 环境。主机可能同时有正常 `/opt/luma-cli` 和不完整用户本地安装。

安装程序在发布 shim 或刷新服务前验证目标环境：依赖导入、CLI/agent 命令加载和 `pip check` 必须通过。包安装失败只有通过这些检查才可使用源码回退。Shim 通过已验证 Python 的 `-m luma.cli` 启动，不复用 shebang 可能指向其他环境的旧脚本。验证失败非零退出，不显示成功、不替换 shim，也不刷新/重启服务。

该发布门禁不会事务性回滚目标目录内已发生的源码或包变化。受影响主机应先恢复到验证正常的环境，再检查 systemd 重启次数与新鲜 Control 心跳；进程存在不代表已重新连接。