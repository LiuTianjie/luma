# 05. 安全、套餐、支付与运维

## 1. 安全模型

LAE 是公开接受用户源码与容器工作负载的平台，默认把以下对象视为不可信：

- Browser/CLI/API 输入。
- 上传的 HTML/ZIP、Git 仓库、Dockerfile、Compose。
- 构建脚本、依赖、镜像和运行中容器。
- Webhook、支付回调和邮件链接。
- 用户提供的域名标签、环境变量名和日志内容。

安全边界不能只靠“用户看不到按钮”；所有限制必须在 API、Agent Policy、Luma Policy 和 runtime 四层至少两次校验。

## 2. 身份与会话

### 2.1 邮件认证

- 验证码/magic link 短时、单次有效，数据库只存 hash。
- 注册、登录、重发接口统一响应，避免 email enumeration。
- 按 email/IP/device/ASN 限流；异常流量触发验证码或人工审核。
- 邮件链接绑定 purpose、tenant context 和 nonce，不能跨用途复用。
- 邮箱变更、token 管理、支付与删除账户需要 recent authentication。

如后续增加密码：Argon2id、逐用户 salt、参数可升级、breached-password 检查、找回后撤销全部 session。

### 2.2 Web Session

- `Secure + HttpOnly + SameSite=Lax/Strict` cookie。
- session ID 高熵，服务端只存 hash。
- state-changing 请求做 CSRF 防护；CORS 只允许 LAE Web origins。
- session 可按设备查看、撤销和全量登出。
- 登录和敏感操作写 audit。

### 2.3 Deploy Token

- 256-bit 随机 secret，prefix 只用于查找和 UI 区分。
- keyed HMAC/hash 存储；只显示一次。
- scopes、expiry、purpose、last used、IP 和 revoke。
- 注册后自动 token 只在邮箱验证完成时创建。
- 默认 token 不允许 billing、token 管理和 admin。
- 服务端 token 比较 constant-time；日志只保留 prefix。

## 3. 多租户隔离

### 3.1 数据层

- 每个业务表包含 `tenant_id` 或可通过不可变外键追溯 tenant。
- repository/service query 必须显式 tenant scope；后台 admin query 使用不同接口与凭据。
- PostgreSQL 可追加 Row Level Security 作为 defense-in-depth，但不能替代应用授权测试。
- 跨 tenant copy/transfer 需要显式工作流，不能直接改 foreign key。

### 3.2 资源层

- Luma job：`lae-<tenant-short>-<app-short>`。
- OCI：`lae/<tenant>/<app>@digest`。
- Object：`tenants/<tenant>/apps/<app>/...`。
- Volume：tenant/app/volume 唯一路径，用户看不到 storage endpoint。
- Logs/metrics/traces：tenant/app/deployment/service labels，在查询网关强制过滤。
- Domain：CSPRNG 随机，不包含 email/user/app name。

### 3.3 Luma 边界

- 普通用户 token 永不发送到 Luma。
- LAE worker 的 Luma credential 作为 Luma Secret 注入，只在内存中使用。
- 生产使用 scoped service credential，只能操作 `lae-*` 和指定 action。
- Luma Dashboard 的 LAE 超管动作必须回到 LAE API，保持 quota/audit/reconciliation。

## 4. Secret 与密钥管理

- tenant secret、Git credential、payment key 使用 envelope encryption。
- data encryption key 按 tenant 或 secret record 生成；master key 与 DB 分离，作为受限 Luma secret 或外部 KMS。
- ciphertext 保存 `key_version`，支持在线 re-encrypt/rotation。
- secret 只允许 set/replace/delete；读取返回 metadata，不返回明文。
- deployment 将最小所需 secret 短时注入 Luma/BuildKit；不把无关 tenant secret 传给同一任务。
- Luma 当前 scoped secret 会明文持久化到 `control.json`，公网发布前需要改成 ephemeral 或 encrypted persistence。
- 日志、event、trace attribute、audit diff 统一 secret redaction。
- 备份中的 ciphertext 与 master key 分开保管；恢复演练同时验证 key 可用性。

## 5. Source 安全

### 5.1 Upload

- multipart/presigned upload，服务端验证 object size/hash。
- quarantine -> scan -> clean 的不可变转移。
- 防 ZIP bomb、路径穿越、symlink、文件数爆炸和稀疏文件。
- 发现 `.env`、私钥、云 credential、SSH key 时阻塞并提示删除；不在页面回显内容。
- 静态站也需内容滥用检测、CSP/安全 header 和恶意下载策略。

### 5.2 Git

