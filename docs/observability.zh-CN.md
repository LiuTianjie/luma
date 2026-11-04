# 可观测 {#observability}

Luma 提供节点与容器采样、当前任务队列、资源历史、运行事件、持久化告警，以及可恢复读取的应用日志。这些观测不等同于终端用户可用性检测。部署和重启时的公共路由检查是独立能力。


## 控制台导航 {#console-navigation}

可观测分为告警事件、应用监控、资源指标、日志、告警规则和通知渠道，各页使用简洁的独立标题。引擎摘要位于告警事件页；通知渠道关注配置与发送历史。内置告警页每 15 秒刷新，资源历史每 30 秒刷新。操作流程及路由关系与连通性探测的区别，见[控制台指南](dashboard-guide.md)。


## 应用告警（`luma-observe`） {#application-alerts-luma-observe}

Control 控制台提供节点与容器采样，实时 allocation 日志仍通过 Control 按需代理。应用 HTTP 5xx、延迟（p90/p95/p99）、Nomad 失败 allocation、重启风暴、链路追踪，以及可搜索的 stdout/stderr，由可选的 [`observe/`](../observe/) Compose 应用提供。可用 Luma 部署它；不部署也不影响 Control 运行。运维人员通过 **控制台 → 可观测 → 应用监控**查看，它在 Control 域名的 `/grafana` 嵌入 Grafana。部署 observe 后，HTTP、Nomad 和 trace 序列立即按 Luma 应用/stack 名标记，无需应用单独加入。失败指标只统计 Nomad 仍希望运行的 allocation，不是累计失败次数。该组件使用主机网络和回环监听器：Prometheus 为 `127.0.0.1:8082`，OTLP 为 `127.0.0.1:4318`，Tempo 为 `127.0.0.1:3200`。应用任务携带 Control 签发的 bearer token，向 Manager Tailscale mesh 的 4319 端口发送 trace。Control 使用主机网络，observe 存在时查询 `http://127.0.0.1:8428`。`127.0.0.1:3100` 上的 Grafana 仅用于本地调试。可观测栈自带的规则在 vmalert/Alertmanager 中评估，不经过 Control SQLite；另外，Control 的控制台应用告警预设读取 VictoriaMetrics，并将事件和通知记录保存在 Control SQLite。Trace 保留 3 天；日志和指标保留 15 天，日志最多占用 4 GiB。Manager 磁盘剩余不足 2 GiB 时，日志和指标停止写入。

