# 故障排查 {#troubleshooting}

先运行：

```bash
luma preflight
luma doctor
```

如果环境检查失败，创建或编辑 `.env`：

```bash
cp .env.example .env
$EDITOR .env
```

## 首次 bootstrap manager 失败 {#first-manager-bootstrap-failed}

Bootstrap 可重复执行。失败步骤会打印 `[fail] <标题>: <原因>` 和 `Fix:`。修好对应层后重跑同一条命令：

```bash
luma bootstrap manager --domain luma.example.com
```

| 失败步骤 | 常见原因 | 修复 |
| --- | --- | --- |
| Install Docker | 没有 sudo，或访问不了 Docker 镜像源 | 修好 sudo/网络后重跑 bootstrap |
| Install Nomad binary / CNI | 访问不了 HashiCorp 下载 | 国内 manager 配置 `EGRESS_SUBSCRIPTION_URL` 后重跑 |
| Install and connect Tailscale | 缺 auth key，或未登录 Tailscale | `luma tailscale connect` |
| Deploy Traefik / Luma control | Nomad 未就绪，或 control 镜像拉不下来 | `nomad job status traefik` / `nomad job status luma-control`；国内默认 GHCR 不要用 `--skip-egress` |
| Sync control DNS | Cloudflare token、zone 或 `LUMA_DNS_EDGE_TARGET` | 修正 `.env` / 交互输入后重跑 bootstrap |
| Deploy egress | 订阅 URL 或镜像源 | `luma egress setup` |

控制面起来后用 `luma doctor` 看 Control 连通性和节点就绪。不要靠配置 `LUMA_LAE_*` 来恢复首次安装；LAE 是可选项。

## 无法安装本地 CLI {#local-cli-cannot-be-installed}

运行：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

如果没有 `python3`，先安装：

```bash
# macOS
brew install python

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl
```

客户端上的 Docker 是可选的。仅运行 Manager 或工作负载的服务器需要安装 Docker。

## `zsh: permission denied: luma` {#zsh-permission-denied-luma}

Shell 把仓库中的 `luma/` 包目录识别为了命令，而不是已安装的 CLI。

修复方法：

```bash
./scripts/install-luma.sh
. .venv/bin/activate
hash -r
which luma
luma preflight
```

备用方式：

```bash
.venv/bin/luma preflight
./scripts/luma preflight
```

## Tailscale 尚未登录 {#tailscale-is-not-logged-in}

在 Tailscale 创建临时或可复用 auth key，然后执行：

```bash
luma tailscale connect
```

## Docker 镜像拉取失败 {#docker-image-pulls-fail}

修复方法：

```bash
luma egress setup
luma doctor --deep
```

私有仓库需要分别检查连通性、Docker daemon 代理和认证：

```bash
# run these on the node that receives the service task
docker info | grep -i proxy -A3
curl -vk https://<registry-host>/v2/
docker pull <registry-host>/<org>/<image>:<tag>
```

`curl /v2/` 返回带 `docker-distribution-api-version` 的 `401`，说明该节点可以访问仓库。如果 `docker pull` 仍在认证前出现 EOF/超时，检查同一节点的 Docker daemon `HTTPProxy`/`HTTPSProxy`，确保私有仓库主机名位于 daemon 的 `NO_PROXY`。这与清单中的 `proxy: true` 无关，后者只控制容器运行时出站流量。

服务固定在 `home` 或 ARM 节点时，确认镜像提供目标平台：

```bash
docker buildx imagetools inspect <image>
```

Luma 按目标节点平台验证镜像拉取，并使用该平台拉取得到的 digest 部署。

## 批量更新失败 {#fleet-update-fails}

如果 **控制台 → 节点 → 更新中心**报告 Control 镜像准备失败，保留发布 ref，打开同一面板中持久化的镜像任务。错误会区分缺少 Builder 能力、缺少 `registryHost` / `pushHost`、外部仓库传输失败和内部 digest 验证失败。修复显示的配置或网络问题，再次点击更新 Control；无需 SSH 到 Manager 或重启应用。内部镜像通过验证前不会开始 Manager 滚动更新。

