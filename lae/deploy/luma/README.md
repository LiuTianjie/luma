# LAE on Luma：首版部署资产与上线门禁

本目录是 `lae-platform` 的第一版可验证 Luma Compose 资产。它把平台本身作为一个受控 Compose 部署到 Luma：Web/API 与受策略约束的 artifact S3 endpoint 使用公网 HTTP，Worker、Agent Controller、PostgreSQL 和 Valkey 只在同一 Nomad group 的内部拓扑中通信。MinIO 数据面虽然有 HTTPS route，但 bucket policy、CORS 和最小权限 credential 只允许 LAE upload/artifact 流程，不能作为管理入口。这里没有公网 TCP/UDP、host bind、Docker socket、host network 或数据库公网入口。

这不是“已经可以生产上线”的声明。截至 2026-07-15，Luma `v0.1.257` 已正式发布，live Control/manager 与在线 fleet `bot/builder/lab/m4/tecent` 已收敛；`gaojiu` 离线、`blg` 按要求未触碰。`lae-platform-staging` 使用 exact commit `4548f6ab27ef115e7918a8f3078d93cca7d81476`，10 个 Compose service 全部健康。PostgreSQL、MinIO 和本地快照位于 manager 本地盘，平台启动不依赖 NFS。真实邮箱、异机恢复和生产安全门禁仍待完成，不能把平台健康外推为 production-ready。

## 文件

| 文件 | 用途 |
| --- | --- |
| `docker-compose.yml` | 生产拓扑；真实 SMTP TLS；不包含 Mailpit |
| `luma.compose.yml` | 生产 Luma sidecar；固定平台域名、cn-edge 路由、受管卷 |
| `docker-compose.staging.yml` | Staging 拓扑；包含持续执行产品冒烟的 `template-smoke` |
| `luma.compose.staging.yml` | 通用 staging sidecar；平台和本地卷固定到 `lae-core` |
| `luma.compose.staging.itool.yml` | 当前共享集群的 staging overlay；平台与本地卷固定到 `manager`，不是 production 默认 |
| `generate-staging-bundle.py` | 仅首次初始化/批准整包轮换：一次性生成 `0700/0600` 的平台 secret、Control principal/broker/signing 与非敏感 Control env bundle；拒绝覆盖 |
| `prepare-staging-release.py` | 普通发布复用既有 bundle，只更新并校验 live cluster、不可变 Analyzer digest 和 runtime placement；不会轮换已有密钥 |
| `docker/{web,api,worker,agent-controller}.Dockerfile` | Luma Repository Import 从仓库根 context 构建的四个平台服务镜像 |
| `docker/agent-runner.Dockerfile` | Builder 执行 `analyze-source` 时使用的固定 digest、rootless LAE Analyzer 沙箱镜像；不属于平台 Compose service |
| `.env.example` | 变量名和非敏感默认值；不包含 secret 值 |

Web 使用 Next.js standalone 产物；本地 Next dev 默认把 `/v1` 代理到 `127.0.0.1:8080`，容器构建阶段显式把经过白名单校验的 `LAE_API_INTERNAL_URL` 固定为 `http://api:8080`，并检查生成的 routes manifest，避免把宿主开发地址烘焙进生产镜像。Python 镜像使用 frozen `uv.lock`、不可变基础镜像 digest 和非 root UID/GID 10001；build/runtime 都把 virtualenv 固定在 `/opt/lae/.venv`，防止 console-script shebang 在搬迁后失效。Compose 中的 Postgres、MinIO 与 Valkey 也都固定到 digest。Luma Repository Import 会给平台源码服务注入构建结果；发布检查必须继续确认最终 job/image 与 exact commit 对应，运行时以 digest 作为唯一事实。

