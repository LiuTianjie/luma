# Luma CLI {#luma-cli}

完整的当前命令和选项清单见[自动生成的 CLI 参考](luma-cli-reference.md)。本文介绍工作流程和运维语义。

Luma CLI 用于安装节点、连接服务提供方、渲染 Nomad job 和部署服务。底层编排器是 HashiCorp Nomad，部署单位是 Nomad job。

默认流程以控制面为入口、Nomad 为编排后端：

```text
luma deploy service.yaml -> Luma Control API -> render jobspec on manager -> sync DNS -> Nomad API (/v1/jobs) -> docker driver
```

Luma Control 负责认证和编排，将清单渲染为 Nomad jobspec，直接提交到 Nomad HTTP API。通过 `luma status`、控制台或 Manager 上的 `nomad job status` 检查部署。

## 安装 {#install}

CI runner 应安装已发布的软件包，而不是运行 Shell 安装程序：

```bash
python -m pip install "luma-infra==0.1.355"
```

软件包名称是 `luma-infra`，安装后的命令仍为 `luma`。

交互式机器使用安装程序：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

安装程序使用 GitHub 归档，而非 `git clone`。它安装到 `~/.local/share/luma/venv`，写入 `~/.local/bin/luma`，并在需要时将 `~/.local/bin` 加入 Shell 配置。可立即使用 `~/.local/bin/luma`；要使用简写 `luma`，请打开新 Shell 或执行 `exec $SHELL -l`。

安装固定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.355 sh
```

从开发检出目录运行：

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

卸载本地 CLI：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

默认保留 `~/.luma.config.json` 和 `~/.config/luma`，便于重装后继续使用本地配置和登录 context。如需一并删除：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

这只卸载本地 CLI，不删除 Docker、Nomad、Traefik、Luma Control、已部署服务或服务器端 `/opt/luma` 状态。

## CI 使用 {#ci-usage}

CI 可将 Luma 作为无状态控制面客户端使用，无需 SSH、Docker、Cloudflare、Nomad 或 `~/.config/luma` 中的文件。

PR 校验：

```bash
python -m pip install "luma-infra==0.1.355"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma validate deploy/app.yaml --format json
luma deploy deploy/app.yaml --dry-run --format json
```

主分支或发布部署：

```bash
python -m pip install "luma-infra==0.1.355"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma status --format json
luma deploy deploy/app.yaml --format ndjson --timeout 3000
```

控制上下文优先级依次为 CLI 参数、环境变量、本地登录 context。CI 常用变量：

- `LUMA_CONTROL_URL`
- `LUMA_DEPLOY_TOKEN`
- `LUMA_INSECURE=true|false`
- `LUMA_RESOLVE_IP`
- `LUMA_CONTROL_CONTEXT`（选择已保存的集群，不改变当前 context）

`LUMA_RESOLVE_IP` 保留 `Host` 请求头中的 Control 主机名，并要求启用不安全 TLS 模式。

支持共享 Control 选项的命令可通过 `--control-context CLUSTER` 为本次调用选择已保存 context。显式参数覆盖环境变量，环境变量覆盖所选 context。显式 Control URL 与保存端点不同时，不会继承原令牌、TLS 或 IP 覆盖设置，需要提供目标端点凭据。`doctor`、`service restart` 和 `service remove` 也遵循此规则，无需预先登录即可在 CI 中运行。

交互登录可隐藏输入令牌，或从标准输入读取：

```bash
luma login https://luma.example.com
printf '%s' "$LUMA_DEPLOY_TOKEN" | luma login https://luma.example.com --token-stdin
luma context list --format json
luma doctor --control-context staging --format json
```

`login` 先接受 `--token` 或 `--token-stdin`，再回退到 `LUMA_DEPLOY_TOKEN`，最后才是隐藏的交互提示。非交互登录未提供令牌会失败并给出设置提示。优先用环境变量或标准输入，避免令牌出现在进程参数中。

声明支持 `--format` 的命令可输出 `text`、`json`、`ndjson`；除响应内容外还应检查退出状态。Doctor 返回 `healthy` 布尔值和各项检查，不健康时非零退出。流式服务日志支持文本和 NDJSON，快照支持 JSON。

## Git 提供方凭据 {#git-provider-credentials}

仓库导入可使用保存的 GitHub/Gitea 凭据，无需在单次仓库 URL 中携带令牌。令牌只写不读，仅注入 Builder 获得短期授权的克隆任务。

```bash
printf '%s' "$GITHUB_TOKEN" | luma git-provider set github personal --username octo --token-stdin

