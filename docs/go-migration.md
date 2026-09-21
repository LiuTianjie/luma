# Luma Go 迁移文档

状态：实施中的迁移文档，尚不能用于生产切换。完整重构、验证和发布完成后，
本文件将补齐实际版本、安装命令、兼容矩阵和验证记录，作为最终交付。

## 目标和范围

- Luma CLI、Node Agent 和 Control 重构为 Go，保留 Nomad 和现有 Dashboard。
- 最终 Luma 运行链路不依赖 Python，完成替换后移除旧 Luma Python 实现及其安装依赖。
- 去掉 NFS 能力。需要存储的服务使用部署节点上的本地目录或本地 Docker 卷。
- 服务部署、数据归属和恢复使用同一个节点标识，取消独立存储服务器和 storage class。
- 保留现有认证角色、部署结果、任务取消和重启恢复语义。
- `lae/` 不整体重写，但 Luma 向 LAE 提供的存储接口及相关部署配置必须适配本地存储。

目标没有完成前，不能将 Go CLI 的局部联调或 Agent 构建通过解释为 Control 已可替换。
实现边界和剩余模块见 [Go 架构文档](go-architecture.md)。

## 当前可验证的入口

```sh
bash scripts/build-go.sh .local-notes/go-bin/luma-go
bash scripts/build-go.sh .local-notes/go-bin/luma-agent-go luma-agent
bash scripts/build-go.sh .local-notes/go-bin/luma-control-go luma-control
.local-notes/go-bin/luma-go version --local
.local-notes/go-bin/luma-agent-go --version
.local-notes/go-bin/luma-control-go --version
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

这些命令只构建本地产物和运行检查，不安装服务或切换线上节点。
`cmd/luma-control` 已提供身份验证、历史查询、工作流记录和独立的 Agent 任务写入模式；
完整部署/构建控制面、安装/升级流程和最终发布包尚未交付。
完整线上功能仍由 Python Control 执行；Go Control 现可在隔离环境中运行内部服务部署，
见下节。不能同时启动两个 Control 写同一状态库。

### 内部服务实际部署验证

在上面的 Agent 任务写入模式中，额外指定 `--nomad-url http://127.0.0.1:4646`
可启用内部服务部署，地址必须指向隔离测试 Nomad。`--rollout-timeout 10m`
控制等待时间。该模式会真实提交 Nomad job，不能使用生产地址做影子验证。

已接入原生与 Compose 的普通、NDJSON 流式和 preview 接口。Control 从已有部署记录、
Nomad 挂载及 allocation 确定本地数据节点，以稳定的 Nomad node ID 固定调度，
将节点和挂载源写回 schema 1；preview 不提交 job、不写部署记录。
在线 Compose 可以省略本地路径的节点，由 Control 确定；离线渲染仍须明确节点。

不配置 YAML 时支持 `exposure: none` 的已有镜像部署。额外指定
`--config /srv/luma/luma.yaml`（或 `LUMA_CONTROL_CONFIG`）可启用 `cn-edge` 和
`external-edge`：加载现有渲染/DNS 配置，写入 jobspec，同步 Cloudflare DNS，
等待 Nomad 健康后清理同名旧入口文件并探测域名。`--skip-dns` 保留已有 DNS；
Go 不创建静态 edge 路由，Traefik 继续通过 Nomad provider 发现实际 allocation。

DNS 凭据沿用配置中的 `apiTokenEnv`/`zoneIdEnv` 名称，优先读取 Control 状态中的
全局 Secret，再使用进程环境；不会将凭据写进部署历史或 preview。
`--route-ca-file /secure/private-ca.pem` 可用于内部 CA，仍校验证书及主机名。
公网探测失败与 Nomad allocation 健康分开报告，`inconclusive` 不代表公网验收通过。

构建工作流、组件安装和管理生命周期仍未接齐；
这些功能请求不会跳过相应步骤后报告成功。
所有功能完成前不替换生产 Control。

已用 Go CLI、TLS Go Control 和隔离的真实 Nomad 1.9.7/Docker 验证原生服务创建、
原生服务更新及单服务 host-network Compose 部署。容器实际读取了原有测试数据，
更新前后保持同一个节点和挂载源，部署状态由 Python 成功读回。
这里的“Python 读回”仅指开发阶段用旧实现验证 SQLite 格式兼容；Go 部署过程
没有调用 Python，也不意味着最终会混合运行两套 Control。
这证明该内部部署路径可运行，尚不覆盖完整入口、构建或生产切换。

另已用隔离 Nomad 1.9.7、Docker、CNI 1.6.2 和 Traefik 3.1 验证原生及 Compose
`cn-edge` 部署：通过真实入口请求读取到了测试容器挂载的指定文件内容。
入口使用隔离环境的自签证书并以测试 Host 请求验证，没有变更公网 DNS 或申请生产证书。
Control 自身探测的 TLS/主机名校验另外由专用 CA 联调覆盖；Cloudflare 创建/更新由
本地 HTTP API 契约测试覆盖，尚未进行生产 Cloudflare 写入。