`agent-runner.Dockerfile` 需要单独构建并推送到 Builder 可拉取的受控 registry，记录 registry 返回的 immutable `repository@sha256:...`。Worker 的 `LAE_ANALYZER_IMAGE_DIGEST` 与 Luma Control/Builder 的 `LUMA_BUILDER_ANALYZE_IMAGE_DIGEST` 必须逐字相同；不能一个使用 tag、一个使用 digest，也不能只比较短 SHA。目标 Builder 节点应预拉该 digest，rootless Docker executor 使用 `--pull never` 并在执行前 inspect 本地镜像，避免任务中从不可信或漂移的 registry 拉取 analyzer。

## Luma 边界

staging bundle 会额外生成私密的 `builder-agent-ai.env`。通过 root-only
通道复制到 Builder 后，执行
`sudo scripts/install-lae-builder-ai-env.sh builder-agent-ai.env`。同一个 scoped
token 一端由 Luma secret 注入 `agent-controller`，另一端落在 Builder 的
`0600` systemd EnvironmentFile；runner 永远拿不到模型 provider API key。
staging 要求 AI 诊断，controller/provider 失败会返回 `diagnostic_failed`，
不得进入部署。

启用 controller 后，分析 sandbox 会从 `network=none` 切换为 Docker bridge
以访问 HTTPS controller。runner 不执行租户源码，但这仍是较宽的 egress；
生产需在宿主防火墙或 egress proxy 上收紧为 controller-only。

- 所有服务位于一个 Luma Compose deployment。当前 renderer 会把它们放在同一个 Nomad group、同一节点和共享 network namespace 中；标准 Compose 下通过 service DNS 通信，在 Luma 下 service name 会映射到 `127.0.0.1`。
- `web` 的 `/v1/*` 由 Next 服务端转发到 `http://api:8080/v1/*`，因此浏览器保持同源 cookie；`api` 仍以 `lae-api.itool.tech` 供 CLI/deploy-token 使用。
- Sidecar 把整个 group 固定到名为 `lae-core` 的 cn 节点。该节点必须在 Luma 中真实存在并有足够资源；当前 sidecar 不能表达 workload-class selector。
- PostgreSQL、artifact store 与本地快照目录都使用平台节点自己的绝对路径；Luma 自动把整个 Compose group 固定到该节点。跨节点复制是独立异步备份任务，不能作为平台 allocation 的 mount 或启动依赖。
- OCI registry 不在这个 Compose 里重复部署。它属于 Luma Builder 底座，通过现有 `luma registry serve` 管理；目标节点只拉取 Luma 注入的内部镜像。
- 当前仓库没有可验证的独立 OTel/metrics/Loki/Grafana Luma deployment 资产。本目录不伪造它；`lae-observability` 仍是生产门禁中的单独交付项。

平台 Compose 的 `node: lae-core` 是内部实现，不进入租户产品。用户应用走独立 LAE Runtime API：租户只提交 `region`，Luma Control 首先要求非空、精确的 `LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON`，再按实时节点 readiness/runtime capability、builder-only/control-plane-only policy、volume compatibility、prior allocation affinity 和 Nomad plan 生成内部 placement；缺失或非法 allowlist 会 fail closed，不会回退到通用 Docker 节点。控制面节点只有同时带显式 `runtime`/`lae-runtime` role 才能进入候选；当前 staging 明确把 `manager` 与 `tecent` 加入 allowlist，`builder` 仍不可进入。生产应换成至少两个专用 runner。具体 node ID/IP/failure domain 只保留在 Luma Control/Job 和授权管理员排障证据中；Web/CLI/用户日志只显示 region、service、route 和状态。Luma Dashboard 的 LAE“调度位置”视图已实现候选、preferred node 和实时 allocation 关联，真实 staging 的故障切换、容量负例和 volume affinity 仍待验收。

## 实际落点与当前集群差距