如果 `luma update fleet` 报告 `HOME: parameter not set` 或 `HOME: unbound variable`，说明节点在没有 `HOME` 的服务环境中运行旧安装流程。将 Manager/CLI 升级到包含 HOME 回退逻辑的版本，再重新批量更新。

如果节点报告 `unsupported node agent task action: update-luma` 或 `node agent does not support fleet update`，该节点 agent 太旧，无法通过批量任务更新自身。请在该节点执行一次：

```bash
luma update
```

如果节点没有保存本地 agent 元数据：

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

此后即可通过 `luma update fleet` 远程更新。

## 应用运行正常，但重启后公网域名一直等待 {#application-is-running-but-its-public-domain-hangs-after-restart}

运行状态和交付状态都正常，才能视为重启完成：

```bash
nomad job allocs <stack>
curl -fsS --max-time 5 http://<allocation-node-tailscale-ip>:<publish-port>/
curl -vk --max-time 15 https://<domain>/
```

然后检查 Manager 的 `/opt/luma/routes/<stack>.yml` 和 Nomad 服务注册。旧 `cn-edge` job 可能仍广播 `10.x.x.x` 等云厂商私网地址，而 Manager 实际需要节点的 Tailscale 地址。不要反复重启健康应用。在 Control `0.1.200+` 上执行一次正常的控制台/CLI 重启；Control 会从实际 allocation 推导 Luma 节点，原子发布更高优先级的文件路由，同步 DNS，等待公共探测通过后才返回 `delivery.status=ready`。

若 Control 报告 `delivery reconcile skipped: deployment record is unavailable`，该 job 创建于 Luma 开始保存部署记录之前。重新导入/部署一次清单，让后续重启、删除和路由恢复有持久化依据。生成的路由正确但 Traefik 仍返回默认 404 时，使用路由重载/证书重试操作，或检查 Traefik file-provider 挂载；不要将 DNS 改为工作节点地址。

Manager 更新也必须保留 `/opt/luma/luma.yaml`。更新后 `luma status` 应报告 DNS `ready=true`。若旧版丢失了 `providers.dns`，从最近的 `/opt/luma/backups/*/luma.yaml` 恢复，保留当前 Control 状态与令牌，并升级到 `0.1.198+`；该版本安装 Manager 配置时会深度合并运维人员维护的配置段。

## Manager 更新输出 `tmpfs: Unknown parameter 'noswap'` {#manager-update-prints-tmpfs-unknown-parameter-noswap}

该消息来自 Manager 上的 Nomad client 准备 allocation 密钥目录时，并非 Control 镜像。Nomad 尝试以 `noswap` 选项将每个任务的 `secrets/` 目录挂载为 tmpfs；旧 Linux 内核不支持该选项，可能输出：

```text
tmpfs: Unknown parameter 'noswap'
```

先确认更新确实失败，还是仅输出内核警告：

```bash
nomad job status luma-control
nomad job allocs luma-control
```

Allocation 正在运行则无需修复；只有命令本身非零退出时，才重新执行 `luma update manager`。

如果 allocation 因 task-dir 或 tmpfs 挂载错误失败，检查 Manager 内核和 Nomad 版本：

```bash
uname -r
nomad version
journalctl -u nomad -n 120 --no-pager
```

长期修复方法是使用在不支持 `noswap` 时会回退的 Nomad 版本，或升级到支持 `tmpfs noswap` 的内核。修复 Nomad 后，重启 Manager agent，再刷新控制面：

```bash
sudo systemctl restart nomad
luma update manager
```

## 控制台终端断开 {#dashboard-terminal-disconnects}

`terminal agent disconnected` 表示浏览器会话已连接，但节点侧终端 agent 的 WebSocket 断开。常见原因：

- 节点 agent 失去 Control API 租约而重启；
- 同一节点运行多个 `node-agent terminal-supervisor`，在控制面互相替换；
- 节点 agent token 过期，导致 `/var/log/luma-node-agent.err` 反复出现 `401 unauthorized`。

在节点上检查：

```bash
pgrep -af 'luma.*node-agent|terminal-supervisor'
tail -n 120 /var/log/luma-node-agent.err
```

