# 日常运维 {#operations}

资源历史、可恢复日志、磁盘采样、告警和认证 Prometheus 抓取见[可观测](observability.zh-CN.md)。单 Manager SQLite、历史保留、备份和恢复见[Control 存储与恢复](control-storage.zh-CN.md)。

本文介绍应用和节点的日常运维。部署见[部署应用](deploying.zh-CN.md)。

## 控制面内存 {#control-memory}

Control job 预留 4 GiB（`MemoryMB: 4096`），`MemoryMaxMB: 0`，因此预留值也是其内存上限。初始化和 Manager 升级共用同一个渲染器，每次提交 Control job 都会应用这一默认值。手动修改 Nomad job 不会形成跨 Manager 升级保留的配置覆盖。

规划 Manager 容量时，除这部分预留外，还需为操作系统、Nomad、Traefik 和其他负载留出内存，建议以 8 GiB 起步。较小的 Manager 应在升级前确认可调度内存充足。升级后在 Manager 上执行 `nomad job inspect -json luma-control`，确认 `luma-control` task 的 `Resources.MemoryMB` 为 `4096`。

## 查看应用 {#inspect-applications}

```bash
luma status                                   # cluster, nodes and application health
luma app list --region cn                     # services and replica health
luma app show api                             # one application or service
luma app events api                           # recent runtime events of the latest allocation
luma app logs api --tail 100
luma app logs api --allocation <alloc-id> --previous
luma app logs api --follow --format ndjson
```

`--tail` 是所有选中来源共享的 1-500 行预算。文本输出用 `[allocation/task/stream]` 标注每一行；`[partial]` 和 `[continued]` 标记片段。需要保留来源和游标元数据时使用 JSON 或 NDJSON。`--follow` 在连接中断后从上次游标继续（退避最长 15 秒，无进展时最多重连 8 次）；按 Ctrl-C 停止。

Control 还保存可检索的构建与部署历史，与应用日志分开：

```bash
luma app history api --kind deployment --status failed
luma app history --source dashboard --since 2026-09-01T00:00:00+08:00 --format json
luma app history --id <record-id> --kind deployment
luma build logs <build-id>
```

每页默认 50 条（最多 100 条）；翻页时用相同过滤条件并把 `nextCursor` 传给 `--cursor`。保留策略见 [Control 存储](control-storage.zh-CN.md)。

在 Manager 上也可以直接使用 Nomad 工具：`nomad job status <app>`、`nomad alloc logs -f <alloc-id>`。

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

## 回滚 {#roll-back}

Nomad 为每个 job 保留版本历史，Luma 在控制台和 CLI 都提供运行时回滚。

控制台打开 `https://<control-domain>/dashboard/`，进入 **应用 → 版本**，检查 job 版本，选择旧版本并确认回滚。

CLI 操作：

```bash
luma app versions <app>
luma app rollback <app>
luma app rollback <app> --to-version <N>
```

`luma app versions` 列出 Nomad job 旧版本（`GET /v1/job/<id>/versions`）；`luma app rollback` 通过 `POST /v1/job/<id>/revert` 回退上一版或 `--to-version` 指定版本。Jobspec 渲染 `update { auto_revert = true }`，新版本健康检查失败时自动回退到最近健康版本。

这只回滚运行 job，不改写 Git 历史或 Control 保存的清单/YAML，不逆转数据库迁移或恢复卷。Compose 回滚作用于整个 job/stack。生产使用固定标签或 digest；`latest` 等可变标签可能让旧 job 拉取到新内容。

如需清单与运行 job 保持同步，采用 Git 优先流程：

```bash
git revert <deploy-commit>
luma deploy <app>.yaml
```

## 重启服务 {#restart-a-service}

无需拉取新镜像或改变存储清单即可重启。重启不仅是进程信号，还会协调交付：等待替换 allocation，刷新 Nomad CNI 主机端口状态，根据记录部署和实际节点重建 HTTP/TCP 路由，同步 DNS，并验证所有公共 HTTP 端点后才返回成功。

