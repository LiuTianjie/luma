# 04. 数据、API、CLI 与 Skill 协议

## 1. 协议原则

- Web、CLI、用户 Agent 共用 `/v1` API，不为界面开私有业务旁路。
- OpenAPI 是请求/响应权威；`DeploymentPlan`、Operation Event 另有 JSON Schema。
- 所有 mutation 接受 `Idempotency-Key`；异步工作返回 `202 + operation`。
- 事件先写 PostgreSQL，再通过 SSE/NDJSON 推送；客户端可按 cursor 重放。
- 所有资源 ID 是 opaque UUIDv7/ULID，不从 email、app name 或 tenant 推导。
- 所有 tenant 资源查询必须在 repository/service 层强制 `tenant_id`，不能只靠前端过滤。
- 用户 API 不返回 Luma node、Tailscale IP、storage endpoint、management token 或原始内部错误。

API 基址建议：`https://lae-api.itool.tech/v1`。

## 2. 核心数据模型

统一使用 PostgreSQL、`timestamptz`、软删除字段和显式 schema migration。以下只列核心字段，所有业务表另有 `created_at/updated_at`。

### 2.1 身份与访问

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `users` | `id,email,status,email_verified_at,locale,last_login_at` | `UNIQUE(lower(email))` |
| `tenants` | `id,type,name,slug,status,owner_user_id` | `type=personal|organization`, `UNIQUE(slug)` |
| `tenant_members` | `tenant_id,user_id,role` | PK `(tenant_id,user_id)` |
| `email_challenges` | `id,email,purpose,token_hash,attempts,expires_at,used_at` | token hash 唯一、一次性 |
| `auth_sessions` | `id,user_id,session_hash,expires_at,revoked_at,last_seen_at,ip,user_agent` | session hash 唯一 |
| `deploy_tokens` | `id,tenant_id,user_id,name,prefix,token_hash,scopes,purpose,expires_at,revoked_at,last_used_at,last_used_ip` | prefix/hash 唯一；每用户一个 active default |

Deploy token 格式示例：`lae_dt_<public-prefix>_<256-bit-secret>`。数据库只保存 keyed HMAC/hash，明文只显示一次。默认 scope：

- `apps:read`
- `apps:write`
- `sources:write`
- `analyses:write`
- `deployments:write`
- `logs:read`

默认不包含 `billing:write`、`tokens:write`、`admin:*`。
可为专用 Agent token 显式授予 `billing:checkout`，它只能创建 checkout
session，不能确认支付、变更套餐或取得任何管理权限，付款仍需用户确认。

### 2.2 套餐、配额与支付

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `plan_versions` | `id,code,version,limits_json,features_json,effective_at` | `UNIQUE(code,version)`, code 为 lite/pro/ultra |
| `subscriptions` | `id,tenant_id,plan_version_id,interval,status,provider,current_period_start/end,cancel_at_period_end` | tenant 仅一个 active/trialing |
| `payment_orders` | `id,order_no,tenant_id,plan_version_id,interval,amount,currency,provider,status,provider_trade_no,expires_at` | order_no 唯一；trade_no 非空唯一 |
| `payment_events` | `id,provider,provider_event_id,payload_hash,processed_at` | `(provider,provider_event_id)` 唯一 |
| `quota_counters` | `tenant_id,period,metric,used,reserved,version` | PK `(tenant_id,period,metric)` |
| `quota_reservations` | `id,tenant_id,operation_id,metric,amount,status,expires_at` | `(operation_id,metric)` 唯一 |
| `usage_ledger` | `id,tenant_id,metric,quantity,source_type,source_id,period` | `(metric,source_type,source_id)` 唯一 |

Compose 加入后，plan limits 至少包含：

- applications
- services_per_app / total_services
- public_http_routes
- persistent_volumes / volume_bytes
- artifact_storage_bytes / upload_bytes
- monthly_analysis_seconds / concurrent_analyses / analyses_per_hour
- monthly_build_seconds / concurrent_builds
- total_cpu / total_memory / per_service_cpu / per_service_memory
- deploys_per_hour / concurrent_deployments
- env_vars_per_app
- log_retention_days / artifact_retention_days
- private_git / scheduled_update_checks / backups

限额为数据，不硬编码在 Web/Agent/CLI。