2026-07-14 当前 staging 实施快照是：本机 CLI、Control、manager agent 与在线 fleet `bot/builder/gaojiu/lab/m4/tecent` 均为正式 Luma `0.1.249`；`manager` 是唯一控制面、LAE 平台节点并显式允许兼任租户 runtime，`tecent` 是另一个 staging runtime，构建固定在 `builder`。离线 `blg` 保持 `0.1.175` 且未触碰；`aly` 是过时历史节点，不参与升级、构建、平台或租户调度。这个快照会漂移，任何再次发布前都必须重新执行 `luma version` 与 `luma status --format json`，不能把下表当成永久配置。

| 层 | 当前事实 | 可接受用途 | 生产要求 |
| --- | --- | --- | --- |
| Control + cn runtime | `manager` 约 8C/15 GiB，是唯一控制面；当前决策允许显式兼任 runtime | staging tenant runtime，并由 Nomad plan 扣除 Control/edge/egress 与现有 workload reservation | 生产建议独立 runner，若继续混部必须有 control-plane resource reservation、优先级与故障演练 |
| 第二个 cn runtime | `tecent` 约 4C/3.9 GiB，已有 workload | 受限 staging workload/无容量负例 | 至少两个专用 cn runner，容量/故障域/网络隔离压测通过 |
| 平台 staging | `manager` 为 Linux/amd64，约 8C/15 GiB | 承载 LAE 10-service 单组 staging；主数据位于 manager 本地盘 | 生产改为专用 `lae-core`，本地盘容量/故障恢复和异机备份均需演练 |
| Builder | `builder` 在 `home`，约 8C/16 GiB | 现有内部 build 与隔离测试 | 公网不可信 build 需要专用 rootless builder pool、临时盘和 egress policy |
| Storage | 平台主数据使用 manager 本地 `/srv/luma/lae/staging/*/v2`；tenant volume 仍使用独立 `lae-staging-runtime-nfs` 定义 | 平台运行不依赖 NFS；本地快照可快速恢复 | PostgreSQL PITR、对象/快照异机复制、registry/tenant-volume 备份与 restore drill |

Production sidecar 故意 pin `lae-core`，并把平台卷固定到该节点的 `/srv/luma/lae/production/*/v1`，因此缺少该节点时真实 manager validate 应 fail closed。共享集群 staging 只能使用 `luma.compose.staging.itool.yml`：平台与平台本地卷都在 `manager`，不依赖 `tailscale-relay` 或 NFS 启动；`aly` 不参与任何新部署。租户 runtime allowlist 为 `manager + tecent`，其中 manager 必须显式带 runtime role；`builder` 和未显式 opt-in 的 control-plane 节点继续被排除。

表中的 `home` 仅是现有 Luma Builder 的内部 region，不属于 LAE 租户协议。Web/API/store/CLI 对外统一只接受 `cn | global`，并在 analysis/upload/template admission 阶段拒绝 `home`。

本轮 import 的 registry 与网络路径是明确配置，而不是从节点名推导：Builder 推送与目标节点拉取均使用 `100.66.177.70:5000`。Luma 会检查持久化 Buildx container 的代理环境是否与本次请求一致，不一致时重建 builder，并持久化 AI Agent 配置；但控制面/节点配置幂等性与 Docker daemon restart 后的 CNI/route 自动恢复仍是单独 P0，不能由 Buildx proxy 检查代替。LAE 平台 Dockerfile 的依赖下载步骤也显式清除代理变量，防止租户 build args 污染平台构建；这不等于所有租户构建都必须关闭代理，租户出口仍按其独立策略执行。

## 前置基础设施

1. 一个 Linux/amd64、`region=cn`、名称为 `lae-core` 的专用 Luma 节点。按当前资源上限，至少准备 8 vCPU / 16 GiB RAM，并避免承载租户 build/runtime。
2. 一个 ready 且具备 `docker-build` capability 的专用 Builder 节点，以及已经运行的 Luma 内置 registry。registry 数据也必须使用已注册 storage class；不要把 registry token 写进 Compose。
   Builder 必须预拉经批准的 `agent-runner` immutable repo digest，并把完全相同的值配置到 Worker `LAE_ANALYZER_IMAGE_DIGEST` 和 Control `LUMA_BUILDER_ANALYZE_IMAGE_DIGEST`。
