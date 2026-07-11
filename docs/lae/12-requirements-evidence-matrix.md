# 12. 原始需求—实现—证据矩阵

> 审计日期：2026-07-12
> 范围：最初 14 项产品需求、后续 Compose/Builder/placement/AI 澄清，以及当前批量 404/502 可用性问题
> 权威边界：[08 实施状态](./08-implementation-status.md) 决定完成度；本文件负责把需求映射到代码、live 证据和剩余门槛。

## 1. 证据口径

本矩阵严格区分四类事实：

- **设计**：协议/交互已经定义，不代表代码存在。
- **Implemented**：代码与自动化测试存在，不代表真实 Luma 可用。
- **Staging partial**：真实组件或部分用户链路已验证，但未覆盖完整 source → Builder → Runtime、故障和安全矩阵。
- **Verified/Done**：必须满足 [08](./08-implementation-status.md) 的定义；当前没有任何一行可仅凭 HTTP 200、Nomad `running` 或页面截图标为 Done。

2026-07-12 的只读 live 基线：

- Luma CLI、Control 与 live fleet 为 `0.1.171`；8 个在线节点 ready。
- `manager` 是唯一控制面；`aly` 是历史名称。
- LAE 平台固定在 `lab`，租户 runtime staging allowlist 为 `manager + tecent`，构建在 `builder`。
- `lae-platform-staging` job version 4 使用 commit tag `20469a4`；9 个平台 task 运行。
- Web、API ready、Agent ready、artifact ready 与 Control health 均为 HTTP 200；Agent ready 报告 `mode=ai`、`configured=true`。
- Mailpit 注册、默认 deploy token、CLI、模板与 analysis 已有冒烟；真实邮箱、最新 provider-backed 四态 verdict、租户 Runtime 部署/生命周期完整 E2E 未完成。
- 控制面升级、Docker 配置变化或其他应用发布后，既有 route 批量 404/502 的 P0 仍未由 live release 关闭。

## 2. 原始 14 项需求