打开 `/grafana` 前先在 `/dashboard/` 登录。默认情况下，Traefik 每次访问 Grafana 都会请求 Control 校验控制台会话或管理令牌；未认证请求返回 401。Grafana 无需二次登录，登录保护的入口依赖 Control 可用。详见[登录与 Passkey](dashboard-guide.zh-CN.md#sign-in-and-passkeys)。

<a id="external-dashboard-sharing"></a>
### 对外分享看板

在 Control 控制台打开 **可观测 → 观测设置 → 允许公开看板链接**。
开关默认关闭。开启时会弹出密码输入框，必须设置 8～128 个字符的分享密码；取消则保持关闭。
Control 只保存加盐后的密码哈希，数秒内生效，无需重启 Observe 或业务应用。
有密码的设置在 Control 重启后保留；升级时，旧的无密码分享配置会自动关闭，
原免鉴权路由会被移除，需要设置密码后重新开启。

打开开关后，在需要分享的 Grafana 看板中开启 **Share externally**，分享
`/grafana/public-dashboards/<access-token>` 链接，并将分享密码提供给访客。
访客先在独立页面输入密码，通过验证后才加载看板数据，无需 Luma 账号。
打开 Control 开关本身不会公开任何看板；Grafana 会检查分享是否启用，并且只执行该看板保存的面板查询。
停用或删除单个分享后，后续读取立即受到限制；关闭 Control 开关后，所有公开链接恢复要求
Luma 登录。选择 **修改密码** 或关闭分享，会使已有访客会话失效；重新开启时必须再次设置密码。
访客会话有效期为 8 小时，不能访问控制台或管理接口。已有控制台会话和管理令牌仍可正常使用。
路由不会缓存看板页面和查询结果。

白名单仅包含公开看板页面、它的只读公开 API（面板查询使用 POST）和静态资源。
看板搜索、普通 `/d/` 页面、Explore、数据源代理、`/api/ds/query` 和分享管理接口
仍要求 Luma 登录。公开页面、API 和静态资源路由先由 Control 校验访客或管理会话，
再移除发往 Grafana 的 Cookie 和 Authorization；Control 不可用时，这些入口也无法访问。
插件或 Grafana 升级若需要额外静态路径，应单独核实，不要放开整个 `/api/`。

公开页面/API 按域名共同限制为每秒 5 个请求、突发 32 个、最多 16 个在途请求，
查询请求体最多 16 KiB，上游响应头超时 15 秒；静态资源使用独立路由。
过载时返回错误，不累积无界请求队列。指标继续使用 VictoriaMetrics 已有的结果缓存、
查询并发和内存上限，无需增加查询后端、数据库或业务缓存。
密码验证每个客户端每分钟最多尝试 5 次，全局最多 30 次，超过后返回 429。
开启后验证密码输入、面板查询、无效或撤销链接、修改密码后的旧会话失效，以及受保护接口。
管理员也可通过已认证的 `GET` / `POST /v1/dashboard/observe/settings` 读取或修改设置，
开启或修改密码时，请求体为
`{"grafanaPublicSharingEnabled": true, "grafanaPublicSharingPassword": "你的分享密码"}`；
关闭时为 `{"grafanaPublicSharingEnabled": false}`。响应仅返回开关状态，不返回密码或哈希。
Control 自动管理已有 Traefik 路由目录里的 `luma-observe-grafana-public.yml`，
无需用户手动编辑文件。内置白名单适配 Grafana 11.5.x。

在 `observe/` 中沿用已记录的部署方式。AMD64 Manager 首次本地构建可执行 `luma build local . --platform linux/amd64`；其他主机应按实际 Manager 架构选择平台。首次部署不需要通知密钥。可观测栈的 Alertmanager 通知需要添加 scoped secret 和对应的 Compose 环境变量引用，再重新部署；控制台规则则使用内置飞书渠道，详见[通知配置](#built-in-alert-rules-and-notifications)。当前 CLI 支持回环 metrics/OTLP 标志后，刷新 Traefik，让 RED 规则有可抓取目标。通过 **控制台 → 可观测 → 应用监控**查看。图表按 Luma 应用展示 Traefik HTTP 请求率、5xx、p90/p95/p99 延迟和 Nomad 健康状态，不是读取每个容器的 `/metrics`。Traefik 直方图桶为 `0.05,0.1,0.25,0.5,1,2.5,5,10` 秒，以区分这些分位数。启用 observe 后，后续 Luma 部署会自动注入官方 OpenTelemetry 环境变量：`OTEL_SERVICE_NAME`、`luma.stack` / `luma.task` / `luma.region`，以及指向 mesh 监听器的 OTLP HTTP 配置。已包含 OpenTelemetry 发行版的应用无需 Luma SDK 即可发送 span。首次启用 observe 后应重新部署已有应用。Trace 导出失败不会阻塞业务；采集器保留 HTTP 5xx、OTel 错误、超过 1 秒的 trace，并对其他请求进行 10% 基线采样。采集器通过 Go 内存目标、memory limiter 和有界队列控制内存，持续过载时会拒收或丢弃遥测数据。不要通过 Manager 公网接口向外发送原始访问日志或 trace。

### Trace 存储容量与升级 {#trace-storage-sizing-and-upgrades}

内置 Tempo 2.10.10 包含上游 Parquet 行池内存修复。默认调度预留为 1 GiB、容器上限 3 GiB、Go 内存目标 2 GiB，Parquet 行组目标为 25 MB。首次部署前，应在 Control、Traefik 和其他 observe 服务之外，为 Tempo 的上限留出内存。过低的容器上限可能导致文件页反复回收和重读，即使没有 OOM 或磁盘写满也会拖慢磁盘。查询缓冲设置不能限制压实内存。Tempo 的 72 小时保留期和写入限速不等于磁盘配额，它也没有剩余磁盘空间不足时停止写入的保护。

从更新后的源码重新构建、部署完整 `observe/` 栈，才能拿到这些默认值；只更新 CLI/Control 或重启旧 job 不会更新 observe。曾手动关闭 Tempo 的实例还需要恢复 collector 的 Tempo exporter。保留现有卷，过期块由后台异步清理。详见[首次部署、升级与验证步骤](../observe/README.md#first-deployment)。CI 使用打包的配置和内存限制验证空存储启动、collector 投递，以及 Tempo 重启后的持久化 trace 查询。

## 应用接入 {#application-integration}

原生 Metrics、独立机器凭证、网格外 OTLP 接入及应用自行维护的看板/规则，见[标准应用观测接入](observe-applications.zh-CN.md)。

部署 [`observe/`](../observe/) 即表示在集群级启用。此后新部署的 Luma job 自动获得 OTel 配置。只重启旧 job 的进程不够，必须重新渲染 Nomad job。应用可选择 [`luma-observe-sdk` Python 包](../sdk/python/docs/integration.zh-CN.md)，也可直接使用官方 OTel SDK；语言 agent 由应用镜像自行安装。

无需修改应用即可获得：

- 按 Luma 应用名标记的 Traefik HTTP RED（请求率、5xx、p90/p95/p99）。
- 按 Luma 应用名标记的 Nomad 运行数、当前失败数和重启数。
- 存入 Tempo 的公共 HTTP（`cn-edge` / `external-edge`）入口 span。

重新部署后，只有镜像已经支持 OpenTelemetry 时，才会获得：

- 导出到 Tempo、带 `luma.stack` / `luma.task` / `luma.region` 的进程 span。
- 在 **控制台 → 可观测 → 应用监控 → Traces** 或 Grafana `/grafana/d/luma-traces` 查看。

仍不会自动获得 nginx、静态 Go 程序或无 OTel SDK 镜像的进程追踪，也不覆盖未经过 Traefik 的流量、请求正文、SQL 文本或每次成功的快速请求。5xx 和超过 1 秒的 trace 会保留，其余按 10% 采样。节点 agent 访问 Control 的长连接（`/v1/node-agent/`、`/v1/terminal/`）本来就会超过 1 秒，不进入 trace 存储，只计入 Traefik 指标。导出失败不会阻塞业务。

### 手动添加 span {#manual-spans}

不要自行设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 或 bearer 请求头，Control 已经注入。使用官方 OpenTelemetry API，并保持导出器失败时不阻塞业务。

在镜像中安装官方发行版和 OTLP exporter，例如 Python：

```text
opentelemetry-distro
opentelemetry-exporter-otlp
```

然后通过 `opentelemetry-instrument`（或 Node/Java 对应方式）启动，让 SDK 读取注入的环境变量。可在业务步骤外围增加 span：

```python
from opentelemetry import trace

tracer = trace.get_tracer("orders")

def charge(order_id: str) -> None:
    with tracer.start_as_current_span("charge_card") as span:
        span.set_attribute("order.id", order_id)
        provider.charge(order_id)
```

Node 示例：

```javascript
const { trace } = require("@opentelemetry/api");
const tracer = trace.getTracer("orders");

async function charge(orderId) {
  return tracer.startActiveSpan("charge_card", async (span) => {
    span.setAttribute("order.id", orderId);
    try {
      return await provider.charge(orderId);
    } finally {
      span.end();
    }
  });
}
```

不要把密钥、令牌或完整请求正文写进 span 属性。自定义 span 名会归属同一进程的 `luma.stack`。镜像没有 OpenTelemetry SDK 时，这些调用不会产生效果，除非添加相应发行版。

## 控制台与日志 {#dashboard-and-logs}

**控制台 → 可观测**默认打开告警事件。指标有独立页面；应用监控的日志视图通过 Grafana Explore 查询 VictoriaLogs（保留 15 天）。规则与通知渠道分别有列表和编辑地址。存储治理位于 **基础设施 → 存储 → 容量与回收**。

指标页显示实际采样时间范围和保留期，区分缺失、过期和查询失败，并在采样缺口处断开曲线。可选择服务查看资源历史，或打开按 stack 筛选的可观测日志。

可观测日志来自 Nomad allocation 的 stdout/stderr，由 luma-observe 发送到 VictoriaLogs。按 `app`（Nomad job / Luma stack）筛选。Traefik JSON 访问日志是 `traefik` job 的 stdout，可用 LogsQL 的 `unpack_json` 解析。Nomad 帧没有事件时间戳，因此日志行时间是观测时间。`luma app logs` 和应用详情的实时日志仍通过 Control 跟随 Nomad 文件，只提供有限片段，并非 VictoriaLogs 的持久化日志存储。

资源数据按 30 秒桶保留，与上报某个服务的节点数量无关。`LUMA_METRICS_HISTORY_POINTS` 默认 720（约 6 小时），范围为 60 到 10000。资源历史独立保存在 Control 状态目录下有界的 `metrics-history.json` 中；长期排查和独立故障检测仍适合外部采集。备份范围见[控制面存储与恢复](control-storage.md)。服务汇总只覆盖已上报节点，缺失贡献在 180 秒后过期，不能证明所有副本均已上报。

## 内置告警规则与通知 {#built-in-alert-rules-and-notifications}

控制台告警页管理规则、通知渠道和事件。规则、事件状态变化及通知发件箱持久化在 Control SQLite 中。评估器使用已上报给 Control 的采样和任务状态；创建资源规则不会启动公共 HTTP 探测。

可用预设包括服务容器内存、节点心跳时长、持续 CPU/内存/磁盘/inode 使用率、排队任务时长、每个应用最近一次失败构建；部署 luma-observe 后还支持应用 p95 延迟、HTTP 5xx 比例和 Nomad 失败 allocation。Observe 采样通过 VictoriaMetrics 即时 PromQL 查询获得；observe 不可用时，相关规则保持上次状态（`noData: keep`），不会因此触发。阈值和持续时长可配置，条件持续达到要求后，事件才由 pending 变为 firing；健康观测会解除事件。每条规则与目标仅维护一个活动事件，可设置持续故障的重复通知间隔。

**服务容器内存接近上限**（`app.memory`）取应用各容器、各副本的 Docker 内存使用率最大值，Compose 包含整个栈的所有任务，不会平均掉单个高占用容器。默认条件为 **超过 80% 持续 2 分钟**。在告警规则页选择此预设、目标应用（例如 `luma-observe`）及通知渠道，保存启用即可。新安装自带预设，不会自动创建规则或渠道；已有集群升级 Control 即可获得，不要求重新部署 observe，也不要求更新已经上报容器统计的 Agent。

此预设直接使用 Agent 采样，不依赖 observe。Control 在数据库事务外将 Nomad allocation ID 映射到应用名；心跳和容器采样均须在 120 秒内，且内存上限为正数。已知副本缺少有效采样或应用身份无法解析时，不会将其余低占用容器当成恢复依据；已有新鲜容器超过阈值时，仍可确认超限。`noData: keep` 会保留已触发事件，并中断 pending 的连续计时。Linux 下 Docker 使用量扣除了不活跃文件缓存，因此它是针对 Docker 所报内存上限的提前预警，不等同于磁盘压力或完整 cgroup 缓存用量；主机内存和磁盘告警仍应保留。

### 新集群首次配置任务告警 {#first-task-alert}

这些通用预设随公开 Control 镜像及 `luma-infra` 发布包提供，完成标准首次安装后，空告警数据库会自动创建告警表，无需旧集群、私有配置文件或 SQL 初始化。新集群不会预置任何其他项目的规则、接收群或凭证。

1. 按 [Observe 部署说明](../observe/README.md) 部署共享观测栈；仅升级本地 CLI 不会部署 Observe 或升级 Control。
2. 应用接入 [公开 Python SDK](../sdk/python/README.md)，在每个进程初始化对应适配器，显式设置 `OTEL_METRICS_EXPORTER=otlp`。Control 默认值仍为 `none`。保留注入的 OTLP 地址、认证头及 `luma.stack` 资源属性；仅有 Trace 不代表任务指标已经上报。
3. 用 `luma observe tasks APP --view overview --since 1h --format json` 确认心跳和需要的指标有采样。`service_name` 是实际上报的 OTel 服务名；不要猜测它一定等于 Compose 服务名。排队等待目前来自 Taskiq 指标，其他框架只有明确上报相同指标才适用。
4. 在 **可观测 → 通知渠道** 配置自己的飞书应用和群，再在 **规则 → 新建** 选择任务指标、应用、服务、阈值及该渠道。心跳规则选择“采样缺失时触发告警”；服务停止监测时停用对应规则。
5. 查看最近评估时间、事件和投递记录。渠道已配置不等于消息已送达，以投递状态为准；需要验证群消息时再主动发送渠道测试。

首次无任务执行时，排队分位数可能没有数据；不得将未上报或采集故障当作零。资源告警不依赖 SDK，仍可单独使用。

Control 提供四种任务 SDK 指标预设：`app.task_heartbeat_age`、`app.task_queue_wait_p95`、`app.task_telemetry_dropped` 和 `app.task_tracking_overflow`。在规则页选择单个应用，并按需填写心跳表中的 OTel `service_name`；心跳规则必须指定预期持续运行的服务。每条规则在 `taskQuery.service` 保存服务范围，与部署服务名可能不同。每集群最多 20 条任务指标规则，查询在 SQLite 事务外执行，并复用相同范围的查询。

- 心跳取指定服务所有进程中的最新值，默认超过 120 秒持续 180 秒；推荐 `noData: alert`，使所有序列消失也能触发。心跳缺失可能来自服务或采集链路，不能直接判定业务进程死亡。服务退役时停用对应规则。
- 排队 P95 按服务、队列分别计算近 15 分钟已消费任务，再取最大值；每组至少 20 次样本。观测到的低流量不触发延迟告警，完全缺失采样不会补零。它不代表实际队列积压或尚未消费任务的等待时间。
- 遥测丢弃统计近 15 分钟 SDK 生命周期日志丢弃和埋点异常；跟踪溢出统计有新鲜心跳进程中当前未被跟踪的活跃任务数。这些指标不覆盖全部 OTLP 或 Collector 丢弃。

已有 SDK 指标通过现有 VictoriaMetrics 查询，不需要重新部署业务或 observe。规则默认不创建、不绑定通知渠道。指标恢复只表示当前条件回到阈值以内，历史失败任务是否补偿成功须核对同一任务的后续执行记录。

缺失数据有明确策略：`keep` 保持已有触发状态，不把缺失当作恢复；`alert` 把缺失视为待评估条件。CPU 规则使用新鲜的 Linux 主机 CPU 采样，不把 macOS 负载均值解释为 CPU 百分比。

构建失败的飞书卡片和告警详情包含可取得的服务名、构建编号、镜像完整引用（Compose 按服务列出）、目标镜像仓库、构建节点、源码仓库与提交、失败阶段、失败时间和原因摘要，并提供构建日志入口。镜像构建、上传和部署阶段的失败通过阶段名称区分；未生成或未保存的镜像引用标为“未记录”，不会把仓库名冒充完整镜像标签。长错误保留开头和末尾，完整内容仍在构建日志中。

“告警持续”是已有失败的重复提醒，不代表又发起了构建。告警保存关联失败构建的快照，后续构建成功、告警恢复后仍可追溯。升级 Control 后，仍在触发且构建记录可用的旧告警在下一次评估时补充详情，后续通知使用新格式；已经发送的飞书消息不修改。Dashboard 与通知实现一起打包在 Control 镜像中，仅此功能不要求更新 Builder 或其他节点 Agent。

告警状态与通知控制分开：`pending` 是等待持续条件，`firing` 是已触发，`resolved` 是观测到恢复，`closed` 是手动或管理关闭。停止提醒和静默都不会把异常标记为健康，也不会发起或重试构建。

| 操作 | 当前告警 | 通知与后续告警 |
| --- | --- | --- |
| 停止提醒（原“确认”） | 保留在当前列表，记录确认时间 | 停掉尚未发送的首次通知和重复提醒；曾通知过的告警恢复时仍通知 |
| 关闭告警 | 移入“已关闭”，保留详情与时间线 | 停掉本次通知；后续新构建失败仍会产生新告警 |
| 维护静默 | 继续评估并记录异常 | 暂停全局或指定规则的通知，到期自动恢复；手动渠道测试不受影响 |
| 自动恢复 | 移入“已恢复” | 必须实际观测到健康；开始重试、取消构建或缺失数据均不算恢复 |

构建告警按失败构建分开：新构建排队或执行时，原失败记录保留并等待结果；新构建失败时，原告警以“新失败已接替”关闭，新告警独立通知，不继承旧告警的确认状态；后续构建成功才恢复当前告警。重试被取消时，原失败仍有效并继续按原设置提醒。手动关闭的构建记录会持久保留抑制，即使重启 Control、重新启用规则或清理历史，也不会重复出现同一次失败。其他指标的手动关闭在实际观测到恢复后解除，之后的新异常可以重新告警；修改规则条件也会重新评估这些指标。

列表和详情页使用相同操作。管理 API 为 `POST /v1/alerting/incidents/{id}/ack` 和 `POST /v1/alerting/incidents/{id}/close`，支持 `expectedBuildId` 防止操作过期页面时影响新失败。关闭不发送恢复通知。停止提醒、关闭和静默均无法撤回已发出的 HTTPS 请求。修改规则条件会以规则变更事件关闭旧活动告警；删除规则同时移除该规则的关闭抑制。隐藏某个浏览器里的总览卡片只影响显示，不影响告警。

内置飞书渠道使用应用机器人。在渠道表单填写 **App ID**、**App Secret** 和目标**群聊 ID**。启用应用机器人能力，授予并发布 `im:message:send_as_bot` 权限，将机器人加入该群。保存渠道后关联规则，再显式发送测试通知。渠道测试会向配置的群发送真实消息，并绕过静默。

Luma 自动申请 tenant access token，仅缓存于进程内存，并在过期前刷新，运维人员无需配置该 token。读取渠道仅返回 App ID、群聊 ID 和 `appSecretConfigured`，不会返回 App Secret。App Secret 保存在私有 Control SQLite 中，属于受保护的备份范围。编辑时省略 App Secret 会保留原值；替换该值即可轮换凭据并获取新的缓存 token。

评估独立运行，约每 15 秒一次。单独的发送循环约每秒检查持久化发件箱，慢提供方请求不会暂停评估。待发送、已发送、重试中和失败均可查看。网络及限流失败会退避重试，最多 8 次；无效凭据、缺少权限和群不可访问会给出可操作的错误分类。每个 HTTPS 请求超时为 8 秒，总发送预算为 16 秒。

成功响应表示飞书已接受消息，不代表有人阅读。发件箱重试使用稳定的消息 UUID 供提供方去重；这仍是至少一次投递，而非无限期的严格恰好一次保证。不要为了再次触发而先修改规则，应先检查发送历史。已恢复/关闭事件和最终发送记录在摘要保留期（默认 90 天）后纳入经审查的存储保留计划；活动事件和未完成发送受保护。见[控制面存储](control-storage.md)。

`/v1/alerting/` 下的管理 API 需要管理令牌，纯指标令牌不能管理告警。不会自动创建通知渠道。Control 必须保持可用，才能评估并发送自己的告警；应保留独立 Prometheus 抓取或外部探针检测 Manager/Control 故障。内置资源告警不探测公网 URL。应用 p95、5xx 比例和失败 allocation 预设读取 luma-observe，不能替代外部 Control 宕机探测。

## 主机磁盘采样 {#host-disk-samples}

更新后的节点 agent 采样 `/opt/luma` 所在文件系统；该目录不存在时采样 `/`。可在节点 agent 服务环境设置 `LUMA_METRICS_DISK_PATH`，指定其他绝对路径。控制台显示采样路径；它不代表所有挂载点、远端 NFS 服务器容量或 macOS Docker VM 容量。文件系统缺失或 inode 计数不受支持时显示不可用，而非零使用率。

磁盘使用率从可用容量中排除保留块，与常见 `df` 语义一致；可用字节数也排除保留块。旧 agent 仍可工作，但更新后才会上报这些新增采样。

## Prometheus 端点 {#prometheus-endpoint}

`GET /v1/metrics` 输出 Prometheus 文本格式。它读取持久化心跳快照，不联系 Nomad 或 agent，也不修改控制状态。支持管理令牌；远程采集器应使用独立只读指标令牌。

内置 luma-observe 采集器与 Manager 共享主机网络，无令牌抓取 `http://127.0.0.1:8080/v1/metrics`，与 Traefik `:8082` 使用相同的回环信任边界。请求带有 `Authorization` 请求头时仍会验证。远程 Prometheus 抓取仍需独立令牌。

在 Manager 的 `/opt/luma/control/metrics-token` 中配置至少 32 个 ASCII 字符的随机令牌。文件必须是 Control 进程可读的私有普通文件（通常权限为 0600），不能是符号链接。现有 `/opt/luma` 挂载使容器可见此默认路径。每个请求都会重新读取，支持原子轮换而不重启 Control。系统不会自动创建令牌。

显式配置 `LUMA_METRICS_TOKEN_FILE` 会覆盖默认路径。自定义安装应在进程/容器内暴露该绝对路径，并在对应环境设置变量；Manager shell 中的任意环境变量不会自动传给 Nomad job。

安全地把令牌复制到 Prometheus 采集器凭据文件。此令牌只被 `/v1/metrics` 接受，不能查询控制台、读取应用日志、部署或重启。它应与其他所有 Luma 令牌区分。端点输出包含基础设施名称与区域标签；远程抓取需要认证，同机 luma-observe 抓取仅限回环。

示例 [Prometheus 抓取配置](./examples/monitoring/prometheus.yml)和[告警规则](./examples/monitoring/luma-alerts.yml)是可选模板。替换主机名与凭据路径，使用当前 Prometheus 版本检查，并配置 Alertmanager 目标后才会有通知。外部模板独立于上述 Luma 内置规则和飞书渠道。应用 Prometheus/Alertmanager 配置是单独的基础设施变更，不会创建 Luma 通知渠道。

主要指标：

| 指标 | 含义 |
| --- | --- |
| Prometheus `up{job="luma-control"}` | 抓取成功，包括连通性与认证，不代表应用健康。 |
| `luma_node_agent_up` | 120 秒内收到心跳，不代表符合 Nomad 调度条件。 |
| `luma_node_heartbeat_timestamp_seconds` | Control 最近收到的心跳时间。 |
| `luma_node_metrics_timestamp_seconds` | Control 最近收到的主机采样时间。 |
| `luma_node_cpu_used_ratio` | Linux 主机 CPU 比例，特意排除 Darwin 负载估计。 |
| `luma_node_load1` | 主机一分钟负载均值。 |
| `luma_node_memory_*` | 主机内存容量、可用量和使用比例。 |
| `luma_node_filesystem_*` | 带路径标签的采样文件系统容量、可用量、使用量和 inode 比例。 |
| `luma_service_cpu_cores` | 按服务和节点汇总的容器 CPU，单位为核。 |
| `luma_service_memory_bytes` | 按服务和节点汇总的容器内存。 |
| `luma_service_observed_containers` | Agent 快照中存在的容器数，不是期望副本数。 |
| `luma_node_unresolved_containers` | 缺少持久化 job 身份、未计入服务指标的容器。 |
| `luma_tasks` | 按任务类型统计的当前排队/运行记录，不是累计计数。 |
| `luma_task_queue_oldest_age_seconds` | 有时间记录的最老排队任务；不同类型可能指向同一流程，不能直接累加。 |

主机和容器资源序列在 120 秒无新采样后过期，会从输出中省略，而不是保留旧值或报告为零。更新 Control 后可获得独立采样时间戳；旧存储记录在被新记录替换前回退到心跳时间。此端点不能推导历史计数、延迟分位数、期望副本数或 HTTP 错误率。

服务资源标签保留上报的任务身份。已注册 Compose job 将 `job + task` 映射为控制台的 `job_task` 名称；其他 job 保留 job 名和独立 task 标签。只知道 allocation ID 的容器计为未解析，不猜测服务名，也不会在抓取时查询 Nomad。判断覆盖程度时请检查未解析数量。

请求率、状态码和延迟需要启用 Traefik metrics，并将 router/service 标签关联到 Luma 应用身份。私网工作节点和绕过 Traefik 的路由需要单独埋点。检测 Control 故障时，采集器或独立探针应运行在 Control 进程及其故障域之外。

官方参考：[Prometheus 抓取配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)、[Traefik 指标](https://doc.traefik.io/traefik/observe/metrics/)、[Alertmanager 分组与静默](https://prometheus.io/docs/alerting/latest/alertmanager/)。