printf '%s' "$GITEA_TOKEN" | luma git-provider set gitea lin \
  --base-url https://gcode.example.com \
  --username lin \
  --token-stdin
```

列出账户并查询仓库：

```bash
luma git-provider list
luma git-provider repos gitea:lin
luma git-provider refs gitea:lin acme/app
```

导入时使用所选提供方账户：

```bash
luma import --provider-id gitea:lin --repository acme/app --build-node builder --env .env
```

公共 GitHub 仓库的位置参数也可写成 `owner/repo`，Luma 展开为 `https://github.com/owner/repo.git`。Gitea/自托管 Git 请使用完整 URL 或 `--provider-id ... --repository ...`。

## 配置 {#configuration}

`luma.yaml` 是项目配置的唯一来源：

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    edgeTarget: 203.0.113.10

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - nomad-server
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/liutianjie
  stackRoot: stacks
  routesRoot: routes
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
  engine: nomad
```

密钥不进入 Git。正常使用时直接运行所需命令：

```bash
luma bootstrap manager --domain luma.example.com
```

本地缺少必需值时，Luma 会先提示填写，以 `0600` 权限保存到 `~/.luma.config.json` 后继续。工作节点在以下操作时也会如此：

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

可通过 `luma configure --role manager|worker` 预先编辑本地密钥，`luma configure --show` 仅列出已配置键名而不显示值。Luma 自动加载 `.env` 和 `~/.luma.config.json`；`--env-file <path>` 指定其他本地环境文件，`--no-env` 禁用本地密钥加载。Shell 已导出的变量优先。Manager 初始化与更新会把必需的 Cloudflare 值保存在 Control 状态（旧数据导入后为 `control.sqlite3`），客户端无需这些密钥。存在 `CLOUDFLARE_API_TOKEN` 但缺少 `providers.dns` 时，初始化和 `luma update manager` 根据 Control 域名推导 Cloudflare zone，在安装 `/opt/luma/luma.yaml` 前写入配置。没有边缘 DNS 目标时，交互初始化询问 `LUMA_DNS_EDGE_TARGET`；非交互更新使用已配置边缘节点公网 IP 或已有的该变量。

## 命令 {#commands}

初始化配置：

```bash
luma init
```

检查本地依赖与 `.env`：

```bash
luma preflight
```

从任意已登录客户端查看控制面与集群状态：

```bash
luma status
```

`luma status` 输出 DNS 就绪状态、编排器 Nomad 及其 server leader，以及 Control 状态中已注册、`role=client` 的 Luma 节点。

保存私有仓库镜像拉取凭据：

```bash
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
luma registry remove ghcr.io
```

部署集群内镜像仓库，供源码构建使用：

```bash
luma registry serve --node build-1
```

`luma registry serve` 在有 `docker-build` 能力的节点部署 `registry:2`，并为所有就绪的非 Manager Linux 节点配置 `insecure-registries`，使其能通过 Tailscale 拉取构建镜像。BuildKit 和目标节点都使用可达的 Builder Tailscale 端点 `<build-node-tailscale-host>:5000`；不要再配置已移除的 `localhost:5000` 推送端点。可选参数：`--port` 默认 `5000`，`--storage-class` 默认 `local`，`--image` 默认 `registry:2`，`--name` 默认 `luma-registry`，`--timeout` 默认 `1800`。完整源码到镜像流程见[使用手册](how-to-use-luma.md)的仓库导入章节。

同一控制面也提供网页控制台：

```text
https://<control-domain>/dashboard/
```

在可信浏览器输入管理令牌，查看就绪状态、节点、服务和推导的流量路径。

Luma 提供两类用户令牌：

- **管理令牌**：供可信 CLI 客户端和控制台使用，适用于 `luma login`、控制台登录、部署、存储、密钥、镜像仓库及节点操作。
- **节点加入令牌**：供加入集群或刷新本地 agent 的服务器使用，适用于 `luma node join`，以及无 agent 元数据的旧节点上的 `luma update --control-url ... --token ...`。

每个节点的 agent 凭据由内部自动安装。用户应通过 `luma node status` 检查状态，无需复制或管理 agent 凭据。

仅列出本地 `luma.yaml` 中的节点：

```bash
luma node list
```

直接在 Manager 服务器执行初始化：

```bash
luma bootstrap manager --domain luma.example.com
```

对于 `single-node`，会安装 Docker，按配置连接 Tailscale，安装并启动 Nomad server，应用节点 `meta`，以 Nomad job 部署 Traefik 和 Luma Control，配置防火墙及出网。Manager 需要代理拉取 Control 镜像时，先设置 `EGRESS_SUBSCRIPTION_URL`。中国大陆 Manager 使用默认 GHCR 镜像时不应跳过出网。

命令实时输出进度：

```text
[start] Install Nomad server
[ok] Nomad server ready
[start] Deploy Luma Control
[fail] Deploy Luma Control
  Fix: Re-run luma bootstrap manager after fixing the error