| ID | 原始目标与当前合同 | 实现证据 | Live/验收事实 | 结论与剩余门槛 |
| --- | --- | --- | --- | --- |
| U-01 | LAE 控制台；邮件注册/登录；自动 personal tenant | `lae/apps/web/src/components/auth-portal.tsx`、`lae/apps/api/src/lae_api/auth_service.py`、`lae/packages/python/lae-store/src/lae_store/auth.py`；`test_auth_*`、`test_email_sender.py` | Web/login 与 API healthy；Mailpit challenge、注册和 session 冒烟通过。Working tree 另有仅限保留 `.invalid` 身份的 staging preview flow | **Staging partial**。preview 不是生产邮件且需随新平台 ref 发布后才算 live；真实 SMTP 当前不可用，Mailpit 不会给用户真实邮箱送信。仍需 production provider、送达/退信/outbox、反滥用和浏览器 E2E |
| U-02 | 主部署界面支持 HTML/ZIP、公有 Git、私有 Git、Dockerfile/Compose；缺 Git 凭据时配置；Agent 诊断、明确 blocker、识别 env、部署动画、成功后进入应用列表 | `lae-console.tsx`、`upload_api.py`、`source_connection_api.py`、`app.py`、`deployment_api.py`、Worker analyze/deployment；`test_static_upload_*`、`test_source_connection_*`、`test_worker_analyze.py`、`test_worker_deployment.py` | 真实模板/Git analysis 冒烟；平台与 Builder 接线 healthy | **Implemented, staging incomplete**。每类 source 的 inspect → env → build → deploy → all-route verify 尚未完成；最新 AI verdict、私有 Git redemption、HTML/ZIP 浏览器上传和失败保旧要逐项验收 |
| U-03 | 流行模板一键拉起 | `template_api.py`、Web 模板湖面、CLI `templates list/launch`、commit-pinned catalog；`test_template_api.py` | catalog、launch/analysis 冒烟已跑 | **Staging partial**。尚缺每模板真实 deploy、每日 smoke、失败计数、自动下架、版本回退和运营流程 |
| U-04 | 应用列表查看状态/信息；停止、重启等生命周期操作 | `application_api.py`、`application_lifecycle_api.py`、`observability_api.py`、Worker lifecycle；`test_application_catalog*`、`test_application_lifecycle_*`、`test_observability_api.py` | Web/API 已部署，代码入口可见 | **Implemented**。suspend/resume/restart/rollback/delete、日志、指标与失败恢复的真实 Runtime 动作矩阵未验收；“stop”产品语义统一为 suspend |
| U-05 | `lae` CLI 提供 Web 等价操作，以 deploy token 鉴权 | `lae/cli/src/lae_cli/__main__.py`；`test_cli.py`、`test_cli_sources_upload.py` | login/whoami、模板/应用等基础 CLI 冒烟已跑 | **Staging partial**。Runtime deploy/watch/lifecycle/observability 纵向 E2E 未完成；token 管理、注册和 deployment history 仍是 Web/API session flow，不应伪造 CLI 命令 |
| U-06 | 注册自动生成用户级 deploy token；CLI 能持续查看部署流程 | `DEFAULT_DEPLOY_TOKEN_SCOPES`、一次展示/哈希存储、Operation cursor/NDJSON；`test_auth_*`、`test_public_resource_*`、`test_cli.py` | 默认 token 与 token verify 已在 staging 冒烟 | **Staging partial**。当前 NDJSON 是 CLI 对 JSON cursor polling 的机器流；服务端 SSE 与 cursor-expired retention 协议尚未实现。真实部署过程的断线续看、cancel/late-success 与配额只扣一次仍需 Luma E2E；默认 token 故意不含 `billing:checkout` |
| U-07 | AI 友好的 Skill，至少登录、检查、部署，可包含注册/支付人机流程 | `lae/skills/lae-deploy/`、版本化 Knowledge Pack、CLI contract/policy；`test_skill_assets.py` | Skill 资产随仓库存在，未形成正式分发证据 | **Implemented**。Skill 使用公开 verdict `deployable/needs_input/unsupported/diagnostic_failed`；注册必须安全跳转 Web，支付只能创建 checkout 并由人确认。仍需打包发布和 clean-room Agent 全流程验收 |
| U-08 | 应用更新时主动触发 Agent 判断部署文件是否要更新 | `update_checks.py`、application lifecycle API/Worker、结构化 `updateCheck`；`test_update_check_result.py`、lifecycle tests | Web 已消费结构化结果 | **Implemented**。真实 Git 变化、无变化、破坏性 env/route/volume diff、人工确认、失败保旧与确认后 deployment 尚未完整验收 |
| U-09 | Agent 为可部署应用生成 Luma 文件并与应用绑定；用户仓库无需包含 Luma 文件 | `app_revisions`/revision services/routes/volumes、`plan_resolver.py`、deployment admission/materializer；`test_deployment_plan_resolver.py`、`test_deployment_admission*` | 平台自身 manifest 已由 Luma 部署；尚无完整租户 revision 运行证据 | **Implemented**。需证明 source snapshot、DeploymentPlan、BuildPlan、normalized Compose、sidecar/manifest、image digest 全部不可变绑定并可重放，且用户仓库不含 Luma 文件 |
| U-10 | Lite/Pro/Ultra；存储、应用数和高级功能配额；月/年付；微信/支付宝，可先 mock | `billing.py`、store billing/models、Web account/checkout；`test_billing_*` | staging mock 可用；production driver disabled | **Partial**。价格/权益是草案，usage ledger 仍有占位；真实微信/支付宝 adapter、sandbox/webhook reconcile/refund、发票与商户均未实现，production 不得收费 |
| U-11 | Luma Dashboard 作为超级管理员查看全部用户和应用 | `admin_api.py`、`luma/control/server.py` admin proxy、`LaeAdminPage.tsx`；`test_admin_api.py`、`test_lae_admin_proxy.py` | 相关服务已部署 | **Implemented**。当前只读；真实 RBAC、跨系统 tenant/app/allocation 关联、管理员写动作、审批/双人复核和审计 E2E 未完成 |
| U-12 | 使用流程尽量简单、成功率高；先支持 Luma 已知的简单应用 | Agent adapter/policy、四态 verdict、模板 catalog、稳定错误/幂等/恢复协议 | 组件健康与部分冒烟通过 | **In progress**。尚无按 source/framework 统计的首次部署成功率，也没有完整 golden-app/negative corpus 的真实 staging 报告；不能用“支持类型多”代替成功率 |
| U-13 | 新颖、一致、高级、低 AI 感 UI；模板湖面/漂浮 icon；考究动效 | `lae-console.tsx`、`luma-dashboard.css`、`globals.css`；Web production build/窄屏/reduced-motion 记录 | 当前 Web 已 live；working tree 有尚未发布的视觉升级 | **Staging partial**。需在真实用户链路完成视觉/键盘/读屏/reduced-motion/性能回归；动画只能映射真实 Operation event，不得用前端计时器伪造部署成功 |
| U-14 | 默认随机字符串 `.itool.tech`；不支持自定义域名 | domain allocator、application routes、runtime adapter/policy；deployment/catalog tests | wildcard TLS/DNS 与平台域名健康 | **Implemented, tenant E2E open**。需验证 128-bit 随机唯一性、所有 Compose route、更新/回滚/暂停域名稳定和删除冷却；V1 明确拒绝自定义域名 |

