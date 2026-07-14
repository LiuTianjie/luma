# Luma Application Engine（LAE）产品与工程设计

> 状态：Draft v0.9；Luma CLI、Control 与 manager agent 包版本为 `0.1.233`，LAE 9 个平台 service 已运行 exact ref `403ba74f2d1ec8b0d140d028a2437652588ee5fa`（Nomad job v52）；四服务 Compose 冷启动、双 HTTPS/双卷产品 E2E、四模板 smoke/自动下架恢复与 clean-room CLI/Skill E2E 已通过，完整来源、安全与数据恢复矩阵仍待完成
> 日期：2026-07-14
> 目标：在 Luma 之上建设面向普通用户和 AI Agent 的多租户应用部署平台；LAE 自身及其依赖全部由 Luma 部署和管理。

## 1. 结论先行

LAE 不能只是给现有 Luma Dashboard 增加注册页。正确边界是：

- **Luma** 是基础设施控制面和超级管理员底座，负责节点、调度、镜像、路由、DNS、存储以及真实部署。
- **LAE** 是 ToC 产品控制面，负责用户、租户、应用、诊断、部署任务、配额、计费、凭据、审计、CLI 和 Agent 体验。
- **LAE Agent** 是独立部署的、受策略约束的公开分析服务：API/controller 负责编排，源码拉取和分析 runner 由 Luma builder 执行。它输出结构化 `DeploymentPlan`，不直接持有 Luma 超级管理员凭据。
- 用户不写、上传或维护 Luma 文件。LAE Agent 结合版本化 Knowledge Pack、确定性项目证据和可配置的 OpenAI-compatible 模型生成 manifest candidate；平台再以确定性 schema/语义/策略校验收敛并保存最终 Luma manifest。AI 不能移除 blocker、放宽权限或绕过平台策略。
- 普通用户与用户级 deploy token 永远不能直接调用 Luma management API。LAE Orchestrator 是唯一允许调用 Luma 的产品服务。
- 每次部署都必须保存不可变的源码快照标识、诊断结果、构建产物 digest、`DeploymentPlan` 和最终 Luma manifest。用户仓库无需包含 Luma 文件。
- 用户只选择 `region`，不能选择或看到具体节点/IP。Luma 根据实时容量、runtime capability、builder 隔离、volume 可达性和上一 allocation 连续性完成 placement；完整拓扑只在内部控制面和授权管理员排障中可见。

产品首要指标不是“支持最多类型”，而是“支持范围内的首次部署成功率高、失败可解释、任务可恢复”。

## 2. 默认产品决策

以下决策用于让设计可以继续落地；其中标记 `待确认` 的项目见 [开放决策](./07-open-decisions.md)。

| 主题 | 当前默认 |
| --- | --- |
| 产品形态 | 面向公众的 ToC 托管平台，底层保留组织/tenant 扩展能力 `待确认` |
| 初始账户 | 界面只开放个人账户；注册后创建一个 personal tenant |
| 登录 | 邮箱验证码或 magic link；后续可增加密码与第三方登录 `待确认` |
| 默认域名 | 应用创建时由服务端生成稳定的 `<128-bit-lowercase-hex>.itool.tech`；更新部署不换域名 |
| 自定义域名 | MVP 明确不支持 |
| 上传部署 | 单个 HTML 或 ZIP 静态产物；ZIP 必须有入口文件 |
| Git 部署 | 单服务与 Compose 都是一等模型；Agent 生成受策略约束的 Luma manifest/sidecar |
| 构建执行 | Git 拉取、确定性分析、Dockerfile/Compose 多镜像构建，以及 Compose 外部镜像的 digest 固定/SBOM/扫描，统一由 Luma builder 执行 |
| 多服务 | Compose 可有多个公网 HTTP 服务，也支持内部服务、后台 worker、依赖服务和受管命名卷；暂不支持 `tcp-relay` |
| 动态部署范围 | Dockerfile 与 Compose 对 Lite/Pro/Ultra 用户均开放；套餐只限制资源和高级能力，不以邀请制区分来源类型 |
| Lite 有状态能力 | 允许受管命名卷和应用内自管数据库；基础备份规格暂按草案执行，仍需确认保留期与恢复方式 |
| AI 使用 | OpenAI-compatible provider 的 base URL/API key/model 可配置；当前 staging 使用 ARK 映射。模型只接收脱敏、限量的结构化项目证据和版本化 Knowledge Pack，不接收源码正文或 secret；确定性校验拥有最终安全裁决权 |
| 计费 | Lite / Pro / Ultra entitlement 先落地，支付先 mock，再接微信/支付宝 |
| Runtime placement | 租户只提交 `cn`/`global` region；节点、IP、候选集和 failure domain 不进入租户投影，内部 placement 仅供 Luma 与管理员审计 |
| 平台部署 | LAE Web/API/Agent/Worker/PostgreSQL/对象存储/观测组件全部由 Luma 部署 |

