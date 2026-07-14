# 10. LAE 运维与排障 SOP

> 文档版本：2026-07-12<br>
> 适用对象：LAE/Luma 值班、平台研发、SRE 和安全响应人员<br>
> 安全默认：先只读、再隔离 staging、最后才做经过批准的生产变更。本 SOP 中的命令不会执行部署；带写入效果的命令只用于明确标注的 staging 恢复步骤。

## 1. 值班原则

每次处理都按同一顺序：

```text
symptom -> checks -> safe action -> verify -> escalate
```

必须遵守：

1. 先记录时间、region、`applicationId`、`analysisId`、`deploymentId`、`operationId`、cursor、公开 error code 和 request ID。
2. 不在终端回显、日志、工单或聊天中粘贴 deploy token、Luma management token、principal token、Git secret、环境变量值、验证码、签名 URL、对象 key、数据库 URL 或完整 `control.json`。
3. 租户只看到 region、应用/service/route 状态和安全错误。实际节点名、node ID、IP、候选集、failure domain、registry 地址和 storage endpoint **仅限内部管理员审计**，不得回传租户。
4. 不直接编辑 `/opt/luma/control/control.json`，不手改 Nomad job，不用 Luma management token 冒充 LAE principal，不把失败任务改成 succeeded。
5. 网络重试必须复用原 Operation/cursor；写请求只有 body 完全相同时才复用 idempotency key。
6. 暂停、重启或取消都不能替代根因判断。PostgreSQL、MinIO、registry 和 volume 的重启/删除尤其需要变更批准和恢复点。
7. 普通删除保留 storage。任何 `--delete-storage`、对象批量删除、registry GC、volume 迁移或 restore cutover 都不属于一线默认动作。

## 2. 信息可见性和 placement 边界

LAE 用户请求只携带 `region`。Luma 内部 placement 会根据以下事实筛选候选：

- 公开值只能是 `cn` 或 `global`；内部 Builder 使用的 `home` 必须在 analysis/template/upload/CLI admission 就被拒绝，不能拖到 deployment 才失败；

- Luma 注册节点与 Nomad 节点身份能匹配；
- Nomad ready、eligible、非 drain，Docker driver 健康；
- node agent ready/新鲜并具备 runtime capability；
- region 匹配；
- 节点名必须位于非空、精确的 `LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON`；缺失、空值、重复或非法名称均 fail closed，不会回退到通用 Docker 节点；
- builder-only 和未显式 opt-in 的 control-plane 节点继续被排除；当前 staging 的 manager 只有同时带 runtime role 且位于 allowlist 才可进入；
- managed volume 的 storage class、eligible nodes、failure domain 和跨 region 可达性匹配；
- 整个 Compose group 的 CPU/memory/port 通过 Nomad plan；
- 更新时将上一健康节点和 failure domain 作为软 affinity，不作为不可恢复硬 pin。

Control state 可以保存包含 node ID 的内部 placement 决策；租户投影明确省略它。当前 Luma Dashboard 的 LAE 页面已提供跨租户用户、应用、Operation、用量和“调度位置”只读视图；placement 页会把内部候选、preferred node 与实时 Nomad allocation 关联。该视图和 API 只接受 Luma management token，节点拓扑不得进入租户工单。真实 staging 中的 RBAC、allocation 关联和故障态展示尚未验收。

当前实现已经具备正向 node allowlist、region/readiness/capability、builder-only/control-plane policy、volume compatibility、prior affinity 和 Nomad plan；staging bundle 默认 allowlist 为 `manager + tecent`。manager 只有显式 `runtime` role 才能进入，移除该 role 或 allowlist 项都会 fail closed。生产应换成至少两个专用 runner；通用 `docker-image` capability 不能单独证明节点适合不可信租户 workload。

本轮真实 staging 的内部拓扑以此为准：`manager` 是唯一控制面，同时是经过显式
opt-in 的 runtime candidate；LAE 平台九个服务固定在 `lab`；租户 runtime 候选为
`manager + tecent`；`builder` 承担 clone/build/registry。`aly` 是历史名称，已过时，
不得作为控制面、构建或租户调度目标，也不属于升级失败节点。该拓扑只面向管理员，
不能出现在租户 API、CLI 或日志中。

## 3. 通用只读检查

从仓库根目录执行。先确认当前 context 是预期集群；不要在命令行传 token：

```bash
REPO_ROOT=/absolute/path/to/infra-stacks
cd "$REPO_ROOT"
.venv/bin/luma version
.venv/bin/luma status --format json
.venv/bin/luma doctor
.venv/bin/luma storage list --format json
.venv/bin/luma registry list --format json
.venv/bin/luma history lae-platform-staging --format json
```

检查 staging 的外部存活和就绪端点：

```bash
curl --fail --silent --show-error https://lae-api-staging.itool.tech/health/live
curl --fail --silent --show-error https://lae-api-staging.itool.tech/health/ready
curl --fail --silent --show-error https://lae-artifacts-staging.itool.tech/minio/health/ready
curl --fail --silent --show-error --output /dev/null https://lae-staging.itool.tech/
```

只验证部署资产，不 import、不 deploy：

```bash
docker compose -f lae/deploy/luma/docker-compose.yml config --no-interpolate
docker compose -f lae/deploy/luma/docker-compose.staging.yml config --no-interpolate
.venv/bin/luma compose validate --import-mode lae/deploy/luma/luma.compose.yml
.venv/bin/luma compose validate --import-mode lae/deploy/luma/luma.compose.staging.yml
```

`compose validate --import-mode` 会在 Repository Import 语义下注入构建结果，并在输出中包含 storage validation。不要再把 `luma storage check` 单独用于这些仍含 `build:` 的 sidecar：该命令没有 `--import-mode`，会在 image 注入前失败，不能作为 LAE 发布门禁。

截至 2026-07-14，Luma CLI、Control 与 manager agent 为 `0.1.233`；本轮未做
worker-wide fleet 升级，在线非 manager agent 主要为 `0.1.228`，离线 `blg` 保持
`0.1.175`。LAE staging 的 9 个 task、wildcard DNS-01 TLS 与 Web/API/Agent/artifact
探针健康；四服务 Compose 双 HTTPS/双持久卷产品 E2E 与 clean-room CLI/Skill E2E
均已通过。Mailpit/preview 仍不能证明真实邮箱送达，ZIP、真实私有 Git与完整安全负例
矩阵仍待完成。

`luma build list/logs` 展示的是 Repository Import build run，不是 LAE Builder v2 service-principal task。不要用它证明 LAE analysis/build 成功。LAE task 先看 LAE Operation；需要内部关联时再使用第 8 节的安全投影。

Luma Dashboard 是 deployed service 日志、指标、allocation event 和节点状态的首选只读入口。不要为了“看日志”先重启服务。

### 3.1 Registry 首次启动后 IPv4/Tailscale 不可达

**Symptom**

- `curl http://localhost:5000/v2/` 因优先使用 IPv6 而成功，但 `curl -4 http://127.0.0.1:5000/v2/` 或其它节点访问失败；
- `registry` task 在运行，宿主机 `:5000` 也有监听，但 Nomad bridge 显示 `NO-CARRIER`；
- allocation event 显示 registry 创建后 Docker daemon 被重启。

这是 `0.1.161` 及更早版本中 `registry serve` 的启动顺序问题：它先创建 allocation，再写 Docker `insecure-registries` 并重启 daemon。`0.1.162` 已改为先配置节点、后部署 allocation；相同配置重复执行时也不再重启 Docker。

**Checks**

```bash
.venv/bin/luma version
curl --fail --silent --show-error --max-time 8 http://<builder-tailscale-host>:5000/v2/
ssh <runtime-node> 'curl --fail --silent --show-error --max-time 8 http://<builder-tailscale-host>:5000/v2/'
ssh <manager> 'nomad job status luma-registry'
```

如果宿主机还有一个非 Nomad 管理、使用同一数据目录和端口的历史容器，先只读核对其 bind mount、创建来源和 registry 数据目录；两个 registry 进程并发写同一目录不受支持。

