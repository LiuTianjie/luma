# 12. 原始需求—实现—证据矩阵

> 审计日期：2026-07-14
> 范围：最初 14 项产品需求、后续 Compose/Builder/placement/AI 澄清，以及当前批量 404/502 可用性问题
> 权威边界：[08 实施状态](./08-implementation-status.md) 决定完成度；本文件负责把需求映射到代码、live 证据和剩余门槛。

## 1. 证据口径

本矩阵严格区分四类事实：

- **设计**：协议/交互已经定义，不代表代码存在。
- **Implemented**：代码与自动化测试存在，不代表真实 Luma 可用。
- **Staging partial**：真实组件或部分用户链路已验证，但未覆盖完整 source → Builder → Runtime、故障和安全矩阵。
- **Verified/Done**：必须满足 [08](./08-implementation-status.md) 的定义；当前没有任何一行可仅凭 HTTP 200、Nomad `running` 或页面截图标为 Done。

2026-07-14 的 live 基线：

- Luma CLI、Control 与 manager agent 为 `0.1.233`；本轮没有 worker-wide fleet 升级，在线非 manager agent 主要为 `0.1.228`；离线 `blg` 保持 `0.1.175`。
- `manager` 是唯一控制面；`aly` 是历史名称。
- LAE 平台当前在 `manager`，租户 runtime staging allowlist 为 `manager + tecent`，构建与内部 registry 在 `builder`。
- `lae-platform-staging` 使用 exact commit `65a4010` 构建的 immutable platform images；9 个平台 task 运行。
- Web、API ready、Agent ready、artifact ready 与 Control health 均为 HTTP 200；Agent ready 报告 `mode=ai`、`configured=true`。
- `0.1.233` 完整产品验收已完成 preview auth、AI 诊断、环境配置、四服务 Compose、Builder build、双公网 HTTPS route、双持久卷、restart/suspend/resume、更新检查、七类 unsupported blocker、delete 与 token revoke；公网探测无失败。FastAPI 模板与 HTML upload 的既有真实链路证据继续有效。真实邮箱、ZIP、真实私有 Git与完整安全负例仍未完成。
- `0.1.229-0.1.233` 增加 Cloudflare DNS-01 wildcard TLS，修复 manager 更新配置所有权、生命周期/初次部署 DNS 授权和 runtime 假异步阻塞。Runtime deployment 现为持久化接受后后台执行；同一幂等请求可在 Control 重启后恢复。长时间多 edge sentinel、Docker/CNI 自愈与 route reconciliation 故障注入仍是 production gate。

## 2. 原始 14 项需求

