# 通用观测平台设计 {#observe-platform-design}

Luma Observe 接收标准遥测，应用拥有埋点、业务字段、面板和告警定义。部署管理与观测接入是两种独立能力。Python SDK 是官方 OpenTelemetry 的可选薄封装；其他语言或已有 OTel SDK 可以直接接入。

本文区分本分支源码与后续方案，不代表生产部署或容量验收已经完成。接入参数见[标准应用观测接入](observe-applications.zh-CN.md)，Python API 见[SDK 指南](../sdk/python/docs/integration.zh-CN.md)。

## 接入身份与外部应用 {#application-identity-and-external-services}

- 资源身份沿用 `service.namespace`、`service.name`、`service.instance.id`、`deployment.environment.name`。应用自行选择稳定的服务名；执行 ID 和业务 ID 属于 Span，不作为指标标签。
- Luma 部署的进程使用 Control 注入的标准 OTLP 配置；外部进程使用已启用的 HTTPS OTLP 入口及原有写入凭据。HTTPS 示例配置需要实际安装、DNS/TLS 和连通性验证，文件存在不代表入口已上线。
- 外部服务可以获得同样的 Metrics、Trace 和 Grafana 查询能力，不要求创建 Nomad Job。Collector 当前没有 OTLP Logs pipeline，公网网关也没有 Logs 写入路由；外部日志尚需独立的标准采集接入，不能声称已有 Nomad 实时 stdout 的全部体验。
- 当前 Prometheus Remote Write 路径要求 cumulative 的单调计数与直方图。外部 SDK 必须配置 cumulative；DELTA 转换是后续平台能力，不能把 OTLP 接收成功解释为任意 temporality 都可存储。
- 保持现有凭据及 `luma secret` 流程。共享读凭据、Grafana 变量和 dashboard UID 均不是租户隔离机制。

## 任务执行与采样 {#task-execution-and-sampling}

每次框架执行建立独立 Trace，用标准 SpanLink 连接投递上下文。重新投递保留旧上下文的 Link；它表达因果关系，不把两次执行合成一次。消息准备 Span 不代表 Broker 已接收。没有 exporter 的生产者仍可写排队时间戳；显式禁用 SDK 时不改投递标签。

独立执行 Trace 避免排队时间提前消耗 Collector 的采样等待窗口。运行中较早结束的子 Span 仍可能触发决定，因此 Collector 对没有父 Span、带 `task.name` / `task.framework` 的 CONSUMER 根 Span 使用单独的完成摘要采样分支：在完成时按错误、耗时和基础比例决定保留。普通子 Span 保持现有采样。存储采样前，每个接收的 Span 在默认 RED 与主动开启的服务级 RED connector 中只走一个、生成一次。开启模式的应用额外生成执行根 Span 专用的 `task_span_*` 指标族，见[按服务聚合 Span 指标](observe-service-span-metrics.zh-CN.md)。任务量不能把 RED calls、任务根计数和原生任务计数相加，RED 还包含生产者与子 Span。

这个设计保留已接收的失败/慢任务根摘要；它不保证长任务所有子 Span 都完整，也不覆盖强杀、进程崩溃、SDK 头采样或导出丢失。需要完整长 Trace 时，应为指定服务安排有配额的全收集或一致的 Trace ID 采样方案，不能无限增大 `decision_wait`。

执行状态由 SDK 执行上下文持有；消息中只携带可序列化的传播标签。Taskiq 使用正常完成钩子和 asyncio Task 完成兜底结束尝试，并在 Broker shutdown 限时 flush；flush 不关闭同进程共享 Provider。Celery 使用公开任务及进程信号。业务异常、返回值、重试、结果保存和 ACK 仍由框架负责。

## 高频负载与可靠性 {#high-frequency-workloads-and-reliability}

| 层级 | 当前机制 | 边界 |
| --- | --- | --- |
| SDK | Trace 内存队列 512、批次 64；Metrics 本地聚合、默认每 30 秒导出；有界超时和 flush | 不阻塞业务等待平台恢复；溢出、硬退出和导出失败可能丢失 |
| Collector | 内存限制、批处理、有界采样状态；Tempo exporter 128 请求队列与重试；PRW 128 OTLP metrics batch 队列 | 当前没有持久导出队列或 PRW WAL；Collector 重启会丢内存数据 |
| 存储 | Tempo、VictoriaMetrics、VictoriaLogs 的现有磁盘存储和保留期 | 仅保护已到达该存储的数据；当前单 Manager、单副本部署不是 HA |
| 业务可靠性 | 业务自己的事务、outbox、消息队列及结果存储 | 计费、审计、任务交付不能依赖可采样、可丢失的遥测 |