**Safe action — 仅限已批准的 staging 恢复**

1. 先把 manager/Control 与 builder node agent 升级到 `0.1.162` 或更高版本；不要在旧 Control 上反复执行 `registry serve`。
2. 记录 storage class 和实际数据 path，确认普通删除会保留 storage；禁止 `--delete-storage`、registry GC 或删除数据目录。
3. 仅当历史 host-network 容器已确认不属于 Nomad 且数据 bind mount 已记录时，停止并移除该容器；保留数据目录。
4. 执行 `.venv/bin/luma service remove luma-registry`，确认输出为 `Storage cleanup skipped`。
5. 用原 node、port 和 storage class 重新执行 `.venv/bin/luma registry serve ...`。
6. 从 builder、manager 和每个 runtime candidate 分别请求 `/v2/`；再推送一个候选镜像并按 `repository@sha256:...` 拉取验证。只看到 task `running` 不算恢复完成。

如果升级后的 allocation 仍反复重启、数据目录损坏或跨节点无法访问，停止 build/deploy lane，保留 allocation/log/storage 证据并升级到 Storage + Luma Platform；不要切到临时公网 registry 绕过。

### 3.2 Repository Import、BuildKit 或内部 registry 失败

**Symptom**

- base image 拉取极慢、`unexpected EOF`、`short read`、`ECONNRESET`，日志仍出现
  历史 `aly` 代理地址；
- `luma import` 能 clone，但 build/push/pull 在不同阶段失败；
- 使用完整 commit 时出现 `Remote branch <40-hex> not found`，说明旧 Builder 把
  commit 错当成 branch，而不是精确 fetch/checkout；
- registry `/v2/` 的 HTTP 探针成功，Docker 却尝试 HTTPS、走公网代理或返回 502；
- BuildKit 推送时报 `http: server gave HTTP response to HTTPS client`；
- 公网 Registry push 的大层在精确 `60.000s` 后返回 `499 Client Closed Request`
  或 `504 Gateway Timeout`，而同一镜像的小层成功；
- build 已 push，但 `lab`、`manager` 或 `tecent` 无法拉取产物。

**Checks**

当前 staging 的普通 Repository Import 使用带 Basic Auth/TLS 的
`registry.itool.tech`，实时 `status.build.registryHost` 与 `pushHost` 应相同；
`100.66.177.70:5000` 是 builder 上的内部 origin，旧 `localhost:5000` 已无监听。
LAE Builder 的短期 registry lease 与普通 Luma Import 的持久 registry credential
职责不同；地址必须以实时 Control 配置和 registry 监听为准，不能根据旧拓扑猜测。

```bash
.venv/bin/luma version
.venv/bin/luma status --format json
.venv/bin/luma registry list --format json
.venv/bin/luma build list --format json
.venv/bin/luma build logs <build-run-id> --format json
curl --silent --show-error --max-time 8 --output /dev/null \
  --write-out 'registry_http=%{http_code}\n' https://registry.itool.tech/v2/
ssh builder 'docker buildx ls && docker buildx inspect luma-builder'
ssh builder 'docker buildx inspect luma-builder-egress'
ssh <runtime-node> \
  'curl --fail --silent --show-error --max-time 8 http://100.66.177.70:5000/v2/'
```

在 builder 上只读检查 BuildKit 容器实际保存的 proxy 环境；不要把输出复制到租户
工单。`luma-builder` 应是 direct builder，`luma-builder-egress` 才允许持有当前
egress proxy。任何 `aly` IP、过期 proxy URL，或内部 registry 不在 `NO_PROXY`
都说明缓存配置陈旧。再在每个 Linux target 查看 Docker daemon 的
`insecure-registries` 和 `NO_PROXY`；这是 HTTP 内部 registry，不能靠一次 curl
成功推断 Docker 配置正确。还要区分宿主 dockerd 与 docker-container driver 内的
BuildKit：后者不继承宿主 `daemon.json`。Luma `0.1.194+` 会在 image exporter 上
显式设置 `registry.insecure=true`，否则即使宿主 Docker 配置正确，push 仍可能错误
升级为 HTTPS。

如果大层只在精确 60 秒失败，先查 Traefik access log 的
`RequestHost=registry.itool.tech`、`RequestMethod=PUT`、`Duration`、
`RequestContentSize`、`DownstreamStatus` 和 `OriginStatus`。多个大 PUT 都在
`60000000000ns` 左右终止，而 Registry allocation 没有重启，说明是 ingress
entrypoint 的整请求体 read timeout，不是镜像损坏或认证失败。当前 Luma 渲染的
Traefik 必须包含：

```text
--entrypoints.websecure.transport.respondingTimeouts.readTimeout=6h
```

这里按“最大单层”计时，不按整个镜像累计。不得用反复重试、删除 Registry blob
或把 TLS 改回内部 HTTP 来规避；也不要配置为无限超时。更新 Traefik 后应先确认新
allocation healthy 和参数生效，再重跑原 import，以真实大层 push + digest pull
作为验收。

**Safe action — 仅限已批准的 staging 恢复**

1. 先确认 manager/Control 是本次已发布版本，builder agent ready，且 health capability
   包含本次需要的 Builder 协议（至少 `build-proxy-mode-v1`）。只有协议或 capability
   不满足时才按部署手册升级 Builder，不能把无关节点的 fleet 版本一致当作重试前提。
   完整 40/64 位 commit 的 detached fetch 和内部 HTTP registry push 要求 Builder
   agent 至少为 `0.1.194`。
2. 原生直连可用时，重新 import 必须显式使用 `--proxy-mode direct`。这会把“禁用
   build proxy”作为协议字段传到 Control/Builder，不等同于在当前 shell 里 unset。
3. 当前版本会比较 BuildKit 容器持久化的 proxy/`NO_PROXY` 并重建不匹配的
   builder。只有确认没有 active build 且自动修复失败时，才在 builder 上移除对应
   `luma-builder` 或 `luma-builder-egress` buildx 实例；不要删除 registry 数据、
   Docker 全部缓存或其它 buildx builder。
4. 重新声明正确构建配置：

   ```bash
   .venv/bin/luma build config \
     --node builder --default-node builder \
     --registry-host registry.itool.tech \
     --push-host registry.itool.tech
   ```

5. 确认 `luma registry list` 中 `registry.itool.tech` 已配置，Builder 和目标节点
   通过 Luma 注入相同 registry host 的凭据；该地址必须走 HTTPS，不能标成
   insecure。内部 `100.66.177.70:5000` 只用于 origin/救援诊断并保持在
   `NO_PROXY`，不得临时改成公开构建产物的 canonical 地址。

**Verify**

- build 日志的 clone → base pull → dependency install → push 顺序闭合，未出现旧
  proxy 地址；
- 完整 commit import 的 resolved commit 与请求逐字相等，checkout 为 detached，
  build 日志不再出现 `Remote branch ... not found`；
- push 日志包含 `registry.itool.tech` 的 manifest digest，且没有降级到 HTTP；
- builder、`lab`、`manager`、`tecent` 都能以已配置凭据从
  `registry.itool.tech` 按同一 digest 查找/拉取；
- deploy 使用 immutable digest，未退回 mutable `latest`；
- `luma status --format json` 的 `build.registryHost` 与 `build.pushHost` 仍为
  `registry.itool.tech`。

**Escalate**

BuildKit 自动重建后仍复用旧代理、registry digest 不一致、HTTP registry 流量离开
Tailscale、或需要删除共享缓存/registry blob 才能继续时，停止 import 并升级
Luma Builder/供应链负责人。

### 3.3 Analyzer digest 漂移或更新检查虚假变化

**Symptom**

- `application.check-update` 在创建 Builder task 前失败，公开错误为
  `LAE_LUMA_VALIDATION_FAILED`，内部 Control 诊断包含
  `agentImageDigest is not allowlisted by Luma Control`；