3. `lae-core` 上容量充足且受监控的本地文件系统，以及独立的异机备份目标。平台 sidecar 自己声明固定的本地绝对路径；备份复制任务不得把远端存储重新挂回平台 allocation：

   ```bash
   sudo install -d -m 0750 /srv/luma/lae/production
   # 通过独立 Luma backup/replication deployment 把 PITR、对象和快照复制到异机目标。
   luma registry serve --node <builder-node> --storage-class <registry-storage-class> --port 5000
   ```

4. Luma Control 的 LAE service principals 必须使用 `/opt/luma/control` 下的 private regular files，而不是 management token 或 inline JSON/token：

   - `LUMA_LAE_SERVICE_PRINCIPALS_FILE`：Builder principal、tenant/application scope 和同目录 `tokenFile`；
   - `LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE`：独立 Runtime principal、runtime scopes、`builderPrincipalRefs` 和同目录 `tokenFile`；
   - `LUMA_CREDENTIAL_BROKER_TOKEN_FILE` / `LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE`：匹配 LAE API broker secret；
   - `LUMA_LAE_ADMIN_TOKEN_FILE`：只供 Luma Dashboard 到 LAE internal admin API；
   - 三个 endpoint 均为闭合 HTTPS URL；Runtime 另需 `LUMA_LAE_RUNTIME_STORAGE_CLASS`、Builder registry/plan signing allowlist。

   Bundle 中的 `lae-control.env` 必须以 root-owned `0600` 安装到固定路径
   `/opt/luma/control/control.env`。它是 Luma 自动读取的严格 `NAME=value` 数据文件，
   不是 shell script，不要 `source`；这样 `luma update manager` 后仍会保留上述
   service-principal、broker、signing 与 placement 配置。配置文件和 token file 使用
   `0600`、不可 symlink，Builder/Runtime/management/broker/admin token 彼此独立。
   检查和轮换流程见 [运维 SOP](../../../docs/lae/10-operations-troubleshooting-sop.md)。

5. 生产 SMTP 必须支持隐式 TLS 465。生产 Compose 固定 `LAE_SMTP_SECURITY=tls`；API 启动时还会拒绝 Mailpit/本地域名、保留测试域发件人和缺少用户名/密码的配置。配置校验通过只表示形状有效，发布门禁仍需真实 canary、退信和送达率验证。
6. 将 `.env.example` 复制为被 Git 忽略的 `.env`，填充后按 deployment scope 导入；不要把值贴进命令历史、YAML 或工单：

   ```bash
   luma secret import .env --scope lae-platform
   ```

`LAE_DATABASE_URL` 是完整的 `postgresql+asyncpg://...` secret；它必须与 `LAE_POSTGRES_PASSWORD` 表示同一凭据。认证、Worker state、应用/部署幂等、环境变量 checksum、私有 Git connection 和 billing 使用的 HMAC key 必须彼此独立，均为至少 256-bit 随机值并以 base64 编码。`LAE_ENVIRONMENT_AEAD_KEYS` 与 `LAE_SOURCE_CONNECTION_AEAD_KEYS` 都是版本号到 32-byte AES key 的 JSON/base64 keyring；私有 Git 另有独立的版本化 HMAC keyring。当前版本由 Compose 固定为 `1`；轮换时先加入新 key，再切换 current version，旧 key 需保留到密文重加密完成。Valkey password 也必须是无换行的高熵值，避免生成的临时配置出现额外指令。`LAE_ANALYZER_IMAGE_DIGEST` 必须是固定的 `name@sha256:...`，不能是 tag。

