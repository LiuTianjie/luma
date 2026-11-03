# Python 应用接入

`luma-observe-sdk` 是独立版本的 Python 包，使用官方 OpenTelemetry API、Provider
和 OTLP/HTTP exporter。平台接收标准信号，应用自行定义业务字段、结果、指标和面板。
SDK 不导入 Luma Control、Django、业务模型或数据库。

## 安装与版本

Python 要求为 3.10 及以上。当前源码版本是 `0.6.1`，独立的 `Observe SDK Checks`
工作流构建 wheel 和 sdist，用于审核与验证；CI 工件会过期，不作为长期依赖来源。
稳定分发方式见[独立 SDK 发版指南](releasing.md)：消费方锁定已发布版本，或使用包含
SDK 的可访问、不可变 Git 提交。当前未提交源码没有可安装的远程 SHA；指南中的
提交占位符需要在审核后的源码推送后替换。

如需检查下载的 wheel，可以选择对应框架 extra：

```sh
python -m pip install './luma_observe_sdk-0.6.1-py3-none-any.whl[taskiq]'
```

Celery 进程使用 `[celery]`，同时运行两种框架的镜像使用 `[taskiq,celery]`。
包 PyPI 发布是独立步骤；发布完成后，消费方可通过自己的包配置安装
`luma-observe-sdk[taskiq]==0.6.1`。Luma 原有 `v*` 发版继续只发布 `luma-infra`。

## 地址、凭证与开关

Luma 管理的应用沿用 Control 注入的 `OTEL_EXPORTER_OTLP_ENDPOINT`、
`OTEL_EXPORTER_OTLP_HEADERS` 和资源属性，现有 Token 与 `luma secret` 配置保持原值。
需要任务指标时，在应用部署声明中配置 `OTEL_METRICS_EXPORTER=otlp`；平台默认值仍是
`none`。网格外进程沿用已配置的 HTTPS OTLP 入口和写入 Token。

```python
from luma_observe import configure, span, task

configure(
    service_name="document-worker",
    resource_attributes={"deployment.environment.name": "production"},
)

def convert(document):
    with task("document.convert", framework="manual", queue="documents",
              attributes={"document.format": "pdf"}) as run:
        with span("document.render"):
            result = render_document(document)
        if result.needs_review:
            run.outcome = "failed"
        return result
```

`task` 记录一次执行的完成结果和耗时，原返回值和异常继续交给业务调用方。
未捕获的异常记录类型及失败结果；结果枚举为 `succeeded`、`failed`、`retrying`、
`skipped`、`cancelled`。业务自行判断交付是否完成。自定义 `attributes` 进入 Span，
不会自动成为指标标签。

在适配器已经观测的任务中，使用 `current_task()` 获取当前执行，无需再包一层任务 Span：

```python
from luma_observe import current_task

def mark_review_required():
    run = current_task()
    if run is not None:
        run.business_outcome = "requires_review"
        run.business_status = "pending"
```

这两个字段允许业务自定义字符串，长度上限 256 字符，进入 Span 和可选生命周期日志的
`task.business.outcome`、`task.business.status`。业务状态从不作为指标标签；业务结果只有
按下文声明后才会成为指标标签。
`set_outcome()` 仅接受前述五种标准结果；框架失败或未捕获异常优先于成功的业务分类。
`current_task()` 按执行上下文隔离，嵌套任务结束后恢复外层；不在活跃任务内时返回
`None`，fork 子进程也不会继承父任务。

需要统计业务结果时，应用在初始化时按优先级声明一次自己的有限取值：

```python
configure(business_outcomes=("failed", "requires_review", "retrying", "succeeded"))
```

声明后，每次执行最终的 `business_outcome` 计入一次 `task.business_outcomes`；声明之外的
取值计为 `other`，Span 和日志仍保留原值。没有声明时不计数。批量处理中某一项失败就应当
决定整次执行时，使用 `run.escalate_business_outcome(value)`：它保留声明中排在最前的值。
未声明的值排在最后，排名相同时以最新的值为准，因此没有声明时等同于直接赋值
`business_outcome`。

需要同时保留事件和错误采样时，调用
`record_business_outcome("failed", status="rejected", failed=True, error_type="provider_error")`。
SDK 处理结果优先级、状态字段、生命周期事件和 Span 错误标记；状态到结果的含义由业务映射，
`failed=True` 由调用方明确声明。`outcome=None` 时只记录给定状态；任务外调用直接返回，
埋点错误保留业务原返回值和异常。