- clone URL scheme allowlist，只允许 HTTPS/SSH。
- DNS rebinding/SSRF 防护；拒绝 metadata、loopback、控制平面和未批准私网。
- submodule/LFS 每个 host 重新校验。
- shallow clone、大小/时间限制；commit 固定。
- GitHub 使用 installation token；generic token/SSH key 通过一次性 credential lease 在 Luma builder task 领取时短期注入，并通过 askpass/fd/secret mount 使用，禁止进入 clone URL 或进程 argv。
- LAE/Luma 持久状态只记录 lease ID、scope、过期和消费状态；builder 内换取的明文不得写入 task payload、`control.json` 或日志。
- credential broker 的 redeem I/O 不持有 Control state lock；claim、失败、取消和最终 credential delivery 之间均重查 task fence，避免双 redeem 和取消后交付凭据。
- Git URL 统一拒绝 userinfo、query 和 fragment；PAT 只经临时 mode-0600 askpass/secret mount 注入，并清除继承的 Git trace/curl verbose 环境。
- builder 的 stdout/stderr、completion message 和任意 result free text 不持久化；Control 只接受严格 result schema 和固定 artifact descriptor，并生成安全的阶段消息。
- 分析和构建绑定相同 resolved commit/source snapshot digest；任何 digest 变化都强制重新分析，阻止 TOCTOU 换码。

## 6. 构建隔离

legacy `build-image` lane 的 agent/rootful Docker/buildx 仍连接宿主 Docker，并使用共享 host-network builder；全局 Git provider credential 仍会持久化到 `control.json`，所以它只适合内部仓库，不能原样承担公网不可信 Dockerfile。Builder v2 的 analyzer 已单独强制显式 rootless Docker socket、kernel peer credential 和 rootless security option 校验，但这不代表 legacy lane 已被加固，也不替代 project quota、egress policy 与 artifact transfer。

公开构建最低要求：

- 专用 builder node，不运行 Luma manager、数据库或 tenant runtime。
- Git fetch、`lae-agent-runner` 和镜像 build 都由 Luma node task 执行；LAE API/Worker 不本地 clone 或 build 用户代码。
- rootless BuildKit/daemon；不挂载 rootful Docker socket。
- 关闭 privileged、host network、device 和 insecure entitlement。
- CPU/memory/PID/ephemeral disk/wall time 限制。
- 默认拒绝管理网、RFC1918/Tailscale、cloud metadata；依赖出口经过 allowlist/proxy。
- build secret 使用 secret mount，不进入 ARG/layer/history。
- 每次 build 临时 workspace，结束强制清理。
- 输出 SBOM/provenance，执行漏洞、恶意软件和策略扫描。
- build cache 不能让 tenant A 读取 tenant B 的私有 layer/secret。
- Luma 服务端从可信 tenant/app metadata 派生 registry namespace，BuildPlan、用户 Compose 和 Dockerfile 都不能覆盖。
- 取消、超时、builder 掉线和 retry 都必须回收 workspace、撤销 credential lease、归还 quota，并保留可审计终态。

Dockerfile、Compose、private Git 面向 Lite/Pro/Ultra 一致开放，不设置申请或邀请制产品开关。构建沙箱未通过上述验收时，LAE 整体不得公开发布这条能力；静态纵向切片可以仅用于内部工程验证。

## 7. Compose 策略

Compose 支持不等于允许全部 Docker 能力。V1 policy：

### 7.1 允许

- 多服务 image/build。
- HTTP route、内部 service、worker、datastore。
- healthcheck、depends_on、受控 command/entrypoint/restart。
- environment 名称引用。
- 受管 named volume。

### 7.2 拒绝

- `tcp-relay`、TCP/UDP 公网入口、host port。
- privileged、device、Docker socket、host bind。
- host/PID/IPC network namespace。
- `cap_add`、危险 sysctl/security_opt/ulimit。
- external network、静态 IP、任意 labels/constraints/node。
- source root 外的 build context、env_file、config、secret。
- 未能验证凭据或来源的 private image。

拒绝报告包含 service、field、source line、reason 和 replacement suggestion。LAE Policy 通过后，Luma server-side policy 再校验一次。

## 8. Runtime 隔离

- 生产使用专用 `lae-runner` pool。当前共享 validation 经明确决策允许 `manager + tecent`：manager 必须带显式 runtime role、进入正向 allowlist，并继续接受资源/网络/容量门禁；这不是生产隔离证明。
- `non-root`、read-only rootfs（需要写时用 tmpfs/volume）、drop capabilities、no-new-privileges、seccomp/AppArmor。
- 禁止 host mount/device/docker socket。
- CPU/memory/PID/ephemeral disk/hard timeout 限制；OOM/重启有用户可见事件。
- 网络默认拒绝 manager/Nomad/Luma/DB/object internal endpoint/metadata/Tailscale；service 间只允许 app topology 需要的路径。
- 出站带宽/连接数/目标策略，防代理、扫描、垃圾邮件、挖矿。
- public HTTP 经过 Traefik rate limit、header、body size 和 timeout policy。