| ID | 原始目标与当前合同 | 实现证据 | Live/验收事实 | 结论与剩余门槛 |
| --- | --- | --- | --- | --- |
| U-01 | LAE 控制台；邮件注册/登录；自动 personal tenant | `lae/apps/web/src/components/auth-portal.tsx`、`lae/apps/api/src/lae_api/auth_service.py`、`lae/packages/python/lae-store/src/lae_store/auth.py`；`test_auth_*`、`test_email_sender.py` | Web/login 与 API healthy；Mailpit challenge、注册和 session 冒烟通过。Working tree 另有仅限保留 `.invalid` 身份的 staging preview flow | **Staging partial**。preview 不是生产邮件且需随新平台 ref 发布后才算 live；真实 SMTP 当前不可用，Mailpit 不会给用户真实邮箱送信。仍需 production provider、送达/退信/outbox、反滥用和浏览器 E2E |
| U-02 | 主部署界面支持 HTML/ZIP、公有 Git、私有 Git、Dockerfile/Compose；缺 Git 凭据时配置；Agent 诊断、明确 blocker、识别 env、部署动画、成功后进入应用列表 | `lae-console.tsx`、`upload_api.py`、`source_connection_api.py`、`app.py`、`deployment_api.py`、Worker analyze/deployment；`test_static_upload_*`、`test_source_connection_*`、`test_worker_analyze.py`、`test_worker_deployment.py` | FastAPI、HTML 与四服务 Compose 已真实完成 Agent → Builder → Runtime → 随机域名/TLS；Compose 覆盖必需 env、双 route、双 volume 和七类明确 blocker | **Staging partial**。核心 Compose 正向链路与 unsupported 负例已通过；ZIP、真实私有 Git、`diagnostic_failed` 与浏览器交互回归仍需逐项验收 |
| U-03 | 流行模板一键拉起 | `template_api.py`、Web 模板入口、CLI `templates list/launch`、commit-pinned catalog；`test_template_api.py` | FastAPI 模板从选择、诊断到真实 Runtime 上线通过 | **Staging partial**。尚缺其余模板真实 deploy、每日 smoke、失败计数、自动下架、版本回退和运营流程 |
| U-04 | 应用列表查看状态/信息；停止、重启等生命周期操作 | `application_api.py`、`application_lifecycle_api.py`、`observability_api.py`、Worker lifecycle；`test_application_catalog*`、`test_application_lifecycle_*`、`test_observability_api.py` | 四服务 Compose 的 restart、suspend/resume、update-check 与 delete 已跑真实 Runtime，双 route 在恢复后均通过探测 | **Staging partial**。rollback、各动作失败恢复和 volume retain/restore 的真实矩阵仍待验收；“stop”产品语义统一为 suspend |
| U-05 | `lae` CLI 提供 Web 等价操作，以 deploy token 鉴权 | `lae/cli/src/lae_cli/__main__.py`；`test_cli.py`、`test_cli_sources_upload.py` | clean-room CLI 已仅凭 deploy token 完成 whoami、FastAPI 模板诊断、deploy、deployment history、restart、route ready 与 delete；既有 check-update、NDJSON cursor、日志/指标证据继续有效 | **Verified（核心路径）**。HTML/ZIP、私有 Git、Compose CLI 负例和断网恢复矩阵仍需扩展；注册、token rotation 保持 Web/API session flow，不应伪造 CLI 命令 |
| U-06 | 注册自动生成用户级 deploy token；CLI 能持续查看部署流程 | `DEFAULT_DEPLOY_TOKEN_SCOPES`、一次展示/哈希存储、Operation cursor/NDJSON；`test_auth_*`、`test_public_resource_*`、`test_cli.py` | 默认 token/token verify 已冒烟；真实 check-update 与 deployment Operation 已通过 NDJSON cursor 1→terminal 续看 | **Staging partial**。当前 NDJSON 是 CLI 对 JSON cursor polling 的机器流；服务端 SSE 与 cursor-expired retention 协议尚未实现。断网后跨进程 resume、cancel/late-success 与配额只扣一次仍需 Luma E2E；默认 token 故意不含 `billing:checkout` |
| U-07 | AI 友好的 Skill，至少登录、检查、部署，可包含注册/支付人机流程 | `lae/skills/lae-deploy/`、版本化 Knowledge Pack、CLI contract/policy；`test_skill_assets.py` | clean-room Agent 已严格按 Skill，仅使用 `lae` CLI/deploy token 完成真实模板部署、历史查询、重启、route 验证与清理 | **Verified（项目内分发）**。Skill 使用公开 verdict `deployable/needs_input/unsupported/diagnostic_failed`；注册必须安全跳转 Web，支付只能创建 checkout 并由人确认。仍需独立包发布与跨 Agent runtime 兼容矩阵 |
| U-08 | 应用更新时主动触发 Agent 判断部署文件是否要更新 | `update_checks.py`、verified plan loader、application lifecycle API/Worker、结构化 `updateCheck`、deployment exact-confirmation admission；`test_update_check_result.py`、deployment/API/CLI/lifecycle tests | 两次真实 check-update candidate digest 相同；用 candidate 部署后第三次检查返回 source/plan/aggregate 均无变化。代码层已输出 service/route/volume/env diff，Web/CLI 消费同一结构并要求稳定 confirmation code | **Staging partial**。真实 Git 破坏性变更的拒绝→逐项人工确认→候选部署与失败保旧负例尚未完整验收 |
| U-09 | Agent 为可部署应用生成 Luma 文件并与应用绑定；用户仓库无需包含 Luma 文件 | `app_revisions`/revision services/routes/volumes、`plan_resolver.py`、deployment admission/materializer；`test_deployment_plan_resolver.py`、`test_deployment_admission*` | FastAPI 模板在用户未提供 Luma 文件的情况下生成并保存计划，materialize 为真实 Runtime Job | **Staging partial**。单 HTTP 已证明绑定链路；仍需对 Git/Compose 证明 source snapshot、DeploymentPlan、BuildPlan、normalized Compose、manifest 与 image digest 可重放且不可变 |
| U-10 | Lite/Pro/Ultra；存储、应用数和高级功能配额；月/年付；微信/支付宝，可先 mock | `billing.py`、store billing/models、Web account/checkout；`test_billing_*` | staging mock 可用；production driver disabled | **Partial**。价格/权益是草案，usage ledger 仍有占位；真实微信/支付宝 adapter、sandbox/webhook reconcile/refund、发票与商户均未实现，production 不得收费 |
| U-11 | Luma Dashboard 作为超级管理员查看全部用户和应用 | `admin_api.py`、`luma/control/server.py` admin proxy、`LaeAdminPage.tsx`；`test_admin_api.py`、`test_lae_admin_proxy.py` | 相关服务已部署 | **Implemented**。当前只读；真实 RBAC、跨系统 tenant/app/allocation 关联、管理员写动作、审批/双人复核和审计 E2E 未完成 |
| U-12 | 使用流程尽量简单、成功率高；先支持 Luma 已知的简单应用 | Agent adapter/policy、四态 verdict、模板 catalog、稳定错误/幂等/恢复协议 | 组件健康与部分冒烟通过 | **In progress**。尚无按 source/framework 统计的首次部署成功率，也没有完整 golden-app/negative corpus 的真实 staging 报告；不能用“支持类型多”代替成功率 |
| U-13 | 新颖、一致、高级、低 AI 感 UI；部署步骤清晰、动效考究 | `lae-console.tsx`、`luma-dashboard.css`、`globals.css`；Web production build/窄屏/reduced-motion 记录 | exact commit `7c1212c` 已 live；模板区为纯白 Luma 画布，应用页和部署分步工作台使用 Luma Dashboard 设计语言 | **Staging partial**。需继续真实用户视觉、键盘、读屏、reduced-motion 与性能回归；动画只能映射真实 Operation event，不得伪造成功 |
| U-14 | 默认随机字符串 `.itool.tech`；不支持自定义域名 | domain allocator、application routes、runtime adapter/policy；deployment/catalog tests | 两个真实 128-bit lowercase-hex 随机域名均有效 TLS/HTTP 200 | **Staging partial**。单 HTTP 已通过；仍需所有 Compose route、更新/回滚/暂停域名稳定和删除冷却；V1 明确拒绝自定义域名 |

