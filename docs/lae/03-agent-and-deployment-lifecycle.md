# 03. LAE Agent 与部署生命周期

## 1. Agent 的职责边界

LAE Agent 是版本化、可审计的部署分析器，不是拥有自由执行权的聊天机器人。它由两个部署单元组成：Luma 上常驻的 `lae-agent` API/controller，以及只在 Luma builder task 内运行的 `lae-agent-runner`。

它负责：

- 识别 source 类型、框架、构建方式、服务拓扑和输出产物。
- 发现环境变量引用并给出证据、类型和 required/sensitive 建议。
- 对单服务或 Compose 做策略校验。
- 生成结构化 `DeploymentPlan`。
- 生成平台控制的 Dockerfile、规范化 Compose 和 Luma manifest/sidecar 候选。
- 解释阻塞原因和可操作修复建议。

它不负责：

- 直接调用 Luma 部署。
- 自主修改用户仓库。
- 绕过 policy 或把 LLM 结论当作安全结论。
- 读取其他 tenant、支付密钥或 Luma management token。
- 在 controller 内 clone、挂载或执行用户源码；源码拉取、runner 分析以及需要执行的 build/test 全部进入隔离 Luma builder task。

最终流程是 `Detector -> Adapter -> Policy -> Luma Validator`。四层都通过才可部署。

## 2. 公共端点

用户看到的公共 Agent 端点是 LAE API 的 `POST /v1/analyses`，而不是直接暴露 agent 容器：

```text
Browser session / LAE deploy token
  -> LAE API authentication + tenant + quota + audit
  -> queued analysis operation
  -> lae-agent controller
  -> lae-worker / Luma adapter
  -> Luma analyze-source task
  -> builder fetch + immutable source snapshot
  -> lae-agent-runner
  -> persisted DeploymentPlan / BuildPlan / evidence
```

分析响应为异步资源，客户端用 operation event stream 查看过程。同步请求不等待 fetch/analyze。公网 endpoint 不返回 builder 节点、内部 registry、credential lease 或 Luma task secret。

## 3. Source Intake

### 3.1 文件上传

按原产品约束，文件上传只接受：

- 单个 `.html`。
- ZIP 形式的已经构建完成的静态目录。

不接受通过文件上传提交动态后端源码、Compose 或 Dockerfile；动态/Compose 使用 Git integration 或模板，以便 source revision 可追踪。

上传校验：

- 文件大小、文件数、展开后总大小和压缩比限制。
- 禁止绝对路径、`..`、NUL、symlink/hardlink、device、FIFO。
- MIME 与扩展名交叉检查。
- ZIP 必须有 `index.html`，或用户在诊断结果中选择唯一入口。
- 拒绝服务端可执行文件不会提升安全性；仍需 malware/钓鱼扫描、CSP 策略和滥用治理。
- 上传 `.env`、私钥、token 文件时阻塞并要求用户移除。

### 3.2 GitHub

- 优先使用 GitHub App installation，而不是长期 PAT。
- 当前第一版已落地的是加密 HTTPS source connection/PAT + exact-host + task-bound 单次 lease；GitHub App installation/OAuth 仍是后续优先演进，不应把目标态写成当前已启用能力。
- source revision 固定为 resolved commit SHA，不使用浮动 branch 作为部署事实。
- `lae-agent` 只提交 repository/ref 和一次性 `credentialLeaseId`；Luma builder 在 task lease 时换取 installation token 并 clone。
- installation token 只在 builder clone lease 中短期存在，不写 Luma state、LAE operation body 和日志。
- Control 先原子 claim node task，再在 state lock 外调用 HTTPS credential broker；redeem request/response 必须完整回绑 `leaseId/builderTaskId/externalOperationId/principal/tenant/application/repository`，TTL 不超过 300 秒。返回凭据只加入本次 node-agent lease HTTP 响应。
- broker transport、TTL、schema 或任一 binding 失败时 parent/child 原子失败且只保存通用错误；兑换期间发生取消则丢弃内存凭据并收敛为 canceled。broker service token 从非 world-readable secret file 读取，不写环境日志。
- Webhook 只创建 update candidate，不默认自动部署。

