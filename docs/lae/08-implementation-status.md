# 08. 实施状态与验收证据

> 本文件是 LAE 完成度的权威清单。设计完成、代码存在或单元测试通过都不自动等于产品可用；只有对应“完成证据”全部成立，条目才能标为 `Done`。

## 1. 状态定义

| 状态 | 含义 |
| --- | --- |
| `Not started` | 尚无可执行实现 |
| `In progress` | 已有对最终目标有用的实现，但完成证据不全 |
| `Implemented` | 代码与自动化测试完成，尚未通过真实 Luma staging/生产验证 |
| `Verified` | 在真实 Luma staging 完成端到端、故障和安全验证 |
| `Done` | 生产发布门槛满足，用户路径与运维路径均有证据 |

禁止用以下证据单独标记 `Done`：页面截图、静态 mock、只测 happy path、只验证 manifest 能解析、只看到 Nomad allocation running、只看到 HTTP 200、未覆盖租户隔离的单元测试。

### 当前结论（2026-07-11）

- **代码与自动化 gate：** Luma `0.1.171` 候选全量 719/719 项测试通过；LAE `make check` 为 351 项通过、23 项条件集成测试跳过。当前 live fleet 仍为 `0.1.170`，版本升级完成前不能把候选能力视为 live。
- **真实 Luma staging：** 9 个平台 task 全部健康、零重启，三个公网域名 TLS 有效；Web、API live/ready 和 artifact ready 探针均返回 200。平台固定在 `lab`，租户 runtime 候选池为 `manager + tecent`。
- **产品 E2E：** 真实邮件注册（经 staging Mailpit）、默认 deploy token、CLI 登录/身份/模板/套餐/应用、模板 launch 与 analysis 已执行。租户 source → Builder → Runtime 部署、随机域名、观测和 lifecycle 动作矩阵仍待本轮最终 E2E；下述 production blockers 继续有效。

## 2. 用户需求追踪

| ID | 必须实现的结果 | 当前状态 | 完成证据 |
| --- | --- | --- | --- |
| R-001 | LAE 及 PostgreSQL、对象存储、registry、观测等全部由 Luma 部署 | In progress | 全部 Luma manifest/sidecar validate/render；staging deployment healthy；恢复演练通过 |
| R-002 | 邮箱注册/登录，自动 personal tenant、Lite entitlement 和默认 deploy token | In progress | 邮件送达、过期/重放/限流、session、token once-display/revoke E2E |
| R-003 | HTML/ZIP、GitHub、私有 Git、Dockerfile、Compose 来源可诊断和部署 | In progress | 每类 golden source 在 staging 完成 inspect -> env -> build -> deploy -> verify；非法样本稳定拒绝 |
| R-004 | Compose 支持多个公网 HTTP service、内部服务、worker、datastore 和 named volume | In progress | 双 HTTP 域名、逐 route probe、内部依赖、数据库持久化和端口冲突用例通过 |
| R-005 | 暂不支持 `tcp-relay`，TCP/UDP/host port 在 LAE 与 Luma 双层拒绝 | In progress | contract、Agent policy、Luma policy 正反例测试与 staging 拒绝证据 |
| R-006 | 独立 LAE Agent 公共端点，需要 session 或 deploy token | In progress | `POST /v1/analyses`、公开 GET、cursor event replay 与 cancel 已覆盖 Bearer scope、cookie CSRF、双凭据冲突、tenant fence、原子 enqueue、幂等 replay 和 late-success fence；仍需 SSE、限流、配额和审计 E2E |
| R-007 | Git fetch、Agent runner、单/多镜像 build 统一在 Luma builder 执行 | In progress | Builder Task typed API、不可变 snapshot、Git/object 单次 redemption、真实 cancel、rootless sandbox、digest output E2E |
| R-008 | 识别必需环境变量，部署前补齐，部署后可管理且 secret 不回显 | In progress | 公共 env metadata/CAS/set/unset、AES-GCM envelope、AAD/HMAC、runtime secret lease/injection、幂等/tenant fence 已实现；仍需多框架 evidence 和 staging 日志/镜像/provenance 无 secret E2E |
| R-009 | 模板可一键拉起，失败模板自动下架 | In progress | 已有 pinned/versioned catalog、Web/CLI launch 和正常 Agent analysis 门禁；仍需每日 smoke、自动下架与真实 deployment E2E |
| R-010 | 应用列表/详情可看状态、服务、路由、日志、指标，并可 suspend/resume/restart/rollback/delete | In progress | tenant-scoped catalog/observability API、Web 日志指标与全部 lifecycle 入口、API/Worker 均已实现，自动化测试证明失败不切 current；仍需逐 action 真实 staging |
| R-011 | CLI 与 Web 等价，deploy token 可登录/诊断/部署/续看/管理 | In progress | JSON/NDJSON contract、非交互退出码、cursor resume、token 不进 argv/日志 |
| R-012 | 提供 AI 友好的 LAE Skill，覆盖注册、登录、诊断、部署、管理和支付人机边界 | In progress | Skill 验证、clean-room Agent 执行、secret/payment 安全测试 |
| R-013 | Lite/Pro/Ultra、月付/年付、mock 及微信/支付宝 adapter、服务端硬配额 | In progress | entitlement/ledger/reservation、webhook 重放乱序、降级、mock 与真实 provider sandbox 测试 |
| R-014 | Luma Dashboard 超管可看全部 LAE 用户/应用/用量并发起审计动作 | In progress | 已有独立 service-token 的只读 admin API、Luma proxy、Dashboard 页面和仅管理端可见的 placement 审计视图；仍需真实 staging 跨系统关联、suspend/reconcile 写动作、审批与双人复核 E2E |
| R-015 | 更新检查重新运行 Agent，展示 plan diff，确认后部署 | In progress | 已有保存来源绑定、原子 check-update Operation、Worker analyze lane，以及 source tree/DeploymentPlan 的结构化比较、闭合 digest 和 Web 结果提示；仍需 env/route/volume 破坏性变更分类、用户确认后部署与真实 staging 失败保旧 E2E |
| R-016 | 平台保存每个应用的 DeploymentPlan、BuildPlan、规范化 Compose、Luma sidecar/manifest 与 image digest | In progress | DB revision 不可变性、hash 复现、用户仓库无 Luma 文件的 Compose 部署 |
| R-017 | 默认稳定随机 `*.itool.tech` 域名，不支持自定义域名 | In progress | CSPRNG/唯一性、wildcard DNS/TLS、update/suspend/rollback 域名不变、删除冷却测试 |
| R-018 | 控制台保持一致的高级视觉、模板湖面和真实事件驱动部署动画 | In progress | design tokens、键盘/读屏/reduced motion、性能预算、真实 event 映射与视觉回归 |