### 2.3 Source、凭据与 Artifact

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `source_connections` | `id,tenant_id,provider,base_url,external_account,credential_ciphertext,key_version,status,last_verified_at` | tenant/provider/account 唯一 |
| `source_credential_leases` | `id,tenant_id,connection_id,operation_id,allowed_action,allowed_host,status,expires_at,consumed_at` | 单次消费；不保存下发明文 |
| `uploads` | `id,tenant_id,object_key,filename,expected_bytes,actual_bytes,sha256,mime,status,expires_at` | object key 唯一 |
| `source_revisions` | `id,tenant_id,kind,connection_id,repository,ref,resolved_commit_full,source_tree_digest,upload_id,template_version_id,subdirectory,snapshot_id,snapshot_digest,snapshot_artifact_id` | snapshot digest + source identity 索引；Git snapshot 由 builder 生成 |
| `artifacts` | `id,tenant_id,kind,object_key,oci_ref,sha256,size_bytes,status,retention_until` | object key 唯一；OCI digest 唯一 |

凭据 ciphertext 使用 envelope encryption，key version 可轮换。Git PAT/SSH key 不进入 source revision、analysis、Luma metadata 或 audit diff。

### 2.4 应用、服务、路由和卷

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `applications` | `id,tenant_id,name,slug,luma_name,kind,desired_state,observed_state,environment_version,current_revision_id,current_deployment_id,deleted_at` | `(tenant_id,slug)` 唯一；`luma_name` 全局唯一 |
| `application_sources` | `id,application_id,kind,connection_id,repository,ref,subdirectory,auto_update,is_active` | 每 app 一个 active source |
| `application_services` | `id,application_id,service_key,role,desired_state,observed_state,current_image_digest` | `(application_id,service_key)` 唯一 |
| `application_routes` | `id,application_id,service_id,kind,hostname,is_primary,status,container_port` | hostname 唯一；每 app 至多一个 primary |
| `application_volumes` | `id,application_id,volume_key,requested_bytes,storage_policy,backup_policy,delete_policy,status` | `(application_id,volume_key)` 唯一 |
| `app_environment` | `application_id,service_scope,name,value_ciphertext,value_checksum,key_version,is_sensitive,required,source` | `(application_id,service_scope,name)` 唯一；全局 CAS 版本在 application 行 |

`kind=pending|service|compose`。首部署先创建计入 app 配额但没有伪造 service 的
`pending` shell，analysis 绑定该 app；只有已验证的分析产物才能原子 materialize
为 `service|compose` 并写入 topology。已 materialize 的 topology 不可原地重写，
更新必须创建 revision。单服务也映射为一条 `application_services`，这样应用详情、
日志和资源模型不需要两套分支。

`application_routes.kind` V1 只能是 `http`。出现 TCP/UDP/tcp-relay 时在 analysis 和 deploy 两层拒绝。

### 2.5 Analysis、Revision 与 Deployment

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `analyses` | `id,tenant_id,application_id,operation_id,source_revision_id,builder_task_id,agent_version,adapter_version,policy_version,status,plan_json,plan_sha256,build_plan_json,build_plan_sha256,warnings,blockers,expires_at` | operation 唯一；plan 绑定 source digest |
| `app_revisions` | `id,application_id,revision_no,analysis_id,source_revision_id,kind,plan_json,normalized_compose,compose_sha256,luma_manifest,luma_manifest_sha256,environment_schema,created_by` | `(application_id,revision_no)` 唯一 |
| `revision_services` | `revision_id,service_key,image_digest,build_artifact_id,runtime_spec,resource_spec,health_spec` | `(revision_id,service_key)` 唯一 |
| `revision_routes` | `revision_id,service_key,hostname,container_port,is_primary` | hostname 唯一 |
| `revision_volumes` | `revision_id,volume_key,storage_class_ref,path_ref,requested_bytes,access_mode,backup_policy` | `(revision_id,volume_key)` 唯一 |
| `deployments` | `id,application_id,revision_id,operation_id,status,luma_cluster_id,luma_external_ref,previous_deployment_id,started_at,finished_at,error_code,error_message` | operation 唯一 |

`app_revisions` 保存平台生成的最终事实。Compose revision 同时保存规范化 Compose 与 Luma sidecar；用户仓库无需包含 Luma 文件。

### 2.6 Operation、Event、幂等和审计