### 3.3 私有 Git/Gitea

- 当前支持 HTTPS token，write-only、envelope encrypted；SSH deploy key 是目标能力，需要独立 fd/agent mount 协议后再开放。
- base URL/host 需经过 allowlist 或管理员批准；解析 DNS 后拒绝 loopback、metadata、control/private network，允许的自托管地址单独配置。
- 禁止把凭据拼进 clone URL、operation event 或 source metadata。
- clone 限制仓库大小、历史深度、LFS 大小、submodule host 和超时。
- DNS/host allowlist 在创建 connection、签发 lease 和 builder 实际连接前都检查，防止解析漂移绕过 SSRF 策略。

当前落地的第一条 broker credential 类型是 `none | git-https`；SSH deploy key 需要独立的 fd/agent mount 协议和同等级测试，在该协议完成前不得让 executor 从旧全局 `gitProviders` 回退。

### 3.4 模板

模板 source 也是不可变 commit/digest；一键部署仍走正常 analysis/build/deploy，不存在绕过诊断的“模板快速路径”。

## 4. 分析流水线

```text
SOURCE_REQUESTED
  -> BUILDER_FETCH
  -> RESOLVE_IMMUTABLE_REVISION
  -> SNAPSHOT_AND_DIGEST
  -> RUN_AGENT_IMAGE
  -> INVENTORY
  -> SECRET_LEAK_SCAN
  -> DETECT_PROJECTS
  -> DETECT_COMPOSE_TOPOLOGY
  -> DETECT_ENVIRONMENT
  -> ADAPTER_SELECTION
  -> POLICY_EVALUATION
  -> BUILD_PLAN
  -> LUMA_ARTIFACT_GENERATION
  -> LUMA_PREVIEW
  -> deployable | needs_input | unsupported | diagnostic_failed
```

`SNAPSHOT_AND_DIGEST` 是安全边界：后续 `build-plan` 必须引用同一 snapshot digest 和 resolved commit。浮动 branch 更新后需要新 analysis，旧 plan 不得继续构建。

以上是公开 verdict。数据库中的兼容 analysis status 仍可能使用
`needs_configuration`/`not_deployable`；Web、CLI 与 Skill 必须消费公开的
`needs_input`/`unsupported`，并把 `diagnostic_failed` 明确解释为平台诊断故障而不是
用户代码不支持。`unsupported` 必须包含稳定 blocker、证据位置和可执行修复建议。

### 4.1 Inventory

输出：

- 文件树摘要、总大小、语言/lockfile、入口文件。
- Dockerfile、Compose、Procfile、package/pyproject/requirements。
- `.env*` 只记录文件名和风险，不读取/返回 secret 值。
- Git submodule/LFS/monorepo workspace。

### 4.2 Detector

Detector 插件示例：

- `static-html`
- `vite-static`
- `astro-static`
- `node-http`
- `python-asgi`
- `python-wsgi`
- `dockerfile`
- `compose`

每个 detector 输出 evidence，禁止只给一个模糊置信度：

```json
{
  "detector": "compose",
  "matched": true,
  "evidence": [
    {"path": "compose.yaml", "rule": "compose-file"},
    {"path": "compose.yaml", "rule": "services", "value": ["web", "worker", "postgres"]}
  ]
}
```

### 4.3 Adapter

Adapter 将 detector 结果转成运行计划：

- build command / context / Dockerfile。
- static output directory。
- start command、container port、healthcheck。
- service dependencies 和 public routes。
- volume、resource profile、runtime user。

任何推断都必须有 adapter version 和 evidence。用户修改推断值时形成 explicit override，并进入 revision。

### 4.4 Builder 分析任务契约

`analyze-source` 输入只包含可审计引用，不含明文 Git 凭据：

```json
{
  "schemaVersion": "luma.builder-task/v1",
  "kind": "analyze-source",
  "externalOperationId": "op_01...",
  "tenantRef": "tenant_01...",
  "applicationRef": "app_01...",
  "payload": {
    "sourceRef": {"repository": "https://git.example/acme/app.git", "ref": "main", "subdirectory": ""},
    "credentialLeaseId": "cl_01...",
    "agentImageDigest": "registry/infra/lae-agent-runner@sha256:...",
    "policyVersion": "2026-07-11",
    "limits": {"cpu": 2, "memoryMiB": 2048, "diskMiB": 4096, "timeoutSeconds": 300}
  }
}
```