```

只有可直连 Control 仓库，或 `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` 指向可拉取仓库时，才跳过出网：

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

从任意客户端登录：

```bash
luma login https://luma.example.com --token <management-token>
luma context list
luma context use <cluster-id>
```

在每台新增服务器上执行加入：

```bash
luma region create batch-a --egress proxy
luma region list
luma node join https://luma.example.com --token <node-join-token> --region cn --name cn-worker-1
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma node join https://luma.example.com --token <node-join-token> --region home --name home-mac-mini
luma node join https://luma.example.com --token <node-join-token> --region batch-a --name batch-a-01
```

`--region` 指定调度池，内置 `cn`、`global`、`home`。使用 `luma region create <name> --egress proxy|direct`（节点页也可操作）创建更多区域，再将机器加入该区域。自定义区域默认仅内部工作负载（`exposure: none`）。`--egress proxy` 与 `cn`/`home` 一样，在加入和拉取镜像时使用 Manager 网关。

`--name` 是 `luma status` 和服务清单使用的 Luma 节点名，写入 Nomad client 的 `meta.luma_node_name`，用于固定调度。Nomad 节点身份是稳定 UUID，同名重新加入不会使固定服务失效。`--engine nomad` 可显式指定默认的 Nomad client 流程。

升级旧节点后刷新已加入的 agent：

```bash
luma update --control-url https://luma.example.com --token <node-join-token>
```

更新所有已注册且 agent 就绪的节点：

```bash
luma update fleet
luma update fleet --install-ref v0.1.355 --timeout 900
luma update fleet --include-manager
```

`--install-ref` 接受版本标签、分支或完整 40 字符 Git commit。协调候选版本发布时应使用完整 commit，避免分支移动导致 Manager 与节点 agent 解析到不同版本。

批量更新通过节点 agent 执行：更新就绪的非 Manager 节点 CLI，再刷新本地 agent 服务和 Tailscale 看门狗。默认跳过 Nomad server（Manager），请在 Manager 主机上单独运行 `luma update manager`。`--include-manager` 用于显式修复，常规批量更新应避免影响活动控制面。Agent 太旧、未声明 `luma-update` 时会跳过；先在节点执行一次 `luma update`，后续即可加入批量更新。

Manager 的 `/opt/luma/control` 仅 root 可访问。运维账户无免密 sudo 时，应提权执行应急 CLI 更新（如 `sudo ~/.local/bin/luma update manager`），或通过 `luma configure --role manager` 配置 `LUMA_SUDO_PASSWORD`。无法访问时，`luma update` 不应回退到用户的客户端 context。旧版本可能走错路径，并因客户端令牌过期返回 401；修改凭据前应先恢复对 Manager 状态的访问。

推荐通过控制台更新中心更新 Manager。它先把所选 Control 镜像镜像化到内部 Registry，再启动持久化的 Manager 操作。直接 CLI 更新会自行拉取镜像；无法访问 GHCR 时，通过 `LUMA_CONTROL_IMAGE` 指定可拉取的内部镜像，或使用控制台流程。

恢复公网 IPv4 已改变的 Manager：先在 Manager 主机上预览，再移除 `--dry-run` 执行：

```bash
luma manager ip-change \
  --old 8.147.65.253 \
  --new 8.145.62.128 \
  --domain luma.itool.tech \
  --dry-run