| 表 | 核心字段 | 约束 |
| --- | --- | --- |
| `operations` | `id,tenant_id,principal_type,principal_id,kind,target_type,target_id,status,phase,result,error_code,error_message,parent_operation_id,cancel_requested_at,lease_owner,lease_expires_at` | tenant/status/time 索引 |
| `operation_events` | `operation_id,seq,event_id,type,phase,status,level,message,data,created_at` | PK `(operation_id,seq)`；event_id 唯一 |
| `idempotency_records` | `principal_id,key,method,route_template,request_hash,response_status,response_body,operation_id,expires_at` | principal/key/method/route 唯一 |
| `audit_logs` | `id,tenant_id,actor_type,actor_id,action,target_type,target_id,request_id,operation_id,ip,user_agent,result,error_code,changes_redacted` | append-only |
| `outbox_events` | `id,aggregate_type,aggregate_id,event_type,dedupe_key,payload,status,available_at,attempts` | dedupe key 唯一 |
| `builder_tasks` | `id,operation_id,luma_task_id,action,source_revision_id,source_digest,plan_digest,status,attempt,started_at,finished_at,result_artifact_id` | Luma task ID 唯一；不保存凭据明文 |
| `luma_clusters` | `id,name,control_url,service_token_ciphertext,status,capabilities,last_seen_at` | control URL 唯一 |
| `luma_bindings` | `application_id,luma_cluster_id,luma_name,external_ref,last_manifest_sha256,last_observed_status` | app 唯一；cluster/name 唯一 |

同一 app 的 deploy/start/suspend/restart/rollback/delete 使用 PostgreSQL advisory lock，并用 partial unique index 限制同时一个非终态 mutation operation。Compose 的 child service build 可以并行。

## 3. 认证 API

### 3.1 邮件注册/登录

- `POST /auth/register`
- `POST /auth/email/verify`
- `POST /auth/email/resend`
- `POST /auth/login/request`
- `POST /auth/login/verify`
- `POST /auth/logout`
- `GET /me`
- `GET /sessions`
- `DELETE /sessions/{id}`

邮箱验证成功后原子创建 personal tenant、Lite entitlement 和默认 deploy token。默认 token 明文只在这次响应/页面显示一次。

### 3.2 Deploy token

- `GET /deploy-tokens`
- `POST /deploy-tokens`
- `POST /deploy-tokens/{id}/rotate`
- `DELETE /deploy-tokens/{id}`
- `POST /auth/token/verify`

## 4. Source 与 Upload API

- `GET /source-connections`
- `POST /source-connections`
- `POST /source-connections/{id}/rotate`
- `DELETE /source-connections/{id}`
- `POST /uploads`
- `POST /uploads/{id}/complete`
- `GET /uploads/{id}`
- `DELETE /uploads/{id}`

大文件使用 presigned/multipart upload。`POST /uploads` 先按 expected bytes 预留额度，complete 时校验服务端 object size 与 sha256。

Generic Git 凭据只通过 stdin、系统 keychain 或安全 Web form 提交，禁止放在 CLI 参数和 clone URL。

当前已实现 connection create/list/rotate/revoke：所有 mutation 需要
`Idempotency-Key`，cookie session 另需 CSRF，带 `sources:write` 的 deploy token
可供 CLI/Agent 使用。API 只返回 provider、display name、canonical base URL、
exact `allowedHost`、username、credential version 与时间戳；secret/PAT 只在
create/rotate 请求内出现一次。`verify`、repository/ref discovery 和 GitHub App
OAuth 安装流程仍是后续能力。

## 5. Agent / Analysis API

`POST /analyses` 是公开稳定 Agent 端点：

```json
{
  "applicationId": "app_01...",
  "source": {
    "type": "git",
    "repository": "https://github.com/acme/app.git",
    "ref": "main",
    "subdirectory": "",
    "connectionId": "conn_01..."
  },
  "intent": {
    "region": "cn",
    "publicProtocols": ["http"]
  }
}
```

当前公共创建切片要求 `applicationId` 指向调用方 tenant 内一个尚未删除的
application；缺失时返回 `422 LAE_APPLICATION_REQUIRED`，不存在与跨 tenant ID
统一返回 `404 LAE_NOT_FOUND`。这是为了不在分析请求里偷偷创建绕过套餐、配额和
状态机的 draft app。首个部署的产品编排必须先通过 `POST /applications` 创建 app，
Web/CLI 可以把两步包装成一个交互。`source.connectionId` 已开放：服务端验证连接
属于同一 tenant、未撤销，且 repository 的 canonical HTTPS `host[:port]` 与连接
allowlist 精确相等；原子入队只把短期 lease ID 放入 Builder Task，不放明文。
repository 仍只接受不含 userinfo、query 或 fragment 的 HTTPS Git URL，并拒绝
IP literal、localhost、单标签以及
`.local`/`.internal`/`.lan`/`.home.arpa` 等私有 DNS 后缀；私有 Git 不会降级为把
PAT 放进 URL。连接 secret 会以独立 keyring 的 AES-256-GCM + AAD/HMAC envelope
保存；轮换或撤销会撤销未领取 lease。内部 broker 的 PostgreSQL 单次领取已实现，
LAE 的 `/v1/internal/credential-leases/redeem` 与 Luma Control 的闭合 HTTPS
service-token redemption、完整 binding 回验、单次消费、取消竞态和日志收口均已
实现；真实 Control principal/token file、内部 TLS 路径与 staging E2E 未完成时仍
fail closed。HTTP redirect、解析后每个 A/AAAA 地址和 DNS rebinding 仍必须由 Luma
builder 的 network-level egress policy 逐跳复核，API 字符串校验不被视为生产
SSRF 防线。