- 相同 commit/source tree 连续检查时，每次 candidate DeploymentPlan digest 都不同；
- `sourceChanged=false`，但 `deploymentPlanChanged=true`，而结构化计划只有 `planId`、
  `sourceRevisionId` 或快照尝试 ID 不同。

**Checks**

1. 仅在管理员安全视图中比较以下三个值，必须逐字相同：
   - LAE Worker 的 `LAE_ANALYZER_IMAGE_DIGEST`；
   - Luma Control 的 analyzer allowlist/`LUMA_BUILDER_ANALYZE_IMAGE_DIGEST`；
   - Builder node agent 实际使用的 runner digest。
2. digest 必须是完整 `repository@sha256:...`，不能混用 tag、旧 registry host 或仅
   `sha256:` 值；不要把这些内部地址回传租户。
3. 对相同保存来源连续执行两次 check-update，记录 candidate source tree 与 plan
   digest。source 相同而 plan 不同则下载内部 DeploymentPlan artifact 做字段级比较；
   只比较闭合计划，不打印环境变量值或签名 URL。
4. DeploymentPlan 的稳定 seed 只能包含 resolved commit、source snapshot/tree digest、
   policy/knowledge version 和真实部署语义。`sourceSnapshotId`、`sourceRevisionId`、
   `planId`、task/operation ID、时间戳和 fetch attempt ID 都不能进入语义摘要。

**Safe action**

- digest 漂移时先构建并校验新的 immutable analyzer image，再按顺序更新 Builder agent、
  Control allowlist 和 LAE staging bundle；三方未一致前停止新的 analyze lane，不要扩大
  allowlist 接受多个未知 digest。
- 计划误报时修复稳定 seed，并增加“两个等价 source snapshot 产生相同计划”的回归
  测试。旧 deployment baseline 使用旧摘要算法时，可以用已成功的新 analysis 对同一
  revision 做一次批准的 staging 重部署来刷新基线；不能直接改数据库 digest。

**Verify**

1. 两次独立 check-update 的 candidate source/plan digest 完全相同；
2. 用其中一个 candidate 完成部署后再检查，baseline/candidate 的 source 与 plan digest
   均相等；
3. 终态 Operation 返回 `sourceChanged=false`、`deploymentPlanChanged=false`、
   `changed=false`；
4. 原应用 route 在 analyze 和重部署期间继续健康，失败候选没有覆盖 current deployment。

本轮 staging 的验证样本最终收敛到 plan digest
`sha256:2632aebaa16e9fa65fff706af46835c67d26f7e60d79329316df8947e9a4b804`；该值只用于
本次证据，不应硬编码到产品或未来 SOP 自动化中。

## 4. Luma Control 或 principal 配置不可用

**Symptom**

- LAE Worker 调用 Builder/Runtime 返回 401、403 或稳定 503；
- Luma Dashboard 的 LAE 页面显示 admin API unavailable；
- Git/object broker 全部失败；
- Luma Control 自身不健康或重启后所有 LAE 调用同时失败。

**Checks**

1. 执行第 3 节的 `luma status` 和 `luma doctor`，确认 Control、manager、node agents 和当前 context。
2. 在 manager 上检查已配置文件是否为 regular、非 symlink、仅 owner 可读写。路径必须位于 `/opt/luma/control`：

   ```bash
   sudo stat -c '%a %U:%G %F %n' \
     /opt/luma/control/lae-builder-principals.json \
     /opt/luma/control/lae-runtime-principals.json \
     /opt/luma/control/credential-broker.token \
     /opt/luma/control/object-broker.token \
     /opt/luma/control/lae-admin.token
   ```

   文件名以实际配置为准。principal JSON 和 token file 推荐统一 `0600`；不能是目录、空文件或链接。
3. 只读取 principal metadata，不读取 token：

   ```bash
   sudo jq 'to_entries | map({id:.key,tokenFile:.value.tokenFile,tenantScopeCount:(.value.tenantRefs|length),applicationScopeCount:(.value.applicationRefs|length)})' \
     /opt/luma/control/lae-builder-principals.json

   sudo jq 'to_entries | map({id:.key,tokenFile:.value.tokenFile,scopes:.value.scopes,builderPrincipalRefs:.value.builderPrincipalRefs,tenantScopeCount:(.value.tenantRefs|length),applicationScopeCount:(.value.applicationRefs|length)})' \
     /opt/luma/control/lae-runtime-principals.json
   ```
4. 验证 builder/runtime token 文件不同，management token 未复用；runtime scopes 至少与当前动作匹配，且 `builderPrincipalRefs` 指向真实 Builder principal。
5. 核对 Control 环境中的闭合 HTTPS URL：
   - `LUMA_CREDENTIAL_BROKER_URL` 的路径为 `/v1/internal/credential-leases/redeem`；
   - `LUMA_OBJECT_SOURCE_BROKER_URL` 的路径为 `/v1/internal/object-source-leases/redeem`；
   - `LUMA_LAE_ADMIN_API_URL` 只能是 LAE API origin，不带 query、fragment 或额外 path；
   - 对应 token 使用 `*_TOKEN_FILE`，不使用 inline secret。
6. 本地运行不接触 live secret 的回归测试：

   ```bash
   .venv/bin/python -m unittest \
     tests.test_lae_principal_files \
     tests.test_builder_credential_broker \
     tests.test_builder_object_source_broker \
     tests.test_lae_admin_proxy
   ```

**Safe action**

- 权限或文件损坏时，从批准的 secret manager/配置备份原子恢复文件，保持同目录、regular file、`0600`；不要把 token 改成 inline 环境变量。
- URL/timeout 错误需要 Luma Control 配置变更时先在 staging 更新并验证，再走 manager 变更流程。
- 轮换必须同时协调 LAE API 端与 Luma Control 端，见第 15 节；单边轮换会造成短时全失败。
- Control 仍可读且只是一个 staging task异常时，优先恢复该 task，不重新 bootstrap 整个 manager。

**Verify**

- `luma status` 恢复；
- Luma Dashboard LAE 六个只读资源（含“调度位置”）都能刷新且无 secret 字段；
- 专用 Builder 与 Runtime principal 分别成功，互相使用对方 token 仍失败；
- broker 的一次性 lease 可用一次，重放失败；
- management token 仍不能调用 LAE Builder/Runtime endpoint。

**Escalate**

出现 token 泄漏、`control.json` 中发现 principal secret、跨 principal/tenant 访问成功、文件权限无法收紧或 Control state 损坏时，立即升级安全事件；停止新部署但保留现有 workload，进入密钥轮换和状态恢复流程。

## 5. LAE Web/API 不可用

**Symptom**

- Web 白屏、登录页不可达；
- `/health/live` 失败；
- `/health/live` 为 200，但 `/health/ready` 为 503；
- 所有用户 API 5xx，或 Web 能打开但数据一直不可用。

**Checks**

1. 执行第 3 节的四个 HTTP 检查，区分 Web、API live、API ready 和 artifact store。
2. 在 Luma Dashboard 查看 `lae-platform[-staging]` 的 `web`、`api` task 状态、最近 events、image digest、日志和资源使用。
3. `health/live` 只证明进程响应；`health/ready` 证明核心服务 wiring 已建立，不替代 PostgreSQL 读写、邮件、Builder、Runtime 或 payment 的功能检查。
4. 检查 API 日志中的安全 `requestId`、error code 和 capability unavailable；不要要求用户提交原始 cookie/token。
5. 核对 migration 是否卡在 API 启动阶段。只读查看当前 revision，数据库凭据必须由批准的 secret-injected shell 提供，不能放在 argv：

   ```bash
   REPO_ROOT=/absolute/path/to/infra-stacks
   cd "$REPO_ROOT/lae"
   uv run --package lae-api alembic -c migrations/alembic.ini heads
   uv run --package lae-api alembic -c migrations/alembic.ini current
   ```

**Safe action**