## 3. 后续澄清与新增硬约束

| ID | 已确认约束 | 实现/文档证据 | 当前结论 |
| --- | --- | --- | --- |
| C-01 | Compose 是一等模型，可有多个公网 HTTP service | Normalized Compose、route-per-service、deployment admission/render tests；[02](./02-architecture-and-infrastructure.md)、[03](./03-agent-and-deployment-lifecycle.md) | 代码已覆盖；真实双 HTTP 域名、逐 route health 和失败保旧 E2E 未完成 |
| C-02 | 允许内部服务、worker、datastore 与受管 named volume；Lite 也可用 | service role/volume models、placement/storage admission tests | 代码已覆盖；真实持久化、备份/恢复、volume affinity 和节点故障仍是门槛；不是托管数据库 SLA |
| C-03 | 暂不支持 `tcp-relay`，也不开放 TCP/UDP/host port | Agent policy、Knowledge Pack、plan resolver、Luma policy 与 Skill 均拒绝 | 需要补真实 staging 负例；任何 UI/CLI 不能提供旁路 |
| C-04 | 拉代码、Agent runner 与构建全部走 Luma `builder` | Builder Task v1、credential/object redemption、analyze/build executors、Worker adapter | 代码和局部真实接线存在；公网多租户 rootless/egress/quota/GC/orphan 故障验证未关闭 |
| C-05 | `manager` 是唯一控制面；`aly` 已过时 | live status、[07 D-016](./07-open-decisions.md)、部署/SOP | 当前文档已统一；任何升级/placement 命令再出现 `aly` 应视为 stale 配置缺陷 |
| C-06 | Staging runtime 可用 `manager + tecent`；manager 可同时是节点；具体 placement 对用户不可见 | LAE runtime allowlist、runtime role、placement admission/admin projection tests | live 配置已存在；production 仍建议至少两个专用 runner，且要完成容量、CNI、volume 与故障切换演练 |
| C-07 | ARK key/model 做成 provider-agnostic 可配置；AI 必须了解 LAE Skill/背景知识；不可部署要明确告知 | `LAE_AGENT_LLM_BASE_URL/API_KEY/MODEL`、`lae/knowledge/v1/knowledge-pack.json`、Agent Controller/runner、四态 verdict | Agent ready 已显示 AI configured；真实 provider-backed `deployable/needs_input/unsupported/diagnostic_failed` 各一例和 blocker 质量仍需验收。用户不提供 API key |
| C-08 | 控制面升级或发布其他应用不得让既有应用 404/502，更不能依赖人工全量重启 | [SOP 11.1](./10-operations-troubleshooting-sop.md#111-控制面升级或其他应用部署后批量-404502)、[部署手册 6.1](./11-deployment-and-upgrade.md#61-升级期间的-route-连续性门禁)；working tree 新增 Linux `nomad_init_*` only-loopback 的 `missingNetworks` 只读诊断 | **Live P0 open**。诊断尚未发布且空结果是 fail-safe evidence，不是健康证明；新 release 必须证明配置幂等、Docker restart 后 CNI/route 自愈、exact JobVersion rollout barrier 和未变更 sentinel routes 连续性 |

## 4. 基础设施落点

| 层 | Staging 当前落点 | Production 目标/缺口 |
| --- | --- | --- |
| Luma Control | `manager`，唯一控制面；live `0.1.171` | Control HA/恢复、严格 service principal、升级无跨应用中断；manager 是否继续承载 runtime 需容量与故障决策 |
| LAE 平台 | `lab` 上单个 9-task Nomad group：Web、API、Worker、Agent Controller、PostgreSQL、MinIO、artifact-init、Valkey、Mailpit | 专用 `lae-core`/平台池；migration job/lock；平台服务健康/滚动策略；不能把单 group 当 HA |
| Builder/registry | `builder`；内部 registry，Git/object task lease，analyze/build | 专用 rootless builder pool、无宿主 socket、CPU/memory/PID/disk/time/egress 强制、registry auth/GC/容量/恢复 |
| Tenant Runtime | `manager + tecent` 正向 allowlist，manager 显式 runtime role | 至少两个专用 runner；管理网/metadata/Tailscale deny、cap drop、read-only rootfs、PID/ephemeral storage、网络策略与 chaos |
| Product data | PostgreSQL + MinIO；staging 使用独立 NFS path；Valkey 非权威 | 独立 storage class、PostgreSQL WAL/PITR、MinIO/registry/volume 跨故障域备份和 restore drill |
| Ingress/domain | Luma route/DNS/TLS，随机 `*.itool.tech` 目标；平台经 `tailscale-relay` | wildcard DNS/TLS、route ownership/reconcile、全 route probe；V1 不支持自定义域名或 TCP relay |
| Email | Mailpit 内部捕获；外部 SMTP 当前不可用 | 真实 SMTP/API provider、SPF/DKIM/DMARC、outbox、退信/投诉、限流与送达监控 |
| Billing | Staging signed mock；production disabled | WeChat/Alipay adapter、sandbox、webhook/reconcile/refund、商户/发票/财务审计 |
| Observability | Luma Dashboard、应用日志/指标 API 基座 | 独立 OTel/metrics/log/alert 资产、SLO、tenant retention、告警 runbook；当前仓库没有完整生产观测 deployment |

## 5. 审计结论与优先级

### P0：发布前必须关闭

1. **跨应用 404/502**：live 故障仍可能由 Docker daemon restart 后 CNI 丢失、route reconciliation 不收敛或 rollout 误判引起；不能把手工 restart 写进标准发布流程。
2. **租户 Runtime 纵向 E2E**：至少覆盖 HTML、私有 Git、单 HTTP、Compose 双 HTTP + volume，从 inspect/env/build/deploy/all-route verify 到 restart/update/rollback/delete，并包含失败保旧。
3. **公网多租户隔离**：专用 builder/runner、网络/metadata 管理面阻断、资源/出口限制、secret 和 artifact 边界必须有真实负例证据。
4. **数据恢复**：PostgreSQL PITR、MinIO/registry/tenant volume restore drill 未完成。

### P1：产品发布门槛

1. 真实 SMTP 与用户邮箱送达；Mailpit 只能证明生成/捕获邮件。
2. AI provider-backed 四态 verdict golden E2E，尤其是 `unsupported` blocker 的稳定 code、证据位置和可执行修复建议。
3. 模板 daily smoke/自动下架；真实微信/支付宝；usage ledger/硬配额；admin RBAC/写动作。
4. Skill 正式分发和 clean-room Agent 验收；CLI/Web/API 同一 contract 的兼容矩阵。
5. 真实浏览器视觉、可访问性、reduced-motion 与 Operation event 驱动动画回归。

### 本轮已纠正的文档冲突

- 将 “live 仍为 `0.1.170` / `0.1.171` 尚未导入” 更新为当前 `0.1.171` 与 staging ref `20469a4`。
- 将 “Mailpit 注册”等同真实邮件送达的表述改为“Mailpit 捕获 challenge”。
- 将 “LLM 只做解释”修正为“AI 受 Knowledge Pack 约束生成 proposal，确定性校验终审”。
- Skill 从内部状态 `needs_configuration/not_deployable` 改为公开 verdict `needs_input/unsupported/diagnostic_failed`。
- API 文档将当前路由与目标态分开：session 管理、analysis rerun、Operation list、billing portal/webhook 和 admin write/abuse API 不再被误写成已实现；同时补入 auth config/preview、templates 和 deployment configuration 的真实路由。
- 部署成功口径补充 exact `JobModifyIndex` → evaluation → deployment → `JobVersion` → 新健康 allocation 的 rollout barrier；历史 healthy allocation 不得误报成功。