## 3. 当前 Luma 事实基线

本设计不是从空白假设出发。2026-07-14 当前 staging 的分层事实是：

- 当前 live CLI、Control 与 manager agent 的包版本为 Luma `0.1.233`，manager Control 已运行 exact ref `d0ffc7a` 的候选镜像；本轮没有 worker-wide fleet 升级，在线非 manager agent 主要为 `0.1.228`，不能写成 fleet 已统一。后续涉及 agent 协议的版本仍必须按 manager → 所需节点 → fleet 顺序升级同一不可变 ref。`manager` 是唯一控制面；`aly` 是过时历史节点，不进入升级或任何 LAE placement。
- LAE 平台 staging 当前在 `manager`；租户 runtime allowlist 是 `manager + tecent`，其中 `manager` 显式具备 runtime role。生产仍应使用专用平台与 runner pool，不能把当前共享节点布局当成生产拓扑。
- 默认构建节点是 `builder`，位于内部 `home` region；该值不属于 LAE 租户协议，公开 analysis/upload/template/Web/CLI 只接受 `cn | global`。当前 Control 中 registry pull/push 地址均为 `100.66.177.70:5000`，内部 registry 使用 HTTP insecure 配置，平台构建使用 direct 模式；旧文档中的 Builder `localhost:5000` push 地址已经失效。
- 当前 staging exact ref `403ba74f2d1ec8b0d140d028a2437652588ee5fa` 的 9 个平台 service（Nomad job v52）均健康，Cloudflare DNS-01 wildcard TLS 有效，Web、API live/ready、Agent ready 与 artifact ready 探针均返回 200；Agent ready 报告 `mode=ai`、`configured=true`。四服务 Compose 已在无人工补写 manifest 的情况下走通 Agent 诊断、Builder 构建、超过旧 3 分钟窗口的首次冷拉、双 HTTPS route、双持久卷与主要 lifecycle；公开 Operation 可见 build/render/volumes/runtime/verify 阶段。四个 starter 已完成真实 smoke，PostgreSQL 持久失败计数、三连败自动下架和成功恢复已在 live 可逆演练。clean-room Agent 也已仅凭 Skill/CLI/deploy token 完成模板部署和清理。preview 仍不等于真实用户邮箱可收信，也不等于所有 source、AI 四态 verdict 与故障矩阵已完成验收。
- 现有 Luma `build-image` 已在 builder 临时目录 clone Git、执行 buildx、推送 registry，并能发现仓库内 Compose sidecar 后构建多个 service；凭据在 task lease 时注入。
- legacy builder 没有“只分析不构建”的 action，也不能直接消费 LAE 生成的多服务 `BuildPlan`，其 Docker/buildx 共享宿主执行形态不满足公网多租户隔离。Builder v2 因此采用不可变 source snapshot、`analyze-source`、显式多 build plan、短期凭据 lease 和 rootless sandbox；其中 analyzer 已拒绝 default/rootful Docker daemon，其他公开门槛见实施状态文档。
- 已有能力包括单服务/Compose 部署、预览、GitHub/Gitea 凭据、仓库构建、内部 registry、NDJSON 进度、部署历史、日志、指标、更新、重启和回滚。
- 当前 Compose 会渲染成一个 Nomad group：所有 service 同节点、同 region、共享 network namespace、单 group 副本；LAE 必须检查端口唯一和整组容量，不能把逐服务 HA 当成现有能力。
- LAE Runtime 已增加 Luma 内部 placement admission：按 region、Nomad/Luma readiness、runtime capability、builder-only 排除、managed volume 兼容和 prior allocation 生成候选约束，并用 Nomad plan 检查整组容量；当前 staging 候选为 `manager + tecent`。公网 HTTP service 使用节点 `luma_tailscale_ip` metadata 注册 upstream，service/router 名包含 deployment slug，避免跨应用 Compose 名称碰撞；真实节点故障、无容量和 volume affinity 仍需验收。
- 现有认证只有一枚全局 management token 和一枚 node join token；`control.json` 是单集群状态文件，不是多租户数据库。
- scoped secret、Git token 和 registry password 当前会进入控制面状态；这不满足公网多租户密钥隔离要求。
- 部署写操作当前由进程内全局锁串行化；它可以支撑低并发运维，但不能直接当作公共平台并发执行层。
- Luma Core 本身没有 LAE 的文件上传、用户/RBAC、套餐、支付、邮件、应用 suspend/resume 或租户级审计语义；这些能力由 LAE 代码提供，但仍需按 [实施状态](./08-implementation-status.md) 分别完成 staging/production 验收。Luma 的公网多租户 namespace enforcement 仍是硬门槛。

