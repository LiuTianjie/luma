# 按服务聚合 Span 指标 {#service-level-span-metrics}

[English](observe-service-span-metrics.md)

应用可通过资源属性 `luma.observe.span_metrics=service` 主动开启服务级 Span 指标。
共享 Collector 会把这些派生指标按服务归并，不再按进程实例拆分。未设置这个精确值的
应用继续使用现有 spanmetrics 行为。存储的 Trace 和原生 OTLP Metrics 仍保留原始资源
属性，包括 `service.instance.id`。

## 适用场景 {#when-to-use-this-mode}

这个模式适合进程频繁轮换、因此产生大量历史 Span 指标序列的 Worker。它使用标准
OTel Span 和 Collector spanmetrics；任务名称、结果、看板和规则仍由应用定义。
它不复用进程标识，也不转换原生累计计数器。

按应用选择需要的信号：

| 配置 | 可用任务信息 |
| --- | --- |
| 服务级 Span 指标，`OTEL_METRICS_EXPORTER=none` | 从收到的执行根 Span 生成已完成尝试数和耗时；适用于主动开启该模式、子进程频繁轮换的 Celery 部署 |
| 默认 Span 指标，`OTEL_METRICS_EXPORTER=otlp` | 原有实例级 spanmetrics，加上 SDK 原生任务计数、耗时、排队时间、活跃尝试和 Worker 新鲜度指标；Taskiq 需要这些指标时保留这个默认模式 |

服务级开关与原生 Metrics exporter 相互独立。开启服务级模式的应用也可保留原生指标，
这些原生指标仍按进程区分。设置 Metrics exporter 为 `none` 后，不再提供 SDK 的
排队时长、活跃数量、心跳、最老活跃时长以及应用自定义原生指标。已完成 Span 计数
不能替代这些数据。Nomad allocation 监控仍可使用；Broker 积压和预期工作量需要
业务或 Broker 自身的信号。

## 启用与部署 {#enable-and-roll-out}

需要 **Control 0.2.26 及以上**，并部署包含服务级 Span 管线的新版 Observe。
这两者独立部署：更新 Control 不会重建 Observe。`luma-observe-sdk==0.1.0` 已兼容，
这个功能不要求升级 SDK；其他官方 OTel 接入也可投递符合相同合同的 Span。