生产 Compose 显式使用 `LAE_BILLING_DRIVER=disabled`：在微信/支付宝真实 provider 尚未接入时，付费端点按 capability 返回 503，但不会让注册、Lite 应用与分析 API readiness 失败。Staging 显式使用 `mock`，价格只来自服务端 `LAE_MOCK_PRICING_JSON`；checkout URL、merchant id、存储 HMAC 与回调签名 key 均需配置，两个 billing key 不得相同。Mock complete route 只在 dev/staging/test 注册，production 即使误配 mock 也会 fail closed。

Production/Staging Compose 都显式设置 `LAE_DEPLOYMENT_WORKER_ENABLED=1` 与 `LAE_LIFECYCLE_WORKER_ENABLED=1`。Lifecycle timeout 当前为 1800 秒；suspend/resume/restart/rollback/delete 使用独立 durable lane。普通 delete 固定 `volumePolicy=retain`，V1 rollback 只接受与 application catalog 的 service/route/volume binding 拓扑兼容的历史 deployment。PostgreSQL 17 migration-backed lifecycle 集成已通过；真实 Luma 故障/恢复演练未通过前，这些配置仍不代表生产验收完成。

## 验证与已执行的 staging import

从本目录运行：

```bash
docker compose -f docker-compose.yml config --no-interpolate
docker compose -f docker-compose.staging.yml config --no-interpolate
../../../.venv/bin/luma compose validate --import-mode luma.compose.yml
../../../.venv/bin/luma compose validate --import-mode luma.compose.staging.yml
../../../.venv/bin/luma compose validate --import-mode luma.compose.staging.itool.yml
```

`--import-mode` 是必须的，因为四个源码服务在 Repository Import 后才会得到 `image:`。当 manager 中还没有 `lae-core` 或两个 storage class 时，真实上下文校验应当失败；先修复基础设施，不要把 sidecar 改成 unmanaged volume 或随便换到已有业务节点。

不登录真实 manager 的结构和 renderer 契约测试：

```bash
cd ../../..
.venv/bin/python -m unittest tests.test_lae_luma_deploy_assets
```

五个镜像本地构建完成后，再验证非 root 用户、Python console-script shebang/可执行性、Analyzer runner 和组件入口：

```bash
cd lae
docker build --platform linux/amd64 -f deploy/luma/docker/web.Dockerfile -t lae-web:asset-test .
docker build --platform linux/amd64 -f deploy/luma/docker/api.Dockerfile -t lae-api:asset-test .
docker build --platform linux/amd64 -f deploy/luma/docker/worker.Dockerfile -t lae-worker:asset-test .
docker build --platform linux/amd64 -f deploy/luma/docker/agent-controller.Dockerfile -t lae-agent-controller:asset-test .
docker build --platform linux/amd64 -f deploy/luma/docker/agent-runner.Dockerfile -t lae-agent-runner:asset-test .
sh deploy/luma/smoke-images.sh
```

2026-07-11 的本地完整 staging Compose 验证结果：`web`、`api`、`worker`、`agent-controller`、`postgres`、`minio`、`artifact-init`、`valkey`、`mailpit` 全部 healthy；注册邮件、验证码、一次展示默认 deploy token、token verify、application/catalog/admin、mock billing 和 CLI E2E 通过。真实 MinIO 以 Worker 最小权限 credential 完成 private object put/head/get，允许 Origin 的 preflight 返回精确 CORS header，非允许 Origin 不返回该 header；聚合容器日志为 0 个 secret pattern、0 个 traceback。

当前 MinIO Community 版本不支持依赖 per-bucket `PutBucketCors` 的初始化流程。本部署使用专用 artifact-store，并以精确的 `MINIO_API_CORS_ALLOW_ORIGIN` 配置 server-wide CORS：staging 为 `https://lae-staging.itool.tech`，production 为 `https://lae.itool.tech`，禁止 `*`。因此该 MinIO 实例不能与需要不同浏览器 Origin 的其他产品混用。