因此，现有 Luma 适合作为 LAE 的执行底座，但不适合直接暴露给租户。

本轮还确认了两个部署约束：其一，BuildKit 的代理配置会持久化在 Buildx container 中，当前代码会在代理 URL、`NO_PROXY` 或 direct/proxy 模式变化时重建不匹配的 builder；LAE 平台 Dockerfile 的依赖下载显式使用 direct 网络，避免继承租户 build args。其二，`artifact-init` 在当前 Compose-to-Nomad 模型中是完成初始化后保持健康的长运行 task，staging 使用 512 MiB memory limit 与 256 MiB reservation；资源过低会让整个 9-service group 无法健康，production 参数必须独立验证且满足 reservation 不高于 limit。

## 4. MVP 支持矩阵

| 来源/能力 | MVP | 公开发布前要求 |
| --- | --- | --- |
| 单 HTML | 支持 | HTML 大小/MIME/CSP 检查，生成受控静态镜像 |
| ZIP 静态产物 | 支持 | 解压炸弹、路径穿越、symlink、文件数和总大小限制 |
| GitHub 公有仓库 | 支持 | 固定 commit、受控 adapter、Webhook 签名验证 |
| GitHub 私有仓库 | 支持 | 当前使用加密 source connection + exact-host + 单次 task lease；GitHub App installation 是后续优先演进方向 |
| 私有 Git/Gitea | 支持 | 加密凭据、host allowlist、SSRF 防护、短期注入 |
| Vite/React/Vue/Astro 静态构建 | 支持 | lockfile 固定、受控构建镜像、输出目录探测与验证 |
| Node/Python HTTP 服务 | 支持 | 专用 runtime node、网络隔离、资源限制和运行时沙箱 |
| Dockerfile | 支持 | 所有套餐开放；rootless BuildKit、无 Docker socket、禁用特权能力、出口策略和供应链扫描 |
| Docker Compose | 支持 | 规范化 Compose + 平台生成 `luma.compose.yml`；逐服务策略、资源和 env 检查 |
| 内部数据库/依赖服务 | 支持 | `exposure:none`、受管命名卷、明确备份/恢复和资源配额；不是托管数据库 SLA |
| 后台 worker/定时服务 | 支持 | 所有套餐开放；不暴露公网端口，命令、重启策略、资源和依赖必须可验证 |
| TCP/UDP 公网入口 | 不支持 | 明确拒绝 `tcp-relay`、host port 和任意端口直出 |
| 自定义域名 | 不支持 | 独立域名验证、证书、滥用和解绑流程 |

## 5. 上线硬门槛

以下条件是公开发布 Dockerfile、Compose 和其他用户代码的整体上线门槛。产品不采用“动态能力仅邀请制”的长期分层；门槛未满足时不公开发布 LAE：