`Idempotency-Key` 只通过 HTTP header 传递，不进入 JSON body。结果必须包含完整 Git object ID（不使用 short SHA）的 `resolvedCommit`、`sourceTreeDigest`、`sourceSnapshotId`、`sourceSnapshotDigest`、`deploymentPlanDigest`、`buildPlanDigest`、`evidenceDigest`、`policyVersion`、`agentImageDigest`，以及 `evidence/deploymentPlan/buildPlan` 三个严格 artifact descriptor。descriptor 只含 digest、固定 media type 和 size，不含本地路径、stdout、debug 或任意 message。

Luma 只持久化 Control 生成的结构化阶段消息；builder stdout/stderr 和 node completion message 都不作为 durable event 保存，避免无法穷举格式的凭据泄漏。Luma task state 只保存凭据 lease 的 ID/状态，不保存换取后的 token/key；workspace 和 lease 在成功、失败、取消、超时后都清理。

### 4.5 分析产物安全摄取契约

descriptor 不是“已经保存”的证明。Luma task 成功后，LAE 只能通过 artifact
broker 申请短时、单次下载租约；租约必须同时绑定 `tenantRef`、
`applicationRef`、`externalOperationId`、Luma task ID、artifact name、digest、
media type 和 size。broker 自己持有内部 URL/credential，公共 API、operation
result/event、日志和可序列化 lease handle 都不能出现这些值。跨租户、跨任务、
descriptor 变化、过期或重复兑换一律返回同形失败。

摄取流程固定为：

1. PostgreSQL 先保存 immutable descriptor，analysis 保持
   `artifactState=descriptor-only`、`planStored=false`。
2. worker 在仍持有 operation lease 且未取消时，把 artifact 标记为
   `uploading`，每次重试申请新的一次性下载租约。
3. 下载按 bounded chunk 流式处理；固定 media type、Content-Length、实际字节数、
   SHA-256 和 16 MiB 单件上限必须全部匹配。timeout、取消和失败不会发布最终对象。
4. S3-compatible adapter 只能写入 LAE 计算的
   `tenants/{tenant}/analysis-artifacts/{closed-kind}/sha256/{digest}.json`；上游
   path/URL 不参与 object key。adapter 必须先写私有 staging，验证后原子发布；
   exact existing object 作为 crash retry 的幂等命中。
5. 三件对象在最终 HEAD 复核后，PostgreSQL 才能在一个事务中把全部 link 视为
   `verified` 并将 analysis 切换为 `stored/planStored=true`。任何缺件或校验失败都
   保持 descriptor-only，不能把部分成功伪装成可部署 plan。

当前 LAE 已实现上述 port、PostgreSQL 状态机和 Fake 端到端；Luma 真实安全下载租约
端点及生产 S3-compatible adapter 仍未接入。production worker 在 verified recorder
不可用时启动失败，禁止通过节点 snapshot 路径、共享卷或任意内部 URL 直连绕过。

## 5. 环境变量发现

Agent 组合多种证据：

- `.env.example` / `.env.sample` 的 key 和注释，不读取真实 `.env` 值。
- Compose `environment`、`env_file` 引用。
- `process.env.NAME`、`os.getenv`、Pydantic settings 等静态引用。
- framework/config adapter 的已知变量。
- Dockerfile `ARG` 与 `ENV`，区分 build-time 和 runtime。

每个变量输出：

```json
{
  "name": "DATABASE_URL",
  "scope": "runtime",
  "services": ["web", "worker"],
  "required": true,
  "sensitive": true,
  "public": false,
  "configured": false,
  "evidence": [
    {"path": "app/settings.py", "line": 12, "rule": "pydantic-required"}
  ],
  "description": "PostgreSQL connection string"
}
```

规则：