- 先修复依赖或配置，不把 readiness 强制改成 200。
- 只有 staging 的 API/Web 进程明确卡死且依赖健康时，使用窄重建：

  ```bash
  .venv/bin/luma service restart lae-platform-staging --service api --mode recreate
  .venv/bin/luma service restart lae-platform-staging --service web --mode recreate
  ```

- 生产只通过批准的 Dashboard guarded action/变更单执行同等动作。本 SOP 不提供生产重启命令。
- 不在事故中执行未审查的 Alembic `upgrade`/`downgrade`。

**Verify**

- Web、live、ready 连续通过；
- `/v1/me` 对有效 session/token 工作，对无凭据仍为 401；
- 原 Operation 没有被重复创建；
- API 重启后 session、应用和事件仍来自 PostgreSQL，未退化为内存 fixture。

**Escalate**

API readiness 依赖冲突、migration 不一致、跨租户读取、持续 5xx、重复写或 Web/API contract drift 时升级平台研发与数据库负责人。

## 6. Worker 队列停滞或 Operation 长时间 queued/running

**Symptom**

- analysis/deployment/lifecycle Operation 长时间无新事件；
- API 可用但任务不推进；
- Worker 反复输出 `worker.schema-unavailable` 或 `worker.lane-failed`。

**Checks**

1. 用租户 CLI 只读查看并保存 cursor：

   ```bash
   lae operation show <operation-id> --format json
   lae operation watch <operation-id> --after <last-cursor> --timeout 30 --format ndjson
   ```
2. 在 Luma Dashboard 查看 Worker task 是否 running、最近退出码、OOM、CPU/memory 和 JSON 事件。
3. 在 Luma Dashboard LAE/Operations 视图确认 queued age、kind、phase、cancel requested，不直接改数据库 status。
4. 区分 lane：upload scan、source analyze、deployment create、check-update；一个 lane 失败不应被误判为所有 lane 完成。
5. 检查 PostgreSQL、artifact store、Luma Control 和对应 service principal 是否同时正常。

**Safe action**

- 网络中断时使用同一 cursor 续看，不重新 enqueue。
- Worker 进程在 staging 明确失活时：

  ```bash
  .venv/bin/luma service restart lae-platform-staging --service worker --mode recreate
  ```

- 等待旧 lease 到期后由新 Worker reclaim；不要手工清空 `lease_owner` 或伪造 checkpoint。
- 对单个用户任务使用 LAE cancel，见第 16 节；不要停止整个 Builder pool。

**Verify**

- 同一 Operation ID/cursor 单调继续；
- 没有重复扣配额、重复 build、重复 deployment 或第二个应用；
- crash 前已持久化 checkpoint 被复用；
- cancel requested 最终变为 terminal，late success 没覆盖 canceled。

**Escalate**

lease 永不释放、outbox 堆积、Operation/Checkpoint 不一致、任务重复执行或 Worker 重启后无法恢复时升级 LAE orchestration 负责人。

## 7. PostgreSQL 或数据一致性故障

**Symptom**

- API ready 503、Worker schema unavailable；
- 用户/应用/Operation 跨请求消失；
- migration 报错、连接耗尽、磁盘/volume 告警；
- 数据库 task pending/dead 或健康检查失败。

**Checks**

1. 查看 Luma Dashboard 中 `postgres` task、allocation events、健康检查、内存/OOM 和所在 storage class 状态。
2. 执行 storage class 清单和 import-mode Compose 校验；后者的结果已包含 storage validation：

   ```bash
   .venv/bin/luma storage list --format json
   .venv/bin/luma compose validate --import-mode \
     lae/deploy/luma/luma.compose.staging.yml --format json
   ```
3. 使用第 5 节的 Alembic `heads/current` 只读比较；不要在故障中盲目 upgrade/downgrade。
4. 检查最近成功 backup/PITR、WAL 连续性、容量和延迟。仓库当前没有可宣称生产完成的 PITR/restore automation；没有外部证据就按“无可验证备份”处理。
5. 检查是否是 NFS mount/locking/fsync 故障，而不是单纯进程故障。

**Safe action**

- 先冻结 schema 变更和新 deployment；保留读路径与已有 workload。
- 不默认重启 PostgreSQL，不删除 `postgres-data`，不切 `initialize: empty`。
- 需要 restore 时只恢复到隔离的新数据库/新 storage path，完成校验后再提交 cutover 变更，见第 14 节。
- 连接耗尽先定位泄漏/长事务并限制入口，不用重启掩盖。

**Verify**

- Alembic revision 与发布版本匹配；
- tenant/application/operation 数量、关键外键和唯一约束通过；
- API/Worker 恢复且同一幂等请求不生成重复记录；
- backup/restore 校验包含时间点、行数、约束和抽样业务对象。

**Escalate**

疑似数据损坏、WAL 缺口、双主、不可解释的数据回退、跨租户行或没有可用恢复点时立即升级数据库/安全负责人，停止所有写入型发布。

## 8. Git/object broker 或 LAE Builder analyze/build 失败

**Symptom**

- 私有 Git analysis 失败，公有 Git 正常；
- HTML/ZIP ready，但 object-source analysis 失败；
- Operation 卡在 source fetch/analyze/build；
- 稳定错误为 broker unavailable、lease unavailable、Builder capacity unavailable 或 artifact verification failure。

**Checks**

1. 从 LAE Operation 看 phase、status、cursor 和安全 error code，不索取 Git token、签名 URL或 raw Builder stdout。
2. 私有 Git 检查 connection 是否 active、host 与 repository 完全匹配、lease 未过期/未取消、operation/application/tenant/principal binding 一致。
3. Object source 检查 upload 为 `ready`，请求的 SHA-256、media type、size 与数据库一致；允许 host 必须等于 S3 endpoint host。
4. 检查 broker token file 和闭合 HTTPS URL，使用第 4 节测试。
5. `luma status --format json` 中确认 Builder node ready 且具备所需 capability；检查 builder queue/capacity、临时磁盘和 rootless executor。
6. 核对 Analyzer 沙箱镜像：Worker 的 `LAE_ANALYZER_IMAGE_DIGEST` 与 Luma Control/Builder 的 `LUMA_BUILDER_ANALYZE_IMAGE_DIGEST` 必须是完全相同的 immutable `repository@sha256:...`；目标 Builder 已预拉该 digest，rootless Docker 使用 `--pull never` 并能 inspect 本地镜像。不要用 tag、短 SHA 或“registry 中看起来相同”代替逐字比较。
7. 授权管理员可在 manager 上按 Operation 做**最小投影**；禁止 `cat control.json`：

   ```bash
   sudo jq --arg op '<operation-id>' '
     [(.builderTasks // {}) | to_entries[].value
      | select(.externalOperationId == $op)
      | {id,kind,status,builderNode,principalRef,tenantRef,applicationRef,externalOperationId,createdAt,startedAt,updatedAt,completedAt}]' \
     /opt/luma/control/control.json
   ```

   `builderNode`、principal、tenant 和 application 是内部数据，只能留在受控 incident evidence 中。
8. 区分 analyze 和 build：analysis 成功不代表镜像构建成功；build 必须返回逐 service OCI digest、SBOM/scan/provenance 并与固定 source snapshot 匹配。

**Safe action**

- 私有 Git credential 错误由用户轮换 source connection 后创建新 analysis；不要把 PAT 加到 URL、Git config 或 Luma state。
- broker 的 409 可能是 lease 已消费/过期/取消；不要重放旧 lease，沿原 LAE Operation 的安全重试逻辑处理。
- Builder capacity 不足时保留 queued Operation，扩容/恢复正确 pool 后再用相同任务恢复；不要临时把任务放到 manager/core 节点。
- Analyzer digest 缺失/不一致时先停止新 analysis；从批准的构建记录取得 immutable repo digest，推送到 Builder 可拉取 registry、在目标 Builder 预拉，再同步更新 Worker/Control 的两个配置。先在 staging 验证，不把 executor 改成在线 `pull always`。
- 用户取消通过 LAE Operation，不能直接改 `builderTasks`。
- object URL 永远只在内存中短时存在；不要为排障延长为永久公开 URL。