Compose 与 Dockerfile 是所有用户都可提交的标准来源，不需要 `allowCompose` feature flag。服务端仍按套餐限制 app/service/route/build/storage 数量；V1 的公开协议只有 HTTP。

返回 `202`：

```json
{
  "analysis": {"id": "ana_01...", "status": "queued"},
  "operation": {"id": "op_01...", "status": "queued"},
  "links": {
    "analysis": "/v1/analyses/ana_01...",
    "events": "/v1/operations/op_01.../events"
  }
}
```

其他端点：

- `GET /analyses/{id}`
- `POST /analyses/{id}/rerun`
- `POST /applications/{id}/update-checks`

Analysis status：`queued | analyzing | deployable | needs_configuration | not_deployable | diagnostic_failed | failed | expired`。公开响应另提供稳定的用户态 verdict：`deployable | needs_input | unsupported | diagnostic_failed`；`unsupported` 必须带结构化 blocker，`diagnostic_failed` 表示诊断基础设施失败而不是用户代码不可部署。

API 在一个 PostgreSQL transaction 内创建 source revision、queued analysis、
operation、builder checkpoint、一次性 source lease、首条 event/outbox 和幂等记录，
再由 Worker/Luma adapter 创建 Luma `analyze-source` task。Git fetch、resolved
commit、snapshot 和 `lae-agent-runner` 均发生在 builder；Compose analysis 响应
必须包含：services、dependency graph、builds、HTTP routes、internal services、
volumes、environment、resource estimate、unsupported fields、source/plan digest
和 blocker evidence。公开响应不返回 Luma principal/task、credential lease、内部
image ref 或原始 repository metadata。

## 6. Application API

- `GET /applications`（`apps:read`）
- `POST /applications`（`apps:write`，创建计入套餐配额的 `pending` draft）
- `GET /applications/{id}`（`apps:read`）
- `PATCH /applications/{id}`
- `DELETE /applications/{id}`
- `GET /applications/{id}/services`（`apps:read`）
- `GET /applications/{id}/routes`（`apps:read`）
- `GET /applications/{id}/volumes`（`apps:read`）
- `GET /applications/{id}/environment`（`apps:read`）
- `PATCH /applications/{id}/environment`（`apps:write`）

当前已实现的 `POST /v1/applications` body **只** 接受 `name`、`slug`，要求
`Idempotency-Key`。同 principal、method、route、key 与同一 canonical body 重放
历史 `201`；key 相同而 body 不同返回 `409 LAE_IDEMPOTENCY_KEY_REUSED`。创建 app、
套餐配额锁、同步 completed operation 与幂等记录在同一个 PostgreSQL transaction；
失败不会留下 app 或幂等成功记录。Cookie session mutation 要求 double-submit CSRF，
Bearer mutation 只按 `apps:write` scope；双凭据时 session authority 胜出，不能用同用户
Bearer 绕过 CSRF。

列表、详情与四个子资源都以 tenant predicate 查询，跨 tenant 与不存在统一为 404。
公开投影不返回 `tenant_id`、`luma_name`、service/route/volume 内部 ID、env value、
ciphertext、checksum 或 crypto key version。`materialize_topology` 没有公共路由；只有
可信 controller 可以从 verified stored plan 将 pending shell 原子物化。

环境变量原子更新：

```json
{
  "expectedVersion": 7,
  "set": {
    "web:DATABASE_URL": {"value": "...", "sensitive": true},
    "*:NODE_ENV": {"value": "production", "sensitive": false}
  },
  "unset": ["worker:OLD_KEY"]
}
```

响应只返回 key metadata、configured 标记和新 version，不返回 value。

`PATCH /v1/applications/{id}/environment` 同样要求 `Idempotency-Key`，并以
`expectedVersion` 做 application-row CAS；旧版本返回
`409 LAE_ENVIRONMENT_VERSION_CONFLICT`。单值 UTF-8 上限 64 KiB、单次 patch canonical
body 上限 512 KiB、set/unset 各至多 128 项；scope 只能是 `*` 或合法 service key，
name 必须符合 shell 环境变量名。敏感与非敏感 value 一律用随机 nonce 的
AES-256-GCM 加密，AAD 绑定 tenant/application/service/name/key version，另存域分离
keyed HMAC-SHA256 checksum；幂等表只保存 keyed request hash 和无 value 的历史响应。