luma manager ip-change \
  --old 8.147.65.253 \
  --new 8.145.62.128 \
  --domain luma.itool.tech
```

命令通过 HTTPS 验证新地址，并保留 Control 主机名用于 SNI 和证书检查。只修改 Manager 节点 `publicIp`、`providers.dns.edgeTarget`，以及内容精确等于旧地址的 Cloudflare A 记录。备份 `luma.yaml`，复用运行中 `luma-control` Nomad job 的镜像，协调控制面，不重装 CLI 或重新部署用户应用。不会全局替换 Control 状态，历史事件内容不变。操作幂等，可重新执行以完成 Cloudflare 部分失败遗留的记录。

排空本地 Nomad client，并可选从控制面注销节点：

```bash
luma node exit --endpoint https://luma.example.com --token <management-or-node-join-token> --name home-mac-mini
```

从任意已登录客户端移除节点：

```bash
luma node remove home-mac-mini
```

控制面删除 Luma 注册记录，再在 Manager 上排空匹配的 Nomad client。匹配依据是保存的 Nomad 节点 ID、`meta.luma_node_name` 或节点名。命令拒绝移除 Nomad server（Manager）。

连接 Cloudflare 并写入 `providers.dns.zoneId`：

```bash
luma cloudflare connect --zone example.com
```

修复或刷新出网网关：

```bash
luma egress setup
luma egress refresh
```

安装/登录 Tailscale：

```bash
luma tailscale connect
```

Manager 和已加入节点在初始化/更新时安装轻量 Tailscale 看门狗。Manager 检查 Tailscale 对端和 Nomad gossip/RPC TCP 可达性；节点检查 Manager Tailscale 与 Nomad server 端口。连续失败后重启本地 Tailscale，旨在恢复 tailnet TCP 卡顿，而不重启 Docker、Traefik、Nomad agent 或应用 job。

交互生成服务清单：

```bash
luma service new
```

校验并渲染：

```bash
luma validate examples/public-cn-service.yaml
luma render examples/public-cn-service.yaml
luma render examples/public-cn-service.yaml --engine nomad
```

`luma render` 在本地渲染；`--engine nomad` 强制使用 Nomad jobspec 渲染器，这也是当前集群默认值。

通过控制面部署：

```bash
luma deploy examples/public-cn-service.yaml
```

回滚或查看已部署服务版本：

```bash
luma history public-cn-service
luma rollback public-cn-service
luma rollback public-cn-service --to-version 3
```

`luma history` 列出 Nomad job 历史版本（`GET /v1/job/<id>/versions`）。`luma rollback` 回退上一版本，或 `--to-version N` 指定的版本（`POST /v1/job/<id>/revert`）。控制台 **应用 → 版本**提供相同操作。Jobspec 也渲染 `update { auto_revert = true }`，新版本健康检查失败时自动回滚。

回滚仅改变运行中的 Nomad job，不改写 Git、不更新 Control 已保存清单、不回滚数据库或恢复卷。生产回滚应使用不可变镜像标签或 digest，避免 `latest`。

搜索保留的 Control 构建与部署尝试：

```bash
luma service history public-cn-service --kind deployment --status failed --limit 50 --format json
luma service history --source dashboard --since 2026-09-01T00:00:00+08:00 --format json
luma service history --id RECORD_ID --kind deployment --limit 50 --format json
luma build list --app public-cn-service --status failed --limit 50 --format json
luma build logs BUILD_ID --limit 50 --format json
```

`service history [NAME]` 搜索构建及部署记录。可按类型（`build`/`deployment`）、来源（`build`/`cli`/`dashboard`）、状态、应用、`--since` 和 `--until` 筛选。时间接受 Unix 秒或带时区的 RFC3339。默认每页 50，最多 100；保持其他筛选不变，把返回的 `nextCursor` 作为 `--cursor`。JSON 包含 `limit`、`nextCursor`、`hasMore`，文本模式将续页信息写到 stderr。分页读取更多已保留记录，不增加保留期。

`service history --id ID --kind KIND` 分页读取单条记录步骤，不能与列表筛选组合。`build logs ID` 也按从旧到新分页。这些是构建/部署执行事件，应用 stdout/stderr 属于 `service logs`。`luma history NAME` 仍是用于回滚的 Nomad job 版本列表，与 Control 尝试历史分开。旧状态导入无法恢复迁移前已裁剪记录。保留和备份边界见[控制面存储](control-storage.md)。

操作前先检查服务：

```bash
luma service list --region cn --format json
luma service inspect public-cn-service --format json
luma service events public-cn-service --format json
luma service logs public-cn-service --tail 100
luma service logs public-cn-service --allocation ALLOCATION_ID --previous
luma service logs public-cn-service --follow --format ndjson
```

`list --stack NAME` 限定到一个 stack。日志限定所选部署，对比副本可用 `--allocation`。`--tail` 接受 1–500 行总预算，由所选来源共享。文本输出用 `[allocation/task/stream]` 标记来源，`[partial]` 表示未完片段，`[continued]` 表示续片，即使其他来源交错也保留标记。这是诊断视图，不是字节精确导出；JSON/NDJSON 可保留来源、游标和片段元数据。运行时仍保留旧实例时，`--previous` 可读取它。Follow 模式在正常 EOF、传输失败或临时 HTTP 失败后，从最近行或心跳字节游标恢复；重连从 0.5 到 15 秒指数退避，最多连续 8 次无进展重连。Ctrl-C 停止。认证、游标无效和协议错误立即失败；旧 Control 不提供恢复游标时直接失败，不会重放快照。没有 `--since` 参数，因为应用时间戳不保证可解析，CLI 不声称具备服务端时间筛选。

不重新部署，直接重启运行服务：

```bash
luma service restart public-cn-service
luma service restart my-stack --service web --mode task
```

`--mode recreate` 重新调度 allocation，`--mode task` 原地重启任务。省略模式时，整个 stack 默认 `recreate`，通过 `--service` 指定单任务时默认 `task`。运行时操作后，Control 协调已保存部署的路由/DNS，并探测公共 HTTP 服务；Compose 协调全部暴露服务。拒绝重启系统 stack `traefik`、`egress`、`luma-control`。详见[运维操作](operations.md)。

删除已部署服务：

```bash
luma service remove public-cn-service
luma service remove public-cn-service --dry-run
```

检查认证、远程 Control 状态、DNS 配置就绪、节点 agent 和 Nomad 健康（不审计本地 Docker/操作系统环境）：

```bash
luma doctor
luma doctor --deep
```

`--deep` 额外评估 Control 状态中的远程节点诊断（Docker 镜像/代理和 Nomad 配置）。DNS 就绪只表示配置存在，不能证明公网解析或应用可用。

## 服务清单 {#service-manifest}

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

必填字段：

- `name`
- `image`
- `region`：`cn`、`global`、`home` 或 `luma region create` 创建的自定义区域。
- `exposure`：`cn-edge`、`tailscale-relay`、`tcp-relay`、`cloudflare-tunnel`、`external-edge` 或 `none`。

公共服务还需要：

- `domain`
- `port`

可选字段：

- `engine`：`nomad`，选择服务编排后端，省略则继承集群默认值。
- `node`：`luma node join --name` 的节点名，将服务固定到一个节点；渲染为 `${node.unique.name}`（或 `meta.luma_node_name`）约束，并保留区域约束。
- `env` / `environment`
- `command`
- `constraints`
- `labels`
- `networks`
- `proxy`：为 `true` 时运行流量走出网代理；Luma 附加代理和默认代理环境变量，调度仍按 `region`。
- `resources`：渲染为 Nomad task 的资源块。CPU 使用 `reservations.cpus`（默认 100 MHz）作为弹性调度份额；旧 `limits.cpus` 仅警告并忽略，不作为硬上限。内存预留映射到 `memory`，限制映射到 `memory_max`；只设限制时预留 min(256 MiB, limit)。注册此类 job 前，Control 自动启用 Nomad 内存超卖，使声明限制成为实际容器硬上限。
- `stackPath`
- `routePath`
- `dns.target`
- `dns.type`
- `dns.proxied`
- `publishPort`
- `relay.host`：可选 tailscale-relay 上游覆盖，通常省略。
- `relay.url`：可选完整 tailscale-relay 上游 URL 覆盖，通常省略。
- `tcp-relay` 根据 `publishPort` 或 `port` 自动推导 Traefik TCP 入口。
- `tunnel.tokenEnv`

需要 Luma 出网代理的工作服务示例：

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
```