**Verify**

- anonymous public Git 不需要 broker credential；private Git 只在一次 task lease 中可用；
- object grant 只能 GET、host 精确匹配、无 redirect、过期后失效；
- analysis/build 使用同一 snapshot/commit；
- Analyzer runner 的 repo digest 在 Worker、Control 和 Builder 本地 inspect 三处一致，rootless executor 没有联网拉取漂移；
- 结果和日志不含 PAT、URL userinfo、object key、signed URL、registry credential；
- 每个 image 以 digest 绑定，mutable tag 不能在 retry 中漂移。

**Escalate**

发现凭据进入 argv/env/log/state/layer、签名 URL 可重用、跨 tenant lease、Builder 接触 Docker socket/宿主设备或 digest 不一致时，按供应链安全事故升级并停止新 build。

## 9. Runtime placement、无容量或 volume affinity

**Symptom**

- 租户侧 `LAE_CAPACITY_UNAVAILABLE`；
- 内部 Luma `capacity_unavailable` / `volume_placement_incompatible`；
- Runtime placement unavailable；
- 更新后 pending，或节点故障后没有重调度。

**Checks**

1. 向租户只确认 region 和稳定错误，不提供候选节点、IP 或资源维度。
2. `luma status --format json` 检查目标 region 内：Nomad ready/eligible、node agent 新鲜、runtime capability、非 drain、Docker healthy。
3. 确认 builder-only、core/stateful 和未显式 runtime opt-in 的控制面节点没有被误纳入；当前 staging 的 manager 应同时满足 runtime role 与 allowlist 两个条件。
4. 对 Compose 汇总所有 service CPU/memory，而不是只看主 HTTP service。
5. 有 volume 时检查 runtime storage class：provider/mode、region、storage node、eligible nodes、failure domains、跨 region Tailscale 可达性。
6. 授权管理员优先在 Luma Dashboard 的 LAE →“调度位置”查看 runtime deployment、tenant/application、region、候选、preferred node、实时 allocation 和 continuity。该页面使用 Luma management token，只能留在内部审计上下文；真实 staging 尚未验证时，空表不代表 placement 不存在。
7. Dashboard/API 不可用且事故需要时，才在 manager 安全终端读取 placement 最小内部投影：

   ```bash
   sudo jq --arg app '<application-id>' '
     [(.laeRuntime.deployments // {}) | to_entries[].value
      | select(.applicationRef == $app)
      | {runtimeDeploymentRef,status,operationRef,revisionRef,deploymentRef,jobSlug,placement,updatedAt}]' \
     /opt/luma/control/control.json
   ```

   该输出可能包含 node ID，只能在内部安全终端/审计库使用。对外仅允许使用 `.placement.summary` 中的 region、candidate count、资源汇总、stateful、continuity 和 decision digest。
8. Nomad plan 的详细 `FailedTGAllocs` 只作内部证据，不复制给租户。

**Safe action**

- 内部 `capacity_unavailable`：先恢复/扩容正确 region 的 runtime pool，或减少下一 revision 的资源；不把 workload 临时调度到 builder，也不把未显式 opt-in 的控制面节点加入 allowlist。当前 manager 已是批准的 staging runtime，不应绕过 Nomad plan 强制落点。
- 内部 `volume_placement_incompatible`：修复 storage class/eligible-node/region，可验证迁移后才采用新后端；不能通过删除 volume 或改为 unmanaged 绕过。Runtime adapter 对外只返回不可重试的 `LAE_CAPACITY_UNAVAILABLE`，不会暴露是容量还是卷拓扑维度。
- 若 Luma 409 是明确 conflict，公开层才映射为 `LAE_IDEMPOTENCY_KEY_REUSED`；未知 409 必须作为协议错误 fail-closed。排障仅使用 bounded `errorInfo.code/requestId`，不复制内部 message、node 或 IP。
- prior node 不健康时允许调度器在兼容候选中 reschedule；prior node/failure domain 只是 affinity。
- 不向用户提供“指定机器”开关，也不把 node/IP 写入公开 Application/Deployment。

**Verify**

- exact rendered group 的 Nomad plan 无 failed allocation；
- job 仍有 region constraint 和内部 candidate set constraint；
- candidate set 是配置的正向 allowlist 与 readiness/region/capability/storage 筛选的交集；builder-only/control/core 节点只有满足其 policy（manager 需显式 runtime role）时才可能进入；
- Luma Dashboard placement 行的 active allocation 与 Nomad 当前 allocation 一致；
- 有状态应用在新节点能挂载同一受管 volume，数据校验通过；
- 用户 API、Web、CLI 和日志中没有 node name/ID/IP/failure domain。

**Escalate**

候选集错误、跨 region 放置、volume 数据不一致、错误把用户 workload 放到 builder/core/未 opt-in 的控制节点，或 placement 细节泄露到租户响应时升级运行时/安全负责人。

## 10. Runtime 已提交但状态不健康

**Symptom**

- Operation 在 deploying/verifying；
- service pending/dead/restarting/OOM；
- route pending，上一版本仍健康；
- Compose 内部依赖启动顺序导致短期失败。

**Checks**

1. 从 LAE `apps show`、Operation、logs 和 metrics 查看产品状态；不要只看 allocation running。
2. Luma Dashboard 查看 job/allocation/task event、image digest、health check、OOM/exit code 和 selected node（admin only）。
3. 检查所有必需 service、内部依赖、端口唯一、环境变量 schema、secret lease 和 volume mount。
4. Compose 当前是一个 task group；一个大 service 的资源不足会影响整组。
5. 确认 health path 是本地 HTTP endpoint，应用确实监听声明的 container port。
6. 若 `artifact-init` 退出 `137`、Nomad event 为 OOM 或 API/Worker 一直等待它，
   检查 rendered task 资源。真实 staging 已证明 `128M` 会触发 OOM；当前硬底线为
   `reservations.memory: 256M`、`limits.memory: 512M`。在 Nomad 中 reservation 才是
   实际调度/运行内存，limit 是 `memory_max`；不能只提高 limit 或把 reservation
   降回 `128M`。

**Safe action**

- 等待正常 rollout/health grace；短暂 pending 或单次 502 不立即重试。
- 只对用户明确要求的 app 发起 LAE restart，随后观看同一 Operation。
- 新 deployment 失败时保留上一健康版本，不手工把 current pointer 指向失败 revision。
- 内部依赖未就绪时修复应用重试/backoff；不能依赖当前 Luma Compose `depends_on` 作为严格生产启动顺序。
- `artifact-init` OOM 时先恢复上述 256M/512M 资源并重新 render/plan，再窄重建该
  task group；不要通过去掉初始化、放宽 bucket policy 或手工伪造 ready 文件绕过。

**Verify**

- 所有 required service healthy；
- route probe ready；
- Application observed state 与 Luma/Nomad 一致；
- 新 deployment 成功后才更新 current pointer；
- 失败 revision 保留审计证据但不接流量。
- `artifact-init` allocation 没有 OOM event，reservation/limit 分别渲染为
  256MiB/512MiB，bucket、service account 和 CORS 初始化结果通过实际读写验证。

**Escalate**

持续 crash loop、不可解释的 allocation replacement、runtime secret 缺失、健康检查与真实服务冲突或 reconciliation 长期不收敛时升级 runtime 负责人。

## 11. Route、DNS 或 TLS 故障

**Symptom**

- 随机域名 NXDOMAIN；
- TLS 证书错误；
- 404/502/504；
- 一个 Compose HTTP route 正常，另一个异常。

**Checks**

1. 从 LAE Application/route 记录取得精确 hostname 和 health path，不自行拼域名。
2. 外部只读检查：

   ```bash
   dig +short <random-hostname>.itool.tech A
   dig +short <random-hostname>.itool.tech AAAA
   curl --silent --show-error --output /dev/null \
     --write-out '%{http_code} %{remote_ip} %{time_total}\n' \
     https://<random-hostname>.itool.tech/<health-path>
   openssl s_client -connect <random-hostname>.itool.tech:443 \
     -servername <random-hostname>.itool.tech -brief </dev/null
   ```