API runtime 必须同时配置以下 secret，任何一项缺失或 key 长度错误都会让 application
catalog readiness fail closed：

- `LAE_ENVIRONMENT_AEAD_KEY_VERSION`：当前正整数 key version。
- `LAE_ENVIRONMENT_AEAD_KEYS`：JSON object，version -> base64 32-byte AES key；轮换期保留旧 key 供读。
- `LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY`：base64，至少 32 bytes。
- `LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY`：base64，至少 32 bytes，与 auth/worker key 分离。

0004 向 0003 downgrade 是显式有损边界：0003 无法表示 `pending`，因此 migration 删除
尚未物化的 draft 及其 analysis/checkpoint/lease/source facts、删除相关幂等响应并保留
operation 审计；不会把 draft 伪造成 `service`，已物化应用不受影响。

## 7. Deployment 与生命周期 API

- `POST /applications/{id}/deployments`
- `GET /applications/{id}/deployments`
- `GET /applications/{id}/deployments/{deploymentId}`
- `POST /applications/{id}/actions/resume`
- `POST /applications/{id}/actions/suspend`
- `POST /applications/{id}/actions/restart`
- `POST /applications/{id}/actions/rollback`
- `POST /applications/{id}/actions/delete`
- `POST /applications/{id}/actions/check-update`
- `GET /applications/{id}/logs`
- `GET /applications/{id}/metrics`

当前观测接口是 bounded JSON snapshot；`GET .../logs/stream` 属于后续目标，不在当前公开路由中。生命周期动作进入独立 durable Worker lane；普通 delete 固定保留 volume，rollback 只接受同 tenant/application、已成功且与 V1 catalog 拓扑兼容的历史 deployment。

部署请求严格只包含 `analysisId` 与 `environmentVersion`。Primary HTTP route、service、
image、volume policy 与最终 manifest 只能来自已经入库并复验的可信 DeploymentPlan；
客户端提交这些字段会因 `extra=forbid` 被拒绝，不能借“确认”改写分析结果。以下情况
返回 409/422，不创建 deployment operation：

- analysis 非 deployable 或已过期。
- source digest 已变化。
- 必填环境变量未配置或 environment version 过期。
- Compose topology/sidecar hash 与 analysis 不一致。
- 检测到 `tcp-relay`/TCP/UDP/host port/禁止字段。
- 配额无法预留。

### 7.1 App 状态

分开保存：

- `desired_state = running | suspended | deleted`
- `observed_state = provisioning | running | degraded | failed | suspending | suspended | unknown`

每个 service 也有 desired/observed。App `running` 需要 required services 健康且 required HTTP routes 验证通过。

## 8. Operation API 与事件流

- `GET /v1/operations`
- `GET /v1/operations/{id}`
- `GET /v1/operations/{id}/events?after=42&limit=100`
- `POST /v1/operations/{id}/cancel`

当前已落地的 JSON polling 基线还包括
`GET /v1/analyses/{id}`。Analysis 读模型只返回公开 status、五类 digest、
`planStored` 和 operation/events links；不返回 artifact 下载地址、source
metadata、credential lease、Luma task、agent image 或内部 URL。由于 v1 token
scope 尚未定义 `analyses:read`，这一读端点暂时要求 `analyses:write`；引入只读
scope 时必须设计旧 token 的迁移/兼容策略，这是已记录的 scope debt。

Operation read/events/cancel 根据持久化 kind 动态使用最小 scope：
`source.analyze -> analyses:write`、`deployment.* -> deployments:write`、
`application.* -> apps:write`。系统先用认证 principal 的 tenant 查询 operation，
再做 kind scope 判断，因此跨 tenant 与不存在/非法 ID 都统一为
`404 LAE_NOT_FOUND`；未显式映射的新 kind 默认不可见。Bearer cancel 不使用
CSRF，cookie session cancel 必须通过 double-submit CSRF；两个 credential 同时
存在时仍以 cookie authority 为准，不能用 Bearer 绕过 CSRF。