```bash
luma app restart <app>
luma app restart <app> --service <task>
luma app restart <app> --mode task
```

两种重启模式：

- `recreate`：停止 allocation，让 Nomad 重新调度新实例，应用放置/重调度规则；整个 stack 默认此模式。
- `task`：在现有 allocation 内原地重启任务；`--service` 指定单任务时默认此模式。

省略 `--mode` 时按上述默认值，也可显式覆盖。Compose 的 `luma app restart <app> --service <svc>` 原地重启一个服务任务，`luma app restart <app>` 重建全部 allocation。`--timeout <seconds>` 限制 Control 响应等待，默认 `120`。


**控制台 → 应用 → 应用详情 → 服务**中，运行服务有 **Shell** 操作；节点 Shell 位于 **基础设施 → 节点**。都打开独立终端页，离开页面结束浏览器会话。通过节点 agent 的 `docker exec` 进入容器，复用节点 Shell 的终端 supervisor。用于实时诊断，不能替代日志或重启。系统 stack 被禁止。节点 agent 需声明 `container-terminal`，不支持时先更新。

响应包含 `replacementAllocations` 和结构化 `delivery`（`routes`、`dns`、`probes`）。平台管理但无保存记录的部署会报告跳过交付协调；托管公共部署只有公网探测成功才返回 `delivery.status=ready`。协调后的 HTTP 文件路由使用显式优先级，安全覆盖仍广播不可达私网地址的旧 Nomad-provider 路由。

拒绝重启 `traefik`、`egress`、`luma-control` 系统 stack；Control 本身就在 `luma-control` allocation 内，应用管理中循环重启会终止自身。

## 本地终端连接节点和容器 {#cli-shell}

使用 `luma login` 保存的登录信息，在 macOS 或 Linux 的本地终端运行：

```bash
luma node shell <node-name-or-id>
luma app shell <full-service-name>
luma node shell <node-name> --transport direct
luma node shell <node-name> --transport relay
```

默认 `--transport auto`：Control 完成认证、节点选择和会话限额检查后，CLI 并行尝试节点地址，优先连接可达的节点 Agent；约 3 秒内无法建立直连时，经 Control 中转。连接后显示 `direct` 或 `relay`。`direct` 要求直连成功，失败就退出；`relay` 始终中转。旧 Control 或旧 Agent 自动使用中转。容器终端沿用网页端的运行状态和系统 stack 限制。

所有通道都使用 Luma WebSocket，不调用 SSH 或 Tailscale SSH。直连通过 TLS 加密：CLI 从已认证的 Control 通道取得节点证书指纹，核验后才发送 20 秒有效、绑定该会话的一次性凭据。管理 Token 只发送给 Control。Control 连接保留用于会话管理和撤销，直连的输入输出不经 Control。连接中断会结束会话，不会在已开始执行命令后静默切换通道或重放输入。

新版节点终端 supervisor 默认监听 TCP `17443`（IPv4），公布本机接口地址；Control 还会补充已登记的节点公网地址。CLI 必须能够访问该端口。不会自动修改防火墙、安全组或设置 NAT 穿透；不可达时使用中转。网页端仍使用原有中转通道。直连是否改善卡顿需在实际网络上对比，并不保证解决节点负载或网页渲染问题。

如需关闭监听、指定绑定地址，或公布 NAT 映射后的公网地址，可在节点 `/opt/luma/node-agent/agent.json` 中增加以下配置并重启节点 agent；已有其他字段保留：

```json
{
  "terminalDirect": {
    "enabled": true,
    "listenHost": "0.0.0.0",
    "port": 17443,
    "advertiseUrls": ["wss://node.example.com:17443/v1/terminal/direct"]
  }
}
```

`advertiseUrls` 可省略。`enabled: false` 关闭直连监听，保留中转。节点更新保留该节点已有的 `terminalDirect` 配置。默认终端权限与网页节点 Shell 一致，即节点 Agent 的运行用户。