3. Luma Dashboard 检查 route、Traefik、DNS sync、certificate、runtime target 和 allocation health。
4. 404 可能表示根路径不存在；以声明的 health path 判断。502/504 需要区分 route 已发布但 upstream 未健康。
5. 多 HTTP Compose 必须逐 hostname 检查；不能用 primary route 代表全部 route。
6. 核对 wildcard DNS/TLS，而不是为每个随机域名单独申请证书。

**Safe action**

- 上游健康但 route 缺失时，通过批准的 Luma route reconciliation 恢复；不手写 Traefik watched file。
- TLS 失败先确认 wildcard 证书、SNI 和证书时间，不关闭验证或给用户 HTTP fallback。
- 只有 edge/Traefik 本身有明确故障证据时才走 guarded restart；不要因一个 app 502 重启整个 edge。
- 不把自定义域名临时接入 V1。

**Verify**

- DNS 在预期 resolver 收敛；
- TLS hostname/有效期/链正确；
- 每条 route 的 health path 返回 2xx/3xx；
- LAE route status 与外部 probe 收敛；
- 更新、restart、suspend/resume、rollback 后随机域名不变。

**Escalate**

wildcard DNS/TLS 整体故障、证书私钥风险、route 指向其他 tenant、错误 hostname ownership 或多个 edge 观测不一致时升级网络/安全负责人。

### 11.1 控制面升级或其他应用部署后批量 404/502

**历史 P0 与当前边界**

`0.1.171` 曾出现组合故障：升级 Control、更新 Docker registry/proxy 配置或发布
另一个应用后，既有 route 可能因 service/router 名称碰撞、跨节点 private-IP
upstream、Docker/CNI 失效或 reconciliation 不收敛而返回 404/502。`0.1.190` 已使用
deployment-scoped service/router 名称，`0.1.192` 已使用 Nomad node
`luma_tailscale_ip` metadata 注册 edge upstream。当前 Control、manager 与在线 fleet
已升级到 `0.1.196`，LAE 平台为 Job v21；升级无需人工重启，针对 LAE Web 的 Control
route sentinel 为 1/1 成功。但长时间外部探针仍有少量 LAE 404/502/timeout，不能把
“没有批量故障”写成“零中断”。

正常发布路径的已复现回归已经关闭，但 Docker daemon restart 后的 CNI 自愈和 route
reconciliation 故障注入仍未完成。手工 recreate allocation 仍只能作为保留证据后的
staging 恢复动作，不能重新成为发布协议。

先按响应层次分类：

- **Traefik 原生 404**：请求已到 edge，但 exact host router 不存在或 watched route
  尚未收敛；检查 route 记录、Traefik 动态配置和 reconciliation。
- **502/504**：router 已匹配，但对应 allocation IP/port 不可达或应用未健康；优先
  检查 CNI、allocation 网络、container port 和 health。
- **应用自己的 404**：router 与 upstream 均工作，只是 path 不存在；用声明的
  health path 复验，不能用 `/` 一概判断。

Control 的全量 route sentinel 当前把所有非 2xx/3xx 都计为失败，因此 API 根路径、
agent/controller 根路径等有意返回 404/501 的服务会产生假失败。发布门禁应传入明确的
sentinel domain，并为每条 route 保存期望 path/status；在该能力补齐前，全量 sentinel
只能作为发现信号，不能单独决定回退。

**Checks**

1. 记录变更前后时间、Luma release、Control job version、受影响 hostname 和一个未参与
   变更的 sentinel hostname；逐个保留状态码、响应头、remote IP 和时间。
2. 只读比较 `luma status --format json`、对应 service history、Nomad job version、
   evaluation/deployment、allocation `JobVersion`、task state 和 health。旧版本仍健康的
   allocation 不能证明本次提交成功。
3. 在目标节点核对 Docker daemon 的 restart/active 时间和变更窗口日志；若 restart
   晚于 allocation 创建时间，再检查该 allocation 的 network namespace、`eth0`、
   Nomad CNI bridge、init container 与目标端口。不要因为容器显示 running 就跳过。
4. agent 诊断会在
   `nodes.items[].diagnostics.nomad.cniHostPorts.missingNetworks` 提供只读 evidence：仅在
   Linux 上检查运行中的 `nomad_init_*`，通过 allocation label 与容器 PID 读取网络
   interface；严格只有 `lo` 时才报告 `allocId/container/name/interfaces`。旧 agent
   没有该字段是预期状态；字段为空也只是 fail-safe“未发现”，因为无 init
   container、Docker/proc/awk 不可用时不会制造误报，不能单独证明 CNI 健康。
5. 检查本次命令是否真的改变 registry/proxy/`NO_PROXY` 配置。期望配置完全相同时，
   任何 Docker restart 都应视为幂等性缺陷。
6. 对 404 检查 route source-of-truth 与 Traefik watched config 是否同时存在 exact
   hostname；对 502/504 从 edge/manager 与 runtime 节点分别探测 allocation target。

**Safe action — staging 恢复**

- 先保留证据，再只 recreate 已确认 CNI 失效的受影响 service/allocation；不要把“重启
  所有应用”当成固定发布步骤，也不要先重启 edge、manager 或 Docker。
- router 缺失而 upstream 健康时，执行经过批准的 route reconciliation；不手写
  Traefik 文件。
- Docker 配置确需变化时必须使用变更窗口。daemon restart 后枚举并修复受影响的旧
  allocation，再验证所有 sentinel routes；不能只验证本次新应用。

**永久修复的发布门槛**

1. registry/proxy/`NO_PROXY` 与目标值一致时配置操作完全幂等，不重启 Docker。
2. 不可避免的 Docker restart 之后，系统自动诊断/修复 CNI 或安全重建受影响
   allocation，并触发 route reconciliation；不能依赖人工逐应用重启。
3. Nomad submit 必须把本次返回的 `JobModifyIndex` 关联到 evaluation、deployment、
   exact `JobVersion` 和该版本的新健康 allocation；历史 healthy allocation、历史
   terminal deployment 或仅 `running` 状态不能越过 rollout barrier。失败、blocked、
   canceled、superseded 和 timeout 均应返回明确失败。
4. manager upgrade、fleet update、registry/proxy 配置和任一应用发布的验收，都要同时
   探测 Control、变更应用以及至少一条未变更 sentinel route，并执行故障注入回归。

**Verify / Escalate**

只有 exact 新 JobVersion 的 required task group healthy、全部声明 route 和 sentinel
route 连续通过，且无需人工全量重启，才算恢复。任一跨应用 404/502、Docker 无差异
重启、running-but-no-CNI、route 与 source-of-truth 漂移，均升级为 Luma Control/
Nomad/CNI P0；在修复发布并完成回归前阻塞 production rollout。

## 12. 邮件注册/登录故障

**Symptom**

- 用户请求成功但没有收到邮件；
- 验证码/一次性链接普遍过期或重放；
- registration/login 大面积失败。

**Checks**

1. 公开 request endpoint 为防账户枚举会统一返回 accepted，HTTP 202 不证明 SMTP 已送达。
2. 检查 API ready、`requestId`、发送结果指标和安全日志；日志不得包含验证码或 magic token。
3. staging 检查 Mailpit task health 和 API 到 `mailpit:1025` 的内部连接；Mailpit 没有公网 route，也不会把邮件投递到用户真实邮箱。
4. 当前外部 SMTP 配置实测返回 `535` 鉴权失败，不能作为可用邮件通道；修复 credential/provider 后再检查 host/465/TLS、发件人、退信/限流、SPF/DKIM/DMARC 和 DNS。
5. 检查验证码 TTL、失败次数锁定、设备/IP 限流和服务器时间。

**Safe action**