长任务可以用阶段或显式标记报告进展。模块级接口在任何位置都能直接调用：
在被观测的执行内会记录，在执行外只是正常运行业务代码，业务函数不需要自己判断：

```python
from luma_observe import mark_progress, observed_stage, stage

@observed_stage("document.prepare")
def prepare(document): ...

with stage("document.render", attributes={"pages": 12}):
    render_pages()
mark_progress()
```

阶段是一个子 Span，同时写一条 `task.stage` 生命周期事件，不占用执行 Span 有限的事件数。进入和离开阶段、调用
`mark_progress()` 都会重置该执行的 `task.no_progress_age`。埋点出错不会改变被包装函数的返回值或异常。
`current_task()` 返回的 `TaskRun` 上仍保留 `stage()` 和 `mark_progress()`。

0.5.0 增加批量阶段汇总模式：

```python
with stage("document.read", attributes={"document.id": document_id}, aggregate=True):
    read_document(document_id)
```

应用声明稳定的阶段名和业务属性；SDK 负责计时、次数、异常关联和任务结束时的汇总。
一次尝试最多保留八种阶段，每种阶段仅首次进入时输出开始日志，结束时在任务根 Span
追加一次累计摘要。此模式新增子 Span 为零，首次失败与省略数量各最多追加一条摘要。
`observed_stage(..., aggregate=True)` 支持同步和异步函数；并行阶段的累计工作耗时可能
大于任务的实际经过时间。原有 `stage()` 默认行为保持为子 Span。
每类阶段保留最近一次的有界属性集合，出现失败时保留首次失败的属性；汇总计数由 SDK 维护。

需要异常正文与堆栈时，在原有 `configure(...)` 中设置 `capture_error_details=True`，
并启用 `lifecycle_logs=True`。SDK 将首次原始异常写入 `task.failure.detail` JSON 事件的
`exception` 对象，自动带上本次 Trace 编号，继续使用已有异步日志队列和存储通道。
正文、堆栈及原因链的 UTF-8 上限分别为 4 KiB、16 KiB，超过时标记 `truncated`；根 Span
保留类型、最多 256 字的摘要和代码位置。默认仅记录异常类型。业务的 `logger.exception(...)`
沿用原行为，应用接入只需配置和阶段声明。

管理后端可安装 `[query]` extra，使用 `luma_observe.query.ObserveClient` 读取现有
Metrics、生命周期、异常日志与 Trace。SDK 负责分页、超时、响应上限和格式解析；业务仅
配置应用范围、指标表达式、字段映射与管理权限。查询客户端独立于采集开关，初始化时
不启动 exporter，也不增加服务、缓存或存储。接口和预算见[查询接入](querying.md)。

应用自己掌握实际发送调用时，可以观测发送结果；适配器的准备 Span 不代表发送成功：

```python
from luma_observe import publication

with publication("document.render", framework="taskiq", queue="documents", task_id=message_id):
    redis.xadd(stream, payload)
```

它记录一个生产者 Span、按 `outcome`（`sent`、`failed`）区分的 `task.publications`，
以及可选的 `task.published` 生命周期事件，并原样抛出原异常。Celery 应用通过适配器的
`publications` 选项得到同样的记录。

以下任一条件完整关闭 SDK，包括可选生命周期日志：

- 初始化时传入 `configure(enabled=False)`。
- 环境变量 `LUMA_OBSERVE_ENABLED=false`。
- 环境变量 `OTEL_SDK_DISABLED=true`。

没有 OTLP 地址时也不会启动 Provider 或后台线程。SDK 使用官方环境变量解析地址、
请求头和采样配置，导出协议为 `http/protobuf`。分别关闭某种 exporter 只控制该信号；
需要整体关闭时使用上述总开关。
生产者没有本地 endpoint 时仍写入投递时间，供 Worker 统计排队耗时，并保留已携带的
上下文；显式禁用时不修改 carrier。`telemetry_disabled()` 只查询显式开关，不初始化
运行时或 exporter。

`get_runtime().enabled` 表示运行时是否成功启用。初始化失败时，
`get_runtime().initialization_error` 只保留异常类名，运行时保持关闭；业务调用路径不会
同步输出配置或异常全文。