## 3. 后续澄清与新增硬约束

| ID | 已确认约束 | 实现/文档证据 | 当前结论 |
| --- | --- | --- | --- |
| C-01 | Compose 是一等模型，可有多个公网 HTTP service | Normalized Compose、route-per-service、deployment admission/render tests；[02](./02-architecture-and-infrastructure.md)、[03](./03-agent-and-deployment-lifecycle.md) | 四服务 Compose、真实双 HTTPS 域名与逐 route health 已通过；失败保旧与破坏性 update diff 仍需 E2E |
| C-02 | 允许内部服务、worker、datastore 与受管 named volume；Lite 也可用 | service role/volume models、placement/storage admission tests | 真实 Compose 已部署内部服务与两个受管 named volume，lifecycle 后绑定保持；备份/恢复、volume affinity 故障和数据库数据语义仍是门槛；不是托管数据库 SLA |
| C-03 | 暂不支持 `tcp-relay`，也不开放 TCP/UDP/host port | Agent policy、Knowledge Pack、plan resolver、Luma policy 与 Skill 均拒绝 | 需要补真实 staging 负例；任何 UI/CLI 不能提供旁路 |
| C-04 | 拉代码、Agent runner 与构建全部走 Luma `builder` | Builder Task v1、credential/object redemption、analyze/build executors、Worker adapter | 代码和局部真实接线存在；公网多租户 rootless/egress/quota/GC/orphan 故障验证未关闭 |
| C-05 | `manager` 是唯一控制面；`aly` 已过时 | live status、[07 D-016](./07-open-decisions.md)、部署/SOP | 当前文档已统一；任何升级/placement 命令再出现 `aly` 应视为 stale 配置缺陷 |
| C-06 | Staging runtime 可用 `manager + tecent`；manager 可同时是节点；具体 placement 对用户不可见 | LAE runtime allowlist、runtime role、placement admission/admin projection tests | live 配置已存在；production 仍建议至少两个专用 runner，且要完成容量、CNI、volume 与故障切换演练 |
| C-07 | ARK key/model 做成 provider-agnostic 可配置；AI 必须了解 LAE Skill/背景知识；不可部署要明确告知 | `LAE_AGENT_LLM_BASE_URL/API_KEY/MODEL`、`lae/knowledge/v1/knowledge-pack.json`、Agent Controller/runner、四态 verdict、Web `analysisFailureMessage` | Agent ready 显示 AI configured；真实 E2E 已覆盖 `deployable`、必需配置 `needs_input` 和带七类稳定 blocker 的 `unsupported`；平台诊断故障与项目不支持明确分开，用户不提供 API key | 仍需 provider-backed `diagnostic_failed` 故障注入与 blocker 文案长期兼容性验收 |
| C-08 | 控制面升级或发布其他应用不得让既有应用 404/502，更不能依赖人工全量重启 | [SOP 11.1](./10-operations-troubleshooting-sop.md#111-控制面升级或其他应用部署后批量-404502)、[部署手册 6.1](./11-deployment-and-upgrade.md#61-升级期间的-route-连续性门禁)；deployment-scoped router/service、Tailscale node metadata、exact rollout barrier | **Staging partial**。Control/manager `0.1.233` 更新、wildcard DNS-01 和完整产品 E2E 均无需人工重启；长时间多 edge sentinel、Docker restart/CNI 和 reconciliation 故障注入仍是 production gate |

## 4. 基础设施落点

| 层 | Staging 当前落点 | Production 目标/缺口 |
| --- | --- | --- |
| Luma Control | `manager`，唯一控制面；CLI/Control/manager agent live `0.1.233` | Control HA/恢复、严格 service principal、更广升级 sentinel；manager 是否继续承载 runtime 需容量与故障决策 |
| LAE 平台 | `manager` 上单个 9-task Nomad group：Web、API、Worker、Agent Controller、PostgreSQL、MinIO、artifact-init、Valkey、Mailpit | 专用 `lae-core`/平台池；migration job/lock；平台服务健康/滚动策略；不能把单 group 当 HA |
| Builder/registry | `builder`；内部 registry，Git/object task lease，analyze/build | 专用 rootless builder pool、无宿主 socket、CPU/memory/PID/disk/time/egress 强制、registry auth/GC/容量/恢复 |
| Tenant Runtime | `manager + tecent` 正向 allowlist，manager 显式 runtime role | 至少两个专用 runner；管理网/metadata/Tailscale deny、cap drop、read-only rootfs、PID/ephemeral storage、网络策略与 chaos |
| Product data | PostgreSQL + MinIO；staging 使用独立 NFS path；Valkey 非权威 | 独立 storage class、PostgreSQL WAL/PITR、MinIO/registry/volume 跨故障域备份和 restore drill |
| Ingress/domain | Luma route/DNS/TLS，Cloudflare DNS-01 wildcard 证书与随机 `*.itool.tech` 目标 | route ownership/reconcile、全 route probe 与长期连续性；V1 不支持自定义域名或 TCP relay |
| Email | Mailpit 内部捕获；外部 SMTP 当前不可用 | 真实 SMTP/API provider、SPF/DKIM/DMARC、outbox、退信/投诉、限流与送达监控 |
| Billing | Staging signed mock；production disabled | WeChat/Alipay adapter、sandbox、webhook/reconcile/refund、商户/发票/财务审计 |
| Observability | Luma Dashboard、应用日志/指标 API 基座 | 独立 OTel/metrics/log/alert 资产、SLO、tenant retention、告警 runbook；当前仓库没有完整生产观测 deployment |

## 5. 审计结论与优先级

### P0：发布前必须关闭

1. **跨应用 404/502 剩余故障矩阵**：已复现的名称碰撞/跨节点 upstream 与正常升级路径已修复并回归；仍需 Docker daemon restart 后 CNI 自愈、route reconciliation 故障注入和多 edge sentinel，不能把人工 restart 写进标准流程。
2. **租户 Runtime 剩余纵向 E2E**：HTML、单 HTTP 与 Compose 双 HTTP + volume 已通过；继续覆盖 ZIP、真实私有 Git、rollback 和失败保旧。
3. **公网多租户隔离**：专用 builder/runner、网络/metadata 管理面阻断、资源/出口限制、secret 和 artifact 边界必须有真实负例证据。
4. **数据恢复**：PostgreSQL PITR、MinIO/registry/tenant volume restore drill 未完成。

### P1：产品发布门槛

1. 真实 SMTP 与用户邮箱送达；Mailpit 只能证明生成/捕获邮件。
2. AI provider-backed 四态 verdict golden E2E，尤其是 `unsupported` blocker 的稳定 code、证据位置和可执行修复建议。
3. 模板 daily smoke/自动下架；真实微信/支付宝；usage ledger/硬配额；admin RBAC/写动作。
4. Skill 独立包正式分发与跨 Agent runtime 验收；项目内 clean-room Agent 核心路径已通过，CLI/Web/API 同一 contract 的兼容矩阵仍需扩展。
5. 真实浏览器视觉、可访问性、reduced-motion 与 Operation event 驱动动画回归。

### 本轮已纠正的文档冲突

- 将旧的 `0.1.171`/`0.1.196` 快照更新为当前 CLI/Control/manager `0.1.233`、平台 exact ref `65a4010`，并明确非 manager fleet 本轮未全量升级。
- 将 “Mailpit 注册”等同真实邮件送达的表述改为“Mailpit 捕获 challenge”。
- 将 “LLM 只做解释”修正为“AI 受 Knowledge Pack 约束生成 proposal，确定性校验终审”。
- Skill 从内部状态 `needs_configuration/not_deployable` 改为公开 verdict `needs_input/unsupported/diagnostic_failed`。
- API 文档将当前路由与目标态分开：session 管理、analysis rerun、Operation list、billing portal/webhook 和 admin write/abuse API 不再被误写成已实现；同时补入 auth config/preview、templates 和 deployment configuration 的真实路由。
- 部署成功口径补充 exact `JobModifyIndex` → evaluation → deployment → `JobVersion` → 新健康 allocation 的 rollout barrier；历史 healthy allocation 不得误报成功。