- 只在 staging 使用专用 canary 邮箱执行一次注册/登录 synthetic；不要使用真实用户邮箱，也不把 OTP 复制到工单。
- provider 限流时降低发送速率并保持统一响应，不关闭 abuse limit。
- 配置错误通过 secret manager 修复后先发 staging canary，再生产灰度。
- 不读取用户邮箱，不要求用户提供完整邮件链接。

**Verify**

- staging canary 在 TTL 内送达；
- code/magic token 一次消费，重放失败；
- 不存在账户仍不能从响应差异枚举；
- 注册原子创建 personal tenant、Lite entitlement 和只显示一次的默认 deploy token。

**Escalate**

验证码泄露、账户枚举、同一 challenge 多次成功、跨邮箱消费或 provider 凭据泄漏时升级身份/安全负责人。

## 13. 套餐、配额或支付故障

**Symptom**

- checkout 503；
- mock 订单完成后 entitlement 不变；
- webhook 重放/乱序；
- 配额计数异常或重复扣用量。

**Checks**

1. 确认环境：production 当前应为 `LAE_BILLING_DRIVER=disabled`；staging 才允许 `mock`。生产 disabled 返回 503 是正确门禁，不是支付成功率事故。
2. 使用 Luma Dashboard LAE 的 tenant/usage/operation 只读视图检查 plan、订单相关 Operation 和当前用量；不读取支付 secret。
3. staging 用专用 tenant 查看：

   ```bash
   lae plans list --format json
   ```

4. 检查同一 provider event 的签名、merchant、order、amount/currency、幂等、时间和顺序；不要只按客户端回跳页面判成功。
5. 检查 quota reservation 是否在 terminal success 转 used、失败/取消释放、crash 后由 TTL/reconciler 回收。

**Safe action**

- mock 只在 staging 由人工完成 checkout；Agent 不代付。
- webhook 重放按原 event id 幂等处理，不手工改 subscription/ledger。
- provider 不确定时保持订单 pending 并 reconciliation，不先给 entitlement。
- quota 争议保留 ledger/reservation evidence；不直接扩大单个 tenant 数据库值。

**Verify**

- 同一 event 重放不重复切 plan/记账；
- amount/provider/merchant mismatch 失败；
- 乱序旧事件不能覆盖新状态；
- 超配额不删现有数据，允许查看/删除/支付但阻止新增资源。

**Escalate**

真实资金与订单不一致、重复 entitlement、跨 tenant 订单、签名绕过或 ledger 不守恒时升级支付/财务/安全负责人。真实微信/支付宝 adapter 未完成 provider sandbox 前不得开启 production。

## 14. 备份和恢复

**Symptom**

- PostgreSQL、MinIO、registry、Luma state 或用户 volume 数据丢失/损坏；
- backup 失败或超过 RPO；
- 需要按时间点恢复或迁移 storage class。

**Checks**

1. 明确数据集、tenant/app、期望时间点、最近可验证 backup、RPO/RTO 和加密 key 版本。
2. 检查 storage class 和 import-mode Compose validation 中的当前挂载/存储结果：

   ```bash
   .venv/bin/luma storage list --format json
   .venv/bin/luma compose validate --import-mode \
     lae/deploy/luma/luma.compose.yml --format json
   ```
3. 验证 backup 不在同一故障域/同一 named volume；单个 NFS volume 不是备份。
4. PostgreSQL 检查 base backup + WAL 连续性；MinIO/artifact 检查 object version/hash；registry 检查 active/rollback manifest 与 blob；Luma state 检查 snapshot 与 Nomad/routes 的时间一致性。
5. 检查对应 AEAD/HMAC/signing key 是否可用；恢复密文而缺 key 等于不可恢复。

**Safe action**

- 先停止相关写入或隔离受影响 tenant，不删除原数据。
- 恢复到新数据库、新 bucket/prefix、新 registry namespace 或新 volume path；禁止直接覆盖生产源。
- 在隔离环境做 schema、约束、行数、对象 hash、image pull、应用级读写和 route dry verification。
- storage 迁移验证后才使用 `adopted: true`；`initialize: empty` 只用于全新数据集，不能绕过迁移。
- cutover、回退点和旧数据保留时间必须有变更单和双人复核。

**Verify**

- 恢复时间点满足声明；
- PostgreSQL 约束/tenant fence/幂等记录正确；
- artifact SHA、registry digest、volume 文件和应用级数据抽样一致；
- Luma/LAE current deployment、routes 和 runtime state 对账；
- 完成一次从 backup 到可用 staging 的实际 drill，不只检查文件存在。

**Escalate**

没有可验证 backup、key 缺失、WAL/blob 缺口、恢复包含其他 tenant、RPO 超标或需要生产 cutover 时由 incident commander、数据库/存储/安全负责人共同决策。

当前仓库仍缺生产 PITR、对象/registry/volume restore automation 和演练证据；这是一项公开发布硬门禁。

## 15. Secret 和 service principal 轮换

**Symptom**

- token/SMTP/S3/Git/payment/AEAD key 疑似泄漏；
- 到期轮换；
- principal 文件权限或作用域需要收紧。

**Checks**

1. 识别 secret 类型、生产者、消费者、存储位置、版本、作用域和可否双 key 并行。
2. 搜索安全日志和审计中是否出现 fingerprint/异常访问；不要搜索/打印明文。
3. principal token 必须区分 management、Builder、Runtime、credential broker、object broker 和 admin；不可复用。
4. AEAD keyring 检查新旧版本是否同时可解密；HMAC/signing key 的轮换语义与 AEAD 不同，不能随意删除旧 key。
5. Git source connection 由 tenant 独立轮换，不更改平台 principal。
6. Control Job 的非内联密钥配置必须持久化在 manager 的
   `/opt/luma/control/control.env`；检查它是 root-owned regular file、不是 symlink，
   且 mode 为 `0400` 或 `0600`。不要以当前 shell 里暂存的 `export` 作为升级输入。

**Safe action**

- **可版本化 keyring**：先加入新 key，验证读旧/写新，切 current version，后台重加密，确认没有旧版本密文后才移除旧 key。
- **Builder/Runtime principal**：创建全新随机 token file，保持 config/token regular `0600`，先在 staging 协调 LAE 与 Luma 两端，再原子切换；两种 principal 永远不同。
- **broker/admin token**：API 端和 Control token file 必须在同一维护窗口切换；失败时回滚到上一文件，不降级为 inline token。
- **S3 credential**：先创建最小权限新 access key，验证 API/Worker 各自权限，再切换并撤销旧 key；API 与 Worker credential 不复用。
- **用户 deploy token/source connection**：由用户或受控支持流程新建/轮换，确认新 token 后撤销旧 token；不通过聊天传递。
- **Control 环境**：由可信 bundle 生成器产生严格 `NAME=value` 文件，先以 `0600`
  原子安装到 `/opt/luma/control/control.env`，再执行 manager refresh。不得 `source`
  该文件，也不得手工添加 allowlist 外键；显式命令环境只在维护窗口用于覆盖同名持久
  值，验证通过后仍应把最终值写回固定文件，避免下次升级回退到旧配置。
- 所有动作先 staging；生产需要变更批准和回退计划。

**Verify**

- 旧 secret 被撤销后确实失败，新 secret 只在预期 audience/scope 成功；
- management/Builder/Runtime 交叉使用失败；
- 日志、state、argv、image layer 和 provenance 无明文；
- 旧 AEAD 密文完成重加密，恢复备份仍包含所需历史 key；
- 轮换未重复 Operation、build 或 billing event。
- `nomad job inspect -json luma-control` 中的 allowlist Env 与
  `/opt/luma/control/control.env` 一致（维护窗口显式覆盖项除外），再次执行
  `luma update manager` 后 service principal、broker、signing 和 placement 配置仍在。

**Escalate**

任何 secret 已进入 Git/image/log/public response，跨 audience 可用，无法确定影响范围，或缺少旧 key 导致数据不可解密时按安全事件处理并暂停新写任务。