在这些本地证据之上，当前共享集群已经执行真实 Luma staging import：9 个构建镜像已推送到 Builder registry，10 个 service 的 Nomad job 已注册且 allocation 健康，DNS、TLS 与 route 已发布，Web、API、Agent、artifact 和 Luma Control 探针均为 HTTP 200；当前平台使用 exact ref `35591c4e789f7d7bec60614d427fed05023b373a`。默认 deploy token、CLI、模板和 analysis 已有冒烟证据；最新 provider-backed 四态 verdict、真实邮箱送达以及租户 runtime deploy/lifecycle 仍需做纵向验收。

`luma import` 没有 preview-only 模式：它会真实 clone、build、push 并继续 deploy。以下命令是本轮已经执行的 staging 发布形状，仅用于可控重放；再次执行前必须先确认 release、bundle、registry、现有 job/volume 和回退点，并显式选择 staging sidecar，不能依赖自动发现：

```bash
REPO=https://github.com/LiuTianjie/luma.git
CANDIDATE_REF=<immutable-staging-or-release-tag>
BUNDLE_DIR=<private-staging-bundle-directory>

../../../.venv/bin/luma import "$REPO" \
  --ref "$CANDIDATE_REF" \
  --build-node builder \
  --compose-sidecar lae/deploy/luma/luma.compose.staging.itool.yml \
  --env "$BUNDLE_DIR/lae-platform-staging.env" \
  --registry-host 100.66.177.70:5000 \
  --format ndjson \
  --timeout 3600
```

`--compose-sidecar` 只接受仓库内规范 POSIX 相对路径，不能与 `--manifest` 同用。CLI 会先检查 Control capability，Control 再要求 Builder 回显完全相同的路径；绝对路径、`..`、symlink escape、缺失或非法 sidecar 都 fail closed，不能回退到 production `luma.compose.yml`。完整发布顺序、manager/fleet 升级和回退点见 [部署、升级与回退手册](../../../docs/lae/11-deployment-and-upgrade.md)。

执行成功后检查 build 记录、6 个镜像 commit/digest、9 个 Nomad task、三个公网 HTTP route 和内部健康状态。`artifact-init` 不是退出即成功的一次性 Nomad task：当前 renderer 把 Compose service 统一渲染成长运行 task，因此它完成 bucket/user/policy 初始化后写 readiness 文件并休眠。Staging 为它设置 `memory limit=512 MiB`、`memory reservation=256 MiB`，避免 `mc` 初始化峰值被过低的 reservation/limit 误判或 OOM；任何 production 参数都必须满足 reservation 不高于 limit，并经过真实 allocation 峰值验证。

## 数据保留与恢复

- `initialize: empty` 表示这些 path 只用于首次创建的新数据集。已有生产数据迁移必须先完成校验，再显式改为 `adopted: true`；不能用 `initialize: empty` 绕过迁移。
- `luma service remove lae-platform` 默认保留受管数据。禁止在普通回滚/下线中使用 `--delete-storage`。
- PostgreSQL 必须在生产前接入 WAL/PITR 到与主数据盘不同的备份目标，并完成 restore drill。本地快照目录不是异机备份。
- Artifact store 必须配置跨故障域复制/备份、bucket lifecycle 与按 tenant 的保留策略。Valkey 在此是易失 cache，不挂卷，也不能成为业务真相。
- 公开 Web 的 preview flow 只允许保留的 `.invalid` 测试身份；普通真实邮箱的验证码不能由 Web 读取或返回。Production Compose 固定关闭 preview mode，真实注册必须由可用 SMTP/API provider 投递。

## 当前硬阻塞项