### Relay 和固定端口部署

Go Control 已接通原生服务及 Compose 的 `tailscale-relay`、`tcp-relay` 和
固定 `publishPort`。路由使用解析后的部署节点地址（优先 Tailscale），也支持显式
upstream；没有节点或显式 upstream 时在提交前报错。Compose 中其他服务或本地卷
带来的节点绑定同样适用于入口服务；macOS 上不支持的 Compose bridge 入口会明确拒绝。

TCP 入口端口在整个集群内共享：预览检查既有 active/pending 部署，提交事务再次检查，
同一部署更新可以保留自己的端口，跨原生/Compose 部署不能重复占用。80/443 保留给
HTTP/HTTPS。新端口通过 `ingressRefreshPorts` 和 warnings 提示需要刷新 Traefik
静态 entrypoint 与防火墙；动态路由文件写入不等于静态入口已经创建。完整 Go manager
更新流程尚待接通，当前不能据此进行生产切换。

Linux Go Agent 增加 `nomad-cni-repair` / `repair-nomad-cni-hostports`，仅在具备
特权、Docker 和 iptables 时提供。Control 在 rollout 后按当前 allocation 所在节点
派发端口清理；Agent 只删除指定端口上位于活跃 allocation 规则之前的陈旧重复规则，
不重启 Docker。结果独立保存在 `networkPrepared`，能力缺失或清理失败会报告 skipped。
HTTP 路由继续做 HTTPS 探测；TCP 路由探测真实 TCP 端口，连接失败报告 inconclusive，
不会把它当作公网可用证据。

隔离实测使用 Nomad 1.9.7、Docker、CNI 1.6.2、Traefik 3.1 和真实 Linux Go Agent，
原生服务与 Compose 的固定端口转发均返回了挂载文件中的精确内容。测试环境预先配置
了 Traefik 静态 TCP 入口。Agent 实测完成 CNI 检查（该轮没有陈旧规则），规则删除范围
另由执行器测试覆盖；不将这项结果解释为 manager 安装/更新已经完成。

Go 还修正旧 Compose 渲染器对 `exposure: none` + `publishPort` 静默忽略的问题：
现在会实际生成端口映射。这意味着该配置会按声明开放宿主机端口；只需内部访问的服务
应不设置 `publishPort`。对照测试只对这个明确差异调整预期。

### 前置检查和本地目录准备

Go Control 已接入 `requirements.capabilities/secrets/init/notes`。原生和 Compose
预览返回区域容量、Cloudflare、Tailscale、出口代理、Secret 和 Registry 的检查结果，
只包含配置状态及缺失项，不返回凭据值。CLI 强制检查显式依赖；Dashboard 来源的部署
同时检查推导出的依赖，并要求 Cloudflare/出口代理的 setup check 为已验证状态。

`requirements.init` 沿用原有的声明语义：验证对应前置条件，实际 DNS、镜像及运行时
操作由各部署步骤完成。它不是组件安装命令；组件配置、安装和管理 API 仍在后续迁移范围。

Compose 的 `volumes.NAME.local.path` 由服务所在节点的 Go Agent 准备。节点必须支持
`managed-volume-path` 和 `prepare-managed-volume-path`；Control 不在管理节点上
代建远端目录。目录准备完成且返回路径吻合后，才提交 Nomad job。

- 缺失目录：创建为可写目录；同节点的重复路径只准备一次。
- 已有目录：保留文件、UID 和权限。
- `initialize: empty`：声明新卷意图，不删除或清空已有数据。
- `adopted: true`：保留已有目录，不绕过数据节点和挂载源绑定。
- Docker named volume：继续由运行节点的 Docker 管理；原生 bind mount 可能是文件，
  不会被自动创建成目录。

已通过真实 Go CLI、Control、Agent、Nomad/Docker 验证目录创建及连续两次 Compose
部署：容器能读取已有数据并写入新数据，更新后数据仍在，节点、挂载源、已有目录的
UID 和权限保持不变，全程未使用 NFS 或远端存储服务器。

### 仓库镜像构建执行器

Go Agent 已支持 `build-image` / `docker-build`：检出 Git 分支、标签或完整 commit，
发现原生/Compose 部署清单，按部署节点或区域架构构建，推送 commit 与 latest 标签。
Compose 可以指定 sidecar、为各服务构建镜像，并将共用同一镜像的 worker 等服务更新到
本次构建结果。构建目录和 Dockerfile 必须位于仓库内；build args 必须有明确字面值。

该能力要求节点安装 Git、Docker 和 Buildx 插件。Agent 配置旁的 `.go-build` 私有目录
记录其专属 Buildx 状态；构建结束或取消会停止专属 BuildKit 容器，保留本地构建缓存卷。
启动及后续执行前会清理未结束的专属构建。Git/Registry 凭据通过临时私有文件使用，
不进入任务结果；显式选择直连会清空继承的代理变量。

