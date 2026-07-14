# 13. LAE staging 最终交付说明

> 基线日期：2026-07-15  
> 产品环境：staging；不是 production GA 声明。

## 1. 当前可用入口

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| LAE Web | `https://lae-staging.itool.tech/` | 注册/预览、部署、应用与账户控制台 |
| Public API | `https://lae-api-staging.itool.tech/v1` | Web、CLI 与用户 Agent 的唯一 API |
| Agent | `https://lae-agent-staging.itool.tech/` | 受认证的项目诊断端点 |
| Artifact | `https://lae-artifacts-staging.itool.tech/` | 私有构建/上传产物存储 |
| Luma Gateway | `https://gateway.itool.tech/healthz` | 内部路由健康检查 |

用户不需要编写 Luma 文件。LAE Agent 读取由 Builder 固定的源码快照，结合版本化 Knowledge Pack 生成候选计划；确定性校验器给出 `deployable`、`needs_input`、`unsupported` 或 `diagnostic_failed`，平台保存最终 DeploymentPlan 与 Luma runtime manifest。

## 2. 已交付范围

- 邮件注册/登录协议、staging preview 登录、用户级 deploy token 的创建、轮换与撤销。
- HTML、静态 ZIP、公有 Git、私有 Git、Dockerfile 与多服务 Compose。
- Compose 多个公网 HTTP 服务、内部 worker/datastore、受管命名卷；公网 TCP/UDP、host port、自定义域名明确拒绝。
- 诊断、必需环境变量识别、用户追加变量、Builder 构建、部署阶段事件、应用列表、日志/指标入口。
- restart、suspend、resume、结构化 update check、重新部署、rollback、失败保留上一健康版本、delete。
- 四个固定版本 starter、CLI 与项目内 Skill；CLI/Agent 仅需 deploy token，不接触 Luma management token。
- Lite/Pro/Ultra entitlement、配额与月/年协议；真实支付 provider 未接入时保持 staging mock/production disabled。
- Luma Dashboard 的 LAE 超管只读聚合与内部 placement 视图。

完整协议、数据模型与需求证据分别见 [用户指南](./09-user-guide.md)、[运维 SOP](./10-operations-troubleshooting-sop.md)、[部署升级手册](./11-deployment-and-upgrade.md) 和 [需求证据矩阵](./12-requirements-evidence-matrix.md)。

## 3. 实际部署拓扑

| 层 | 当前 staging 位置 | 约束 |
| --- | --- | --- |
| Luma Control | `manager` | 唯一控制面 |
| LAE 平台 10 services | `manager` | PostgreSQL、artifact、backup 使用该节点本地盘，不依赖 NFS |
| Builder + internal registry | `builder` | build 与 push/pull 均使用 `100.66.177.70:5000` |
| 租户 runtime allowlist | `manager + tecent` | 用户只看到 region，不看到节点/IP |
| 公网入口 | Luma/Traefik + Cloudflare DNS-01 | 默认随机 `*.itool.tech`，不支持自定义域名 |

当前 Compose runtime 是单个 Nomad group：同一应用内服务同节点、共享网络命名空间，受管本地卷会形成节点亲和。生产扩容前不能把它描述成逐服务跨节点 HA。

## 4. 发布基线

- Luma 正式版本：`v0.1.257`。
- Control 内部镜像：`100.66.177.70:5000/luma-control:v0.1.257`。
- 在线节点 `manager/bot/builder/lab/m4/tecent` 已升级到 `0.1.257`。
- `gaojiu` 当前离线，无法升级；`blg` 按明确要求不处理；`aly` 为历史节点，不参与调度。
- LAE staging exact commit：`4548f6ab27ef115e7918a8f3078d93cca7d81476`，Nomad job `lae-platform-staging` v9，10 services。
- 平台镜像全部由 Builder 构建并写入 Builder registry；manager 不承载 registry。

`0.1.254-0.1.257` 修复 stateful rollback checkpoint，并把 wildcard 主域与 ACME resolver 显式绑定到 HTTPS entrypoint 和每个公开 router，避免历史裸域证书阻止随机租户域名获得可信 TLS。LAE 最新平台同时使 Worker 使用三个独立 lease owner 并发领取，长时间冷拉/失败 rollout 不再阻塞分析和生命周期队列。

## 5. 日常操作顺序

### 发布 LAE

1. 将代码提交到不可变 commit；不要部署工作区或 mutable branch。
2. 在 Builder 构建并推送平台镜像。
3. 使用显式 staging sidecar 与环境文件执行 `luma import`。
4. 等待 Nomad rollout healthy，再检查 Web/API/Agent/Artifact/Gateway。
5. 执行 `lae/scripts/staging_product_e2e.py`；失败时保留 operation/application/build/evaluation ID。

### 发布 Luma

1. bump package version、提交、创建 annotated tag。
2. 等 Control image 与 Python package workflow 成功。
3. 将 Control image 镜像到 Builder registry并校验 digest。
4. 先更新 manager/Control，再运行 fleet update。
5. 对比节点上报版本与升级前后的 route sentinel；离线节点单独记录，不伪报成功。

禁止通过修改 Docker daemon 全局代理、在 manager 临时部署 registry、手改数据库状态或手工 recreate allocation 作为常规修复。必须修复 Luma/LAE 的声明式配置、状态机或恢复协议，再用正式版本升级验证。

## 6. 仍是 production gate 的事项

- 真实 SMTP/API 邮件投递、SPF/DKIM/DMARC、退信与送达率 canary。
- 微信/支付宝真实 provider、签名回调、对账、退款和资金安全演练。
- 专用 LAE 平台节点与至少两个专用 runtime failure domain；当前共享 `manager` 只适用于 staging。
- PostgreSQL PITR、artifact/registry/tenant volume 的异机恢复演练和 RPO/RTO 证据。
- Docker/CNI/route reconciliation 故障注入、长时间多 edge sentinel、容量与滥用治理。
- `gaojiu` 恢复在线后的同版本升级；`blg` 只有在所有者另行授权时处理。

这些门槛不影响当前 staging 体验验证，但未关闭前不得对外宣称 production GA。
