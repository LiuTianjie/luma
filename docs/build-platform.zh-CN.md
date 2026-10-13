# 持久化构建平台 {#build-platform}

可选的仓库构建后端完全运行在自己的 Luma 基础设施内。Control 继续作为 CLI 和控制台的认证入口；Temporal 负责持久编排，Nomad 调度原生构建任务，BuildKit 生成镜像，PostgreSQL 保存构建元数据，Harbor 保存 OCI 制品和缓存，对象存储保存源码快照和日志块。外部 CI 可以触发 Luma，构建本身由 Luma 执行。

这一实现需要显式启用。源码检查不等于生产验收，也不能证明十个构建的实际吞吐。切换 Control 后端前，应完成下述环境验证。

## 状态归属与恢复 {#ownership}

| 事实 | 权威来源 |
| --- | --- |
| 已接受请求、容量、执行尝试、制品、发布代次 | PostgreSQL |
| Activity 重试、定时等待、worker 恢复 | Temporal |
| 执行器是否真正终止 | Nomad allocation 状态 |
| 不可变镜像内容 | Harbor OCI digest |
| 源码归档和已确认的日志内容 | 对象存储 |
| 新旧构建的统一历史 | 可重建的 Control SQLite 投影 |

请求与启动工作流的 outbox 同事务提交。工作流、Nomad job 均使用稳定 ID；Nomad 提交采用条件更新。响应丢失后先核对已有结果，再决定是否提交。Activity 心跳帮助替换 worker 接管故障任务；恢复仍依赖相应基础设施可达。

分支在源码准备开始时解析为完整 commit。上传前先持久化 SHA，准备阶段重试继续检出该 SHA；构建重试也固定在这一提交。要构建更新后的分支，需要新建请求。

一个逻辑构建按顺序执行各原生平台任务，在平台之间释放执行槽位。每个任务拥有独立 BuildKit、CPU/内存额度和临时磁盘预留，不挂载宿主机 Docker socket。Registry 缓存按尝试生成独立标签，按构建配方和架构导入。Dockerfile 的 `RUN --mount=type=cache` 内容不会被 registry 层缓存导出器跨节点传递。

构建成功先保存不可变制品，再进行发布；发布重试复用制品。目标代次采用请求接受顺序，晚完成的旧构建不能覆盖已应用的新发布。持久化的 Nomad modify-index 基线也阻止中断的发布在重试时覆盖中途的手动部署。Control 仍串行处理 DNS、入口等发布副作用，镜像构建与该锁独立。

取消是工作流观察的 PostgreSQL 命令。Nomad 确认所有执行尝试终止后才释放容量；unknown/lost allocation 保持正在取消并占用容量。进入发布阶段后拒绝取消，应等待结果再使用已有回滚流程。源码准备阶段取消会等待当前有超时约束的克隆/上传 Activity 返回。

## 安装 {#installation}

服务镜像使用 Python 3.13，原有 CLI 和 agent 继续兼容 Python 3.9。服务依赖位于 `build-platform` extra。构建两个新镜像，为 amd64 和 arm64 发布执行器，并用 digest 固定实际部署镜像。

```sh
docker build -f Dockerfile.build-platform -t luma-build-platform:review .
docker build -f Dockerfile.build-executor -t luma-build-executor:review .
python -m luma.build_platform migrate
python -m luma.build_platform serve
python -m luma.build_platform worker
```

后三个命令需要将 `deploy/build-platform/platform.env.example` 的配置载入进程环境。Fernet 密钥只生成一次，并与 PostgreSQL 一起备份；丢失密钥后无法恢复已保存的部署 secret 覆盖值。内部 API token 不面向用户，用户继续使用 Luma 管理 token。

准备私有 PostgreSQL 数据库、自托管 Temporal namespace、S3 bucket 和 Harbor project。`deploy/build-platform/compose.yml` 运行连接这些依赖的 Luma API 和 worker，不会隐式安装数据库或替换现有 registry。Temporal 使用官方 server/schema 工具，Harbor 使用官方安装器管理生命周期。API、worker、schema、执行器应协同升级，已有 Temporal 历史必须保持 replay 兼容。