- static analysis 无法证明 optional 时不得自动标成 required；返回 `needs_confirmation`。
- 名称包含 TOKEN/SECRET/PASSWORD/KEY/URL credential 的默认 sensitive，但允许 adapter 更精确覆盖。
- `NEXT_PUBLIC_`、`VITE_` 等 public 变量明确标记“会进入客户端 bundle”。
- build-time secret 只通过 BuildKit secret mount 注入，不进入 layer、build arg 或 image history。
- secret 值 never-read-back；更新使用 env schema version 做乐观锁。

## 6. DeploymentPlan 协议

`DeploymentPlan` 是 Agent 与 Orchestrator 的稳定边界，使用 JSON Schema 版本控制：

```json
{
  "schemaVersion": "lae.deployment-plan/v1",
  "planId": "plan_01...",
  "sourceRevisionId": "src_01...",
  "sourceDigest": "sha256:...",
  "agentVersion": "1.0.0",
  "adapter": {"name": "compose", "version": "1.0.0"},
  "kind": "compose",
  "services": [],
  "routes": [],
  "volumes": [],
  "environment": [],
  "builds": [],
  "warnings": [],
  "blockers": [],
  "resourceEstimate": {},
  "policy": {"version": "2026-07-11", "decision": "allow"}
}
```

### 6.1 Service

```json
{
  "key": "web",
  "role": "http",
  "image": {"source": "build", "buildKey": "web"},
  "command": null,
  "port": 8080,
  "healthcheck": {"type": "http", "path": "/healthz", "intervalSeconds": 10},
  "dependencies": ["postgres"],
  "environmentNames": ["DATABASE_URL"],
  "resources": {"cpu": "0.50", "memoryMiB": 512},
  "securityProfile": "lae-default-v1"
}
```

`role` 支持 `http | worker | internal | datastore | cron`。`cron` 只有 Luma/Nomad renderer 有明确周期任务语义时才开放；否则 blocker。

### 6.2 Route

```json
{
  "serviceKey": "web",
  "kind": "http",
  "primary": true,
  "hostnameRef": "domain_01...",
  "containerPort": 8080,
  "healthPath": "/healthz"
}
```

V1 只允许 `kind=http`。检测到 TCP/UDP、`tcp-relay` 或 host port 时 `policy.decision=deny`。

### 6.3 Volume

```json
{
  "key": "pg-data",
  "serviceKeys": ["postgres"],
  "mountPath": "/var/lib/postgresql/data",
  "class": "persistent",
  "requestedBytes": 10737418240,
  "accessMode": "ReadWriteOnce",
  "backupPolicy": "daily-7d",
  "deletePolicy": "retain"
}
```

用户不能指定 storageClass/node/path。Orchestrator 根据 tenant/plan/region 选择 Luma storage class，并生成唯一 path。

### 6.4 BuildPlan

`BuildPlan` 与面向运行态的 `DeploymentPlan` 分开签名。它是 LAE 到 Luma builder 的唯一构建输入，用户不能直接提交：

```json
{
  "schemaVersion": "lae.build-plan/v1",
  "sourceSnapshotDigest": "sha256:...",
  "resolvedCommit": "4f2c...",
  "policyVersion": "2026-07-11",
  "builds": [
    {
      "key": "web",
      "context": ".",
      "dockerfile": "services/web/Dockerfile",
      "target": "runtime",
      "platform": "linux/amd64",
      "buildArgNames": ["APP_VERSION"],
      "secretMountNames": ["NPM_TOKEN"],
      "dependsOnBuilds": []
    }
  ],
  "externalImages": [
    {
      "key": "database",
      "ref": "postgres:17",
      "resolvedDigest": "sha256:...",
      "platform": "linux/amd64"
    }
  ],
  "signature": {"keyId": "lae-plan-2026-01", "value": "..."}
}
```

约束：