Compose datastore 仍是用户 workload，不因使用 Postgres/MySQL image 获得平台管理员权限。

## 9. 域名与滥用

- wildcard DNS/TLS，域名 allocator 有保留字、唯一约束和删除冷却期。
- 明确禁止钓鱼、恶意软件、开放代理、垃圾邮件、挖矿、违法内容和攻击工具。
- 公共举报入口、证据保留、tenant/app suspend、申诉和恢复流程。
- 安全封禁优先在 LAE desired state 生效，再由 Luma 执行；紧急时超管可隔离 route，但必须补 audit/reconciliation。
- Content/abuse 状态不与 billing 状态混在一个字段。

## 10. 套餐模型

套餐是版本化 `entitlement + limits`，不是代码中的 if/else。初始讨论值如下，尚不是对外承诺：

| 能力 | Lite（草案） | Pro（草案） | Ultra（草案） |
| --- | ---: | ---: | ---: |
| 应用数 | 3 | 20 | 100/定制 |
| 每 app service | 5 | 20 | 50/定制 |
| 每 app HTTP route | 2 | 8 | 20/定制 |
| 总持久存储 | 2 GB | 50 GB | 200 GB+ |
| 单次上传 | 100 MB | 1 GB | 5 GB/定制 |
| 并发诊断 | 1 | 2 | 5+ |
| 并发 build | 1 | 2 | 5+ |
| build 分钟/月 | 200 | 2,000 | 定制 |
| 部署并发 | 1 | 2 | 5+ |
| 日志保留 | 3–7 天 | 30 天 | 90 天/定制 |
| 私有 Git | 可限 1 个连接 | 支持 | 支持 + 审计 |
| 自动 update check | 手动 | 定时 | 定时 + policy |
| Volume 备份 | 基础每日快照，暂按 3 天保留 | 每日，暂按 30 天保留 | 可配置/专属 |

Lite 明确允许 named volume 和应用内自管数据库；“每日 3 天”是为继续设计采用的临时备份参数，不是已确认价格权益。价格、年付折扣、恢复次数和实际资源必须在新增 builder/runner/stateful 容量压测后确认。当前 live 集群不能支持上述对外承诺。

### 10.1 计数语义

- running 与 suspended app 都计应用数；软删除 app 不允许恢复时才释放。
- service 按 revision 中实际 required service 计；build helper 不计 runtime service。
- volume 以 logical provisioned/actual bytes 中可解释的规则计量；明确展示。
- artifact/image 引用解除且 GC 完成后才释放 storage usage。
- analysis seconds 从 Luma `analyze-source` task 开始到终态计；同 source/policy/agent digest 的安全缓存命中不重复计。
- build seconds 从 Luma builder task 开始执行到 build 终态计，不把 API/队列等待和 source analysis 时间混入 build 用量。
- 失败因平台故障可自动返还 build usage；用户代码失败是否计费由产品规则明确。

### 10.2 Quota Reservation

上传、analysis、build、deploy、创建 app/route/volume 前先原子 reserve。Operation 成功转 used，失败/取消释放，worker/builder crash 由 TTL + reconciler 处理。

套餐降级导致 over-quota 时：

- 不自动删除/停止现有应用。
- 允许查看、导出、删除、suspend、支付。
- 阻止新 app、新 deployment、增 volume/route/service。
- 给出恢复合规的明确清单。

## 11. 支付

### 11.1 Provider 抽象

```text
create_order -> checkout_payload/url
query_order
verify_webhook
refund
close_order
reconcile
```

实现：`mock`、`wechat_pay`、`alipay`。支付 adapter 部署在 Luma；支付网关本身是外部依赖。

### 11.2 状态机

```text
CREATED -> PENDING -> PAID -> FULFILLED
                 |-> CLOSED
                 |-> FAILED
PAID/FULFILLED -> REFUND_PENDING -> REFUNDED | REFUND_FAILED
```

- 前端/CLI 提交 `plan + interval`，价格和金额只由服务端 plan version 决定。
- webhook 验签、provider event 幂等、金额/币种/商户号校验。
- 先落 payment event/order，再事务更新 subscription/outbox。
- 不能只依赖浏览器支付成功回跳；定时 query/reconcile 补单。
- 月付/年付都使用 period 和 immutable plan version。
- mock provider 走同一 webhook/fulfillment 逻辑，不直接改 subscription 表。
- Agent Skill 只能生成 checkout URL，必须由人类确认付款。

## 12. 邮件