## 3. 基础设施与安全硬门槛

| Gate | 当前状态 | 必须证明 |
| --- | --- | --- |
| Scoped LAE -> Luma credential | In progress | management token 不被 LAE/tenant 获得；action/namespace 最小权限 |
| Builder isolation | In progress | 专用 pool、rootless BuildKit/等价沙箱、无 host socket/network/device、资源和 egress 限制 |
| Runtime isolation | In progress | Luma 内部 Placement 已要求非空、精确的 runtime node allowlist，并继续按 region、`linux/amd64`、ready/eligible agent、builder/control-plane policy、managed-volume 可达性与 prior placement 收敛候选，再以 Nomad plan 验证容量；真实 staging 已配置 `manager + tecent` runtime 候选池，其中 manager 已显式 runtime opt-in。生产仍需至少两个专用 runner、管理网/metadata 拒绝、cap drop、PID/storage/network 限额和真实 staging 故障演练 |
| Credential safety | In progress | Git/registry/env credential 不进入 state、argv、event、log、layer、provenance |
| Durable operations | In progress | PostgreSQL lease/outbox、幂等、cursor replay、cancel、crash reclaim、reconciliation |
| Stateful recovery | Not started | PostgreSQL PITR、object/registry restore、Lite/Pro volume restore drill |
| Abuse/compliance | Not started | 举报/封禁/申诉、内容治理、条款/隐私、地域和支付主体决策 |
| Capacity/SLO | In progress | 最终 Job 已由 Nomad plan 做实时 CPU/memory feasibility；Luma 内部稳定区分 no-capacity/placement/volume，租户侧故意把容量和卷拓扑统一成不泄露维度的 `LAE_CAPACITY_UNAVAILABLE`；仍需 builder/runner/stateful 容量模型、load/soak/chaos 与告警 |

## 4. 当前实施批次

### Batch 1：协议与 Builder v2 基座

- [x] `luma.builder-task/v1` typed contract。
- [x] scoped LAE service authentication。
- [x] durable `builderTasks` parent、幂等、cursor、queued cancel 和 late completion protection。
- [x] 旧 `/v1/builds`/CLI/Dashboard 完整兼容。
- [x] LAE monorepo 与唯一 contracts source。
- [x] API/Worker/Agent/CLI 最小可运行入口和 CI。

说明：`analyze-source` 与 `build-plan` 都已通过 active task heartbeat 接收取消并终止本地进程组；Control 保留 durable cancellation fence，阻止晚到成功复活。两条 executor 都会在返回终态前清理 task workspace；跨进程/节点故障后的 orphan sweeper 仍是后续门槛。

### Batch 2：真实 analyzer/build executor

- [x] 完整 Git object ID、deterministic tree/snapshot digest、task-scoped snapshot handle 与本地 snapshot store。
- [x] server/node 双重 digest allowlist 的 `lae-agent-runner`、静态/Compose detector、env evidence 和 fail-closed deny policy。
- [x] 显式 multi-service BuildPlan、Compose `externalImages`、逐 service immutable image digest/SBOM/provenance/scan。
- [x] analyze/build 真实 process cancellation、task workspace 清理与 late-success fence。
- [x] analyzer rootless Docker 与 build rootless BuildKit 的 daemon/socket/peer/security option fail-closed 校验。
- [ ] 跨进程/节点 orphan sweeper、tenant cache/registry namespace 与网络层 egress policy。