## 框架接入

Taskiq 将观测中间件放在已有链的第一项，让完成清理位于原有错误、重试处理之后：

```python
from luma_observe.instrumentation.taskiq import TaskiqMiddleware

broker.with_middlewares(TaskiqMiddleware(), existing_retry_middleware)
```

使用真实 broker 的队列与任务名，无需维护应用任务目录。重试、结果保存、ACK 和原有
中间件仍由 Taskiq 管理。执行状态保存在当前回调上下文中，后续中间件可以复制或重建
消息。carrier 只携带传播字段和可序列化的私有 invocation ID，不序列化运行中的对象。
Taskiq 默认线程池支持执行上下文传播；自定义进程池中的业务
函数需要自己的进程初始化与传播，接收端的尝试观测仍可保留。

Celery 在应用初始化时连接公开的任务、进程信号：

```python
from luma_observe.instrumentation.celery import instrument_celery

observation = instrument_celery(app=app, publications=True)
```

适配器作用于当前进程，重复注册保持幂等；Worker 子进程初始化 Provider，生产者在首次
使用时初始化。任务排空后可调用 `observation.uninstrument()` 移除信号连接。
`publications=True` 会包装该应用的 `amqp.send_task_message`，即实际把消息交给 broker
的调用，因为 Celery 的发布信号无法报告发送失败。每次发送按上文 `publication()` 的方式
记录，`task_queue` 取 Celery 实际发布用的 routing key，与执行端记录的值一致：默认 direct
exchange 下就是队列名，其他情况取显式或队列配置中的 routing key。原返回值和异常保持不变。
`uninstrument()` 只在 SDK 的包装位于最外层时恢复 Celery 原发送函数；之后其他组件追加的
包装保持不变，被包在里面的 SDK 这一层停止记录。
同一任务选择一套适配器，避免同时安装 contrib `CeleryInstrumentor` 产生重复记录。

两种适配器只通过应用提供的可选回调读取业务参数。回调接收
`(task_name, args, kwargs)`，返回的字段写入本次执行的 Span 和生命周期事件。回调声明了 `arguments`
参数时，还会收到按任务函数参数名绑定好的调用参数，位置参数和关键字参数的调用结果一致：

```python
def business_fields(name, args, kwargs, arguments):
    return {"document.id": str(arguments.get("document_id", ""))}

TaskiqMiddleware(attributes=business_fields)
instrument_celery(attributes=business_fields)
```

只返回可以存入 Trace 和日志的编号。框架字段不能被覆盖；回调出错时计入
`observe.sdk.dropped`，不影响本次执行。Taskiq 的 Worker 和调度进程在 broker 启动时
声明自己的角色，Worker 同时导出已注册的任务名，并为这些任务导出值为 0 的 `task.active`，从未在该 Worker 上执行过的任务因此显示为空闲而不是缺数据；Celery 进程池在 `worker_process_init`、
Beat 在 `beat_init` 时完成同样的声明。

两种框架均传递标准 W3C 上下文和 baggage。每次框架执行单独开启一条 Trace，通过
Span Link 关联生产者，让执行完成后的采样判断独立于较早结束的生产者。手动 `task()`、
`span()` 仍默认沿用父 Trace，也支持显式传入 `context` 和 `links`。
配套 Collector 源码把已经完成的执行根 Span 与较早结束的子 Span 分开采样，保留的是
完成后的执行概要，不保证长 Trace 的所有子 Span。现有 Collector 需要基于该源码重新
构建部署后才具备这一行为；仅安装 SDK 不会更新线上 Collector。
生产者 Span 表示本地消息准备，在实际传输前结束；
它不充当投递成功凭证。发送结果只来自 `publication()` 或 Celery 的 `publications` 选项；
broker 持久化和可靠交付状态仍由应用原有链路维护。

适配器观测进入执行钩子的尝试；执行前撤销或进程被强制终止，可能没有完成 Span。
Taskiq 标准 SmartRetry 的 `NoResultError` 转换可识别退避重试；使用
`no_result_on_retry=False` 保留原错误时，公开钩子只提供失败尝试，无法确认最终重试
结果。队列状态、业务交付和执行尝试分别解释。

