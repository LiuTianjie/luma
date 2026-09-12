# 可观测 {#observability}

Luma 提供节点与容器采样、当前任务队列、资源历史、运行事件、持久化告警，以及可恢复读取的应用日志。这些观测不等同于终端用户可用性检测。部署和重启时的公共路由检查是独立能力。


## 控制台导航 {#console-navigation}

可观测分为告警事件、应用监控、资源指标、日志、告警规则和通知渠道，各页使用简洁的独立标题。引擎摘要位于告警事件页；通知渠道关注配置与发送历史。内置告警页每 15 秒刷新，资源历史每 30 秒刷新。操作流程及路由关系与连通性探测的区别，见[控制台指南](dashboard-guide.md)。


## 应用告警（`luma-observe`） {#application-alerts-luma-observe}

Control 控制台提供节点与容器采样，实时 allocation 日志仍通过 Control 按需代理。应用 HTTP 5xx、延迟（p90/p95/p99）、Nomad 失败 allocation、重启风暴、链路追踪，以及可搜索的 stdout/stderr，由可选的 [`observe/`](../observe/) Compose 应用提供。可用 Luma 部署它；不部署也不影响 Control 运行。运维人员通过 **控制台 → 可观测 → 应用**查看，它在 Control 域名的 `/grafana` 嵌入 Grafana。部署 observe 后，HTTP、Nomad 和 trace 序列立即按 Luma 应用/stack 名标记，无需应用单独加入。失败指标只统计 Nomad 仍希望运行的 allocation，不是累计失败次数。该组件使用主机网络和回环监听器：Prometheus 为 `127.0.0.1:8082`，OTLP 为 `127.0.0.1:4318`，Tempo 为 `127.0.0.1:3200`。应用任务携带 Control 签发的 bearer token，向 Manager Tailscale mesh 的 4319 端口发送 trace。Control 使用主机网络，observe 存在时查询 `http://127.0.0.1:8428`。`127.0.0.1:3100` 上的 Grafana 仅用于本地调试。告警在 vmalert/Alertmanager 中评估，不经过 Control SQLite。Trace 和日志保留 7 天，指标保留期单独配置。

在 `observe/` 中执行 `luma build local . --platform linux/amd64 --env .env` 部署。当前 CLI 支持回环 metrics/OTLP 标志后，刷新 Traefik，让 RED 规则有可抓取目标。通过 **控制台 → 可观测 → 应用**查看。图表按 Luma 应用展示 Traefik HTTP 请求率、5xx、p90/p95/p99 延迟和 Nomad 健康状态，不是读取每个容器的 `/metrics`。Traefik 直方图桶为 `0.05,0.1,0.25,0.5,1,2.5,5,10` 秒，以区分这些分位数。启用 observe 后，后续 Luma 部署会自动注入官方 OpenTelemetry 环境变量：`OTEL_SERVICE_NAME`、`luma.stack` / `luma.task` / `luma.region`，以及指向 mesh 监听器的 OTLP HTTP 配置。已包含 OpenTelemetry 发行版的应用无需 Luma SDK 即可发送 span。首次启用 observe 后应重新部署已有应用。Trace 导出失败不会阻塞业务；采集器保留 HTTP 5xx、OTel 错误、超过 1 秒的 trace，并对其他请求进行 10% 基线采样。接近内存上限时丢弃新 trace，而不是触发 OOM。不要通过 Manager 公网接口向外发送原始访问日志或 trace。

## 应用接入 {#application-integration}

部署 [`observe/`](../observe/) 即表示在集群级启用。此后新部署的 Luma job 自动获得 OTel 配置。只重启旧 job 的进程不够，必须重新渲染 Nomad job。这里没有 Luma 遥测 SDK，也不会注入语言 agent。

无需修改应用即可获得：

- 按 Luma 应用名标记的 Traefik HTTP RED（请求率、5xx、p90/p95/p99）。
- 按 Luma 应用名标记的 Nomad 运行数、当前失败数和重启数。
- 存入 Tempo 的公共 HTTP（`cn-edge` / `external-edge`）入口 span。

重新部署后，只有镜像已经支持 OpenTelemetry 时，才会获得：