已完成部分不会放宽公开门槛：analyzer runner 已强制使用显式的 `unix:///run/user/<uid>/docker.sock` rootless Docker endpoint，并同时核验 socket owner、Linux `SO_PEERCRED` daemon UID、Docker `SecurityOptions=rootless` 与本地 runner digest；不会回退 `DOCKER_HOST`、default context 或 rootful daemon。`diskMiB` 仍只是 preflight/watchdog，不是覆盖 clone/materialize/output 的真实 project/filesystem quota。Runner 的外部镜像输出只是内部 proposal；analyze executor 通过独立 `crane` 和 Control/节点 exact registry allowlist 固定 digest 后才持久化 candidate，signed plan 包含 `resolvedDigest`，build executor 重解析不一致即失败，避免 tag 在 retry/延迟执行时漂移。Build executor 另要求 rootless BuildKit、Syft、Cosign、Trivy 离线库和逐镜像 supply-chain artifact；外部镜像生成 immutable digest、CycloneDX、离线 Trivy 和 LAE external-resolution statement。LAE 与 Luma 已具备受控 Git credential/object-source 下载租约端点、consumer/task/snapshot 精确绑定、一次使用、S3-compatible verified-write/read port、tenant namespace object key、流式 digest/size/media 校验、取消与幂等重试状态机；production 在缺少显式 broker principal、对象存储或真实 Control 配置时继续 fail-closed。仍未关闭的门槛包括真实 registry auth、build args/secret mounts、DNS/redirect 网络层 egress enforcement、project/filesystem quota、tenant cache/registry GC、跨节点 orphan sweeper，以及完整隔离 staging E2E。

### Batch 3：LAE 第一条端到端纵向切片

- [x] PostgreSQL operation/outbox/event、tenant-scoped repository、lease/reclaim/cancel 与 typed Fake Luma/HTTP adapter 基座。
- [x] 邮箱验证码/magic-link 认证、personal tenant、default token、Lite entitlement 数据与 API 纵向切片。
- [x] 已有 app 的公开 HTTPS Git analysis 原子创建：source/analysis/operation/checkpoint/lease/event/outbox/idempotency 同事务，worker 可恢复消费。
- [x] Analysis/operation 公共读、cursor replay 与 cancel：tenant 隔离、kind 最小 scope、cookie CSRF/Bearer 区分、固定事件文案/data 白名单、state-idempotent cancel 和 late-success fence。
- [x] HTML/ZIP quarantine upload、S3 conditional PUT、流式复验、隔离 scanner 与 upload analysis admission 基座。
- [x] HTML/ZIP 的 object-source redemption broker、真实 Worker inspect/build/deploy 接线、稳定随机域名与 fail-closed 配置边界；本地真实 MinIO private S3/CORS 已验证，Luma staging 的浏览器上传→Builder→artifact ingest 纵向验证仍是门禁。
- [x] pending 应用公共创建/列表/详情/子资源与环境变量 API：配额+幂等同事务、apps read/write、session CSRF/Bearer、tenant fence、CAS、AEAD envelope 与只返回 metadata。
- [x] deployment admission API：只接受 analysis/environment version，可信 object-store plan resolver 边界内原子创建 revision/deployment/operation/event/outbox；deployment Worker 已接 build-plan materializer、Luma Builder/Runtime、runtime secret、健康门禁、取消和失败保旧状态机。
- [x] Luma runtime Placement admission：公共协议只开放 `cn/global`，内部要求非空精确 runtime node allowlist，只接受 `linux/amd64` 并排除 builder-only、未显式 runtime opt-in 的 control-plane、not-ready/down/draining/ineligible 节点，校验 managed volume，注入候选 constraint/prior affinity，并在 submit 前调用 Nomad plan；节点/IP/pool/failure domain 不进入公开投影或稳定错误。
- [x] 私有 Git connection catalog/API 与 analysis 绑定：独立 AES-GCM/HMAC keyring、tenant fence、exact host allowlist、幂等 create/rotate/revoke、consumer-bound 单次 lease，以及 LAE-to-Luma 闭合 HTTPS service-token redemption endpoint。
- [x] CLI JSON/NDJSON、operation cursor 断线恢复、公开/私有 Git、HTML/ZIP、模板、env、部署、观测、lifecycle 与项目内 `lae-deploy` Skill 安全基线。
- [x] Web 核心工作台接入真实 session、application、公开/私有 Git、HTML/ZIP、template、analysis、env、deployment、operation event、日志指标与 suspend/resume/restart/check-update。
- [x] Application lifecycle API/store/Worker：保存来源的 check-update、source tree/DeploymentPlan 结构化比较、闭合 digest、suspend/resume/restart/rollback/delete、幂等/cancel/timeout、失败恢复 desired state、rollback 精确目标与 delete retain-volume policy。
- [x] 内部只读 admin API、Luma Control proxy 与 Dashboard 页面：用户、tenant、应用、operation、usage 和 placement 聚合，使用独立 token file，拒绝用户 deploy token；节点拓扑只进入 Luma management 视图，不进入租户 API。

Web 工作台的 `Stillwater Instrument` 视觉基线已可运行：模板湖面、Git/私有 Git/静态产物入口、诊断/部署状态机、环境变量表单、应用运行带、日志指标抽屉、账户/token/套餐页面、窄屏底部导航与 reduced-motion 均已实现。身份、应用岸线、四类来源的 draft→analysis→cursor replay→deployment admission 已接真实 API；失败时只显示诚实错误/空态，不再用 fixture 或前端计时器伪造运行应用。Web 会查询 deployment history，只在存在上一条 succeeded deployment 时开放 rollback；rollback/delete 都使用可聚焦的高级 `alertdialog` 二次确认，支持 Escape 和 reduced-motion，删除明确保留持久卷。check-update 已在终态 Operation 中读取结构化比较，并区分无基线、无变化、仅 source 变化、仅 DeploymentPlan 变化和两者均变化。当前明确缺口是破坏性 env/route/volume diff 的逐项确认、模板每日 smoke/自动下架，以及已导入真实 Luma staging 上的浏览器全流程验收。