已在隔离环境用真实 Go Agent、带认证的 Git 与 Registry 验证一个原生镜像和两个
Compose 镜像，并从 Registry 导出核对镜像文件内容及共用镜像引用。旧 Control 在该项
测试中仅作为任务协议入口；Go 构建执行器没有调用 Python。

Go Control 已接通仓库构建队列、Agent 调度及构建后部署：`POST /v1/builds`（以及
`/v1/builds/stream`）接收请求，`queue: true` 返回构建 ID，之后通过现有构建历史接口
读取结果。`POST /v1/builds/{id}/cancel` 取消构建。同一项目串行，不同项目由后台 worker
并行处理；请求断开不会丢弃已接受的任务。Git/Registry 凭据只在领取任务时附加。
原生清单使用新镜像，Compose 使用 Agent 返回的内容及共用镜像映射；部署复用已有
Go 部署模块，保留节点、本地目录及卷的归属。

已用真实 Go Control 二进制和 TLS 验证原生/Compose 构建结果交接、取消前阻止部署、
凭据不落任务记录和队列清理。该项测试模拟 Agent 构建完成响应及 Nomad API，不能
代替真实 Nomad 调度验证。另外已串联真实 Go Control、Go Agent、带认证的 Git、
BuildKit 与 Registry，完成原生和 Compose 构建、镜像文件导出核对及部署结果交接；
这次联调的 Nomad 仍为 API 测试替身。

Go CLI 已支持仓库导入及基础构建管理：

```bash
luma import https://github.com/team/app.git --ref main
luma import --provider-id team-git --repository team/app --compose-sidecar deploy/luma.compose.yml
luma import https://github.com/team/app.git --proxy-mode direct --env production.env
luma build list --app my-app --limit 50
luma build logs BUILD_ID
luma build cancel BUILD_ID
luma build retry BUILD_ID --env production.env
luma workflow record my-app -- import https://github.com/team/app.git --ref main
luma workflow run my-app --path /path/to/checkout
```

导入先检查工作流，再提交一个队列任务并按 ID 等待；CLI 超时不会重新提交构建。
Agent 日志同时进入构建历史，CLI 使用分页位置增量读取。成功后由 Control 保存远程导入工作流，即使 CLI 已退出也会保存；
部署记录保存 Git 来源、版本、节点及构建 ID。同仓库的多个应用分别保存工作流。
`workflow run` 固定当前 Control 连接，只按 checkout 解析本地 manifest/env 文件，
远程仓库、构建上下文及 sidecar 路径保持原义；发现应用名不一致会在部署前拒绝。

`build retry` 从已完成的远程构建记录创建新尝试，保留原记录，并记录父尝试及最初尝试的 ID。
正在运行的构建和本地构建不能用这个入口重试。已成功构建的应用会固定原应用名，仓库中的
清单改名不会意外创建另一个应用。工作流也可以记录、执行重试命令：

```bash
luma workflow record my-app -- build retry BUILD_ID --env production.env
luma workflow run my-app --path /path/to/checkout
```

Control 的 `POST /v1/applications/update` 接受 `name`（原生服务）或 `stack`（Compose），
按部署记录中的 Git 来源重新构建；使用 `queue: true` 返回构建 ID。仅以镜像清单部署的应用
没有 Git 来源，需要通过清单更新。`/v1/builds/{id}/retry` 提供对应重试 API。
这两个入口均支持 `/stream`；非队列模式按顺序输出 Agent 日志及最终结果。

队列工作流由 Control 持久保存待执行的配方和版本号。若构建期间工作流被修改，部署仍可
成功，但结果中的 `workflow.saved` 为 `false` 并附带原因，不覆盖新配方，也不重新部署。
Git/Registry 凭据及部署 secret 值不会写入公开工作流或重试参数。

构建默认值可通过 Go CLI 查看和修改：

```bash
luma build config
luma build config --node builder-a --node builder-b --default-node builder-a
luma build config --registry-host 100.64.0.8:5000 --push-host 100.64.0.8:5000
luma build config --direct-egress-node builder-a
luma build config --clear-direct-egress
```

不带修改选项时只读当前配置；`--node` 和 `--direct-egress-node` 可重复。
节点名、显示别名或注册 ID 会转换为注册节点的正式名称；未知、歧义节点会导致整次修改
失败。构建必须选择已声明且可执行 `build-image` 的节点；节点暂时离线仍可预先配置。
`--node` 替换声明列表，未指定默认节点时使用列表首项；单独指定 `--default-node` 会将
该节点设为声明列表。节点自身已有的 builder 标签仍然有效。