## 16. Cancel、孤儿任务和 GC

**Symptom**

- 用户取消后 Builder/Runtime 仍在运行；
- Operation terminal，但 agent task、upload、artifact、image、route 或 volume 残留；
- storage/registry 持续增长；
- GC 可能删除 current/rollback 资源。

**Checks**

1. 查看 LAE Operation、`cancelRequested`、terminal status、cursor、checkpoint 和关联 deployment。
2. 用户侧安全取消并续看：

   ```bash
   lae operation cancel <operation-id> \
     --idempotency-key <cancel-key> --format json
   lae operation watch <operation-id> --after <last-cursor> --format ndjson
   ```
3. 用第 8 节的 Builder task 投影检查 parent/agent task；用第 9 节的 runtime 投影检查 deployment/job。
4. 在 LAE DB/read model 中检查 upload/source revision/artifact/image/deployment 引用计数和 retention deadline。
5. 确认 current deployment、上一可回滚 deployment、active build、未决 restore 和法律/安全 hold 均不会被 GC。

**Safe action**

- 取消由 LAE 向下游转发；用户取消在 LAE 状态上优先于 late Builder success。
- 不直接从 `control.json` 删除 task。等 lease/heartbeat 超时，reconciler 标记 orphan 后再按 retention 清理。
- upload 使用明确 delete；artifact/image 先解除所有引用并经过 grace period；volume 默认 retain。
- route orphan 先验证 hostname ownership 和 current deployment，再由 Luma reconciliation 移除。
- GC 当前尚未完成生产级自动化时，只做隔离、标记和容量保护，不做临时批量删除。

**Verify**

- Operation terminal 且 cursor 不再变化；
- canceled 不能被 late success 覆盖；
- agent/runtime 不再消耗资源；
- current/rollback digest、volume 和 route 仍可用；
- 删除有 audit、对象数/字节数和 hash evidence；
- 重复执行 GC 幂等。

**Escalate**

跨 tenant 删除、current/rollback 资源被回收、取消无法收敛、orphan 数量增长或存储接近满载时升级 orchestration/storage 负责人；在证明引用图正确前停止 destructive GC。

## 17. 生命周期动作故障

**Symptom**

- suspend/resume/restart/rollback/delete/check-update Operation 不推进；
- desired state 与 observed state 长期不一致；
- rollback 指向错误 deployment；
- delete 意外触及 volume。

**Checks**

1. 查看应用 current deployment、目标 action、lifecycle request binding、Operation phase 和幂等键。
2. check-update 属于 analysis lane；其他动作属于 Runtime lifecycle，不能互相替代。成功的 check-update 应只在终态 Operation 公开闭合的 `updateCheck`，并包含 baseline 可用性、source/DeploymentPlan 是否变化、SHA-256 digest、candidate analysis 以及 service/route/volume/environment 差异；普通 analysis、失败或运行中的 Operation 不应返回该字段。若 plan 已变化但 `changes` 为空/null，说明是旧版或不完整结果，必须重新检查，不得直接部署。
3. destructive update 的确认以 Operation 返回的 `changes.confirmations` 为唯一来源。Web/CLI 只能在用户明确批准后原样提交；API 返回 `LAE_DEPLOYMENT_CONFIRMATION_REQUIRED` 时按 `requiredConfirmations` 重新展示风险，不能自动补齐。`LAE_UPDATE_CHECK_DETAILS_REQUIRED` 表示先重新运行 check-update。
4. rollback target 必须属于同 tenant/application、已成功且仍有 image/plan；不能由用户提交任意 Luma job/version。
5. LAE 普通产品 delete 固定使用 `volumePolicy=retain`；Luma Runtime body 必须显式携带该值。卷数据删除是独立、可审计且尚未开放的流程，不能把普通 delete 改成 `delete` 绕过。
6. Luma Runtime principal scopes、current runtime binding、jobSlug 和 secret/volume cleanup 状态必须一致。
7. V1 rollback 要求目标与当前 application catalog 的 service/route/volume binding 拓扑兼容；不兼容必须 fail closed 后走新的 analysis/deployment。
8. 检查 Runtime mutation durable checkpoint：提交前 cancel 应恢复原 desired state；提交后 late cancel 不能假装底层动作未发生，必须继续收敛同一 Runtime 结果。

**Safe action**

- 对 retryable failure 使用同一 Operation 的恢复语义，不创建竞争动作。
- restart 不接受新 manifest；源码变更走 check-update + analysis + deployment。
- rollback 前展示 route/env/volume diff并人工确认。
- delete 先 suspend/隔离 route 是否必要由 incident commander 决定；不默认删除 storage。

**Verify**

- desired/observed 收敛；
- check-update 无基线时保守标记有变化，有基线时 `changed == sourceChanged || deploymentPlanChanged`，且不会自动切换 current deployment；
- stable hostname 保持不变；
- rollback 只有成功后才更新 current pointer；
- delete 后 route/job/secret variable 按策略清理，catalog volume 明确标记并保持 `retained`；
- 失败动作不覆盖上一健康 deployment。

**Escalate**

lifecycle Worker 未启用/配置失败、状态机卡死、current pointer 错误、重复动作并发或 volume policy 不明确时停止该应用后续写动作，升级 LAE lifecycle/runtime 负责人。当前 executor 代码、自动化测试与 PostgreSQL 17 migration-backed 集成已验证；真实 Luma staging 的故障恢复证据仍是发布门禁。

## 18. 事故关闭和升级材料

关闭前必须具备：

- 用户影响、开始/恢复时间、region、错误率和受影响 tenant/app 数；
- 公开 ID：request/application/analysis/deployment/operation 和 cursor；
- 内部 ID/placement 只存受控 incident evidence，不进入用户复盘；
- 根因、触发条件、为何现有门禁未拦截；
- 执行过的命令和变更批准；
- 恢复验证、数据完整性、secret 暴露判断；
- 新增回归测试、监控/告警和文档 owner/截止日期。

按以下边界升级：

| 现象 | 首要 owner | 必须同步 |
| --- | --- | --- |
| Luma Control、node、placement、route | Luma/runtime | LAE on-call、网络/存储 |
| API、Worker、Operation、lifecycle | LAE backend | DB/runtime |
| PostgreSQL/backup/restore | Database | LAE、storage、安全 |
| MinIO/registry/volume/GC | Storage | Builder/runtime、安全 |
| Git/object broker、image provenance | Builder/security | LAE、Luma |
| 登录/邮件/deploy token | Identity/security | LAE、邮件 provider |
| 计费/webhook/ledger | Billing/finance | LAE、安全 |
| 跨租户或 secret 泄漏 | Security incident commander | 所有相关 owner |

## 19. Runbook 自检

文档或代码变更后，至少执行以下无部署检查：

```bash
REPO_ROOT=/absolute/path/to/infra-stacks
cd "$REPO_ROOT"
.venv/bin/python -m unittest \
  tests.test_lae_placement \
  tests.test_lae_principal_files \
  tests.test_lae_runtime_api \
  tests.test_lae_admin_proxy \
  tests.test_lae_luma_deploy_assets

bash -n scripts/setup-lae-builder.sh
sh -n lae/deploy/luma/docker/api-entrypoint.sh \
  lae/deploy/luma/docker/artifact-init.sh \
  lae/deploy/luma/docker/worker-entrypoint.sh \
  lae/deploy/luma/smoke-images.sh

cd lae
make check PYTHON=.venv/bin/python
```

`scripts/setup-lae-builder.sh` 使用 Bash 语法，必须由 `bash -n` 检查；其余列出的容器脚本是 POSIX `sh`，使用 `sh -n`。若 workspace Python 路径不同，以仓库当前 CI/Makefile 为准。自动化测试和本地完整 staging Compose E2E 通过仍不等于 `Verified`；真实 Luma staging 还必须覆盖多 HTTP Compose、私有 Git、HTML/ZIP、无容量、节点故障重调度、volume affinity、route/TLS、cancel/orphan 和 backup/restore drill。