1. **控制面隔离**：LAE 用户凭据不能兑换或推导出 Luma management token。
2. **运行隔离**：用户 workload 只能调度到专用 runner pool，不能访问 manager、Nomad API、Tailscale 管理网段或云 metadata。
3. **构建隔离**：用户构建不接触宿主机 Docker socket；构建有 CPU、内存、PID、磁盘、时间和出口限制。
4. **Secret 安全**：源码凭据、环境变量和支付密钥需要 envelope encryption、轮换、审计和日志脱敏。
5. **任务持久化**：诊断/构建/部署状态写入 PostgreSQL，可重放事件、幂等重试、断线恢复和后台 reconciliation。
6. **配额强制**：应用数、存储、构建并发、构建分钟、运行资源和 API 速率在服务端强制执行。
7. **路由准备**：使用 wildcard DNS 与 wildcard TLS，避免每个随机域名单独触发 DNS/证书申请。
8. **滥用治理**：至少具备举报、封禁、审计、速率限制、钓鱼/挖矿/代理滥用响应和内容下线流程。
9. **恢复能力**：PostgreSQL、artifact、registry、Luma state 均有备份和经过演练的恢复流程。

## 6. 文档导航

- [01 产品范围与体验设计](./01-product-and-experience.md)：用户旅程、信息架构、模板湖面和部署动画。
- [02 系统架构与基础设施](./02-architecture-and-infrastructure.md)：服务边界、Luma 拓扑、节点池、域名、存储与观测。
- [03 LAE Agent 与部署生命周期](./03-agent-and-deployment-lifecycle.md)：检测规则、环境变量、构建、manifest 生成、更新与回滚。
- [04 数据、API、CLI 与 Skill 协议](./04-data-api-cli-skill.md)：核心表、公开 API、事件协议、错误模型和机器友好 CLI。
- [05 安全、套餐、支付与运维](./05-security-billing-operations.md)：多租户安全、配额、计费、支付、SLO、备份和合规。
- [06 分阶段并行研发计划](./06-delivery-plan.md)：协议先行、工作流拆分、验收门槛和研发协作方式。
- [07 开放决策](./07-open-decisions.md)：需要产品负责人确认的问题、默认值和影响范围。
- [08 实施状态与验收证据](./08-implementation-status.md)：逐项需求、研发状态、权威证据和发布门槛。
- [09 用户使用指南](./09-user-guide.md)：Web、CLI 与 Agent Skill 的注册、诊断、配置、部署、观测、生命周期和计费流程。
- [10 运维与排障 SOP](./10-operations-troubleshooting-sop.md)：值班检查、Luma/LAE/Builder/Runtime/placement、数据恢复、密钥轮换与 GC。
- [11 部署、升级与回退](./11-deployment-and-upgrade.md)：不可变 release、manager/fleet 顺序、显式 staging sidecar、验收与回退点。
- [12 原始需求—实现—证据矩阵](./12-requirements-evidence-matrix.md)：逐条映射原始 14 项需求、后续澄清、代码证据、live 证据和剩余门槛。

## 7. 名词与状态源

| 名词 | 含义 | 权威状态源 |
| --- | --- | --- |
| User | 登录主体 | LAE PostgreSQL |
| Tenant | 资源、权限和计费边界；个人账户也有 tenant | LAE PostgreSQL |
| App | 用户看到的稳定应用与域名 | LAE PostgreSQL |
| Source Revision | 一次不可变的上传包或 Git commit | LAE PostgreSQL + artifact store |
| Diagnosis | 对某个 source revision 的确定性分析结果 | LAE PostgreSQL |
| Deployment Plan | Agent 输出、Policy 校验后的平台部署计划 | LAE PostgreSQL |
| Deployment | 一次构建与发布尝试 | LAE PostgreSQL |
| Operation | 可重试、可恢复的异步任务 | LAE PostgreSQL |
| Luma Deployment | Luma 实际记录的 Nomad 部署 | Luma Control |
| Artifact | 上传包、构建日志、SBOM、构建产物元数据 | S3-compatible store |
| Image | 以 digest 固定的 OCI 镜像 | LAE internal registry |
| Entitlement | 套餐赋予的能力和配额 | LAE PostgreSQL |
| Usage Ledger | 可审计、不可覆盖的用量事件 | LAE PostgreSQL |
| Placement Decision | region 到内部候选节点约束、容量与 volume affinity 的不可变决策；租户只看安全投影 | Luma Control + Nomad plan/allocation |

LAE 是产品语义的权威来源，Luma/Nomad 是运行态的权威来源。后台 reconciler 持续对比两者，不允许控制台通过“猜测”把运行态标成成功。

## 8. 顶层验收标准