- context/Dockerfile 必须在 snapshot 内，路径规范化后再次校验；禁止绝对路径和 `..` 逃逸。
- plan 只列 build arg/secret mount 名称；值在执行时由 environment version 与短期 lease 绑定，secret 不进入 plan、layer 或日志。
- registry host、repository、tag 和 push credential 不属于用户可控字段；Luma 按 tenant/app/service 派生，结果只以 digest 进入 revision。
- `externalImages` 与 `builds` 的 key 全局唯一；外部引用必须是显式非 `latest` tag 或单独的 `sha256` digest，只允许公共 DNS registry 默认端口，禁止 URL/userinfo/query/fragment、localhost、IP、私网后缀和 `tag@digest` 混写。每项必须同时携带 analyze 阶段固定的 `resolvedDigest`。
- independent builds 可由 Luma `build-plan` parent task 按套餐并发上限拆成 child tasks；失败的已推送 orphan digest 进入延迟 GC，不影响 active revision。
- Compose 如果全部使用预构建 image，`builds=[]` 但 `externalImages` 非空，仍必须运行 resolver、SBOM 与离线漏洞扫描，不能返回空成功。
- V1 需要明确实现 `args`、`target`、BuildKit secret/credential mount；不支持的 SSH/cache/additional-context 字段必须形成 blocker，不能像当前 builder 一样静默忽略。

签名与 snapshot 绑定规则：

- network-disabled runner 只产出内部 `lae.build-plan-proposal/v1`；tag 项没有 `resolvedDigest`，原生 digest ref 可预填同值。Luma analyze executor 用独立匿名 resolver 固定所有外部镜像后，才生成并持久化 `lae.build-plan-candidate/v1`。candidate 没有 `signature`，但每个 external image 必须有 `resolvedDigest`；`buildPlanDigest` 和 artifact descriptor digest 都是该 canonical candidate 实际字节的 SHA-256：UTF-8、对象 key 排序、紧凑分隔符、`ensure_ascii=false`、无尾随换行。
- LAE controller 校验 candidate digest 后，将 `schemaVersion` 转换为 `lae.build-plan/v1` 并加入 signature；不能在 artifact 中放一个“假签名”占位。
- controller 使用 HMAC-SHA256 签名 canonical envelope：`schemaVersion=luma.builder-plan-signature/v1`、`tenantRef`、`applicationRef`、`sourceSnapshotId` 和 unsigned signed-plan content；签名值为无 padding 的 base64url。
- Luma Control 按 `keyId` 从服务端 signing-key allowlist 验签，并把 signed plan 反向转换成 candidate schema 复算实际 candidate digest，再将 snapshot id/digest、完整 commit、policy、service principal、tenant 和 application 与成功的 analyze 记录逐项比较。
- 任一字段不匹配、snapshot 未知/过期、签名 key 未受信或签名错误都在排队前拒绝，不能只检查两个客户端字符串“自洽”。

## 7. 单服务生成

平台生成的 Luma manifest 示例：

```yaml
name: lae-tn7a-ap9k
image: 100.66.177.70:5000/lae/tn7a/ap9k@sha256:...
region: cn
exposure: cn-edge
domain: a-4k7sm2yd9qcx8r5p.itool.tech
port: 8080
replicas: 1
env:
  DATABASE_URL: ${DATABASE_URL}
resources:
  limits:
    cpus: "0.50"
    memory: 512M
healthcheck:
  test: [CMD, wget, -q, -O-, http://127.0.0.1:8080/healthz]
```

用户看见的是 DeploymentPlan，而不是可自由编辑的 manifest。高级用户可以下载只读 manifest 作为诊断证据。

## 8. Compose 生成

### 8.1 规范化

Agent 保存用户 source 中的 Compose digest，并生成规范化快照：

- 展开 anchors/extends 后形成稳定结构。
- build context 固定在 source snapshot 内，拒绝 `..` 和绝对路径。
- image tag 在 build/pull 后固定为 digest。
- 删除或拒绝 host `ports`，由 Luma sidecar 表达公开端口。
- env 值替换为名称引用；真实值不进入 Compose。
- named volumes 转为平台 volume key。
- 逐服务注入 security/resource policy；用户不能覆盖。

### 8.2 允许项

- `services`、image/build、command/entrypoint 的受控形式。
- environment 名称、healthcheck、depends_on、restart policy。
- named volumes、内部 service dependencies。
- 一个或多个 HTTP service；其他 service `exposure:none`。

### 8.2.1 当前 Luma Compose 运行约束