认证切片已具备枚举防护、原子 challenge consume、session/CSRF、限流和一次展示的默认 deploy token；SMTP adapter 支持 implicit TLS/STARTTLS，production 禁止 console/plain SMTP，magic link 使用 URL fragment 并由登录页先清除再交换。Staging Mailpit 已进入 Luma 清单；真实邮件供应商送达、durable delivery/outbox 和不持久化明文 challenge 仍是“真实邮件可用”门槛。

LAE Agent 已形成“确定性基线 + AI 提案 + 确定性终审”的闭环：OpenAI-compatible provider 使用 `LAE_AGENT_LLM_BASE_URL/API_KEY/MODEL` 配置，staging 当前映射 ARK；版本化 Knowledge Pack 明确 LAE/Luma 能力、限制、manifest 形状、环境变量规则和四态 verdict。用户无需编写 Luma 文件；平台保存与 revision 绑定的 manifest candidate/最终 manifest。公开分析返回 `deployable`、`needs_input`、`unsupported` 或 `diagnostic_failed`；只有 `unsupported` 携带结构化 blocker，`diagnostic_failed` 表示诊断基础设施失败，不能污名化为用户代码不支持。AI 不能删除确定性 blocker、修改拓扑/route/volume、伪造 secret 或弱化环境变量要求。

CLI 已具备严格 HTTPS/localhost endpoint、stdin/env deploy token、稳定错误/退出码、应用 draft/查询、公开与私有 Git connection、HTML/ZIP upload、模板、env、部署、日志指标、lifecycle、billing checkout、operation show/watch/cancel、cursor resume 和 NDJSON；项目内 `lae-deploy` Skill 已固化先创建应用再 inspect/deploy、secret 人机边界、取消恢复与支付确认规则。本地完整 staging Compose 已完成 CLI E2E，真实 Luma staging 也已完成平台 import，但 CLI 的 Builder/Runtime 完整纵向 E2E 仍在收尾；默认注册 token 仍故意没有 `billing:checkout`。因此 CLI/Skill 仍不能标为 `Verified` 或 `Done`。

模板 catalog 当前是 checked-in、commit-pinned、带版本和 Agent verification metadata 的四个 starter；launch 不绕过诊断，而是创建正常 application 与新的 Builder analysis。它还不是自动运营系统：每日 smoke、失败计数、自动下架、版本回滚和真实模板部署验收尚未实现。

更新检查当前会从数据库中已保存的 source/ref/connection 复制新 source revision，不接受调用者注入 repository/ref/connection，并复用 analysis Worker；它不会自动替换健康部署。成功结果以闭合、无 secret 的 `updateCheck` 返回 `baselineAvailable`、`sourceChanged`、`deploymentPlanChanged`、`changed` 和 baseline/candidate SHA-256 digest；没有可用部署基线时保守视为有变化。Web 已消费这些状态。env/route/volume 破坏性变更分类、逐项展示、人工确认和确认后创建 deployment 仍是下一阶段门槛。

管理员切片当前只读，且刻意与用户 session/deploy-token 鉴权分离。它能为 Luma Dashboard 聚合 user/tenant/application/operation/usage；Luma Control 还提供 management-token-only 的 placement 视图，关联候选节点、preferred node 和实时 Nomad allocation。该拓扑不会进入 LAE 租户 API。管理员写动作、审批、双人复核和真实 staging RBAC/关联验证尚未完成；Luma management token 也不能代替 LAE 专用 admin token。

公开 region 契约已经统一为 `cn | global`：analysis API、template API、Git/upload store command、Web 类型和 CLI 都在各自边界拒绝内部 `home`，避免 source lane 接受而 deployment lane 才失败。`home` 仍可作为 Luma 内部 Builder region，但不是租户可选值。

后续批次按 [06 分阶段并行研发计划](./06-delivery-plan.md) 继续；任何批次完成后都必须回填本文件的测试、部署、运行和恢复证据。

## 5. 验证记录