省略 Registry 参数会保留原值；显式 `--registry-host=` 或 `--push-host=` 清除对应配置。
pull 地址必须可由运行节点访问，push 地址必须可由构建节点访问。保存地址会将 Registry
检查结果标为待重新检查。直连列表只改变后续构建的出口策略；清空后恢复区域策略，
`import --proxy-mode direct` 仍可单次覆盖。已有节点的直连标签不会被清空列表删除。

Control 对应 `GET/POST /v1/builds/config`，沿用 Dashboard 的现有写接口；只读 Control
仅允许 GET。已用真实 Go CLI/Control TLS 验证配置持久化和 Agent 领取到的代理参数。

Go 本地构建执行器已能直接读取本机工作目录，识别 Git 来源、未提交状态，以及
原生/Compose 的目标节点和区域。镜像路径受上传预留的项目范围约束，清单内的
`build.repo` 等覆盖项不会将本地上传写入其他项目。执行时使用临时 Docker 配置，
保留调用方的认证、credential helper、插件及 context；明确指定的 Buildx builder
由调用方管理，执行器只清理自行创建的临时 builder 和缓存。

已用真实 Docker Buildx 和认证 Registry 验证原生及 Compose：导出三个镜像，确认其内容
来自未提交文件，worker 共用镜像引用正确，指定的 builder 保持运行。这项验证未经过
Control 或 Nomad。

Go CLI 与 Control 的本地构建链路也已接通：

```bash
luma build local .
luma build local /path/to/checkout --repo-url https://github.com/team/app.git --env production.env
luma build local . --compose-sidecar deploy/luma.compose.yml --builder my-builder
luma workflow record my-app -- build local . --env production.env
luma workflow run my-app --path /path/to/checkout
```

本机需要 Docker 和 Buildx；Git 用于读取 origin、版本及未提交状态，未配置 origin 时可传
`--repo-url`。本机必须能访问 Control 配置的 Registry pull 地址，并使用本机 Docker 认证
（例如事先执行 `docker login REGISTRY_HOST`）。本地上传不使用远程 Builder 的 localhost
push 地址，也不要求远程 Agent 具备构建能力。`--builder` 复用指定的已有 builder；不指定
时创建并清理临时 builder。默认总超时为 7200 秒。

Control 的 `POST /v1/builds/local/prepare` 返回上传仓库、唯一标签及覆盖目标节点/区域的
平台集合。`POST /v1/builds/local/{id}/complete` 提交构建结果，`queue: true` 返回原构建 ID，
之后按该 ID 查询日志和结果。仅接受预留项目及标签的镜像，应用名必须与预期一致；重复
完成请求不会再次部署。`/{id}/fail` 用于结束尚未提交部署的失败上传。

本机上传阶段不占用项目执行队列；完成上传后的部署与远程构建共用项目串行和目标替换
规则。未过期上传预留及待执行部署可跨 Control 重启保留。部署记录保存 Git 来源和 dirty
版本信息；之后的 Git 更新从远程仓库重新构建，本机未提交内容仍需再次执行本地构建。
工作流在成功后由 Control 保存。

已用真实 Go CLI、Go Control、本机 dirty checkout、BuildKit 和认证 Registry 完成原生与
Compose 构建，再通过 `workflow record/run` 重建；导出四个镜像核对文件内容，确认使用
实际未提交文件。本次组合链路的 Nomad 仍是 API 测试替身，尚不等于完整生产切换证明。

源码快照与分析制品的文件处理已实现为 Go 模块：生成快照、计算摘要、解包、按内容摘要
存储及校验读取。它直接使用 Builder 上现有的 `LUMA_BUILDER_SNAPSHOT_ROOT` 目录布局
（默认 `/var/lib/luma/builder/snapshots`），已有快照和分析制品无需搬迁或重新生成。
生成的新 tar 可能与旧 tar 的字节摘要不同，但相同源码的逻辑树摘要保持一致。
旧文件按原摘要直接读取，快照句柄仍与构建任务绑定。

已验证旧格式快照、中文/长路径、执行权限、符号链接、未提交文件和清理互斥锁。
源码分析的获取阶段也已改为 Go：支持带认证的 HTTPS Git、固定 commit 和仓库子目录，
以及通过临时下载地址提供的 HTML/ZIP。获取后生成固定快照，再建立分析容器需要的
workspace/input/output 目录；Git 凭据和下载地址不进入元数据。已通过真实 HTTPS Git
及文件服务器验证，并检查了任务超时、工作目录磁盘限制和失败后的临时文件清理。

上述验证覆盖源码获取和文件处理；分析容器运行、结果校验、build-plan 执行器及 Control
制品接口仍待接通，当前仍不能替换生产 Builder。

### 私有镜像和 Builder 缓存

Go Control 已接通已有 Registry 凭据、Builder 镜像缓存及部署端认证。
可直接通过 Go CLI 管理凭据：

```bash
luma registry login registry.example.com --username USER --password-stdin
luma registry list
luma registry remove registry.example.com
```