JSON polling 将数据库 `operation_events.seq` 原样映射为公开 `cursor`，只接受
`0 <= after <= 2^63-1` 和 `1 <= limit <= 500`，并按 cursor 严格递增重放。Envelope 同时
返回 `cursor/status/hasMore/terminal`；即使 operation 已终态，只要当前页尚未
追到 `last_event_seq`，`terminal` 仍为 false，CLI 必须继续拉取。事件仅输出
`eventId/operationId/cursor/type/phase/status/level/message/data/createdAt`：type、
phase、message 使用平台白名单/固定文案，data 按 event type 拷贝显式公开且经过
类型检查的 key，未知类型降级成无 data 的 `operation.progress`。内部 URL、
credential/lease、Luma 标识、image ref 与原始 stdout/stderr 即使误入持久层也
不会从此读模型返回。

Cancel 由 PostgreSQL row lock 下的状态机证明幂等：queued 只生成一次 canceled，
running 只生成一次 cancel-requested，终态重复请求保持原状态，因此此端点不
要求 `Idempotency-Key`，并返回 `Idempotency-Policy: state-transition`。Worker
终态提交仍会重新检查 `cancel_requested_at`，所以 cancel 与上游成功竞争时取消
优先。

当前 retention gate：尚未启用 operation event GC，也尚未定义 cursor-expired
响应和每租户保留水位。在实现 retention watermark、稳定的 cursor-expired
错误/恢复策略以及 CLI 回退到 terminal snapshot 之前，不得删除可续看的事件。

目标态的流式事件端点支持：

- `Accept: text/event-stream`
- `Accept: application/x-ndjson`
- `Last-Event-ID` 或 `after=<seq>`
- 15 秒 heartbeat
- 先回放 DB，再切实时通知

当前实现仅提供上述数据库 JSON polling；SSE/NDJSON socket、heartbeat 和
`Last-Event-ID` 是后续 transport，不得绕过同一 tenant-fenced read model。

示例：

```json
{"eventId":"evt_01","operationId":"op_01","cursor":1,"type":"operation.started","phase":"source.fetch","status":"running","level":"info","message":"Operation started","data":{},"createdAt":"2026-07-11T10:00:00Z"}
{"eventId":"evt_02","operationId":"op_01","cursor":2,"type":"compose.detected","phase":"analysis.topology","status":"running","level":"info","message":"Application topology detected","data":{"services":3,"routes":1,"volumes":1},"createdAt":"2026-07-11T10:00:02Z"}
{"eventId":"evt_03","operationId":"op_01","cursor":3,"type":"build.service.completed","phase":"build","status":"running","level":"info","message":"Service image build completed","data":{"service":"web"},"createdAt":"2026-07-11T10:00:18Z"}
{"eventId":"evt_04","operationId":"op_01","cursor":4,"type":"deployment.ready","phase":"verify","status":"succeeded","level":"info","message":"Deployment verification succeeded","data":{},"createdAt":"2026-07-11T10:00:31Z"}
```

事件和日志禁止包含 env 值、deploy token、Git/registry 凭据、presigned URL、Luma token。

## 9. 幂等与配额

- 幂等作用域：`principal + method + normalized route + key`。
- 同 key 同 canonical request：返回同一个 operation/历史响应。
- 同 key 不同 request：`409 LAE_IDEMPOTENCY_KEY_REUSED`。
- 普通幂等记录至少 24 小时，支付至少 30 天。
- secret request 只保存 keyed request hash，不保存原 body。
- Compose request hash 包含 source digest、plan digest、environment version、volume confirmation version。
- Worker 执行前再次核验 subscription/quota/source/env，不只在 API 接收时检查。
- quota reservation 使用 TTL/heartbeat；失败、取消和 worker crash 由 reconciler 释放。
- downgrade 不自动删 app；标记 over-quota，允许查看/suspend/delete/export/pay，禁止新建和新部署。
- volume/artifact/registry GC 实际完成后才写负 usage ledger。

## 10. 计费 API

- `GET /plans`
- `GET /usage`
- `GET /billing/subscription`
- `POST /billing/checkout-sessions`
- `GET /billing/orders/{id}`
- `POST /billing/portal-sessions`
- `POST /billing/webhooks/{provider}`
- `POST /billing/mock/orders/{id}/complete`（仅 dev/staging）

模板、Web、CLI 和 Skill 调用同一个 checkout session API。CLI/Skill 只能生成付款 URL；套餐变更必须由人类打开并确认。

## 11. Admin API

仅 Luma Dashboard 内部 service credential 可用：

- `GET /internal/v1/admin/users`
- `GET /internal/v1/admin/tenants`
- `GET /internal/v1/admin/applications`
- `GET /internal/v1/admin/operations`
- `GET /internal/v1/admin/usage`
- `GET /internal/v1/admin/abuse-cases`
- `POST /internal/v1/admin/applications/{id}/suspend`