- `email-service` adapter 发送 verification、login、security、billing 和 incident 邮件。
- dev/validation 使用 Mailpit/mock；production 接可替换 SMTP/API provider。
- 邮件任务走 outbox + worker，重试/退信/投诉有状态。
- 模板版本化，链接短时签名，邮件中不包含 deploy token/secret。
- 供应商未就绪时允许 mock，但注册页面明确测试环境，不伪造真实送达。

## 13. 可观测性

### 13.1 Correlation

每条请求和异步链路携带：

- request_id
- trace_id
- tenant_id（内部 label，日志导出时按权限过滤）
- app_id / deployment_id / operation_id
- service_key
- luma external_ref / job ID（仅内部）

### 13.2 用户可见

- durable deployment events 与 build logs。
- 逐服务 runtime logs。
- CPU/memory、request/error/latency、health status。
- route/public readiness 和最近故障。
- volume 使用、备份状态。
- 数据导出与明确 retention。

### 13.3 平台可见

- registration/login/mail success。
- analysis/build/deploy success rate 和 phase latency。
- queue depth/age、worker lease、reconciliation lag。
- Luma/Nomad/registry/object/DB/Traefik health。
- runner/build/stateful capacity 与 noisy tenant。
- payment webhook/reconciliation。
- cross-tenant access denial、安全/滥用事件。

实时图表提供 pause、文本 KPI 和数据表；不依赖颜色表达异常。

## 14. SLO 与告警（初始目标）

以下是进入公测前的工程目标，需容量测试确认：

| 指标 | 目标 |
| --- | --- |
| LAE API 可用性 | 月度 99.9%（支付 provider 故障单独计算） |
| 支持范围首次部署成功率 | >= 95%，公测后目标 98% |
| Operation event durability | 已确认事件不丢失，重放顺序一致 |
| 静态部署 p50 | <= 60s（不含用户输入） |
| Compose 部署 | 按 service/build 分布建立基线，不承诺伪统一时长 |
| Runtime status freshness | <= 30s，超时显示 unknown 而非 healthy |
| 跨租户数据/secret 泄漏 | 0 |

告警必须指向 runbook，至少覆盖 DB、queue age、builder capacity、runner capacity、registry disk、object disk、Luma unavailable、route error、backup failure、payment mismatch。

## 15. 备份与恢复

| 数据 | 备份 | 恢复要求 |
| --- | --- | --- |
| PostgreSQL | WAL/PITR + 每日 base backup | 定期隔离 restore，验证行数、约束和 key |
| Artifact store | versioning/replication 或离线备份 | 抽样 hash 校验和完整恢复 |
| OCI registry | manifest/catalog + blob 存储备份 | active/rollback digest 可拉取 |
| Luma control state | 加密快照 | 与 Nomad/job/routes/secret 一致性检查 |
| Volume | Lite 暂按每日/3 天、Pro 暂按每日/30 天、Ultra 按配置 | 用户可见最近成功时间；恢复形成 operation；均非托管数据库 SLA |
| Encryption keys | DB 外独立备份 | 双人/受控恢复流程，定期轮换演练 |

建议初始目标：业务 DB RPO <= 15 分钟、RTO <= 2 小时；用户 volume 根据 plan 分级。当前单 manager/单数据节点不满足高可用承诺，应在定价页明确 beta SLA。

## 16. 运维与发布

- LAE 自身用 Luma manifest/Compose，固定 image digest。
- 数据库 migration 采用 expand/migrate/contract，先兼容旧 worker，再切新版本。
- Web/API/Agent/Worker 独立版本，contracts 有兼容矩阵。
- kill switch 只用于事故止损；Compose/private Git 不按邀请或套餐隐藏。feature flag 可控制真实 payment provider 和 auto update 等尚未发布能力。
- 每次 release 在 validation Luma 跑 HTML、单服务、Compose+volume、双 route、失败回滚 E2E。
- production deploy 使用 canary/rolling；readiness 失败自动保留旧版本。
- 每个事故保留 operation/trace/audit，形成 runbook 与回归测试。
- 用户操作以 [09 用户使用指南](./09-user-guide.md) 为准；值班检查、placement 可见性、恢复、轮换和 GC 采用 [10 运维与排障 SOP](./10-operations-troubleshooting-sop.md)。SOP 默认只读和隔离 validation，不把节点/IP 或 secret 带入租户工单。

## 17. 合规开放项

若面向中国大陆公众运营，需要在发布前确认：

- ICP/公安备案与域名主体。
- 用户协议、隐私政策、数据保留与删除。
- 内容安全、举报/处置、日志留存。
- 是否需要实名或高风险能力实名。
- 微信/支付宝商户主体、退款与发票。
- 用户源码是否跨境、是否允许外部大模型处理。

这些会改变注册、日志、存储 region、支付和 AI 分析设计，不能留到上线当天处理。
