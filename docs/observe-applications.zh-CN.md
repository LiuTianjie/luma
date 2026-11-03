# 标准应用观测接入 {#standard-application-observability}

`luma-observe` 提供共享的标准 OTLP 接入、指标/日志/Trace 存储和查询认证。应用自行定义埋点、业务状态、交付预算、看板和告警规则；Luma 使用官方组件消费这些数据，观测栈独立于 Control 部署。

## 标准数据流 {#data-flow}

```text
Official SDK ── OTLP ── Collector ── native Metrics ────── VictoriaMetrics
                            ├───── spanmetrics ───────── VictoriaMetrics
                            └───── tail sampling ─────── Tempo
Nomad stdout/stderr ── existing log shipper ────────────── VictoriaLogs

Backend / operator ── read credential ── vmauth ───────── standard query APIs
External producer ── ingest credential ── vmauth ──────── Collector OTLP HTTP
Application config ── read-only extension volume ──────── Grafana / vmalert
```

原生 Metrics 直接进入指标存储；spanmetrics 在 Trace 采样前生成。Trace 存储保留错误、HTTP 5xx、超过一秒的慢链路和 10% 基础采样。无父级、带 `task.name` / `task.framework` 的 CONSUMER 根 Span 在独立分支按完成结果采样，避免长任务较早结束的子 Span 决定丢弃最终失败摘要；子 Span 仍可能不完整。统计代表 Collector 接收到的信号，SDK 采样、缓冲满、内存拒收和导出失败仍可能造成证据缺口。Collector 诊断指标从 `127.0.0.1:8888/metrics` 抓取，vmauth 诊断使用 `127.0.0.1:8429/metrics`。

应用使用官方自定义 Metrics/Span 描述业务阶段和结果。指标标签使用有限的服务、任务类型、结果枚举；执行编号放在 Span 或结构化日志中。导出通过后台有界缓冲完成；观测不可用时，业务的返回、异常、重试和确认语义仍由业务自身决定。

## Luma 内应用 {#luma-managed-producers}

部署共享观测栈后，重新部署带有官方 OTel SDK 的应用，Control 会注入原有 `OTEL_*` 地址、认证与资源属性。Manager Tailscale `:4319` 同时接收 Trace 和原生 Metrics。

现有自动埋点的默认值仍是 `OTEL_METRICS_EXPORTER=none`。需要指标的应用自行设置 `OTEL_METRICS_EXPORTER=otlp`，或初始化官方 MeterProvider/有界 Reader。应用可选择独立版本的 [`luma-observe-sdk` Python 包](../sdk/python/docs/integration.zh-CN.md)，复用有界 SDK 初始化、Taskiq/Celery 生命周期适配、自定义 Span 和 Metrics。它沿用官方 OTLP 协议及现有凭证，业务字段与结果仍由应用定义。直接使用官方 OTel 接入同样受支持，无需新增事件数据库或每应用 Collector。

Control 0.2.26 及以上按键合并 `OTEL_RESOURCE_ATTRIBUTES`：应用设置的值优先，只追加
缺少的 `luma.stack`、`luma.task`、`luma.region` 等注入键，其他应用环境值仍作为显式
覆盖保留。此前自行设置资源属性字符串的应用，重新部署时可能一次性补齐缺失的
Luma 标签、改变相应序列身份；已获得完整注入属性的应用不受这次合并影响。

Worker 频繁轮换的应用可以显式选择[按服务聚合 Span 指标](observe-service-span-metrics.zh-CN.md)。
资源开关精确为 `luma.observe.span_metrics=service` 时，仅聚合新生成的派生指标；原始
Trace 和原生 Metrics 的实例标识保持完整。需要单独部署新版 Observe，SDK 0.1.0
已兼容。未开启的应用继续走默认 spanmetrics 分支。

## 可选的机器访问 {#optional-machine-access}

`observe-access` 使用官方 vmauth 1.114.0；启动脚本只生成其配置，认证、路由、并发与超时均由 vmauth 实现。默认凭证为空，以零授权用户启动。

网关仅监听 `127.0.0.1:8427`，数据存储与诊断入口保持回环监听。读取凭证有整个集群观测数据的读取权限，适合可信后端或运维工具；应用的用户权限仍由应用后端负责。凭证只进入服务端配置，Luma 管理凭证保持独立。