超管 action 仍由 LAE 创建 operation/audit 再调用 Luma，不能绕过 LAE 直接修改 Nomad。

## 12. 公开错误协议

```json
{
  "error": {
    "code": "LAE_COMPOSE_POLICY_DENIED",
    "message": "compose.yaml 包含暂不支持的 TCP 公网入口",
    "requestId": "req_01...",
    "retryable": false,
    "details": {
      "service": "mysql",
      "field": "ports",
      "reason": "tcp-relay is not supported"
    }
  }
}
```

| HTTP | 错误码示例 |
| --- | --- |
| 400 | `LAE_INVALID_ARGUMENT`, `LAE_UNSUPPORTED_SOURCE`, `LAE_INVALID_ARTIFACT` |
| 401 | `LAE_UNAUTHENTICATED`, `LAE_INVALID_TOKEN`, `LAE_EMAIL_NOT_VERIFIED` |
| 403 | `LAE_PERMISSION_DENIED`, `LAE_PLAN_REQUIRED`, `LAE_FEATURE_NOT_AVAILABLE` |
| 404 | `LAE_NOT_FOUND` |
| 409 | `LAE_OPERATION_CONFLICT`, `LAE_IDEMPOTENCY_KEY_REUSED`, `LAE_VERSION_CONFLICT`, `LAE_SOURCE_CHANGED` |
| 413 | `LAE_UPLOAD_TOO_LARGE`, `LAE_STORAGE_QUOTA_EXCEEDED` |
| 422 | `LAE_NOT_DEPLOYABLE`, `LAE_ENVIRONMENT_REQUIRED`, `LAE_COMPOSE_POLICY_DENIED`, `LAE_LUMA_VALIDATION_FAILED` |
| 429 | `LAE_RATE_LIMITED`, `LAE_BUILD_CONCURRENCY_EXCEEDED`, `LAE_QUOTA_EXCEEDED` |
| 502 | `LAE_LUMA_DEPLOY_FAILED`, `LAE_BUILD_FAILED`, `LAE_SOURCE_UPSTREAM_FAILED` |
| 503 | `LAE_CAPACITY_UNAVAILABLE`, `LAE_LUMA_UNAVAILABLE` |
| 504 | `LAE_OPERATION_TIMEOUT`, `LAE_HEALTH_CHECK_TIMEOUT` |

## 13. LAE 到 Luma 内部协议

```text
Authorization: Bearer <dedicated-luma-service-token>
Idempotency-Key: <lae-operation-id>:<step>
X-Request-Id: req_...
X-LAE-Operation-Id: op_...
X-LAE-Tenant-Id: ten_...
X-LAE-Application-Id: app_...
X-LAE-Revision-Id: rev_...
X-Manifest-SHA256: ...
```

legacy 兼容能力：

- Git 构建：当前 `build-image` 已支持 builder clone/buildx/push，并在仓库自带 sidecar 时支持 Compose 多 build；只作为迁移基础。
- 单服务：preview -> deploy stream。
- Compose：compose preview -> storage check/apply -> compose deploy stream。
- 状态/日志：dashboard/status/logs 过滤。
- 生命周期：legacy restart/history/rollback/remove；LAE dedicated Runtime API 与 Worker 已实现 suspend/resume/restart/rollback/delete 的受限协议、durable checkpoint、失败恢复、拓扑兼容校验和 delete retain-volume，真实 PostgreSQL/Luma staging 验证仍按实施状态门禁。

Builder v2 已实现以下可恢复 node task API：

- `POST /v1/builder/tasks`：创建 typed `analyze-source` 或 `build-plan` task。
- `GET /v1/builder/tasks/{id}`：读取持久终态与 result refs。
- `GET /v1/builder/tasks/{id}/events?after=<cursor>`：断线重放结构化事件。
- `POST /v1/builder/tasks/{id}/cancel`：幂等请求取消并清理 lease/workspace。

这些是只对 scoped LAE service principal 开放的 Luma 内部 API，不是租户端点。每个 task 带 `schemaVersion`、`externalOperationId`、limits 和 policy version；`Idempotency-Key` 仅使用 HTTP header，并按 `principal + route + tenant + application` 定域。Luma 不提供任意 shell/command action，Agent runner image 必须与服务端 allowlist 中的固定 digest 完全一致。

```text
analyze-source(sourceRef, credentialLeaseId, agentImageDigest, policyVersion, limits)
  -> resolvedCommit + sourceSnapshotDigest + DeploymentPlan/BuildPlan/evidence digests

build-plan(sourceSnapshotId, sourceSnapshotDigest, signedBuildPlan, credentialLeaseId, trustedTenantAppRef, limits)
  -> per-service image/SBOM/provenance digests
```