健康节点应有一个 `node-agent run` 进程和一个 `node-agent terminal-supervisor` 子进程。如有旧的孤儿 supervisor，清理后重启节点 agent：

```bash
sudo pkill -f 'node-agent terminal-supervisor'
sudo systemctl restart luma-node-agent.service
# macOS:
sudo launchctl kickstart -k system/io.luma.node-agent
```

当前 Luma 会在短暂租约失败期间保持 agent 运行，并通过每节点锁确保只运行一个终端 supervisor。

## 无法打开应用容器 Shell {#application-container-shell-is-unavailable}

控制台应用详情可进入运行中的服务容器。Control 解析 Nomad allocation 后，节点 agent 对带对应 alloc/task 标签的容器执行 `docker exec`。常见失败原因：

- 服务未运行，没有可进入的 allocation；
- 节点 agent 太旧，没有声明 `container-terminal` 能力，更新后重试；
- 终端 supervisor 断开，与节点终端故障相同；
- 镜像是 distroless / scratch，没有 `bash`/`ash`/`sh`。

该流程不会进入系统 stack（`traefik`、`egress`、`luma-control`、`luma-storage*`）。

## Nomad client 已断开，但容器仍运行 {#nomad-client-is-disconnected-but-containers-still-run}

这是预期行为。每个 Luma job 渲染 `max_client_disconnect = 1h`；Mac mini 等家庭节点失去到 Nomad server 的 tailnet 路径时，client 标记为 `disconnected`，但本地 allocation 保持运行，链路恢复后重新连接。短暂 WAN/DERP 波动不会立即终止任务并重新调度。

先检查节点和 server RPC 路径，再判断应用问题：

```bash
nomad node status
nomad node status -self
tailscale ping <node-tailscale-ip>
nc -vz <server-tailscale-ip> 4647
nc -vz <server-tailscale-ip> 4648
```

如果 `tailscale ping` 正常，但 `4647`（RPC）超时，可能是 Tailscale 看起来在线、实际 tailnet 数据路径已卡住。在无法连接对端 TCP 的一侧重启 Tailscale：

```bash
sudo systemctl restart tailscaled
# macOS:
sudo launchctl kickstart -k system/W5364U7YZB.io.tailscale.ipn.macsys.network-extension
```

Manager 和节点更新会安装执行这些检查的 Tailscale 看门狗，仅在连续失败后重启本地 Tailscale。如果 client 断开超过 `max_client_disconnect` 窗口，Nomad 会在满足 job 区域/节点约束的其他位置重新调度 allocation。

## 尚未登录 {#not-logged-in}

部署提示 `not logged in` 时，向 Manager Control API 认证：

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

## Nomad 部署失败 {#nomad-deploy-fails}

重新初始化 Manager，刷新 Nomad server 与 Manager Control 状态：

```bash
luma bootstrap manager --domain luma.example.com
```

如果还未放置任何 allocation 就失败，检查 Nomad server 是否运行且有 leader：

```bash
nomad server members
nomad status               # leader + jobs
nomad node status          # clients ready, meta.region correct
```

Job 持续 `pending` 并出现 `Placement Failures` 时，说明约束没有匹配到就绪 client。检查失败的 evaluation：

```bash
nomad job status <service>
nomad eval status -verbose <eval-id>
```

常见原因包括：没有就绪 client 满足 `region`；通过 `meta.luma_node_name` 固定的节点处于 `disconnected`；镜像平台不匹配；唯一合格 client 的 CPU/内存耗尽。Apple Silicon client 的 CPU 指纹误读可能让 `cpu.totalcompute` 接近零而阻塞放置，需要显式设置 `cpu_total_compute`，Luma 节点配置会处理。

如果 Luma Control 无法访问 Nomad server，确认 client 与 server 之间的 RPC 路径：

```bash
nc -vz <server-tailscale-ip> 4647
nomad server members        # all servers alive, one leader
```

`4647/tcp` 路径卡住会让 client 变为 `disconnected`，即使节点本地 `docker info` 仍正常。

## 公共路由不健康 / 找不到 Traefik router {#public-route-unhealthy-traefik-router-not-found}

`cn-edge` 或 `external-edge` 服务可能已完成 Nomad allocation 放置，但公共路由探测失败：