小型 Manager 上限制资源的服务示例：

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: none
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

## 部署顺序 {#deploy-order}

执行 `luma deploy service.yaml` 时，Luma 依次：

1. 解析并校验服务清单。
2. 如提供 `--env <file>`，在本地解析 `.env`，只保留清单以 `${NAME}` 引用的变量。
3. 从 `~/.config/luma` 读取当前登录 context。
4. 向 Manager Control API 提交清单与筛选后的应用作用域密钥。
5. 按服务 `name` 保存密钥，在渲染前解析 `${NAME}`。
6. 在 Manager 渲染 `stacks/<region>/<service>/<service>.nomad.json` jobspec。
7. 为 `tailscale-relay` 或 `tcp-relay` 渲染 `routes/<service>.yml`。
8. 创建或更新 Cloudflare DNS，除非明确跳过。
9. 通过 `PUT /v1/jobs` 创建或更新 Nomad job。
10. 探测 `cn-edge` 和 `external-edge` 服务的公共路由。

客户端在提交前、等待 Control 时及每个 Control 步骤中输出进度。Luma 验证生成的 Traefik 路由，在监视目录外暂存，再原子发布。公共路由探测报告 `/` 的 HTTP 状态：应用 404 表示路由可达但可能无根页面，Traefik 默认 `404 page not found` 则视为缺少 router 和路由失败。缺少 router 或短暂 `502`/`503`/`504` 时，Control 会重建一次 allocation 并重新探测，再判失败。单服务和 Compose job 给冷镜像获取 30 分钟、滚动进度 40 分钟；默认部署响应超时为 3000 秒，让 Control 等待超过有界 Nomad 窗口。可用 `--timeout <seconds>` 覆盖。