完成 MVP 不是“页面能点通”，而是以下端到端场景全部可重复通过：

- 新用户完成邮箱验证后，自动得到 personal tenant 和一枚只显示一次的 deploy token。
- 用户上传一个合法 HTML/ZIP，诊断给出可解释结果，补齐环境变量后部署到稳定随机域名。
- 用户连接一个只包含标准 Compose、没有 Luma 文件的合法仓库，builder 中的 Agent 识别多服务拓扑、内部依赖、命名卷和一个或多个 HTTP 入口，生成并保存规范化 Compose 与 Luma sidecar 后完成部署。
- 分析与构建使用同一完整 commit/source snapshot digest；两个以上 build service 都返回逐服务结构化事件和 OCI digest。
- 浏览器刷新、关闭、重新登录或 CLI 断线后，仍可从事件 cursor 继续查看同一个任务。
- 同一个幂等键重复提交不会重复扣配额、重复构建或创建第二个应用。
- 部署失败不会替换上一健康版本；可从构建产物检查点重试并保留完整审计证据。
- 用户只能读取自己的应用、日志、指标、secret 名称和部署事件。
- 超级管理员可从 Luma Dashboard 的 LAE 区域查看用户、应用、用量和关联 Luma/Nomad 资源。
- 租户响应、日志和 CLI 不出现 node/IP/failure domain；授权管理员可关联 placement 决策与实际 allocation，并留下审计证据。
- Lite/Pro/Ultra 的配额由服务端强制，mock 支付能通过真实同构 webhook 流程切换 entitlement。
- CLI 与 Agent Skill 可用纯 JSON/NDJSON 完成登录、诊断、部署、等待、状态查询与重试。

## 9. 当前代码证据入口

- `luma/control/server.py`：Control API、NDJSON stream、Git provider、build/import、部署/回滚/重启与 dashboard 数据。
- `luma/agent.py`：node-agent lease、legacy import/build 与 Builder task 下发。
- `luma/builder_tasks.py`、`luma/builder_executor.py`、`luma/builder_build_executor.py`：Builder v2 协议、隔离分析、多镜像构建和结果收口。
- `luma/credential_broker.py`：Git/object source 的短期 broker redemption 和无重定向下载边界。
- `luma/gitops.py`：当前 shallow Git clone 路径。
- `luma/nomad_render.py`：单服务/Compose 到 Nomad job/group 的渲染事实。
- `luma/lae_placement.py` 与 `luma/lae_runtime.py`：LAE region-only placement、候选约束、Nomad plan、volume affinity 和公开投影边界。
- `luma/control/state.py`：当前单文件 state 和全局 deploy/join token。
- `luma/control/secrets.py`：scoped secret 的导入、持久化和渲染。
- `luma/cli.py`：现有 CLI、`--format json|ndjson`、deploy/import/build 命令。
- `dashboard-src/src/deploy/`：现有部署表单、模板、GitHub 导入和进度 UI。
- `lae/`：LAE Web/API/Worker/CLI/Skill、PostgreSQL migrations、contracts 和 Luma 部署资产。
- `docs/architecture.md`：Luma region / exposure / Nomad / Traefik 架构。
- `docs/deployment-yaml.md` 与 `docs/compose-storage.md`：Luma manifest 和存储语义。

## 10. 技术选型依据

建议的 Web 栈采用 Next.js App Router、React、TypeScript、Tailwind theme variables、Radix Primitives、Motion 和 TanStack Query；API/Agent 使用 FastAPI + Pydantic；平台统一输出 OpenAPI、SSE/NDJSON 和 OpenTelemetry。相关官方资料：

- [Next.js App Router](https://nextjs.org/docs/app)
- [Motion layout animations](https://motion.dev/docs/react-layout-animations)
- [Radix Primitives](https://www.radix-ui.com/primitives/docs/overview/introduction)
- [Tailwind theme variables](https://tailwindcss.com/docs/theme)
- [TanStack Query](https://tanstack.com/query/latest/docs/framework/react/overview)
- [FastAPI features](https://fastapi.tiangolo.com/features/)
- [Docker rootless mode](https://docs.docker.com/engine/security/rootless/)
- [GitHub App installation token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [OpenTelemetry Collector deployment](https://opentelemetry.io/docs/collector/deploy/)