Taskiq in-memory 嵌套内联执行中，若子任务中间件抛错后被父任务捕获，父观测上下文
要到下一个 SDK hook 才恢复；这段间隙不能用当前上下文证明父任务进展。
进程被强制终止时也无法执行完成钩子。

## 字段与指标

使用 `get_tracer()`、`get_meter()` 获取 SDK Provider 上的官方 OTel 接口，补充应用自己的
阶段和指标。直接调用官方接口时沿用其语义。`counter()`、`histogram()` 提供隔离采集
异常的便捷接口，名称和字段由应用定义：

```python
from luma_observe import counter, histogram

completed = counter("document.completed")
size = histogram("document.pages", unit="1")
completed.add(1, {"document.format": "pdf", "outcome": "succeeded"})
size.record(12, {"document.format": "pdf"})
```

顶层 `counter()`、`histogram()` 返回的句柄可以在 `configure()` 前声明，并在 fork
后的 Worker 中复用；每次写入使用当前进程的运行时。从显式 `Runtime`、`get_meter()`
或 `get_tracer()` 取得的 Instrument 属于该运行时和进程，需要在 Worker 初始化时创建。

任务框架指标经共享 Collector 转换后为：

| 指标 | 含义 |
| --- | --- |
| `task_executions_total` | 已结束的执行尝试，按 `outcome` 区分 |
| `task_duration_seconds_bucket` | 执行耗时直方图 |
| `task_queue_wait_seconds_bucket` | 有已知投递时间时的等待耗时直方图 |
| `task_active` | 当前进程的活跃尝试 |
| `worker_heartbeat_seconds` | 采集进程本次采集时刻，Unix 秒 |
| `task_oldest_active_age_seconds` | 按任务名、队列和框架分组的最老活跃尝试时长 |
| `task_active_tracking_overflow` | 超出有界时长登记表的活跃尝试数 |
| `task_no_progress_age_seconds` | 活跃执行距开始、切换阶段或标记进展的最长时间 |
| `task_last_started_seconds` | 本进程最近一次开始该任务的 Unix 时间 |
| `task_last_finished_seconds` | 本进程最近一次结束该任务的 Unix 时间，按 `outcome` 区分 |
| `task_business_outcomes_total` | 按最终声明过的 `task_business_outcome` 统计的已结束执行（未声明的计为 `other`） |
| `task_publications_total` | 应用观测到的实际发送，按 `outcome`（`sent`、`failed`）区分 |
| `task_registered` | Worker 进程在框架中注册的任务 |
| `task_process_started_seconds` | 进程启动时间，按 `task_framework`、`task_role`、`task_queue` 区分 |
| `observe_sdk_dropped_total` | SDK 内部丢弃的遥测，按 `reason` 区分 |

最近开始、结束时间保存在进程内存中，和任务名一样有上限（2048 个键）。跨 Worker 轮换
保留时，取各实例的最大值，例如使用 recording rule。`task_registered` 和
`task_process_started_seconds` 用来区分"任务没有存活的 Worker、调度进程缺失"和
"任务只是空闲"。`observe_sdk_dropped_total` 统计有界写入队列丢弃的生命周期事件
（`lifecycle_logs`），以及为保护业务而被 SDK 吞掉的异常（`instrumentation_error`）。

固定任务维度为 `task_name`、`task_queue`、`task_framework`；标准资源属性提供
`service_name`、`deployment_environment_name` 及平台已有的 Luma 维度。
应用自定义指标可以选择有限的字段，如文件格式或结果枚举；单次执行编号进入 Span
或结构化日志。进程退出后的历史活跃样本需要结合存活和数据新鲜度解释。

心跳和 overflow 是进程级指标。最老活跃时长沿用 `task_active` 的有限任务维度：
`task_name`、`task_queue`、`task_framework`，按单调时钟计算每组当前活跃尝试中最老的
时长。它能定位持续执行的任务类型，但不代表“距离业务上一次取得进展的时长”；阈值
需要按任务类型定义。

每进程最多登记 8192 个活跃尝试，不保留历史任务键缓存。空闲分组没有新时长样本，
溢出期间也不输出时长样本。示例面板按同一进程标识和全部三个任务维度，与
`task_active > 0` 关联，再过滤过期心跳和非零 overflow，排除查询窗口内残留的已结束
任务旧时长。只有新鲜心跳加明确为 0 的活跃数量才能表示本地空闲；缺失、过期、溢出
不能解释为空闲。