只需要读取自身数据的应用后端，应改用按应用限定范围的读取身份。`LUMA_OBSERVE_SCOPED_READERS` 是一个 JSON 列表，每一项给出读取身份的 `name`、独立的 `token`、`namespace`，以及允许读取日志的 Luma 应用 `apps`：

```json
[{"name": "orders", "token": "<32-512 chars>", "namespace": "orders", "apps": ["orders-workers"]}]
```

过滤条件由 vmauth 在服务端添加，客户端无法覆盖：

| 信号 | 限定读取身份的权限 |
| --- | --- |
| 指标 `/metrics/api/v1/query`、`query_range` | 只能读取 `service_namespace=<namespace>` 的序列 |
| 日志 `/logs/select/logsql/query` | 只能读取 `app` 属于 `apps` 的日志流 |
| Trace `/traces/api/traces/<32位十六进制ID>` | 只能按完整 Trace ID 读取，不能搜索 |

Tempo 没有网关层的过滤能力，所以限定读取身份不能搜索 Trace，只能读取已知 ID 的 Trace，通常来自自身的生命周期日志。Trace ID 是随机的 128 位值，这个身份也无法列出其他应用的 ID；但 ID 一旦泄漏就可以被读取，应用仍应校验返回 Span 的 `service.namespace` 属于自己。所有凭证（包括限定和不限定的）必须互不相同。

需要启用的角色，在实际部署的 `observe/docker-compose.yml` 中把空值替换为作用域 Secret 引用，保留同服务的其他环境配置：

```yaml
environment:
  LUMA_OBSERVE_READ_TOKEN: ${LUMA_OBSERVE_READ_TOKEN}
  LUMA_OBSERVE_INGEST_TOKEN: ${LUMA_OBSERVE_INGEST_TOKEN}
```

未启用的角色继续保持字面量 `""`。Luma 使用 `${NAME}` 引用 Secret，Compose 的 Shell 默认值语法不参与 Luma 渲染。按需执行 `luma secret set LUMA_OBSERVE_READ_TOKEN --scope luma-observe` 或 `luma secret set LUMA_OBSERVE_INGEST_TOKEN --scope luma-observe`，CLI 会私密提示输入凭证；两个角色使用各自随机生成的 32–512 字符 Bearer Token。

变量清单见 [`observe/.env.example`](../observe/.env.example)。在 `observe/` 通过原有工作流重建/部署独立栈，例如 `luma build local . --platform linux/amd64`。Control/CLI 升级与这个栈的更新各自独立，原有数据目录、Tempo 2.10.10 内存配置及 72 小时保留期继续沿用。

| 身份 | 路径 | 标准 API |
| --- | --- | --- |
| 读取 | `/metrics/api/v1/query`、`/metrics/api/v1/query_range` | Prometheus 兼容查询 |
| 读取 | `/logs/select/logsql/query` | LogsQL |
| 读取 | `/traces/api/search` | TraceQL 搜索 |
| 读取 | `/traces/api/traces/<32位十六进制ID>` | Tempo Trace 查询 |
| 写入 | `/otlp/v1/metrics`、`/otlp/v1/traces` | OTLP/HTTP，JSON 或 protobuf |

每个读取身份（包括按应用限定的读取身份）最多 16 个并发，写入身份保持 4 个并发，网关总并发 32，上游响应头等待预算 3 秒。入口限额允许多个管理页面共享读取身份；VictoriaMetrics 仍最多同时执行 4 个查询，每查询内存预算 64 MiB，沿用其查询结果缓存。网关没有额外结果缓存或重复请求合并。调用方应把过载作为数据暂不可用处理，并设置整体超时及响应/条数上限。Authorization 在转发到回环上游前移除；数据删除、写入、reload 和 debug 路径由读取身份的固定路由集隔离。vmauth 自身诊断位于独立端口。

网格外的官方 SDK 可通过 HTTPS 接入。替换 [`traefik-access.example.yml`](../observe/traefik-access.example.yml) 的 DNS/TLS 域名后，由运维显式安装到 Manager 现有 Traefik file-provider 目录，并按现有入口配置 DNS。模板只暴露认证网关，既有 `/grafana` 登录认证保持当前机制。

```text
OTEL_EXPORTER_OTLP_ENDPOINT=https://observe-api.example.com/otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<ingest-token>
```