第一条命令从标准输入读取密码；省略 `--password-stdin` 时使用不回显的终端输入。
`list` 仅返回地址、用户名和配置状态，不返回密码。写入会更新 Registry 的 setup check，
只读 Control 仅允许查询。Registry 服务安装、镜像库存、保留/删除策略和 GC 尚待迁移。

沿用状态中的 `build.registryHost`、`build.pushHost`、`build.defaultNode`，
Builder 必须已注册、在线并支持 `runtime-image-cache-v1` / `cache-runtime-image`。
Control 显式设置 `LUMA_LAE_BUILDER_REGISTRY_INSECURE=0`（HTTPS）或 `1`
（允许 HTTP/不验证 TLS 的传输模式）；隔离测试使用 `1`，实际切换须与现有仓库匹配。
仅配置 pull 或 push 一端会报错，不会跳过缓存。

外部镜像由 Go Agent 缓存到内部 Registry，Control 核对任务返回的镜像引用及 digest，
再用内部 `repository@sha256:…` 部署。Compose 中相同镜像/平台只缓存一次；
内部镜像可以直接使用，push 地址按需转换成 runtime pull 地址。
源仓库和 push 凭据只在 Agent 领取任务时注入；运行时拉取凭据按最终镜像地址查询，
只注入提交给 Nomad 的 job，不写入 preview、生成文件、部署历史或持久化任务载荷。

已用真实 Go CLI、TLS Go Control、Go Agent、crane、两个需要认证的隔离 Registry
及 Nomad/Docker 验证：凭据通过 Go CLI `registry login --password-stdin` 保存后，
私有源镜像完成缓存与 digest 校验，原生服务和双服务 Compose
均实际启动，Compose 重复镜像只产生一次缓存任务。未访问生产 Registry，未修改
本机 Docker 全局认证或 daemon 配置。

未配置 Builder Registry 时，指定节点且 Agent 支持 `docker-image` / `resolve-docker-image`
的部署会先在目标节点预拉取。`latest`（含省略 tag）强制拉取，并要求返回有效 digest；
其他 tag 可使用节点已有镜像。源凭据仍只在任务领取时注入，Compose 同节点重复镜像
只预拉取一次。返回的镜像必须属于请求仓库，已固定的 digest 不允许改变。

未指定节点或节点不支持预拉取时，`latest` 由 Go Control 通过 Registry V2 查询并固定
digest，支持 Basic/Bearer 认证；其他版本 tag 交给 Nomad 在实际节点拉取。预览不查询
Registry。跨平台索引保留其 digest，由运行节点的 Docker 选择对应平台。

这两条路径已使用需要认证的真实隔离 Registry 和 Nomad/Docker 验证：未指定节点的
原生服务、指定节点的原生服务及双服务 Compose，共四个容器成功启动，两个指定节点
部署共生成两次实际 Go Agent 预拉取任务。重复部署解析新的 `latest` digest 另有 HTTP
契约测试覆盖。

Docker 拉取代理自动修复及重启 daemon 后的 allocation 协调仍待迁移。目前拉取失败
会明确报告，不会执行尚未接齐的 daemon 修改流程。

### Secret 管理和部署替换

Go CLI 的 `secret list/set/import/remove` 已直接接入 Go Control。`secret set NAME
--scope APP` 可交互输入且不回显，也支持 `--value-stdin`；`secret import FILE
--scope APP` 导入应用范围内的变量。查询只返回名称，写入结果只返回操作元数据。
只读 Control 可以查询名称，拒绝修改。

原生及 Compose 部署已支持 `${NAME}` 环境变量替换、`requirements.secrets` 和
`--env` 导入。应用已有 scoped secrets 或显式传入 `--env` 时，所有引用必须由该应用
范围满足，不回退到全局；空 env 文件也保留这一语义。旧的全局 Secret 模式继续兼容。
部署导入只保存实际引用的名称，并与待部署记录在同一事务内保存。

preview、生成的 jobspec 文件和部署历史保留占位符；仅提交给 Nomad 的 task Env
包含实际值。Secret 值保存在私有 Control 状态库中，Nomad 运行配置也包含实际值。
替换按字面量执行一次，不会再次展开 Secret 值里的 `${...}`。

隔离真实 Nomad/Docker 验证已覆盖原生和 Compose 容器实际收到包含引号、换行和
`${...}` 的完整值，同时检查生成文件及历史没有 Secret 值。交互式输入与中断后的
终端恢复由真实伪终端测试覆盖。这些是 Go 运行链路验证，Python 仅用作开发测试驱动。

### 运行时出站代理和 Cloudflare Tunnel