心跳只说明进程导出链路仍在运行，不证明事件循环或业务在推进。本地空闲也不代表
Broker 队列健康或为空；积压数量和最老未消费消息时长仍由业务或 Broker 提供指标。

不同进程保留独立实例标识，避免多个累积计数写入者互相覆盖。Worker 回收会产生历史
序列，生产验收需测量实际进程轮换频率、任务维度及存储预算。协议检查提供合成多进程
样本的序列数量，不代表生产容量；新鲜度设置和查询语义见[面板指南](dashboards.md)。

默认 Trace 缓冲 512 条、每批 64 条、每 5 秒导出，Metrics 每 30 秒导出；HTTP 导出预算
1.5 秒，退出预算 2 秒。`lifecycle_logs=True` 可启用有界后台 JSON 日志，使用
`luma_observe.events` 的 INFO 日志。需要由已有 Nomad 采集通道接收时，让 SDK 直接写到 stdout：

```python
from luma_observe import configure

configure(lifecycle_logs=True, log_stream="stdout")
```

`log_stream=None`（默认）时，日志走应用自己为该 logger 配置的 handler。应用最终退出时调用 `shutdown()`，SDK
也注册了 atexit；Celery 使用进程退出信号。关闭时在同一预算内写完已入队的生命周期事件，
回收的 Worker 不会丢失最后的 `task.finished` 记录。关闭单个 Taskiq broker 会执行有界
`force_flush()`，不会停止其他 broker 共享的遥测。应用也可在已知 Worker 退出前调用
`force_flush(timeout=2)`：它返回队列 flush 是否在预算内完成，不初始化缺失的运行时，
不关闭 Provider，也不代表下游已经持久化数据。

遥测丢失不会成为可靠业务账本；计数代表实际采集到的信号。SDK 自身采集故障保持业务
返回、异常和确认语义；实际吞吐、延迟、进程轮换和故障恢复仍需要消费方验收。

## 业务自己的 Grafana 面板

从 [`examples/dashboards`](../examples/dashboards/) 复制配置，按应用指标修改查询、
字段、单位和阈值，离线生成标准 Grafana JSON：

```sh
luma-observe-dashboard application.json --output application-dashboard.json
```

每个应用使用唯一 UID。将审核后的文件按现有运维交付流程安装到
`/srv/luma/data/luma-observe/extensions/dashboards/`；Grafana 在下一次 30 秒扫描后
加载到 Applications 文件夹。该工具不连接平台、不携带 Token，也不部署服务。

告警仍使用标准 Prometheus/vmalert YAML，安装到 `extensions/rules/` 前用现有版本的
vmalert 校验，再关联已配置的 Alertmanager 路由。字段或图表变化通过应用配置交付，
无需增加 SDK 接口或修改 Control。

完整平台凭证、标准查询 API 和扩展目录说明见[标准应用观测接入](../../../docs/observe-applications.zh-CN.md)。

## 本地与 CI 验证

在 Luma 仓库根目录的隔离 Python 环境中执行：

```sh
python -m pip install -e './sdk/python[taskiq,celery,query,test]'
bash scripts/check-observe-sdk.sh --output-dir /tmp/luma-observe-sdk-dist
```

脚本检查 SDK、在临时源码副本中构建 wheel/sdist、校验元数据并导入已安装 wheel。
CI 分别覆盖 Python 3.10 + OTel 1.38.0 最低支持版本，以及 Python 3.12 + 当前依赖版本。
结束时清理本次构建和安装临时目录，只保留显式要求输出的分发包。脚本不访问生产，
不部署观测栈，也不发布 Registry 包。

准备 Docker 后还可执行单独的 SDK 协议检查：

```sh
python -m pip install -e .
python scripts/check-observe-sdk-protocols.py --platform linux/amd64
```

对应 CI job 在隔离容器中安装 SDK，以合成凭证将真实任务指标和自定义指标经 Collector
投递到 VictoriaMetrics，再加载生成的 Grafana 示例并对这些数据执行面板的 PromQL。
检查不暴露宿主端口，结束后清理自己的容器、卷和临时镜像标签。平台已有协议检查
继续覆盖其他共享投递与查询链路；消费方仍需验证真实部署的连通性、业务故障隔离、
数据新鲜度和性能预算。