1. 部署新版 Control，使应用资源属性按键合并。
2. 按既有[部署工作流](../observe/README.md#deploy)重建、重新部署独立 Observe 栈。
3. 在应用设置资源开关并重新部署，先从应用测试环境接入。沿用既有地址、凭证和其他
   环境变量。

选择使用 Span 派生任务指标的服务，可采用以下环境变量片段：

```yaml
environment:
  OTEL_SERVICE_NAME: document-worker
  OTEL_RESOURCE_ATTRIBUTES: service.namespace=documents,deployment.environment.name=production,luma.observe.span_metrics=service
  OTEL_TRACES_EXPORTER: otlp
  OTEL_METRICS_EXPORTER: none
```

使用应用自身稳定的服务名和环境名。如果已经配置 `OTEL_RESOURCE_ATTRIBUTES`，在
原值中追加开关。匹配区分大小写且要求值精确为 `service`；未设置或使用其他值都会
进入默认分支。如果应用代码也设置资源属性，需要核对实际 Span 的资源开关。
SDK 埋点和任务生命周期钩子仍需应用接入，环境变量注入本身不会产生任务 Span。

Control 保留应用显式设置的每个资源键，包括显式空值，只追加尚缺少的注入键，例如
`luma.stack`、`luma.task`、`luma.region`，不会替换应用其他环境配置。此前自行设置
资源属性字符串的应用，重新部署时可能一次性补齐这些缺失标签，序列身份因此变化。
原本已获得完整注入属性的应用，不会因为这次按键合并再产生额外变化。

## 资源标识与指标口径 {#identities-and-metric-contract}

开启后，派生指标的资源只保留以下六个标识键：

| OTel 资源键 | Prometheus 标签 |
| --- | --- |
| `service.name` | `service_name` |
| `service.namespace` | `service_namespace` |
| `deployment.environment.name` | `deployment_environment_name` |
| `luma.stack` | `luma_stack` |
| `luma.task` | `luma_task` |
| `luma.region` | `luma_region` |

同时固定携带 `luma_observe_span_metrics="service"`。请保留这个模式标签：默认分支与
服务级分支共享 `traces_span_metrics_*` 命名空间，需要用它区分写入者，部署切换期间
也一样。进程标识、SDK 版本等其他资源属性只从开启模式后新生成的派生指标中移除；
原始 Trace 和原生 Metrics 资源保持完整。六个标识共同分组，避免把不同服务、环境、
Luma 任务或地区误合并。

开启模式的 Span 仍生成通用 `traces_span_metrics_*` RED 指标。每个接收的 Span 在
默认 RED 与服务级 RED 两个 connector 中只走一个、生成一次。服务级任务指标是对
符合条件的执行根 Span 的额外统计；计算任务量时，不能把 RED calls、
`task_span_calls_total` 和原生 `task_executions_total` 相加，RED 还包含生产者和子
Span。新增的任务指标只接收同时满足以下条件的 Span：

- kind 为 `CONSUMER`。
- 没有父 Span。
- 同时存在 `task.name` 和 `task.framework` 属性。

生产者 Span、嵌套子 Span 不进入任务指标。SDK 0.1.0 的 Taskiq 和 Celery 适配器会
输出符合条件的执行根 Span。每个收到的已完成根 Span 表示**一次执行尝试**，一次
重试后成功可以贡献两次尝试；取消只有在完成根 Span 实际输出并送达时才有计数。

| 指标 | 含义 |
| --- | --- |
| `task_span_calls_total` | 收到的已完成执行根 Span 数 |
| `task_span_duration_seconds_bucket` | 执行尝试耗时直方图桶 |
| `task_span_duration_seconds_sum` / `task_span_duration_seconds_count` | 耗时总和与直方图观测次数 |

任务维度为 `task_name`、`task_framework`、`task_queue`、`task_outcome`，此外还有
connector 默认的 Span 维度及上述服务标识。来源中的 `succeeded`、`failed`、`retrying`、
`skipped`、`cancelled` 可以分别筛选；缺少队列或结果属性不能推断成业务成功。任意
业务字段不会自动成为新的任务指标维度。

## 查询示例 {#query-examples}

按目标服务、环境和模式筛选。如果只查询某个部署，继续加上 `luma_stack`、`luma_task`
或 `luma_region`。以下示例与前面的环境变量片段对应。

最近一小时观测到的执行尝试，按结果区分：

```promql
sum by (task_name, task_framework, task_queue, task_outcome) (
  increase(task_span_calls_total{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service"}[1h])
)
```

同一窗口的失败尝试；重试和取消使用各自独立的结果枚举：

```promql
sum by (task_name) (
  increase(task_span_calls_total{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service", task_outcome="failed"}[1h])
)
```

成功尝试的 P95 耗时，单位为秒：

```promql
histogram_quantile(0.95,
  sum by (le, task_name) (
    rate(task_span_duration_seconds_bucket{service_name="document-worker", service_namespace="documents", deployment_environment_name="production", luma_observe_span_metrics="service", task_outcome="succeeded"}[5m])
  )
)
```

查询窗口需要覆盖任务的正常执行频率和指标导出间隔。样本稀疏、序列缺失及计数器重置
都会影响 `rate` / `increase`；这些表达式用于运行观测，不是精确执行账本。应用的
Grafana JSON 继续使用现有[看板扩展目录](observe-applications.zh-CN.md#application-owned-dashboards-and-rules)。

## 无执行告警模板 {#no-execution-alert-template}

下面的例子适用于预期定期完成的任务。任务名称、窗口和 `for` 时长需要按业务工作量
确定，并配置业务工作时段或维护静默。队列空闲可能完全正常；没有完成 Span，也可能
表示任务尚未结束或观测投递故障。本指南不会安装告警，也不会开启通知。

```yaml
groups:
  - name: application-observation-example
    rules:
      - alert: ApplicationHasNoObservedExecutions
        expr: |
          (
            sum(increase(task_span_calls_total{
              service_name="document-worker",
              service_namespace="documents",
              deployment_environment_name="production",
              luma_observe_span_metrics="service",
              task_name="document.convert"
            }[30m])) or vector(0)
          ) == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "No completed execution spans observed for document.convert"
```

这个模板把序列缺失也视为“未观测到执行”，不能据此证明 Worker 已宕机。需要结合
部署存活、预期任务需求和 Collector 数据新鲜度审核，再按既有
[应用规则交付流程](observe-applications.zh-CN.md#application-owned-dashboards-and-rules)验证、安装已批准的规则。

## 交付边界与核对 {#delivery-limits-and-reconciliation}

任务计数描述 **Collector 在存储尾采样前收到的已完成根 Span**。SDK 头采样、导出队列
满、超时或投递失败、Collector 内存拒收、Worker 被强制终止及重启都会影响覆盖范围，
重复投递的根 Span 也可能改变计数。低平均 QPS 不能证明突发安全，需要按真实部署
测量峰值流量、每任务 Span 数、缓冲拒收和导出失败。

spanmetrics connector 把累计状态保存在 Collector 内存中。沿用的五分钟资源组空闲
过期策略和 Collector 重启都可能重置状态；单条指标序列的状态可能要等整个资源组
被移除才释放。历史存储样本仍在，跨越这些边界的 `rate` / `increase` 窗口需要谨慎
解释。此模式不增加持久累计状态，也不修改现有过期策略。

对照日志时，使用相同时间窗口、时区、任务、框架、队列和结果口径，区分执行尝试与
最终业务任务，并解释上述投递缺口和重置边界。不能保证一天的指标增量等于一天的
日志总数。任务和 Span 维度稳定时，服务聚合可以降低进程轮换带来的序列增量；它不是
存储配额，也不会约束任意 Span 名称或任务维度的数量。

## 关闭与验证 {#disable-and-verify}

从应用资源属性中仅移除 `luma.observe.span_metrics=service`，再重新部署应用。
之后收到的 Span 会回到默认 spanmetrics 分支。旧派生序列仍可查询，要等查询 lookback
和存储保留期退出，不会因部署立即删除。切换期间，看板继续明确筛选模式。如果此前
关闭了原生 Metrics，想恢复时还需显式设置 `OTEL_METRICS_EXPORTER=otlp`；删除资源
开关不会改变 exporter 配置。

开启或关闭后，用一个有代表性的应用核对：

- 开启模式的派生序列带服务模式标签，不含进程实例标签；未开启应用的 spanmetrics
  仍保留原来的身份。
- 存储的 Trace 仍能识别原进程；原生 Metrics exporter 开启时，其资源属性保持原样。
- 任务计数、耗时涵盖预期的完成根 Span 和结果，生产者、子 Span 不计入任务指标。
- 结合业务任务与日志观测，检查查询新鲜度、导出/接收失败以及真实序列增量。

平台验证与标准访问入口见[标准应用观测接入](observe-applications.zh-CN.md#verification)。