| 日期 | 范围 | 结果 | 证据 |
| --- | --- | --- | --- |
| 2026-07-11 | 变更前 Luma baseline | 511 项 `unittest` 通过 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` |
| 2026-07-11 | LAE 设计/用户/运维文档 | `docs/lae/*.md` 与部署 README 共 14 个 Markdown 文件，本地链接、代码围栏、尾随空格和 `git diff --check` 通过 | 本地只读文档校验；未执行部署 |
| 2026-07-11 | Luma Builder Task v1 + analyze executor | 550 项 Luma `unittest` 通过；principal/tenant/app scoped auth、严格 schema、签名/snapshot 绑定、幂等、cursor、真实 analyzer cancel、closed result/log、防 late success 与旧 build 兼容 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` |
| 2026-07-11 | Luma rootless multi-image build executor | 573 项 Luma `unittest` 通过；rootless daemon `SO_PEERCRED`、snapshot/path/registry scope、DAG、cancel/timeout、zero-build、immutable digest、SBOM/provenance/scan 与 strict result binding 全覆盖 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`；`tests.test_builder_build_executor` |
| 2026-07-11 | Luma rootless analyze executor | 576 项 Luma `unittest` 通过；仅 Linux、显式 `/run/user/<uid>/docker.sock`、socket/path/peer UID、Docker rootless security option、本地 runner digest、空 Docker credential 环境、cancel/cleanup fail-closed 全覆盖 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`；`tests.test_builder_analyze_executor` |
| 2026-07-11 | Compose external image resolution/scan | 584 项 Luma `unittest` 全通过；runner proposal 经 analyze-side crane 固定 `resolvedDigest` 后才成为 candidate，signed plan 在 build retry 时拒绝 tag drift；`builds=[]` + `externalImages` 真实执行 Syft/Trivy，Control/节点 allowlist 精确绑定，拒绝 mutable/private/credential-bearing ref，三类 artifact 与 image digest 一一绑定。LAE 全量 107 tests 通过（3 个条件集成测试 skip），contracts 为 7 schemas/8 valid/9 invalid | Luma full suite；`tests.test_builder_analyze_executor`；`tests.test_builder_build_executor`；`cd lae && make check && uv lock --check && pnpm check` |
| 2026-07-11 | Luma Builder resource cleanup regression | 590 项 Luma `unittest` 在 `ResourceWarning` 视为错误的模式下全部通过；Unix socket connect 失败路径显式关闭 fd，未再出现资源泄漏 | `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'` |
| 2026-07-11 | Git import credential argv 防护 | PAT 不进入 clone URL/argv/env；拒绝 URL userinfo/query/fragment，清除继承 Git trace；完整 commit 校验 | 临时 mode-0700 askpass 目录 + mode-0600 credential files + 定向测试 |
| 2026-07-11 | LAE workspace/contracts/runner | 6 schemas、8 events、7 valid/6 invalid examples、17 tests、真实 deterministic runner、5 component smoke、pnpm check 全部通过 | `cd lae && uv sync --all-packages --locked && make check && uv lock --check && pnpm install --frozen-lockfile && pnpm check` |
| 2026-07-11 | LAE/Luma Builder contract cross-check | 两个 valid create request 被 Luma validator 接受，kind/payload mismatch 被拒绝 | canonical examples -> `luma.builder_tasks.validate_builder_task_request` |
| 2026-07-11 | LAE durable store + Luma adapter | LAE 默认 57 tests 通过（1 real-PG 条件用例 skip）；一次性 PostgreSQL 17 数据库实测 migration upgrade/downgrade、并发 claim、幂等、lease reclaim/cancel、单调 events 与 outbox 全通过；`alembic check` 无模型漂移 | `LAE_TEST_POSTGRES_ALLOW_DDL=1 ... test_store_postgres_integration.py`；两次一次性测试库均已删除 |
| 2026-07-11 | LAE Web visual baseline | Next.js production build/typecheck 通过；桌面与 390px 窄屏真实浏览器验收无横向溢出或 console warning/error；模板/部署动效可操作，身份与应用目录读取真实 API，API 不可用时 0 个 fixture 应用并显示诚实空态 | `pnpm --filter @lae/web check`；Codex in-app Browser DOM/screenshot verification |
| 2026-07-11 | LAE email identity slice | 13 项 Auth API/domain 测试通过；真实 PostgreSQL 17 实测 migration upgrade/downgrade、并发单次 consume、同邮箱原子建户、Lite/default token、TTL/5 次锁定/限流、session/CSRF、失败与 crash fence | `tests.test_auth_api`、`tests.test_auth_domain`、`tests.test_auth_postgres_integration` |
| 2026-07-11 | LAE public analysis enqueue | API 覆盖 Bearer/CSRF/scope/双凭据/缺 app/跨 tenant/HTTPS-only/幂等冲突；真实 PostgreSQL 17 验证并发同 key 只生成一组 source/analysis/operation/checkpoint/lease/event/outbox，worker 可加载并原位完成同一 analysis ID，migration upgrade/downgrade 通过 | `tests.test_analysis_api`、`tests.test_analysis_request_postgres_integration` |
| 2026-07-11 | LAE public analysis/operation read + replay/cancel | 9 项 API/contract 单测覆盖 kind 最小 scope、跨 tenant/不存在统一 404、confused deputy、cookie CSRF 与 Bearer cancel、事件固定文案/data 白名单、错误信息收口、分页终态 drain 和 state-idempotent cancel；1 项真实 PostgreSQL 17 用例覆盖 tenant fence、单调 cursor、重复 cancel 单事件及 cancel 胜 late success，0001→0004 upgrade/downgrade 通过且临时容器已删除。Retention GC/SSE 仍为显式 gate | `tests.test_public_resource_api`、`tests.test_public_resource_postgres_integration` |
| 2026-07-11 | LAE CLI + deploy Skill safety baseline | 15 项 CLI 测试与 2 项 Skill asset guard 通过；token 不可作为 argv、repr/error 不泄露、HTTPS/localhost boundary/redirect guard、稳定 HTTP 错误、应用 draft、inspect/deploy/env/billing 请求约束、单调 cursor resume、终态分页 drain 与 NDJSON 终态均覆盖；Skill 官方结构校验通过 | `tests.test_cli`、`tests.test_skill_assets`、`skill-creator/scripts/quick_validate.py` |
| 2026-07-11 | LAE Application 公共 API + env envelope | 10 项 API/crypto 单测覆盖 apps read/write、cookie CSRF/Bearer、双凭据、严格 tenant 404、公开投影、幂等冲突、CAS、64 KiB/512 KiB 边界、AES-256-GCM 随机 nonce、AAD 错绑、HMAC、key rotation、repr/error redaction 与 runtime 缺 key fail closed；2 项真实 PostgreSQL 测试覆盖并发同 key 原子创建、Lite quota、跨 tenant、密文持久化、历史响应无 secret、CAS/replay，并以真实 pending 数据验证 0001→0005→base 有损 downgrade 边界 | `tests.test_application_api`、`tests.test_environment_crypto`、`tests.test_application_api_postgres_integration` |
| 2026-07-11 | LAE private Git source connections | 14 项专用 API/crypto/broker/真实 PostgreSQL 测试覆盖 `sources:write`、cookie CSRF/Bearer、tenant fence、并发幂等、canonical HTTPS/exact host、独立 runtime key 配置、AES-256-GCM 随机 nonce、AAD/HMAC/key rotation、secret canary、lease TTL/consumer binding/concurrent single-use、rotate/revoke open-lease fence与默认 broker fail-closed；PostgreSQL 17 实测 0001→0007、`alembic check` 无漂移并完整 downgrade 到 base。当时记录的 Luma redemption 缺口已由后续内部 broker/API 记录关闭 | `tests.test_source_connection_api`、`tests.test_source_connection_crypto`、`tests.test_source_credential_broker`、`tests.test_source_connection_postgres_integration` |
| 2026-07-11 | LAE HTML/ZIP static upload | 12 项 security/API 单测与 2 项真实 PostgreSQL 测试覆盖 plan storage quota、tenant/idempotency、single-use conditional PUT、URL once-display、HEAD+流式 size/SHA/media 复验、quarantine/scanning/ready/cleanup，以及 ZIP bomb、traversal、NUL、symlink/hardlink/device、casefold duplicate、encrypted/multidisk/nested archive/executable 拒绝；fresh 0001→0007、`alembic check`、downgrade base 通过。当时记录的 object-store/redemption 接线缺口已由后续 object-source broker 与 Worker runtime 记录关闭 | `tests.test_static_upload_security`、`tests.test_static_upload_api`、`tests.test_static_upload_postgres_integration` |
| 2026-07-11 | LAE Deployment admission | 7 项 unit/API 与 2 项真实 PostgreSQL 测试覆盖严格 `analysisId + environmentVersion` 请求、scope/CSRF/tenant fence、可信 plan resolver、Compose 多 HTTP、named volume、配额、环境版本/必需变量/拓扑兼容，以及 revision/deployment/operation/event/outbox/idempotency 原子创建。当时默认 resolver fail-closed；后续已接 verified object-store resolver 与 deployment Worker，见后续记录 | `tests.test_deployment_admission`、`tests.test_deployment_api`、`tests.test_deployment_admission_postgres_integration` |
| 2026-07-11 | LAE analysis artifact 安全摄取 | 11 项专用流式摄取测试与 worker Fake E2E 共 31 项定向测试通过；租约精确绑定 tenant/application/operation/Luma task/descriptor，覆盖单次使用、超时重试、取消、16 MiB 上限、media/size/SHA-256、tenant object key、断点幂等及 bytes/token/内部 image ref 不外泄。一次性 PostgreSQL 实测 migration upgrade/downgrade 与 `pending -> uploading -> verified`、三件全验证后原子 `stored` 通过，测试库已删除 | `tests.test_worker_artifact_ingest`、`tests.test_worker_analyze`、`tests.test_worker_postgres_integration` |
| 2026-07-11 | LAE on Luma 首版部署资产 | Production/Staging Compose 与 sidecar、6 个构建镜像、9 个平台注册服务、Web/API 双公网 HTTP 和内部 Worker/Controller/PostgreSQL/MinIO/Valkey/Mailpit 已实现；非 root、locked uv、Next standalone/internal proxy 与镜像 smoke 通过。早期缺少 storage class 的 fail-closed 已解决；真实 staging import 已完成，最终健康/E2E 验收仍在进行 | `tests.test_lae_luma_deploy_assets`、`tests.test_nomad_compose`、`lae/deploy/luma/README.md`；真实 `luma import` 记录 |
| 2026-07-11 | LAE Lite/Pro/Ultra 安全 mock 计费切片 | LAE 全量 190 tests 通过（12 个条件集成测试 skip）；7 项 billing API/model 测试覆盖 session CSRF、显式 `billing:checkout`、默认 token 无权限、服务端定价、Lite 禁购、production disabled readiness/503/route 404。2 项真实 PostgreSQL 17 测试覆盖并发幂等、tenant fence、价格快照、签名事件后的事务切订阅、replay/mismatch/out-of-order，0001→0005 upgrade、`alembic check`、downgrade base 均通过。价格仍是显式 dev 配置，usage ledger 为不计费的零值占位；微信/支付宝 adapter、真实 ledger/配额和 provider sandbox 尚未实现 | `tests.test_billing_api`、`tests.test_billing_postgres_integration`、`tests.test_store_models`；`cd lae && make check` |
| 2026-07-11 | Luma LAE runtime Placement admission | Placement 要求非空精确 runtime node allowlist，缺失、空值、重复、非法名称均 fail closed；再叠加 public `cn/global`、`linux/amd64`、readiness、builder/control-plane policy、managed-volume、prior affinity 和 Nomad plan。真实 staging 已准入 `manager + tecent`，manager 已显式启用 runtime role；平台自身固定在 `lab`。租户投影不泄露 topology/failure dimension。真实 tenant allocation、故障重调度和 chaos 尚未完整验收，因此 Gate 保持 In progress | `tests.test_lae_placement`、`tests.test_lae_runtime_api`、`tests.test_nomad_render`、`lae/tests/test_staging_bundle.py`；真实 staging import/节点状态 |
| 2026-07-11 | Git/object redemption 闭环 | LAE internal credential/object-source API 与 Luma Control broker 已完成 closed HTTPS service-token、严格 bounded body、task/tenant/application/consumer/snapshot 绑定、一次使用、cancel/replay fence 和通用安全错误；token file 必须是私有普通文件。本地真实 MinIO private object 与 CORS 已通过，相关服务已由真实 Luma import；真实私有 Git/上传到 Builder redemption 的端到端与网络策略验收仍在进行 | `lae/tests/test_credential_broker_api.py`、`lae/tests/test_object_source_broker_api.py`、`tests/test_builder_credential_broker.py`、`tests/test_builder_object_source_broker.py` |
| 2026-07-11 | Template 一键诊断切片 | 四个 starter 使用 version + 完整 commit pin，公开 catalog 不泄露 repository，Web/CLI launch 创建正常 application 与新的 Builder analysis，不绕过 Agent/scan/配额/鉴权/幂等门禁。每日 smoke、自动下架与 staging deploy 尚未实现 | `lae/tests/test_template_api.py`、`lae/tests/test_cli.py`、`lae/apps/web/src/components/lae-console.tsx` |
| 2026-07-11 | Application lifecycle 与结构化更新检查 | API/store/Worker 覆盖 check-update 保存来源绑定、source 注入拒绝、不可变已部署基线解析、source tree/DeploymentPlan 比较、闭合 digest、无基线保守判定及只在成功的 `application.check-update` Operation 公开结果；Web 区分无基线/无变化/source 变化/plan 变化。suspend/resume/restart/rollback/delete、租户/幂等 fence、runtime-start durable checkpoint、取消/timeout/失败恢复 desired-state、精确 rollback 与 retain-volume 也已覆盖；真实 Luma staging 已部署，逐动作验收尚未完成 | `lae/tests/test_update_check_result.py`、`lae/tests/test_public_resource_api.py`、`lae/tests/test_worker_analyze.py`、`lae/tests/test_application_lifecycle_postgres_integration.py`、`lae/tests/test_worker_lifecycle.py` |
| 2026-07-11 | Deployment Worker 生产接线 | 统一 Worker 并行服务 analysis/deployment/lifecycle lanes；Production/Staging Compose 显式启用 deployment 与 lifecycle。deployment lane 使用 verified object artifact、签名 BuildPlan、Builder build、runtime secret、Luma Runtime、多 route 健康门禁、取消与失败保旧。缺配置时 fail-closed；PostgreSQL/MinIO 本地集成已通过，真实 Luma import 已构建/推送平台镜像并注册服务，tenant source → Builder → registry → Runtime 的纵向 E2E 尚在进行 | `lae/tests/test_worker_wiring.py`、`lae/tests/test_worker_build_plan_materializer.py`、`lae/tests/test_worker_deployment.py`、`lae/tests/test_worker_runtime_secrets.py`；真实 `luma import` 记录 |
| 2026-07-11 | Luma Dashboard LAE 超管只读切片 | internal admin API 使用独立 constant-time Bearer 与 mode-0600 非 symlink token file，拒绝用户 deploy token，聚合 users/tenants/applications/operations/usage 且 `no-store`；Luma Control 另以 management token 保护 `/v1/dashboard/lae/placements`，Dashboard 已有“调度位置”页签，展示内部候选与实时 allocation，且不进入租户 API。真实 staging 服务已注册，但管理员写动作、完整 RBAC 与 tenant allocation 关联验收尚未完成 | `lae/tests/test_admin_api.py`、`tests/test_lae_admin_proxy.py`、`dashboard-src/src/pages/LaeAdminPage.tsx` |
| 2026-07-11 | Runtime 409 安全映射 | Adapter 只读取 bounded `errorInfo.code/requestId`；`volume_placement_incompatible` 映射为不可重试 `LAE_CAPACITY_UNAVAILABLE`，明确幂等 conflict 映射为 `LAE_IDEMPOTENCY_KEY_REUSED`，未知 409 按协议错误 fail-closed，message/node/IP 不进入公开错误 | `lae/tests/test_luma_runtime_adapter.py` |
| 2026-07-11 | 文档收口定向验证：用户与内部 API | 29 项通过；覆盖 template、admin、Git/object broker API、source/upload CLI 与 tenant-scoped observability | `cd lae && .venv/bin/python -m unittest tests.test_template_api tests.test_admin_api tests.test_credential_broker_api tests.test_object_source_broker_api tests.test_cli_sources_upload tests.test_observability_api -v` |
| 2026-07-11 | 文档收口定向验证：部署与生命周期 | 37 项通过；覆盖 lifecycle API/Worker、unified Worker wiring、BuildPlan materializer、deployment Worker、runtime secret 与 409 安全映射 | `cd lae && .venv/bin/python -m unittest tests.test_application_lifecycle_api tests.test_worker_lifecycle tests.test_worker_wiring tests.test_worker_build_plan_materializer tests.test_worker_deployment tests.test_worker_runtime_secrets tests.test_luma_runtime_adapter -v` |
| 2026-07-11 | 文档收口定向验证：Luma 边界 | 67 项通过；覆盖 Git/object broker、admin proxy、placement/runtime、部署资产与文件型 principal；未执行 import/deploy | `.venv/bin/python -m unittest tests.test_builder_credential_broker tests.test_builder_object_source_broker tests.test_lae_admin_proxy tests.test_lae_placement tests.test_lae_runtime_api tests.test_lae_luma_deploy_assets tests.test_lae_principal_files -v` |
| 2026-07-11 | LAE 当前全量 workspace gate | 351 项通过、23 项条件集成测试跳过；contracts、compileall 与 API/Worker/Agent Controller/Agent runner/CLI smoke 全部通过。跳过项与真实 Luma staging 不因此视为完整租户 Runtime E2E | `cd lae && make check` |
| 2026-07-11 | LAE Web 当前 gate | workspace scaffold、TypeScript 与 Next.js 16.2.10 production build 通过；`/`、`/login`、`/account`、`/orders/[orderId]` 等路由构建成功 | `cd lae && pnpm check` |
| 2026-07-11 | Public region 契约收敛 | 31 项通过；analysis/template API、Git/upload store 与 CLI 均拒绝内部 `home`，Web TypeScript/production build 通过，源码检索不再存在 public `home` union | `cd lae && .venv/bin/python -m unittest tests.test_analysis_api tests.test_template_api tests.test_cli`；`pnpm check` |
| 2026-07-11 | Lifecycle 真实 PostgreSQL 集成 | PostgreSQL 17 migration-backed 3/3 通过；覆盖 rollback 原子切换 pointer/revision/image、tenant fence、runtime-start 后 cancel + lease reclaim 以外部结果收敛，以及后续 Runtime failure 恢复 admission desired-state。真实 Luma staging 已部署，但 lifecycle 动作矩阵尚未执行完成 | `tests.test_application_lifecycle_postgres_integration`；隔离临时 PostgreSQL 17 |
| 2026-07-11 | Web rollback/delete gate | Web 查询 deployment history，只对上一 succeeded 版本开放 rollback；rollback/delete 使用带目标与影响说明的 `alertdialog` 确认，delete 明示持久卷默认保留，并支持 Escape/reduced-motion。TypeScript 与 Next.js production build 通过；真实浏览器 + Luma staging 动作仍未验收 | `lae/apps/web/src/components/lae-console.tsx`、`lae/apps/web/src/lib/lae-api.ts`；`cd lae && pnpm check` |
| 2026-07-11 | Luma 0.1.171 候选全量 gate | 719/719 项 `unittest` 在 `ResourceWarning` 视为错误的模式下通过；当前 live fleet 仍为 `0.1.170`，待发布步骤完成 | `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'` |
| 2026-07-11 | LAE 真实 PostgreSQL 17 集成矩阵 | 12 个 migration-backed 模块共 22/22 项通过，覆盖 migration up/down、auth/token、analysis、application/catalog、billing、private Git、upload、deployment admission、public events、Worker、structured update-check 和 lifecycle；临时数据库已删除 | 隔离 PostgreSQL 17；`LAE_TEST_POSTGRES_ALLOW_DDL=1` 集成测试矩阵 |
| 2026-07-11 | 本地完整 staging Compose E2E | `web`、`api`、`worker`、`agent-controller`、`postgres`、`minio`、`artifact-init`、`valkey`、`mailpit` 全部 healthy；完成注册邮件、验证码验证、一次展示默认 deploy token、token verify、application/catalog/admin、mock billing 与 CLI E2E。MinIO 私有 S3 put/head/get、最小权限和精确 Origin CORS 正反例通过；聚合容器日志为 0 个 secret pattern、0 个 traceback | 本地 `docker compose -f lae/deploy/luma/docker-compose.staging.yml`；隔离临时 bundle/volume，完成后删除 |
| 2026-07-11 | 真实 Luma staging 平台 | **平台健康，产品最终验收进行中。** 9 个 task 全部健康且零重启；三个域名 TLS 有效，Web、API live/ready、artifact ready 均为 200。真实注册/token/CLI/template/analysis 已跑；tenant Runtime deploy/lifecycle 尚未完成，因此仍未标为产品 `Verified` | 真实 allocation、TLS、probe 与用户流程记录；不得由平台健康推导 tenant deploy 已通过 |
| 2026-07-11 | Production readiness | **未就绪。** 专用 production `lae-core`/至少双 runner、独立 PostgreSQL/artifact storage、恢复演练、真实 SMTP、微信/支付宝 provider、容量/滥用治理与完整安全故障验收仍是硬阻塞 | production rollout 必须单独完成发布门禁，不得由 staging import 推导 |