- 所有 service 是同一 Nomad group，运行在同一节点/region，共享 network namespace。
- service name 映射到 `127.0.0.1`；整个 app 内监听端口必须唯一。
- group 当前固定单副本，不能宣称逐 service replica 或跨节点 HA。
- Compose `depends_on` 不会自动变成 Nomad 严格启动顺序；服务需要连接重试，严格顺序拓扑在诊断中阻塞。
- Agent 汇总整个 group 的 CPU、memory、ephemeral disk 和端口，确认至少一个 runner 能容纳。
- Agent 的资源检查是静态计划检查；Luma 必须再次以当前 ready runtime 候选和最终 Nomad Job plan 做动态 admission。只有两层都通过才进入 `DEPLOYING`。

### 8.3 默认拒绝项

- `privileged`、device、Docker socket、host bind。
- `network_mode: host`、`pid: host`、`ipc: host`。
- `cap_add`、不安全 seccomp/AppArmor override、`security_opt` 绕过。
- host port、UDP、公网 TCP、`tcp-relay`。
- external network、静态 IP、MAC、sysctl、ulimit 的用户自定义。
- 从 source root 外读取 Dockerfile/context/env_file/config/secret。
- 非 allowlist registry，或凭据无法安全注入的 private image。

### 8.4 Sidecar 示例

```yaml
name: lae-tn7a-ap9k
compose: compose.normalized.yml
region: cn

volumes:
  pg-data:
    storageClass: lae-cn-app-data
    path: tenants/tn7a/apps/ap9k/volumes/pg-data
    accessMode: ReadWriteOnce

services:
  web:
    exposure: cn-edge
    domain: a-4k7sm2yd9qcx8r5p.itool.tech
    port: 8080

  admin:
    exposure: cn-edge
    domain: a-h8f6r0m1q2vz7t4c.itool.tech
    port: 8081

  worker:
    exposure: none

  postgres:
    exposure: none
```

实际 `storageClass` 由 Orchestrator 选择；示例名称只是目标语义，不表示当前 live 集群已经存在该 class。

### 8.5 Compose 部署顺序

1. 将签名 `BuildPlan` 和 analysis 的 source snapshot digest 提交给 Luma builder，逐 build 并固定所有 image digest。
2. 生成 `compose.normalized.yml` 和 `luma.compose.yml`。
3. Luma compose validate/preview。
4. Luma storage check；新 path 需要 `initialize: empty` 的平台确认。
5. 预留/提交 volume usage，并取得绑定 tenant/app 的 opaque managed-volume ref。
6. LAE 只提交 `cn/global` region；Luma Placement 从实时 node/agent/Nomad 状态中排除 builder-only、manager/edge/control-plane-only、not-ready、down、draining、ineligible 和错误 region 节点，并校验 managed-volume 可达性。显式 runtime 角色可允许经过审核的混合节点；历史 duplicate node ID 只在当前 Nomad 名称无法唯一匹配时作为唯一 fallback。用户协议中不存在 node/IP/pool/failure-domain 字段。
7. Luma 为完整 Compose group 汇总 CPU/memory，读取 prior allocation，生成内部 candidate constraint 与软 affinity；prior node 故障时保留 compatible reschedule 候选。
8. 对最终 Job 执行 Nomad plan。无容量返回稳定 `capacity_unavailable`；Nomad 状态不可读返回 `placement_unavailable`；volume/topology 不兼容返回 `volume_placement_incompatible`。错误和 operation event 不包含候选节点、IP、pool、failure domain 或 Nomad failure dimension。
9. storage apply。
10. compose deploy stream。
11. 逐服务 readiness。
12. 逐 HTTP route 公网验证。
13. active revision 切换和旧版本回收。

数据库/volume 是用户应用的一部分，并不自动获得“托管 PostgreSQL”级别 SLA。控制台必须明确备份状态、最近备份和恢复入口。

如果当前 Luma 不能从 Compose healthcheck 生成逐服务 Nomad check，LAE 不能仅凭 task `running` 标记 ready：HTTP service 由 LAE 做内部/公网探测；内部 service 使用受限 TCP/命令检查或明确显示 `health unknown`。Compose healthcheck renderer 属于 GA 前 Luma 扩展。

## 9. 构建流水线