Nomad 要求：

- 按下文通过 Control 显式指定 Linux 构建节点。`LUMA_BUILD_POOL` 表示实际 Nomad NodePool，默认为 `default`，所选节点需属于该池；不再要求额外的构建池 metadata 标签。
- 仅在可信构建池启用 privileged Docker task。执行器在独立任务内运行带容器隔离的 BuildKit；这不是用于恶意多租户的安全沙箱。
- 创建 `builds` namespace。worker ACL 示例位于 `deploy/build-platform/nomad-worker-policy.hcl`。启用 workload identity 和 Variables，每个任务从其 job/group/task 路径读取引导 token。
- 接受任务前，先通过节点侧 registry 认证或预加载，使构建节点能够拉取固定的执行器镜像。构建 robot 凭据在任务启动后才租用，不能用于这一次初始拉取。
- 所有构建节点必须能访问构建 API 和对象存储。源码签名 URL 在租用时签发，十五分钟过期。

Harbor 为构建 push/tag 和运行时 pull 分别配置 robot，运行时只读。执行器租约不可使用 Harbor 系统管理员凭据；目标 project 需预先建立。可选 GC 接口默认 dry-run，需要对应 Harbor 管理权限；不要因此扩大构建 robot 的权限。

## Control 切换 {#cutover}

Control 配置 `LUMA_BUILD_PLATFORM_URL`、`LUMA_BUILD_API_TOKEN`、`LUMA_BUILD_REGISTRY_URL` 和两个 `LUMA_BUILD_REGISTRY_PULL_*` 变量。`LUMA_BUILD_EGRESS_PROXY` 是构建池出站代理，单次 `proxyMode=direct` 可禁用。仓库导入要求不含内联凭据的 HTTPS 地址；准备源码时才读取现有 Git provider token。

URL 非空后，新仓库导入使用构建池并默认自动分配节点；显式选择的节点必须在允许名单中，且与目标原生架构一致；单节点不能同时承担两种原生架构。Registry 覆盖必须匹配配置的 Harbor host。CLI 流式响应可以先返回已接受的 run ID，CLI 继续追踪分页日志，控制台打开持久历史记录。新后端不可用时明确失败。

已接受的旧构建继续使用原后端。已有本地构建仍在调用者机器运行，并使用现有上传/发布 API，不会被改成远程仓库构建。旧 registry 的清单和删除控制仍管理旧 registry；新 Harbor 有自己的清单和生命周期。只要部署或回滚版本仍引用旧 digest，就保留旧 registry。

构建服务提供认证后的 `/health`、`/v1/builds`、`/v1/builds/{id}`、`/v1/builds/{id}/cancel`、`/v1/builds/{id}/retry`、`/v1/registry/inventory`、`/v1/registry/gc`。这些是内部接口，应用用户继续通过 Control 操作。仓库请求可提供 `idempotencyKey`：相同 key/body 返回同一个 run，不同内容复用 key 会失败。`autoDeploy: false` 只生成制品。

设置 `LUMA_BUILD_PLATFORM_ENABLED=0` 可将后续新请求切回旧后端。仍需保留 URL、构建服务与 worker 直到已接受任务完成，并供新记录详情和操作使用；现有 Harbor 部署也需要保留拉取凭据。数据库 schema 高于服务版本时禁止降级启动。从 SQLite 备份恢复后，可重置 PostgreSQL 中已投递的 `project` outbox 项，让 worker 重建历史投影；构建日志始终留在对象存储。

## 指定构建节点与原生调度 {#native-placement}

在仓库导入页面展开“配置构建节点”，勾选已注册的 Linux 节点并保存。CLI 与控制台共用这份名单，API 是携带 `nodes` 数组的 `POST /v1/builds/config`。只有显式名单授予新池准入资格，旧默认节点和 builder 角色标签不会额外加入节点。CLI 可替换同一份名单：