支持窗口尺寸变化、中文输入和常用终端程序。`Ctrl-C` 发送给远端前台进程，`Ctrl-]` 断开本次连接，`exit` 结束远端 shell 并返回退出码。需要 stdin/stdout 都是 TTY；此命令不提供非交互脚本执行或文件传输。


### 中转源站配置 {#public-relay-origin}

可在节点 Agent 配置中设置 `terminalRelay: {"resolveIp": "CONTROL公网源站IP"}`，
仅优化 Agent 到 Control 的终端链路，不改变客户端网络要求和其他 Control API
请求。优先和回退链路均校验域名证书；不可达时重连原有 Control 路径，不重放命令。
网页终端和 CLI 共用该 Agent 出站链路，客户端到 Control 的路径不变。
Agent 升级保留该字段。启用前验证源站可达性；它不会修改防火墙或打开端口。

### AI Agent 与脚本执行命令 {#commands-for-agents-and-scripts}

`node exec` 无需 TTY，分别传输二进制 stdout/stderr，并返回远端退出码。默认走
relay：普通公网客户端通过 HTTPS 连接 Control，节点通过 Agent 出站连接接受命令。
不要求客户端加入 Tailscale，也不要求节点开放入站端口。

```sh
luma node exec bd-prod -- uname -a
luma node exec --timeout 30 bd-prod -- sh -lc 'uptime; df -h'
luma node exec --stdin bd-prod -- sha256sum < artifact.tar
```

Luma 参数放在节点名前，后续参数按原样作为 argv；管道、变量展开等使用显式
`sh -lc`。未加 `--stdin` 时立即发送 EOF。远端超时默认 300 秒、最大 86400 秒。
正常返回远端退出码；超时 124、无执行权限 126、程序不存在 127、Ctrl-C 130、
SIGTERM 143、传输失败 255。静默命令按执行期限管理，不套用交互 Shell 的空闲
超时；持续输入输出也不能延长该期限。断线可能使最终结果未知，不自动重试命令；变更命令
重试前先查状态。断线/超时会取消受监督的进程组；网络故障下要等节点感知断线或
远端期限到达。CLI、Control、Agent 均须支持 `exec-v1`，只安装 CLI 不足以使用。

使用 `luma node shell NODE --transport relay --diagnose` 或
`luma node exec --diagnose NODE -- uptime` 查看实际 socket 地址和 Control ping。
该 ping 只测客户端到 Control。直连输出中的内网/Tailscale 地址不能证明公网可达；
`198.18.*` 是虚拟网络路径，不能当作远端服务器地址。先比较网络、CDN 和代理
路由，再判断是否需要调整 PTY 缓冲。

操作方提供可达的 Control 公网源站 IP 时，终端命令的 `--resolve-ip` 可保留域名、
SNI 和证书校验，只改变 TCP 目标。它不能绕过系统 TUN 或打开已关闭的端口；节点
终端 `resolveIp` 同样保留域名校验。这些终端连接不需要添加 `--insecure`。
优化 relay 入口必须以普通公网客户端可达为前提，不能把私网访问作为通用解法。

## 删除部署 {#remove-a-deployment}

使用已部署的服务名或 Compose 应用名：

```bash
luma app remove <app>
```

Control 使用最近成功部署记录的清单，删除 Luma 管理的公共 DNS，注销并清除 Nomad job（`DELETE /v1/job/<id>?purge=true`），删除 Manager 生成文件。单服务与 Compose 共用命令，客户端没有本地 YAML 也可删除网页创建的部署。tailscale-relay 同时删除 `/opt/luma/routes/<service>.yml`。cloudflare-tunnel 主机名仍归 Cloudflare Zero Trust 管理，跳过其清理。

默认保留存储。如需删除记录中引用的可移除存储，先预览再执行：

```bash
luma app remove <app> --dry-run --delete-storage
luma app remove <app> --delete-storage
```