```text
LAE signed BuildPlan + immutable source snapshot ID/digest
 -> Luma build-plan task
 -> verify plan signature / policy / snapshot binding
 -> ephemeral rootless workspace
 -> built images: rootless BuildKit
 -> external images: anonymous crane resolver + exact registry allowlist
 -> dependency/build cache scoped by tenant + adapter
 -> image/SBOM/provenance/scan digests
 -> vulnerability/policy scan
 -> internal registry digest
 -> cleanup workspace
```

源码拉取和构建都由 Luma builder 完成，但分析与构建是两个可恢复 task。build task 同时携带 opaque `sourceSnapshotId` 和 digest；实现可以对同一不可变 snapshot 使用短期缓存，缓存 miss 时从 artifact store 取回并校验。只有在明确的恢复路径中才可按 resolved commit 重新 materialize，且必须校验 digest，不能重新解析 branch HEAD。

Analyzer lane 的 node 配置必须同时设置固定 digest 的 `LUMA_BUILDER_ANALYZE_IMAGE_DIGEST` 与显式 `LUMA_BUILDER_ANALYZE_DOCKER_HOST=unix:///run/user/<uid>/docker.sock`。Capability 和每次执行都会重新检查：仅 Linux、socket/runtime directory owner 与路径 UID 一致且非 0、Linux `SO_PEERCRED` daemon UID 一致、Docker `SecurityOptions` 声明 rootless、本地 runner `RepoDigests` 精确包含 allowlist digest。所有 Docker CLI 调用都显式带 `--host`，使用空临时 `DOCKER_CONFIG`，并以 `--pull never` 启动；不得回退继承的 `DOCKER_HOST`、default context、proxy 或 credential 配置。

Compose 的 `BuildPlan` 显式列出每个 service 的 context、Dockerfile、target、platform、build args 名称和依赖关系。Luma builder 不依赖仓库内存在 `luma.yaml` 或 `luma.compose.yml`；LAE 生成并保存这些文件，在镜像 digest 返回后再交给 Luma deploy validator。

Compose 中只有 `image:` 的 service 进入 `externalImages`。Control 从独立服务端配置生成 registry allowlist，并把它放进仅对当前 node lease 可见的字段；analyze/build builder 都必须与本机 allowlist 精确相等后才执行。解析器固定为独立的 `crane digest --platform linux/amd64`，使用空 `DOCKER_CONFIG` 且不继承 proxy/credential 环境；私有 registry 在专用 auth broker 接入前匿名失败关闭。analyze executor 将解析结果写进 canonical candidate，controller 对包含 `resolvedDigest` 的 plan 签名；build executor 会重新解析 tag，但只在结果等于 signed `resolvedDigest` 时继续，tag 漂移、重试或延迟执行都不能改变镜像。随后 Syft 与 Trivy 只消费 immutable reference。第三份 artifact 是 LAE 自己生成的 `https://itool.tech/lae/external-image-resolution/v1` in-toto statement，绑定原 ref、platform 与 resolved digest；它明确不是镜像发布者的 SLSA provenance。

Control 配置键为 `LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON`，builder node 配置键为 `LUMA_BUILDER_EXTERNAL_REGISTRIES_JSON`；两者都是排序、去重的公共 registry DNS JSON 数组，例如 `["docker.io","ghcr.io"]`。端口、IP、localhost 和私有 DNS 后缀不能进入该数组。

CLI 级 host allowlist 不能约束 registry 返回的 token-service/CDN redirect，因此生产开放前仍要由 builder 网络层实现 DNS/IP/redirect egress enforcement，并通过真实 staging 验证；当前实现不能据此宣称 production-ready。

每个 build：

- CPU、memory、PID、ephemeral disk、wall time 限制。
- 默认无内网访问；只允许依赖 registry/package mirror 和显式 source host。
- 不挂载 control socket、host Docker socket、tenant credential store。
- 原始 stdout/stderr 不进入 operation event 或 durable state；Control 只持久化固定阶段消息。后续若提供诊断日志 artifact，必须经过独立的流式 redaction、大小上限和凭据 canary 测试后再开放。
- build 成功、部署失败时保留 image digest，从 `VALIDATING` 检查点重试。
- registry repository 由 Luma 服务端按 tenant/app/service 注入，用户和 Agent 不能指定其他 namespace。
- 进度必须是结构化事件：fetch、snapshot、每个 service 的 queued/build/push/scan/digest、parent completion；原始 buildx 输出只作为受脱敏日志，不能代替状态协议。