1. 当前 live cluster 没有 production `lae-core`；`aly` 已退出 live 清单，若再次出现只按 stale 历史注册清理。Production sidecar 因而继续 fail closed。`luma.compose.staging.itool.yml` 明确使用 `manager` 本地盘，不代表 production topology。
2. Staging runtime 明确使用 `manager + tecent`：manager 需要显式 runtime role，二者仍由正向 allowlist、实时 readiness 和 Nomad plan 收敛。专用 production runner pool、无容量、drain、节点故障重调度、volume affinity，以及 admin placement 与真实 allocation 的关联仍需 Luma staging 演练。
3. Builder/Runtime principal files、Git/object broker、LAE admin proxy、plan signing、registry、Analyzer repo digest 与 runtime storage class 已有 staging 配置；live 候选与平台 ref `35591c4e789f7d7bec60614d427fed05023b373a` 仍须完成 controller scoped token、Builder `0600` EnvironmentFile、Worker/Control analyzer digest 三端一致及真实 provider-backed analysis 验收。不能用 management token、inline secret 或 mutable analyzer tag 简化。
4. S3 artifact/upload、MinIO policy/CORS、API/Worker 分权 credential 和一次性 object redemption 已完成本地真实 MinIO 最小权限与 CORS 正反例；Luma staging 的浏览器上传、恶意 ZIP、cancel/replay、Builder download、artifact ingest 和日志/state 无 URL/key E2E 仍是上线门禁。
5. 当前平台 Compose renderer不把标准 Compose `healthcheck`/`depends_on` 全部转换成严格 Nomad readiness/启动顺序。Runtime API 会生成 HTTP check，但平台服务自身仍必须重试依赖，并验证故障/恢复行为。
6. Agent Controller 已进入 live staging，并实现 OpenAI-compatible provider、闭合 schema、Knowledge Pack 版本握手、认证、限流/并发/熔断与结构化诊断；staging 使用平台侧 ARK 映射，用户只需 LAE deploy token，ready 当前显示 `configured=true`。生产仍缺用户同意与审计、私有入口、task-bound 单次 lease、controller-only egress、DNS rebinding/host allowlist 和成熟 ASGI/WAF 限流；Controller 的 200 ready 不能替代真实 provider-backed analysis 与四态 verdict 验收。
7. API 启动时执行 Alembic migration 是单副本 MVP 过渡方案。扩到多副本前必须提供 Luma init/migration job 或数据库 advisory lock，避免并发 migration。
8. 当前 staging 的 PostgreSQL、MinIO 与本地快照位于 manager 本地盘，已解除 NFS 对平台启动和重启的耦合；仍必须补齐磁盘容量/损坏告警、PostgreSQL PITR、对象与快照异机复制，并完成整机丢失 restore drill。
9. PostgreSQL PITR、MinIO/registry/volume backup+restore、容量告警、引用安全 GC、独立观测栈和租户 runtime 真实纵向 E2E 尚未完成；当前平台 10 service、DNS/TLS/route 和基础用户/Git 分析冒烟已经通过。生产还缺专用 `lae-core`、至少两个专用 runtime runner、真实可用 SMTP 凭据和真实微信/支付宝等 payment provider；mock 不能进入 production。
10. Runtime/server-side policy 仍需在真实节点证明 read-only rootfs、cap drop、no-new-privileges、PID/ephemeral disk、管理网/metadata/Tailscale 阻断和滥用治理，不能只依赖 Compose 表面字段。
11. lifecycle API/Worker、结构化 update-check、更新/失败保旧、回滚、删除保卷和日志/指标已有代码、自动化测试与 PostgreSQL 17 集成证据；placement admin 精确审计视图也已实现。两者仍需真实 Luma staging 场景验收。以 [实施状态](../../../docs/lae/08-implementation-status.md) 为准。

这些门禁未清零前，只允许进行结构校验、镜像构建验证和隔离 staging 演练，不执行 production live deploy。

用户操作见 [LAE 用户指南](../../../docs/lae/09-user-guide.md)，值班排障、placement 可见性、恢复、轮换和 GC 见 [运维与排障 SOP](../../../docs/lae/10-operations-troubleshooting-sop.md)。