单服务会删除 `storage.<volume>.path` 托管路径和清单中的 Docker 命名卷，跳过 bind mount。Compose 删除 sidecar 引用的托管卷子目录，不删除 class 本身。不能与 `--skip-orchestrator` 同用。

只预览清理，不修改 Manager：

```bash
luma app remove <app> --dry-run
```

部分清理时可保留 DNS 或运行 job。`--skip-orchestrator` 不移除 Nomad job：

```bash
luma app remove <app> --skip-dns
luma app remove <app> --skip-orchestrator
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

Manager Control 删除 Luma 注册并排空匹配 client（`PUT /v1/node/<id>/drain`），死亡 client 随后由 Nomad 自动回收。如果 Nomad 对该 ID 返回 404 或 `node not found`，Control 会跳过 drain（`nomadDrainSkipped: nomad_node_not_found`）并继续删除 Luma 注册记录。网络错误、鉴权失败或 Nomad 不可用仍会让删除失败。适用于已本地退出的旧节点、失败加入或退役工作/家庭机器。Manager/Nomad server 受保护，不能用此命令删除。

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
luma manager egress
```

在目标节点验证镜像拉取：

```bash
sudo docker pull hello-world:latest
```

## 升级 Luma {#update-luma}

```bash
luma update                                  # manager: CLI + Control; node: CLI + agent; client: CLI
luma update --install-ref v0.2.56           # a specific release, branch or full commit
luma update fleet                            # every non-manager node with a ready agent
luma update fleet --include-manager          # explicit repair only
```

未指定 `--install-ref` 时安装最新发布版；协调发布候选版本时请使用完整提交。控制台的 **设置 → 系统维护** 是升级 Manager 的首选方式：它先把 Control 镜像同步到内部镜像仓库，再带进度和路由检查滚动升级。在 Manager 上直接执行 `luma update` 会自行拉取镜像；无法访问 GHCR 时，把 `LUMA_CONTROL_IMAGE` 设为可拉取的镜像。

在 Manager 上，`luma update` 会刷新防火墙、Traefik、Tailscale 看门狗、journald 大小上限、Control 配置和 `luma-control` job（启用 Nomad auto-revert），然后刷新本机节点 agent。它不会重启 Docker 或 Nomad agent，不会重新配置出站代理，也不会重新部署应用。`/opt/luma/control` 下的 Manager 状态仅 root 可读；没有免密 sudo 时执行 `sudo ~/.local/bin/luma update`。

批量升级最多同时升级四个节点。安装或重连检查失败的节点最多重试五次；从设置 → 系统维护发起时，开始时离线的节点也会等待并重试。每次安装会保留新版本、上一版本（用于回滚）以及仍被进程使用的版本，并删除只包含 `src` 和 `venv` 的旧 `releases/candidate.*` 目录；设置 `LUMA_KEEP_RELEASES=1` 可保留。

在已加入的节点上，`luma update` 刷新 CLI 和节点 agent。agent 版本过旧、不支持批量升级的节点会被报告为跳过；在该节点上执行一次 `luma update` 即可。没有保存 agent 元数据的旧节点，加上 `--control-url https://luma.example.com --token <node-join-token>`。

Manager 和节点都运行一个小的 Tailscale 看门狗，连续探测对端失败后重启 Tailscale，不影响 Docker、Nomad 或应用。

## Builder 的安装与重启恢复 {#builder-lifecycle}

Builder 是加入集群的普通 Luma 节点，通过 `luma build config --node <name> --default-node <name>` 指定其构建职责。节点上的 agent 负责领取构建任务，Docker Buildx 按需创建 `luma-builder` 和 `luma-builder-egress` 构建引擎。它们不需要额外的后台启动脚本。

`luma node join` 安装节点服务。Linux 使用 `luma-node-agent.service`（开机启用、`Restart=always`），macOS 使用 `io.luma.node-agent` LaunchDaemon（`RunAtLoad`、`KeepAlive`）。Nomad 由自己的系统服务管理，BuildKit 容器由 Docker 管理。macOS 上的 Docker Desktop 或 OrbStack 仍须配置为随用户登录启动；agent 自启不等于 Docker 已经可用。