- 导出到 Tempo、带 `luma.stack` / `luma.task` / `luma.region` 的进程 span。
- 在 **控制台 → 可观测 → 应用 → Traces** 或 Grafana `/grafana/d/luma-traces` 查看。

仍不会自动获得 nginx、静态 Go 程序或无 OTel SDK 镜像的进程追踪，也不覆盖未经过 Traefik 的流量、请求正文、SQL 文本或每次成功的快速请求。5xx 和超过 1 秒的 trace 会保留，其余按 10% 采样。导出失败不会阻塞业务。

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

**控制台 → 可观测**默认打开告警事件。指标有独立页面；日志通过 Grafana Explore 查询 VictoriaLogs（保留 7 天）。规则与通知渠道分别有列表和编辑地址。存储治理位于 **基础设施 → 存储 → 数据治理**。

指标页显示实际采样时间范围和保留期，区分缺失、过期和查询失败，并在采样缺口处断开曲线。可选择服务查看资源历史，或打开按 stack 筛选的可观测日志。

可观测日志来自 Nomad allocation 的 stdout/stderr，由 luma-observe 发送到 VictoriaLogs。按 `app`（Nomad job / Luma stack）筛选。Traefik JSON 访问日志是 `traefik` job 的 stdout，可用 LogsQL 的 `unpack_json` 解析。Nomad 帧没有事件时间戳，因此日志行时间是观测时间。`luma service logs` 和应用详情的实时日志仍通过 Control 跟随 Nomad 文件，只提供有限片段，并非 VictoriaLogs 的持久化日志存储。

资源数据按 30 秒桶保留，与上报某个服务的节点数量无关。`LUMA_METRICS_HISTORY_POINTS` 默认 720（约 6 小时），范围为 60 到 10000。资源历史独立保存在 Control 状态目录下有界的 `metrics-history.json` 中；长期排查和独立故障检测仍适合外部采集。备份范围见[控制面存储与恢复](control-storage.md)。服务汇总只覆盖已上报节点，缺失贡献在 180 秒后过期，不能证明所有副本均已上报。

## 内置告警规则与通知 {#built-in-alert-rules-and-notifications}

控制台告警页管理规则、通知渠道和事件。规则、事件状态变化及通知发件箱持久化在 Control SQLite 中。评估器使用已上报给 Control 的采样和任务状态；创建资源规则不会启动公共 HTTP 探测。

可用预设包括节点心跳时长、持续 CPU/内存/磁盘/inode 使用率、排队任务时长、每个应用最近一次失败构建；部署 luma-observe 后还支持应用 p95 延迟、HTTP 5xx 比例和 Nomad 失败 allocation。Observe 采样通过 VictoriaMetrics 即时 PromQL 查询获得；observe 不可用时，相关规则保持上次状态（`noData: keep`），不会因此触发。阈值和持续时长可配置，条件持续达到要求后，事件才由 pending 变为 firing；健康观测会解除事件。每条规则与目标仅维护一个活动事件，可设置持续故障的重复通知间隔。

缺失数据有明确策略：`keep` 保持已有触发状态，不把缺失当作恢复；`alert` 把缺失视为待评估条件。CPU 规则使用新鲜的 Linux 主机 CPU 采样，不把 macOS 负载均值解释为 CPU 百分比。

确认事件会记录确认操作并停止重复通知，但不会把底层条件标记为健康。全局和规则级定时静默只抑制通知，评估及事件历史仍保留。待发送和重试通知等待静默期结束；静默无法撤回已经发出的 HTTPS 请求。修改规则条件会以规则变更事件把旧事件标记为 `closed`，不是健康恢复的 `resolved`。在某个浏览器里临时隐藏总览卡片属于另一种界面偏好，不会静默告警。

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

安全地把令牌复制到 Prometheus 采集器凭据文件。此令牌只被 `/v1/metrics` 接受，不能查询控制台、读取应用日志、部署、重启或访问 LAE 租户 API。它应与其他所有 Luma 令牌区分。端点输出包含基础设施名称与区域标签；远程抓取需要认证，同机 luma-observe 抓取仅限回环。

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