原生服务和 Compose 的 `proxy: true` 已接入区域策略：`cn`/`home` 使用代理，
`global` 直连，自定义区域使用状态库里的 `egress` 配置。在线部署要求区域已注册。
Control 依次从状态中的 `nomadRpcAddr`、`nomadServerAddr`、配置中的
`defaults.nomadServer`、已注册管理节点地址、非本机 Nomad HTTP 地址解析网关，
并为需要代理的任务补充 `HTTP_PROXY`/`HTTPS_PROXY`（网关端口 7890）。
manifest 中显式设置的变量优先，包括空字符串；未启用代理的 Compose 任务不受影响。
如果需要补充代理变量却无法解析网关，部署会在提交前报错，避免意外直连。
隔离真实 Nomad/Docker 已验证原生和 Compose 容器的 HTTP 请求到达本地测试代理。
这不代表网关安装、订阅配置或 Docker 拉镜像代理管理已经全部迁移。

原生 `cloudflare-tunnel` 已支持 `tunnel.tokenEnv`（默认
`CLOUDFLARE_TUNNEL_TOKEN`），从应用 Secret 或显式 `--env` 文件取值，仅注入
`cloudflared` 任务的 `TUNNEL_TOKEN`。CLI 会保留此类结构化字段引用的变量，
即使 manifest 没有 `${...}` 占位符。生成文件和历史不保存 token 值。
Tunnel 跳过普通 DNS A 记录同步，公网 hostname 和 origin 配置仍由 Cloudflare 管理。

Go 修正了旧版 Tunnel 的容器网络：不设置 `publishPort` 时，应用与 sidecar
使用共享的 Nomad bridge 网络，不发布宿主机端口；Cloudflare origin 可配置为
`http://localhost:<port>`。这要求目标节点有可用的 Nomad CNI bridge。
与旧版渲染结果的这一差异是有意修正，已单独纳入契约检查。
隔离真实 Nomad/CNI/Docker 已验证 sidecar 通过 localhost 读取应用响应；
该测试将外部连接器替换为本地探测进程，没有联系 Cloudflare 公网或使用生产 token。
公网 Tunnel 连通性尚未验收，Compose Tunnel 仍不支持，与原有接口边界一致。

## 恢复副本上的 Go Control 验证

先按 [Control 备份恢复文档](control-storage.md) 创建一致性备份，并恢复到新的隔离目录。
完整保留 `control.sqlite3` 和 `control-sqlite-authority.json`；不要只复制在线数据库主文件，
也不要手工修改 authority 标记。恢复后的数据库和标记必须是私有普通文件。

```sh
.local-notes/go-bin/luma-control-go \
  --state-dir /srv/luma-control-recovery \
  --read-only \
  --listen 127.0.0.1:9081
```

该进程检查数据库完整性、schema 1、初始化标记和 databaseId 对应关系。
缺库、损坏、较新 schema、身份不符都会退出，不会导入旧 `control.json` 或生成新凭据。
运行后每次读取也检查数据库身份；已打开的数据库文件被替换时必须重启验证。

可检查 `/v1/health`；用恢复副本中的管理 token 验证 `/v1/auth/login/verify`，
查询 `/v1/history`、`/v1/history/build/{id}`、`/v1/history/deployment/{id}`、
`/v1/builds` 和 `/v1/deployments/history`。分页游标可跨 Python/Go 使用。
部署历史详情 `/v1/deployments/history/{id}` 和构建详情 `/v1/builds/{id}` 已实现。
排队中的构建详情包含当前项目队列位置和等待对象；查询不会启动或修改队列。
还可查询 `/v1/secrets` 的名称列表、`/v1/workflows`、`/v1/workflows/{name}`，并向 `/v1/workflows/check`
提交工作流做差异检查；检查本身不写库。其他读取接口返回 404，未迁移写操作返回 501。

监听仅允许显式 loopback IP。这里的 HTTP 地址是本机迁移验证入口，不能直接作为
现有 HTTPS CLI 的线上 endpoint。管理凭据不要放进文档、命令历史或诊断输出。
只读模式不派发任务、不执行部署，也不调用 Python 作为运行依赖。

SQLite 使用只读连接且拒绝数据写入，但 WAL 的共享内存协调文件可能创建或更新；
“只读”指数据库记录/schema/authority 不变，不代表目录内绝无临时文件变化。
验证结束按 Ctrl-C 正常退出。此步骤验证的是恢复副本读取能力，不代表线上切换完成。

### Agent 任务写入联调

仅在已恢复的隔离环境中，把只读模式替换成下列模式；测试节点的 Agent 使用该 TLS
地址及恢复副本中属于该节点的 agent 凭据。不要把线上 Agent 重定向到恢复副本。

```sh
.local-notes/go-bin/luma-control-go \
  --state-dir /srv/luma-control-recovery \
  --agent-tasks \
  --tls-cert /secure/test-control-cert.pem \
  --tls-key /secure/test-control-key.pem \
  --listen 127.0.0.1:9081
```