加入节点、agent 更新和独立安装脚本复用 `luma/agent.py` 中的服务定义及节点 Tailscale 看门狗安装逻辑。独立安装脚本只会刷新已有 `agent.json` 的节点，不会把普通 CLI 客户端变成 builder。不要在单台机器上另加无源码的启动补丁作为正常安装步骤。

Linux 的 `luma-node-tailscale-watchdog.timer` 在开机约两分钟后开始检查，随后约每分钟检查一次。它从 Nomad 配置读取 Manager 地址，检查 Tailscale 连通性和实际 TCP 端口，默认连续三次失败才重启本机 Tailscale。进程已经启动但控制面仍显示离线时，先检查这一层；SSH 可达和 agent 到 Manager 可达是不同的连接。

节点看门狗的 Linux 配置文件是 `/etc/default/luma-node-tailscale-watchdog`，安装和升级保留该文件。可设置 `LUMA_NODE_TAILSCALE_WATCHDOG_THRESHOLD`（默认 `3`）、`LUMA_NODE_TAILSCALE_WATCHDOG_PORTS`（空格分隔，默认 `4647`）和 `LUMA_NODE_TAILSCALE_WATCHDOG_MANAGERS`（逗号分隔的 tailnet 地址；留空时读取 Nomad）。下一次定时执行会读取配置。

```bash
systemctl is-enabled luma-node-agent.service luma-node-tailscale-watchdog.timer
systemctl status luma-node-agent.service luma-node-tailscale-watchdog.timer --no-pager
journalctl -b -u luma-node-agent.service -u luma-node-tailscale-watchdog.service
luma node list
```

恢复验收要同时确认系统服务、最新 Control 心跳和 builder 的 `ready` 状态。发现开机时间大幅跳变时，还应检查宿主机/虚拟机 RTC 与系统校时服务；不能仅凭时间跳变认定网络故障根因，也不应默认给所有开发者安装或强制依赖某一种 NTP 服务。

## 节点磁盘清理 {#node-disk-cleanup}

每个节点 agent 每天清理一次自己的磁盘，agent 启动约十分钟后开始第一次检查。每次清理：

- 删除没有任何容器使用、且超过 7 天的 Docker 镜像；
- 清理 7 天未使用的构建缓存，包括默认 builder 和正在运行的 `luma-builder` / `luma-builder-egress`；
- 在 Linux 上维护 `/etc/systemd/journald.conf.d/luma.conf`，把 journal 上限设为 `SystemMaxUse=500M`。

以下镜像一定保留：被容器使用（运行中或已停止）、被本节点的 Nomad 分配引用、属于 agent 或 Control 当前版本的 `luma-control` 镜像、Nomad 或 Luma 按需启动的辅助镜像（桥接网络用的 `registry.k8s.io/pause`、主机修复用的 `ubuntu:22.04` 或 `LUMA_HOST_PREP_IMAGE`、`registry:2` 以及 macOS Compose 网络镜像），以及无法确定时间的镜像。镜像的时间取构建时间和在本机打标签时间中较晚的一个；Docker 的 containerd 镜像存储不记录打标签时间，这时 agent 升级之后才出现的镜像从 agent 第一次看到它的时间算起。读不到本机 Nomad API 时不删除任何镜像。删除从不强制：期间被新容器使用的镜像会保留。

在 Dashboard 打开节点详情，使用 **磁盘清理**：**预览清理** 列出将被删除的镜像，不做任何修改；确认后再点删除按钮执行清理。脚本里可以发同样的请求：

```bash
curl -fsS -X POST https://luma.example.com/v1/nodes/disk-cleanup \
  -H "Authorization: Bearer $LUMA_DEPLOY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"node": "manager", "dryRun": true}'
```

只有发送 `"dryRun": false` 才会真正删除，否则都是预览。同一张卡片显示节点上次清理的时间、删除的镜像数和释放的空间。