```text
Public route unhealthy: https://myapp.example.com/ -> HTTP 404 (Traefik router not found)
```

这表示探测到达边缘节点，但 Traefik 返回了自身默认的 `404 page not found`，即尚无匹配此主机名的 router。这不同于应用真实路由返回 404，应用 404 会被判断为可达。常见原因是 Traefik 尚未读取新发布的 file-provider 路由，或路由文件的主机名/标签与请求域名不一致。

Luma 原子写入路由：验证渲染结果，在受监视目录之外暂存，再一次性移入最终文件，避免 Traefik 读到半写入内容。公共路由不健康（找不到 router 或短暂 `502`/`503`/`504`）时，Control 会先执行一次 **Recover public route**，重建服务 allocation 并重新探测，然后才判定部署失败。自动重试后仍失败，请检查：

```bash
# on the manager
ls /opt/luma/routes/                      # the <service>.yml route file exists
cat /opt/luma/routes/<service>.yml        # host rule matches the requested domain
nomad job status <service>                # allocation is running/healthy
```

确认清单 `region` 和 `exposure` 确实生成边缘路由（仅 `cn-edge`/`external-edge` 有公共 Traefik router），`domain` 与 DNS 记录一致，且 Traefik 正在运行。重新执行 `luma deploy` 会重新发布路由文件。

## macOS 节点加入时在 Docker 步骤失败 {#macos-node-join-fails-at-docker}

macOS 工作节点和家庭节点在执行 `luma node join` 前，必须已安装并启动 Docker Desktop。Luma 无法自动安装 Docker Desktop。

加入前先在本地验证：

```bash
command -v docker
docker info
```

Docker Desktop 缺失或仍在启动时，`luma node join` 会在向 Control 注册节点前停止。启动 Docker Desktop，等 `docker info` 成功后重试相同加入命令。

## Cloudflare DNS 失败 {#cloudflare-dns-fails}

使用限定 Zone 范围的 API 令牌：

```text
Zone / DNS / Edit
Zone / Zone / Read
Specific zone: your domain
```

然后执行：

```bash
luma cloudflare connect --zone example.com
```

## Manager 公网 IP 变更 {#manager-public-ip-changed}

不要从头初始化健康集群，也不要在 Manager Control 状态中全局替换旧 IP。迁移到 SQLite 后，旧 `control.json` 已不是权威数据源。先确认 Nomad allocation 和 Traefik 正常，再在 Manager 上预览有限范围的恢复操作：

```bash
luma manager ip-change --old <old-ip> --new <new-ip> --domain <control-domain> --dry-run
```

预览必须显示预期 Manager 节点、有类型的配置字段、仍指向旧地址的全部 Cloudflare A 记录、新地址直接 HTTPS 健康检查成功，以及当前运行的 `luma-control` 镜像。移除 `--dry-run` 后应用。命令保存带时间戳的配置备份，仅修改精确匹配的 A 记录内容，沿用当前运行镜像进行协调，再检查 `/v1/health` 和 `/dashboard/`。

成功后，从不共享 Manager 解析缓存的网络验证普通 DNS 路径：

```bash
curl -fsS https://<control-domain>/v1/health
curl -fsS https://<control-domain>/dashboard/ >/dev/null
luma status
```

如果新 IP 直连检查成功，但普通主机名仍失败，检查权威 DNS 并等待旧记录剩余 TTL。不要仅因某个本地解析器缓存过期，就把 DNS 改回已经停用的地址。

## Sudo 失败 {#sudo-fails}

使用 sudo 执行初始化，配置免密 sudo，或设置：

```bash
LUMA_SUDO_PASSWORD=...
```

## 无法访问 Nomad server {#nomad-server-is-not-reachable}

检查：

```bash
nomad server members
ufw status
```

Nomad HTTP API 使用 `4646`，RPC 使用 `4647`，Serf gossip 使用 `4648`，均绑定 `0.0.0.0`，但 UFW 仅在 `tailscale0` 接口放行。如果 `ufw status` 未显示该接口的相应放行规则，或 `nomad server members` 为空，重新初始化以修复 agent 配置：

```bash
luma bootstrap manager --domain luma.example.com
```