部署是 upsert。同名重复 `luma deploy service.yaml` 更新现有 Nomad job（ID 是服务 slug），不创建副本。当前渲染 jobspec 为更新依据，Nomad 保留上一版，可通过 `luma rollback` 或控制台 **应用 → 版本**回退。

项目已有部署环境文件时，用 `luma deploy service.yaml --env .env`。作用域密钥按服务名隔离，`api/DATABASE_URL` 与 `worker/DATABASE_URL` 是不同值。只有应用没有作用域密钥时，才使用旧全局 `luma secret set NAME` 的值。

源码到镜像部署的 `luma import` 使用同样的作用域密钥模型：

```bash
luma import https://github.com/acme/app --build-node builder --env .env
luma import acme/app --build-node builder --env .env   # GitHub owner/repo shortcut
```

共享 Builder 较慢时，可在自己电脑构建当前检出，并将产物保留在同一个 Luma 项目：

```bash
cd app
luma build local . --env .env
```

`luma build local` 根据检出目录的 `origin` 推导项目身份（没有 origin 时用 `--repo-url`），在 Control 预留项目，通过本地 Docker Buildx 构建，推送到与 `luma import` 相同的 `owner/repository` 命名空间，再走正常部署。电脑必须能访问 `build.registryHost`，认证仓库需先 Docker 登录。支持单服务清单和 Compose sidecar，以及 `--compose-sidecar`、`--platform`、`--context`、`--dockerfile`。可用 `--builder <name>` 复用支持所需平台或仓库/镜像配置的本地 Buildx builder。`--proxy <url>` 为本地基础镜像和 Dockerfile 网络访问指定 HTTP 代理，内部仓库保留在 `NO_PROXY`。本地构建和 Builder 导入都根据目标节点或区域内就绪节点推导容器架构：Darwin/ARM 构建 `linux/arm64`，同时有 amd64 与 arm64 的区域构建多平台镜像。显式 `--platform` 必须覆盖全部已解析目标架构。