在 `/opt/luma/node-agent/agent.json` 中配置节点，或在 agent 服务上设置环境变量（环境变量优先）：

| `agent.json` 的 `diskHygiene` 键 | 环境变量 | 默认值 |
| --- | --- | --- |
| `enabled` | `LUMA_DISK_HYGIENE=off` | `true` |
| `intervalHours` | `LUMA_DISK_HYGIENE_INTERVAL_HOURS` | `24` |
| `minAgeDays` | `LUMA_DISK_HYGIENE_MIN_AGE_DAYS` | `7` |
| `buildCache` | `LUMA_DISK_HYGIENE_BUILD_CACHE` | `true` |
| `journaldMaxUse` | `LUMA_JOURNALD_MAX_USE` | `500M`（`off` 关闭） |

```json
{"diskHygiene": {"enabled": true, "minAgeDays": 14, "journaldMaxUse": "1G"}}
```

修改 `agent.json` 后重启 agent。`luma bootstrap`、Manager 上的 `luma update` 和 `luma node join` 也会直接写入 journald 上限，同样读取 `LUMA_JOURNALD_MAX_USE`。

## 修复控制面 {#repair-the-control-plane}

```bash
luma bootstrap --domain luma.example.com
luma doctor
```

初始化是幂等的，但会涉及 Docker、防火墙、Traefik、Nomad 和出站代理；完整重跑请放在维护窗口进行。

## Manager IP 变更 {#manager-ip-change}

在 Manager 上执行，先预览：

```bash
luma manager ip-change --old 203.0.113.10 --new 203.0.113.20 --domain luma.example.com --dry-run
luma manager ip-change --old 203.0.113.10 --new 203.0.113.20 --domain luma.example.com
```

它先通过 HTTPS 验证新地址，然后只更新 Manager 的 `publicIp`、`providers.dns.edgeTarget` 以及与旧地址完全匹配的 Cloudflare A 记录。它会备份 `luma.yaml`，复用正在运行的 Control 镜像，不会重新部署应用。部分失败后重跑即可补完剩余记录。

## Registry 可清理镜像 {#registry-cleanup-candidates}

Control 保护当前 Nomad Job、活跃 allocation（包括发布过程中仍在运行的旧版本）、排队/运行/收尾中的构建和活跃 Agent 任务所引用的镜像。历史成功构建、非活跃 Nomad 版本、残留部署记录、每仓库及系统版本保留数量，不再提供永久保护。

镜像首次在完整扫描中确认无引用后，开始记录未使用时间。连续观察到未使用满 7 天后，标记为可清理；重新使用会重置计时，扫描不完整时保守重置并标记为 `unknown`。不会以镜像创建时间推断未使用时长，也不追踪外部客户端直接拉取 Registry 的次数。计时按仓库和 digest 保存在 Control 状态目录的 `registry-usage.json` 中，与可失效的镜像列表缓存分开，Control 重启后保留。

升级时旧策略迁移为 7 天和 `recommend` 模式，只标记可清理，不自动删除。已有镜像不会按创建日期倒推未使用时长。`--max-age-days` 现在表示连续未使用天数；已弃用的 `--keep-last` 和 `--system-keep-last` 不再提供豁免。管理员之后仍可显式切换模式。此改动升级 Control 后生效，无需重新部署 Registry 或升级节点 Agent。

## 新节点上的私有镜像仓库 {#private-registries-on-new-nodes}

`luma node join` 和 `luma node nomad-join` 在配置好 Docker 和 Tailscale 之后、启动 Nomad 之前，准备托管 HTTP 镜像仓库访问。地址来自控制面登记的 HTTP transport。保留一条窄的历史兼容规则：已配置的 Builder Registry 使用已注册节点的 Tailscale IP 和 `5000` 端口，且未登记明确 transport、未保存该仓库凭据时，也按 HTTP 处理。不会将其他私有地址、公共镜像仓库或已登记的 HTTPS 地址推断为 HTTP。