凭证按对应官方 SDK 的规则进行 URL 编码。Luma 部署的进程继续使用自动注入的网格地址与 Token。查询后端使用 `/metrics`、`/logs`、`/traces` 为 base URL，并携带独立读取 Token。日志仍通过原有 Nomad stdout/stderr 采集。

外部应用不要求由 Luma 部署。按标准 `service.namespace`、`service.name`、`service.instance.id` 和 `deployment.environment.name` 标识服务后，可使用同样的 Metrics、Trace 和 Grafana；它不会自动获得 Nomad 部署状态或实时 stdout。当前没有外部 OTLP Logs 写入路由，外部日志需要另外接入。

当前 PRW exporter 要求单调计数、直方图等使用 cumulative temporality。SDK 默认满足该条件；外部 SDK 或显式设置 DELTA 的进程需要改回 cumulative，本栈尚无 delta-to-cumulative 转换。接入身份、长任务采样和持久队列的现状与后续方案见[通用观测平台设计](observe-platform-design.zh-CN.md)。

轮换时替换对应 scope Secret、重新部署，再更新消费者；每个角色同时接受一个 Token，切换期间允许短暂查询中断或遥测丢弃，业务执行继续。停用角色时把其环境值恢复为字面量空字符串后重新部署；停用 HTTPS 访问时移除显式安装的 Traefik 配置文件。

## 应用自行维护看板与规则 {#application-owned-dashboards-and-rules}

可选 Python 包提供离线工具 `luma-observe-dashboard`，将应用 JSON 配置转换为标准 Grafana 看板。指标、变量、面板类型、单位与阈值由应用配置，见 [SDK 示例](../sdk/python/examples/dashboards/)。生成文件使用以下现有扩展交付流程，无需增加服务或凭证。

Grafana 和 vmalert 以只读方式挂载 `observe-extensions`，Manager 路径为 `/srv/luma/data/luma-observe/extensions`，首次部署可为空。应用使用现有运维文件交付流程安装已审核的标准定义：

```text
extensions/
  dashboards/<application>.json    # standard Grafana dashboard JSON
  rules/<application>.yml          # standard vmalert/Prometheus rule groups
```

Grafana 将看板放在 Applications 文件夹并递归发现 JSON；vmalert 同时读取 `rules/*.yml`、`rules/*.yaml` 和内置基础设施规则。二者每 30 秒重扫。各应用使用独立的 dashboard UID、规则组与记录指标名；目录需要可遍历，文件需要可读。使用不同扩展名的临时文件写完后原子 rename，避免读取半份文件。

规则属于可信运维配置，先用官方 `victoriametrics/vmalert:v1.114.0 -rule=<file> -dryRun` 校验，再检查 Grafana 实际看板和 vmalert `/api/v1/rules`。规则解析失败保留上一份有效配置；删除对应文件后下轮扫描移除。通知仍使用既有 Alertmanager 路由、分组、维护静默和已授权接收器；加载规则本身不会创建通知目标。

## 验证 {#verification}

安装 Luma 依赖并准备 Docker，在仓库根目录执行：

```bash
python scripts/check-observe-protocols.py --platform linux/amd64
python scripts/check-observe-tempo.py --platform linux/amd64
python scripts/check-observe-sdk-protocols.py --platform linux/amd64
```

CI 执行上述检查。通用协议检查以官方真实组件和合成输入验证：零用户启动、读写隔离、网格 Metrics 认证、原生计数、采样前 spanmetrics、错误/慢 Trace 留存、LogsQL/TraceQL、诊断采集，以及扩展空目录启动和配置热加载。隔离网络无宿主端口，通知发送关闭，脚本结束清理自己的容器、卷和镜像标签。Tempo 的独立检查继续验证全新存储及重启后读取持久化块。

SDK 独立检查安装分发包，将任务原生指标和自定义指标经 Collector 投递到 VictoriaMetrics，再加载两份生成的 Grafana 示例，并使用采集数据执行各面板的 PromQL。

部署后核对实际入口与凭证，再观察代表性应用的链路、导出失败/拒收、新鲜度、CPU/RSS、磁盘增速和保留期回收。这些是协议与生命周期检查；生产吞吐和资源预算需要各应用按自身验收手册测量。Tempo 仍与 Manager 共享磁盘，保留时间与写入速率上限并非磁盘配额，既有存储和内存保护需要持续保留。