Control 声明 `build-queue-v1` 后，CLI 自动将仓库导入和 `build retry` 提交到每项目持久化 FIFO。本地构建使用 Control 分配的唯一标签，可并发构建上传；**上传完成后**，部署加入同一 FIFO。队列按提交顺序（本地构建为上传完成顺序），而非本地构建开始顺序。同一仓库项目一次只执行一个排队操作；其他项目可使用其他槽位，但仍受 Builder 容量和运行时部署锁约束。失败或取消不会丢弃后续任务。

等待时 CLI 显示构建 ID、队列位置及阻塞任务。`--timeout` 只限制客户端等待，服务端接受后关闭 CLI 不会取消任务。用 `luma build logs <id>` 查看，用 `luma build cancel <id>` 取消等待任务。队列可跨 Control 重启保留；被中断的活动任务明确标为失败，不自动重放部署副作用，重试前先检查运行时。未完成上传的本地构建仍依赖调用者电脑。每次尝试的环境值只在排队/执行期间存入私有 Control 状态，与其他尝试和公开构建历史隔离。

旧 Control 保留之前的活动构建冲突即失败限制，需同时升级 Control 和 CLI 才能排队。预构建镜像的 `deploy` 和 `compose deploy` 仍使用同步运行时锁。镜像超出预留项目仓库/标签范围的本地上传仍会被拒绝。

或使用保存的 Git 提供方账户：

```bash
luma import --provider-id gitea:lin --repository acme/app --ref main --build-node builder --env .env
```

Import 自动发现单服务清单（`.luma.yml`、`luma.yml`、嵌套 `*.luma.yml`）与 Compose sidecar（`luma.compose.yml`、`.luma.compose.yml`、`*.luma.compose.yml`、`*.compose.luma.yml`、`docker-compose.luma.yml`）。Compose 文件名不参与单服务匹配，因此 `docker-compose.luma.yml` 会按 Compose 处理。

仓库还没有部署文件时，可从 CLI 提供：

```bash
luma import --provider-id github:personal --repository acme/app \
  --build-node builder \
  --manifest deploy/app.luma.yml \
  --env .env
```

与普通 `luma deploy` 不同，import 可能在 Builder 克隆后才发现最终清单。因此 CLI 将 `.env` 值发送给 Control，再由控制面仅保留最终清单或 Compose 引用的值，存入最终服务/stack 作用域。

对于 Compose 仓库，`luma import` 构建仍有 `build:` 的服务，并在部署前注入产出的 `image:`。普通 `luma compose validate` 和 `luma compose deploy` 不构建；本地检查该流程请用 import 模式校验：

```bash
luma compose validate --import-mode luma.compose.yml
```

仓库有多个部署 sidecar 时，请显式选择克隆仓库内的 Compose sidecar，不依赖自动发现：

```bash
luma import https://github.com/acme/platform.git \
  --ref v1.4.0 \
  --build-node builder \
  --compose-sidecar deploy/staging.luma.compose.yml \
  --env .env
```

`--compose-sidecar` 仅接受规范的 POSIX 仓库相对路径，不能与 `--manifest` 同用。CLI 构建前要求 Control 支持对应能力；Control 要求 Builder 回传同一路径；Builder 拒绝绝对路径、`..`、缺失/无效 YAML 和符号链接逃逸。显式选择不会回退到自动发现。任一侧缺少能力时先更新 Manager 和 Builder agent。

`--dry-run` 本地渲染，不提交部署。本地无法读取可选集群上下文（节点/存储元数据）时，JSON 包含 `validationMode: "degraded"` 和警告，文本输出 `[warn]`。`--skip-dns` 与 `--skip-orchestrator` 会传给 Control API。控制面部署模式已弃用 `--commit`、`--push`。

Luma 在外部操作前记录部署状态。成功标记为 `active`；若前面已改变 Manager，而后续 DNS、Nomad 提交、路由渲染或探测失败，则保留 `status: failed_partial`，供控制台和 `luma service remove <name>` 找到部分生效的 job。