高频调用优先使用本地 Counter / Histogram 聚合，避免每次调用发一个网络请求。标签必须有限；任务参数、用户 ID、执行 ID、异常全文不进入通用指标标签。

进程心跳时间戳表示指标采集/导出路径仍在工作，最老活跃执行时长按任务名、队列和框架表示尚未结束的尝试已经运行多久。面板须按完整任务与进程身份匹配 `task_active > 0`，并检查心跳新鲜度及 tracking overflow，排除已结束任务的旧时长。心跳新鲜且活跃数明确为零才能解释为本进程空闲；活跃时间持续增长需要结合任务预算判断。指标线程存活不证明事件循环或业务有进展，缺失数据也不等于零。Broker 积压数、最老未消费消息年龄和业务进度需要由应用或 Broker 的标准指标提供。

`service.instance.id` 必须区分并发写入 SDK 累计指标的进程。简单移除该维度会把独立计数器和直方图混为一条序列，破坏 Single Writer 语义。主动开启的服务级 Span 指标使用另一条路径：共享 Collector 先将 Span 聚合为服务级计数器和直方图，再作为写入方导出。Celery 的完成 Span 统计已通过这条路径避免按 child 进程拆分序列；Taskiq 原生指标继续保留进程身份。上线验收仍需测量进程轮换率、任务/队列/结果维度和 VictoriaMetrics 实际新增序列。重写多个进程各自导出的累计指标标签，不等于由 Collector 聚合 Span。

下一阶段可靠性方案（本轮未实施）：

1. Tempo exporter 使用官方 `file_storage` 持久发送队列；Prometheus Remote Write 使用该 exporter 专用 WAL。配置独立持久挂载、磁盘容量/剩余空间预算、重试上限和故障恢复操作。
2. 监控接收拒绝、SDK/Collector 丢弃、队列占用与导出失败、磁盘增长、信号新鲜度，并测量积压后的恢复速率。写入网关按预算限流；客户端保持有界退避，不能无限重试挤占业务资源。
3. 用代表性 payload、spans/s、活跃 trace 数、series 数和进程轮换率验收；覆盖下游不可用、Collector 重启、磁盘不足和超载。仅队列大小不能推导吞吐承诺。
4. 有跨节点持久性、长时间回放或多消费者的实测需求时再引入 Kafka 等消息系统；Collector 扩容尾采样时同时保证 Trace ID 路由一致性，存储也需要对应的 HA 方案。

持久 exporter 队列仍不覆盖入队前的 batch / tail sampling 内存状态，也不能保护 SDK 进程崩溃前尚未导出的数据。服务返回成功的时点及持久化边界必须随实现记录，不能称为端到端零丢失。

## 应用详情中的面板 {#dashboards-in-application-details}

当前已有 Grafana 标准 JSON 的扩展目录、离线配置工具和应用详情的 Grafana 嵌入。自定义面板可以沿用这些能力。以下注册和 UI 方案尚未实施：

- 应用关联一个或多个 Grafana dashboard UID，以及 `service`、`environment` 等变量到观测身份的映射。在应用详情的观测区域展示，保留日志检索、链路追踪及实时日志入口。
- 查询、单位、布局和业务字段留在 Grafana 配置中。React 页面只处理面板选择、身份筛选、嵌入及加载/缺失状态，不硬编码每个业务的指标。
- 外部服务拥有独立的观测条目，通过观测身份登记和关联面板；无需伪造 Luma 部署对象。身份注册不自动授予数据读取权限。

SDK 面板编译器是可选离线工具。已有标准 Grafana JSON 的应用可以直接交付，不需要转成另一份格式。

## 版本与发布 {#versions-and-releases}

SDK 版本与 `luma-infra` 独立。`observe-sdk-v*` 发布流程完成检查后将同次构建的 wheel、源码包及校验和发布到 GitHub Releases，可作为稳定依赖来源。PyPI OIDC 发布需先建立独立 Trusted Publisher 并启用仓库发布变量；工作流源码不代表包已经在 PyPI 可用。消费方锁定发布 wheel 的校验和或完整 Git SHA，不使用会过期的 CI 工件 URL 作为长期依赖。参见[发布指南](../sdk/python/docs/releasing.md)。

Collector 固定版本为 contrib 0.122.1，实际 PRW 组件为 0.122.0。该版本 DELTA 限制、专用 WAL、队列单位和 Alpha 多 worker 开关见 [PRW README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.122.0/exporter/prometheusremotewriteexporter/README.md) 与[发行清单](https://github.com/open-telemetry/opentelemetry-collector-releases/blob/v0.122.1/distributions/otelcol-contrib/manifest.yaml)。升级组件时重新验证这些合同。