`lae-agent`/Worker 只签发一次性 credential lease 并编排任务。Luma builder 在 task lease 时换取 tenant source credential、拉取/校验源码、分析或构建并推送 image；不得把用户 PAT 写入当前全局 Luma Git provider state。Luma 把 task 绑定到鉴权得到的 service principal，并按该 principal 的 tenant/application scope 校验 payload；读取、事件和取消操作也必须由原 principal 执行。`build-plan` 必须接受 LAE 显式生成的多服务 build specs，不能要求用户仓库包含 Luma 文件。

上述协议、scoped principal、rootless executor、credential/object lease、snapshot binding、显式多 service BuildPlan、幂等/cursor/cancel、Runtime API、生命周期 Worker 与稳定错误已具备代码和自动化测试，生命周期 PostgreSQL 17 migration-backed 集成也已通过。公网多租户剩余门禁集中在真实 principal/registry/storage 配置、专用 builder/runner 隔离与 egress、tenant registry/cache/GC、完整配额/审计/reconciliation、真实 Luma lifecycle E2E、备份恢复及 staging/chaos；以 [实施状态](./08-implementation-status.md) 为准。

## 14. LAE CLI

### 14.1 命令面

```text
lae doctor
lae login [--token-stdin]
lae whoami

lae apps create --name <name> --slug <slug> --idempotency-key <key>
lae inspect --app <id> --repo <https-url> --ref <ref> --idempotency-key <key>
lae inspect-file --app <id> --file <artifact.html|artifact.zip> --idempotency-prefix <prefix>
lae deploy --app <id> --analysis <id> --environment-version <version> --idempotency-key <key>
lae operation show|watch|cancel <operation-id>

lae apps list|show|logs|metrics <app>
lae apps check-update|suspend|resume|restart <app> --idempotency-key <key>
lae apps rollback <app> [--deployment <id>] --idempotency-key <key>
lae apps delete <app> --yes --idempotency-key <key>
lae env list|set|unset <app>
lae source-connections list|create|rotate|revoke
lae uploads create|show|complete|delete
lae templates list|launch
lae plans list
lae billing checkout --plan pro --interval month
```

本地 path 部署遵守文件上传约束，只接受 HTML/静态产物。Compose 通过 Git/模板进入，除非后续明确扩大 source upload 策略。

邮箱注册/验证码交换和 deploy-token 创建/轮换当前通过 Web/API 完成，尚无 `lae register` 或 `lae tokens` 子命令；Agent 不得自行用未发布命令或 raw Luma API 替代。

### 14.2 AI 友好约束

- 所有 list/show/inspect/deploy 支持 JSON；长任务支持 NDJSON。
- JSON stdout 只输出协议，进度/诊断写 stderr 或 NDJSON event。
- `--non-interactive` 下缺少输入立即返回稳定错误，不打开浏览器或 prompt。
- secret 通过 stdin、env 或 OS keychain；不接受明文命令行参数。
- `--idempotency-key` 可显式传入；默认由 CLI 为同一次命令持久化。
- watch 输出包含 operation ID 和 cursor，断线后自动 resume。

建议退出码：

| code | 含义 |
| ---: | --- |
| 0 | 成功 |
| 2 | 参数/本地输入错误 |
| 3 | 未认证/无权限 |
| 4 | 需要用户补充配置 |
| 5 | 不支持/策略拒绝 |
| 6 | 配额/套餐限制 |
| 7 | source/build 失败 |
| 8 | deploy/runtime 验证失败 |
| 9 | 平台暂时不可用/可重试 |

## 15. LAE Agent Skill

Skill 至少包含：

1. 检测 CLI 是否安装与当前登录态。
2. `register/login` 的人机边界和 token 安全说明。
3. `inspect`，读取结构化 blocker/required env/Compose topology。
4. 安全收集 env：让用户在终端/Web 中输入，不要求在对话中粘贴 secret。
5. `deploy --format ndjson` 与 operation resume。
6. 应用状态、逐服务日志、check update、suspend/resume/restart/rollback。
7. quota/plan 解释。
8. 支付只生成 checkout URL，并明确等待用户确认。

Skill 的默认行为：

- 先 inspect 再 deploy。
- 不擅自修改套餐、不自动付款、不上传 `.env`。
- 不绕过 `tcp-relay`/Compose policy blocker。
- 不把 deploy token、Git token、环境变量值写入仓库、日志或 prompt。
- 用户要求部署时消费 LAE API；不生成/调用底层 Luma management token。