`luma service remove <name>` 根据 Control 上次成功部署记录的清单，移除对应单服务或 Compose slug。此清单是依据，因此即使客户端没有 YAML，也能删除和清理网页创建的部署。默认删除 Luma 管理的 Cloudflare DNS，注销并清除 Nomad job，删除生成的 `stacks/<region>/<service>/<service>.nomad.json` 或 `stacks/compose/<name>/<name>.nomad.json`，以及 tailscale-relay/tcp-relay 路由文件。`--dry-run` 预览，`--skip-dns` 保留 DNS；只有有意仅删 Luma 文件而不停止 job 时，才用 `--skip-orchestrator`。默认保留存储数据；`--delete-storage` 删除记录中声明且可移除的存储。单服务清理 `storage.<volume>.path` 指向的托管路径与 `data:/data` 等 Docker 命名卷，跳过 bind mount；Compose 清理 sidecar 中的托管路径。`--delete-storage` 不能与 `--skip-orchestrator` 同用。`cloudflare-tunnel` 公共主机名仍由 Cloudflare Zero Trust 管理，Luma 会报告跳过该清理。

## 自动构建/部署流程检查 {#automatic-builddeploy-workflow-checks}

Luma 在 Control 记录每个应用成功的 CLI 构建/部署流程，跨机器和 agent 共享，独立于检出目录及有界构建历史。已有应用无需回填。

在 `luma import`、`luma build local`、`luma build retry`、`luma deploy` 或 `luma compose deploy` 开始构建、预留上传或提交部署前：

- 无记录：正常继续，成功后创建记录。
- 匹配记录：正常继续并刷新成功记录。
- 流程不同：显示差异，确认后才能构建/部署。包括远程/本地/镜像/Compose 方式、显式 Git 仓库/ref、builder、平台、上下文/Dockerfile、sidecar、区域、入口/域名/端口、显式环境文件路径和网络选项。输出格式、超时、凭据和说明备注不触发变化。

交互文本终端询问 `Confirm this workflow change and deploy? [y/N]`。JSON/NDJSON、quiet 和非交互调用者非零退出。Agent 必须展示差异并获得用户批准，才能用 `--accept-workflow-change` 重试，不得自行添加该标志来消除错误。CI 也只能在流程变更获批后使用。

```bash
# Normal deployment: automatic check and recording; no extra flag needed.
luma import --provider-id github:me --repository acme/app --ref main --build-node builder

# Read the shared record, including the command and last recorded success.
luma workflow show app
luma workflow list --format json

# Add context to the record on the next successful deployment.
luma import acme/app --ref main --workflow-note 'Build on Builder; this project requires remote network access'

# Only after the user has approved the displayed differences:
luma build local . --platform linux/amd64 --accept-workflow-change

# Explicitly record/edit a workflow without executing it.
luma workflow record app --note 'Release from the main branch on Builder' -- \
  import acme/app --ref main --build-node builder

# Execute the recorded command from a checkout, using current login credentials.
luma workflow run app --path /path/to/app
```

记录保留参数数组、方式、可选备注及最后成功配方/证据。手动编辑标为 `manual`；旧成功保留原配方，不能证明修改后的命令已执行。失败、中断流、dry-run 和 `--skip-orchestrator` 不覆盖成功记录。部署成功但保存流程失败时，CLI 报告成功并附 `workflow.saved: false` 和警告；不要为了修复记录错误盲目重新部署。并发编辑会保留。

清单标识服务/Compose 目标。Import 从仓库或 monorepo 中所选 sidecar 查找记录；匹配多个应用时用 `--workflow-app APP` 选择。不同应用名有独立记录。仓库相对路径可随检出移动；外部绝对配置/环境路径必须在下一台机器存在。配方不保存环境文件内容、管理令牌或 URL 凭据。自由文本备注由用户撰写，不要写入密钥。重试记录原构建参数和 `build retry ID`，重放要求原构建记录仍存在。检查比较的是 CLI 流程参数，不是源码变更、镜像标签或清单内容。

CLI 和 Control 都必须支持 `deployment-workflow-v1`，请先升级 Control。Control 不可达或太旧意味着检查不可用，不等于没有记录，因此部署会停止并说明原因。该流程是 CLI 协调机制；旧客户端及直接 API/控制台部署不参与这些 CLI 检查。