```sh
luma build config --node build-amd64-1 --node build-arm64-1
```

暂停新任务分配时清空名单：

```sh
luma build config --clear-nodes
```

Worker 根据原生架构、实时 Nomad 就绪/可调度状态、Docker driver 健康状态、NodePool 和资源规格过滤已选节点。任务带有 Nomad 节点 ID 硬名单和 CPU 架构约束。执行器领取租约时复查最新名单，下载源码前核对容器架构、Nomad 宿主架构与目标架构。移除的节点不能再领取新构建租约；已提交给它的任务可能在启动时失败，而不是回到队列。已开始执行的构建继续完成。选择节点意味着授权，不代表性能基准保证，应选择适合工作负载的硬件。

部署目标架构来自注册身份，节点离线也保留已知架构；架构未知就明确失败，不默认 amd64。缺少可用同架构构建节点的请求保持排队，不占执行槽位。源码准备单独限制为每个编排 worker 同时两个 checkout。每种架构最多一个尚未分配节点的任务临时占用执行预留，默认分配期限为六十秒（`LUMA_BUILD_PLACEMENT_TIMEOUT`）。allocation 已分配节点后，冷拉镜像及引导使用独立的六百秒启动预算（`LUMA_BUILD_STARTUP_TIMEOUT`）。分配失败后先停止并确认，再归还容量；修正节点可用性或配置后重试。全局仍最多十个执行槽位。

重叠 Activity 的执行恢复按构建 ID 使用 PostgreSQL advisory lock 串行处理。尝试状态区分尚未提交、提交结果不确定和已确认提交；对不确定的首次提交，用只能创建一次的停止状态 job 阻止迟到提交启动，再完成取消和释放槽位。无法确认退出的运行 allocation 继续保留容量。

## 容量与保留策略 {#capacity}

默认十个活跃逻辑构建、一百个额外未完成请求、每项目最多十个。所有 worker 通过 PostgreSQL 共享容量计数，Nomad 进一步预留实际 CPU 和内存。单执行器默认请求 2,000 MHz 并启用 Docker CPU 硬限制、4 GiB 内存和 8 GiB 临时磁盘；MHz 不代表独占核心数；磁盘预留不等于硬文件系统配额。十个重型构建需要相应容量的构建池，修改并发数不能增加硬件容量。多架构目标顺序使用同一个逻辑槽位。

Harbor 负责在线 GC 和标签保留机制。构建标签为 `build-bld2-*`，缓存标签按尝试独立生成。初始部署不自动删除制品。启用保留策略前，应从两个 Control 后端清点运行中和回滚引用，保护相应 digest，并预览 Harbor 策略。删除标签而不保护这些 digest，可能导致替换 allocation 拉取失败，即使当前容器仍在运行。不要对 Harbor 使用 Luma 的旧离线 registry GC。对象存储生命周期独立配置，源码和日志保留时间应覆盖历史要求。PostgreSQL、加密密钥、Temporal 持久化和制品存储一起备份。

## 验收 {#acceptance}

在隔离环境安装后验证：原生 service 与 Compose 导入；amd64/arm64 原生 manifest；Dockerfile ignore 与依赖缓存复用；十并发及满队列；排队/执行中取消；Nomad 提交后杀 worker；registry/Control 响应丢失；对象存储故障；Control 重启；旧构建晚于新发布结束；只构建制品；只重试发布；重试期间手动部署；本地构建兼容；Harbor 保留策略对运行中和回滚镜像的保护。分别测量接受和读取延迟、排队时间、构建时间、缓存命中、内存、磁盘增长及未确认日志损失。

单元检查不能证明这些运行时和容量结果。生产切换和自动删除是独立的运维步骤。

参考：[Temporal 自托管](https://docs.temporal.io/self-hosted-guide)、[Nomad Variables](https://developer.hashicorp.com/nomad/api-docs/variables)、[BuildKit](https://github.com/moby/buildkit)、[Harbor 文档](https://goharbor.io/docs/)。