单服务和 Compose 正式部署在提交最终 Nomad job 之前，根据最终 Docker 镜像中的托管 HTTP 地址、实际落点约束和健康的 task driver，检查全部符合条件的目标节点。修改任何目标的配置前，所有目标都必须声明 `managed-registry-access-v2`。按 region 部署也会检查新加入的合格节点；旧 agent、未知运行时或无法判断的落点约束都会阻止提交。Manager 若属于目标，只做只读验证；缺少配置时需在宿主机维护，预检不会修改 Manager 的 Docker 或代理配置。

滚动升级时，修正后的 agent 同时声明 v1 和 v2，保持公开 action 和 payload 不变，统一使用同一实现。仍要求 v1 的旧 Control 可以调用修正后的 agent；新 Control 要求 v2，拒绝仅支持 v1 的旧 agent。应先升级所有合格的目标 agent，再升级 Control。

Linux 使用本机 `/var/run/docker.sock`，读取 Docker 实际 JSON 字段 `HttpProxy`、`HttpsProxy` 和 `NoProxy`。只有 HTTP transport 或有效代理绕过需要配置时，才补充精确的托管地址；保留无关配置，并在修改 `daemon.json` 前备份。缺少代理事实会报错，不能当作直连。

macOS 会识别实际 OrbStack socket 的所有者。HTTP transport 由该用户的 `~/.orbstack/config/docker.json` 管理，代理策略则读取 OrbStack 原生的 `network_proxy` 和 `network.proxy.exclude`；`auto` 模式还会检查 macOS 系统实际生效的代理配置。不能仅凭 Docker 中的 OrbStack 内置 broker 地址非空，就判断存在外部代理。确认有效上游代理需要绕过时，Luma 通过原生配置入口保留已有排除项，只追加缺失的托管地址。未知模式、不支持的 PAC/WPAD 配置、不完整的检查结果及其他 macOS Docker 运行时都会明确失败。配置后回读生效状态，并通过 HTTP 探测各仓库的 `/v2/`；仅文件写入成功不算验证通过。

已有配置与引擎实际状态都满足要求时，即使有运行中容器也可以完成验证。任何需要的配置修改，包括无需重启 Docker 即可生效的原生代理排除项，都要求引擎空闲；否则需在维护窗口排空节点后重试。Luma 仅在 HTTP transport 配置需要时重启 Docker，并准确返回是否重启。单独升级 agent 不会配置或重启 Docker；后续正常 join 或 deploy 才执行所需验证，并在允许时配置。

`luma registry serve` 记录托管镜像仓库使用 HTTP 还是 HTTPS，并部署 registry 服务，不再为集群全部节点配置 Docker。registry 服务自身最终使用的镜像也走同一目标预检；使用新仓库的节点在各自 join 或 deploy 时按需准备访问。

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

自定义 Region 可通过 `luma region update render --exposure tailscale-relay` 开启中继，无需移动节点。集群升级顺序、Mac 网络、验收与回退见[自定义 Region 公网入口](region-public-ingress.zh-CN.md)。它不是常规公共流量的默认路径。

## 安装路径变化后节点 agent 失败 {#node-agent-fails-after-an-installer-path-change}

Nomad 节点可能仍 `ready`，但 Luma agent 离线。安装缺失 Python 包前，检查服务实际 `ExecStart`、启动器及其 Python 环境。主机可能同时有正常 `/opt/luma-cli` 和不完整用户本地安装。

安装程序在发布 shim 或刷新服务前验证目标环境：依赖导入、CLI/agent 命令加载和 `pip check` 必须通过。包安装失败只有通过这些检查才可使用源码回退。Shim 通过已验证 Python 的 `-m luma.cli` 启动，不复用 shebang 可能指向其他环境的旧脚本。验证失败非零退出，不显示成功、不替换 shim，也不刷新/重启服务。

该发布门禁不会事务性回滚目标目录内已发生的源码或包变化。受影响主机应先恢复到验证正常的环境，再检查 systemd 重启次数与新鲜 Control 心跳；进程存在不代表已重新连接。