此模式会实际修改恢复库并交付其中已有、受支持的节点任务。当前支持卷目录、镜像和
Docker 配置及 CNI 端口清理任务的领取、进度和终态确认；不会交付 NFS 动作或尚未迁移的构建父子任务。
它也支持管理 token 调用 `POST /v1/workflows` 保存工作流；Go CLI 的
`workflow record/show/list` 已能直接与 Go Control 联调。记录仍保存为 Python 可读的
schema 1 配置，版本冲突返回 409 并保留较新的记录。记录构建配方不等于执行构建。
同一节点只有一个运行中任务；终态响应丢失时，重复上报只确认已保存的决定。
运行中任务在 Agent 明确不再持有它且越过交付宽限期后标记失败/取消，不会自动重放。
未携带 `activeTaskId` 或其值为 null 的心跳，不代表 Agent 空闲，不触发该恢复处理。

Go 写入进程持有 `control-writer.lock` 独占锁；当前 Python 数据库适配层在连接期间
持有共享锁，双方不能通过这些适配层同时写入。旧 Python 版本和直接 SQLite 工具
不受该锁保护。切换前仍必须停止旧 Control 并排空外部任务，不能靠锁代替切换流程。
不要删除锁文件绕过冲突。停止 Go 后，Python 可重新读取/接管兼容的 schema 1 数据。

这组联调已覆盖终态响应丢失、两端重启、回执补交、跨节点鉴权、并发领取和旧字段保留。
取消信号目前来自已有任务上的取消请求；完整上层部署/构建取消接口、节点加入和升级
仍待迁移，因此该模式尚不能作为生产 Control 的完整替代品。

## 存储模型变化

| 原配置/行为 | Go 目标行为 | 迁移动作 |
| --- | --- | --- |
| managed/external NFS storage class | 不再提供 | 先核查真实消费者；只有实际使用 NFS 的数据才需要搬迁 |
| Compose `storageClasses`、卷 `storageClass/path` | 校验失败并提示迁移 | 换成部署节点上的 `local.path` 或本地 named volume |
| 原生 manifest 的 `storage` 元数据 | 校验失败并提示迁移 | 保留 `volumes`，改为迁移后的节点本地挂载 |
| 自定义 Compose `driver_opts`、远程卷驱动 | 不再接受 | 使用普通本地卷或目录 |
| 多节点共享同一远程卷 | 不再提供 | 共同读写的服务放在同一部署节点；需要跨节点数据同步时由应用实现 |
| 有持久化数据的服务自动漂移到另一节点 | 不允许静默漂移 | 先完成显式数据迁移，再更新节点绑定 |

本地存储不意味着已有 NFS 数据自动变成本地数据。只删除 YAML 中的 `storageClass`
会令 Docker 创建空卷，因此旧配置必须明确报错，不能自动降级。
Docker 中遗留的同名卷也可能仍使用 NFS 驱动，不能仅凭卷名判断已经迁移。

## 先核查依赖，再决定是否搬数据

Go 重构不要求重新创建现有应用卷。默认迁移路径是保留原节点、原路径和原卷名，
完成程序与控制面状态切换；只有真实挂载依赖 NFS 或主动更换部署节点时才搬迁数据。

核查应结合以下证据，记录采集时间和未能访问的范围：

1. Control 中的 storage class、部署记录、待执行任务和待部署 manifest。
   遗留配置或 Agent 广告中的 `nfs-client`/`nfs-host` 仅说明旧能力存在，不能证明服务在用 NFS。
2. Nomad 当前 Job 和运行中的 allocations：同时检查 Docker `Config.mount`、
   旧 `Config.volumes`、group volumes 和 task volume mounts。Dashboard 的服务存储摘要
   不能代替这一步，它可能不包含系统 Job 或旧格式挂载。
3. 在 allocation 所在节点检查实际容器挂载。对 bind 路径用 `findmnt -T PATH`
   检查底层文件系统；对 named volume 用 `docker volume inspect VOLUME` 检查
   driver、options 和实际 Mountpoint 的文件系统。`local` 驱动也能配置 NFS，
   因此必须一起检查 options 和实际文件系统。
4. 检查主机的 NFS 挂载。权限不足、容器挂载命名空间不可见、Docker context
   指向其他 daemon 都应记为未核实，不能当作空结果或“无依赖”。不要输出容器环境变量或凭据。

| 核查结果 | 迁移路径 |
| --- | --- |
| 数据已在原部署节点的本地盘上 | 原地保留数据，备份并验证恢复能力；不复制、不改卷名、不重新初始化 |
| 仅残留未使用的 NFS 配置 | 确认当前及待执行任务没有消费者后移除配置；不安排应用数据搬迁 |
| 发现真实 NFS 消费者 | 仅对受影响数据集执行下面的条件迁移流程 |
| 驱动或实际文件系统未能核实 | 保留现状并补齐证据，暂不把该卷标记为已通过迁移检查 |

无 NFS 的原地切换要保留节点的稳定身份、挂载源和目标、UID/GID、权限及卷名。
Control/Agent 升级本身不应清理卷、排空服务或强制重建 allocations；控制面的在途
部署/构建任务仍需排空。应用一致性备份所需的停写窗口由具体数据库/备份方案决定，
无需仅因为更换 Luma 实现语言安排一次全量应用停机和数据复制。