## 10. 部署状态机

```text
DRAFT
 -> SOURCE_PREPARING
 -> ANALYSIS_QUEUED
 -> ANALYZING
    -> NEEDS_CREDENTIAL
    -> NEEDS_CONFIGURATION
    -> NOT_DEPLOYABLE
    -> READY
 -> QUOTA_RESERVED
 -> BUILD_QUEUED
 -> BUILDING
 -> SCANNING
 -> ARTIFACT_PERSISTED
 -> PLAN_VALIDATING
 -> STORAGE_PREPARING
 -> DEPLOYING
 -> WAITING_SCHEDULER
 -> STARTING
 -> ROUTING
 -> VERIFYING
    -> ACTIVE
    -> DEGRADED
    -> FAILED_RECOVERABLE
    -> FAILED_PARTIAL
    -> CANCELED
```

Compose 的多个 build/service/route 作为 child step，父 operation 仍有一个单调递增事件序列。

规则：

- `NEEDS_*` 暂停并释放 build slot，但保留 operation 上下文。
- operation event 先持久化再推送，不能只存在于 SSE/NDJSON socket。
- retry 新建 operation，并关联 parent/checkpoint；不篡改历史终态。
- deployment `ACTIVE` 需要所有 required service 健康和所有 required HTTP route 通过。
- optional service 失败可以是 `DEGRADED`，必须由模板/plan 明确 optional，不能临时忽略。

## 11. Update Check 与 Plan Diff

更新检查不直接调用现有 Luma “重新 build 并 deploy”动作，而是：

1. resolve 新 commit/source digest。
2. 运行新 analysis。
3. 比较 old/new `DeploymentPlan`：
   - services add/remove/role change
   - image/build adapter change
   - route/port change
   - env schema change
   - volume add/remove/path/mount change
   - resource/security policy change
4. 将变更分成 `safe / requires-input / destructive / blocked`。
5. 用户确认后创建 deployment。

删除 stateful service、移除 volume、改变数据库 image major、切换 storage backend 属于 destructive，不能一键自动批准。

## 12. Suspend、Resume、Rollback

- Suspend 保存 plan、Compose、sidecar、domain、secret refs、volume 与 image；停止 workload 并禁用 upstream route。
- Resume 使用最后 active revision，不重新分析浮动 branch。
- Rollback 恢复 image/Compose/sidecar 与非 secret env schema；数据库/volume 数据不会自动回滚。
- Compose rollback 必须验证旧 image 仍在 registry、旧 volume plan 仍兼容。
- 若 schema migration 不可逆，UI 明确阻塞或要求用户承担风险。

## 13. Reconciliation

周期任务对比：

- LAE desired state。
- Luma deployment record。
- Nomad desired/running/failed allocation。
- healthcheck。
- Traefik route。
- 公网 probe。

发现差异时先更新 observed state 和告警，再根据策略执行幂等恢复。公网 probe 只是最后一层证据，不能替代 Luma 正向发布流程。

需要处理：卡死 lease、孤儿 build、孤儿 volume、registry 无引用 image、过期 upload、失败后未释放 quota、active record 但 runtime dead、route 已存在但 backend missing。

## 14. 测试语料库

建立 golden repositories：

- 合法 HTML、SPA、静态 ZIP。
- Vite/React/Vue/Astro。
- FastAPI/Flask/Express/Hono。
- Compose：web+worker、web+postgres、双 HTTP route、命名卷、多 build。
- 缺 Dockerfile、缺 lockfile、缺 env、错误 port、healthcheck 失败。
- privileged/host network/docker.sock/host bind/tcp-relay，应稳定拒绝。
- ZIP bomb、path traversal、symlink、Git SSRF、submodule credential leak。
- build success/deploy fail、worker crash、SSE disconnect、重复 idempotency key。

每个 adapter/policy 变更必须跑 golden plan snapshot、Luma preview、staging 真部署和失败清理测试。