## 配置转换示例

仅在核查发现实际 NFS 消费者时，原来的 NFS sidecar 可按以下方式转换：

```yaml
name: database
region: cn
volumes:
  data:
    storageClass: shared
    path: database/data
services:
  db:
    node: worker-a
```

数据复制并验证完成后，对应本地 sidecar：

```yaml
name: database
region: cn
volumes:
  data:
    local:
      path: /srv/luma/database/data
    adopted: true
services:
  db:
    node: worker-a
```

Compose 主文件的 `data:/var/lib/postgresql/data` 挂载可以保留。
本地路径跟随 `db` 的部署节点，不再填写另一台存储服务器。
原生服务可以使用 `node: worker-a` 和
`volumes: [/srv/luma/database/data:/var/lib/postgresql/data]`。
`adopted: true` 是已有数据的声明，不是复制、初始化或校验数据的命令。

## 条件流程：确实需要搬迁的数据集

已经使用本地存储且保持原节点的服务跳过本节的数据复制步骤。

记录服务/部署名称、实际运行节点、原 NFS 端点和相对路径、挂载目标、
所有共享写入方、数据大小、UID/GID、备份位置、目标节点和目录、停写窗口、
校验方法和负责人。凭据不得写入迁移记录。

优先使用可检查的独立本地目录。若使用 Docker named volume，必须记录其实际
驱动、选项和挂载点，并确认它不是遗留 NFS 卷。

迁移顺序：

1. 在旧版本上确认服务健康，并为数据库制作可恢复的一致性备份。
2. 确认目标节点的磁盘容量、权限、架构和运行环境，准备独立目标路径。
3. 停止所有写入该数据集的服务。多个服务共用数据时按数据集一起停写。
4. 将数据复制/恢复到目标节点，保留应用所需的所有权和权限。
   不把运行中的数据库文件直接复制作为一致性备份。
5. 按数据类型检查数量、大小、校验和或数据库恢复/一致性结果。
6. 更新挂载和节点绑定，在目标节点启动服务，检查实际挂载、读写和健康状态。
7. 验收实际读写后再清理旧数据及未使用的 NFS 配置。

## 程序和控制面切换的顺序

迁移的主体是替换 **Control、各节点 Agent 和 CLI**。Nomad、Docker、Traefik、
已有应用容器及本地数据沿用。没有真实 NFS 依赖且保持原节点的应用，不需要搬数据，
也不需要仅为替换 Luma 而重新部署。迁移不以完整失败注入、混合版本或回滚演练为前提。

最终安装命令以完成后的 Go 发布包为准，执行以下流程：

1. 记录当前版本、配置和节点身份，暂停新的部署/构建，等待在途任务结束。
2. 对 Control SQLite 制作一次一致性备份，同时保留配置及凭据文件。
   沿用本地数据的应用不因程序替换额外复制数据；真实 NFS 消费者才执行上一节。
3. 停止旧 Control，安装并启动 Go Control，沿用状态目录、对外地址和管理凭据。
   验证现有节点、部署记录、历史及授权能够读取，始终只运行一个 Control 写入方。
4. 逐节点停止旧 Agent 并启动 Go Agent，保留节点身份、配置和原数据目录，
   确认心跳和任务领取成功。同一节点只运行一个 Agent 实例。
5. 更新运维机和 CI 中的 CLI，验证登录、状态查询，以及一次实际部署和构建。
6. 确认已有应用访问正常、DNS/入口正常、本地持久化数据仍可读写，恢复部署/构建入口。

开发中的过渡测试可使用 Python Control 的 `supportedActions` 协商，只向 Go Agent
派发已实现的动作。最终完整替换不要求先在生产部署这一过渡版本。

## 出现问题时

优先修复当前 Go 版本并继续完成迁移，保留一次状态备份用于排查和恢复。
对结果不确定的任务先检查实际状态，避免重复执行主机操作或覆盖应用数据。
清理旧程序和备份安排在验收后；不为本次迁移另建复杂的自动回滚体系。

## 完成前必须补齐的证据

- Go Control、完整 CLI/Agent 功能与发布安装流程全部可运行。
- NFS 创建、挂载、管理入口从目标 Control、Agent、CLI、Dashboard 和配套配置移除。
- 实际部署能保持服务的原节点、原路径/卷名和持久化数据。
- Linux systemd、macOS launchd、终端、构建取消和远端子进程清理有真实运行验证。
- Go Control 能读取现有 SQLite 状态，替换后 Agent 正常接入，实际部署与构建成功。
- 最终文档列出实际版本/文件校验和、配置迁移表、命令及各项验收结果。

当前本文件是持续维护的实施文档；上述证据齐全后才标记为可执行的完整迁移指南。
