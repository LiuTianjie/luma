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

原生 Metrics 直接进入指标存储；spanmetrics 在 Trace 采样前生成。Trace 存储保留错误、HTTP 5xx、超过一秒的慢链路和 10% 基础采样。统计代表 Collector 接收到的信号，SDK 采样、缓冲满、内存拒收和导出失败仍可能造成证据缺口。Collector 诊断指标从 `127.0.0.1:8888/metrics` 抓取，vmauth 诊断使用 `127.0.0.1:8429/metrics`。

应用使用官方自定义 Metrics/Span 描述业务阶段和结果。指标标签使用有限的服务、任务类型、结果枚举；执行编号放在 Span 或结构化日志中。导出通过后台有界缓冲完成；观测不可用时，业务的返回、异常、重试和确认语义仍由业务自身决定。

## Luma 内应用 {#luma-managed-producers}

部署共享观测栈后，重新部署带有官方 OTel SDK 的应用，Control 会注入原有 `OTEL_*` 地址、认证与资源属性。Manager Tailscale `:4319` 同时接收 Trace 和原生 Metrics。

现有自动埋点的默认值仍是 `OTEL_METRICS_EXPORTER=none`。需要指标的应用自行设置 `OTEL_METRICS_EXPORTER=otlp`，或初始化官方 MeterProvider/有界 Reader。应用执行链路独立于 Collector；接入使用官方协议与 SDK，共享存储沿用现有组件。

## 可选的机器访问 {#optional-machine-access}

`observe-access` 使用官方 vmauth 1.114.0；启动脚本只生成其配置，认证、路由、并发与超时均由 vmauth 实现。默认凭证为空，以零授权用户启动。

网关仅监听 `127.0.0.1:8427`，数据存储与诊断入口保持回环监听。读取凭证有整个集群观测数据的读取权限，适合可信后端或运维工具；应用的用户权限仍由应用后端负责。凭证只进入服务端配置，Luma 管理凭证保持独立。

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

每个角色最多 4 个并发，总并发 8，上游响应头等待预算 3 秒。调用方还需设置整体超时及响应/条数上限。Authorization 在转发到回环上游前移除；数据删除、写入、reload 和 debug 路径由读取身份的固定路由集隔离。vmauth 自身诊断位于独立端口。

网格外的官方 SDK 可通过 HTTPS 接入。替换 [`traefik-access.example.yml`](../observe/traefik-access.example.yml) 的 DNS/TLS 域名后，由运维显式安装到 Manager 现有 Traefik file-provider 目录，并按现有入口配置 DNS。模板只暴露认证网关，既有 `/grafana` 登录认证保持当前机制。

```text
OTEL_EXPORTER_OTLP_ENDPOINT=https://observe-api.example.com/otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<ingest-token>
```

凭证按对应官方 SDK 的规则进行 URL 编码。Luma 部署的进程继续使用自动注入的网格地址与 Token。查询后端使用 `/metrics`、`/logs`、`/traces` 为 base URL，并携带独立读取 Token。日志仍通过原有 Nomad stdout/stderr 采集。

轮换时替换对应 scope Secret、重新部署，再更新消费者；每个角色同时接受一个 Token，切换期间允许短暂查询中断或遥测丢弃，业务执行继续。停用角色时把其环境值恢复为字面量空字符串后重新部署；停用 HTTPS 访问时移除显式安装的 Traefik 配置文件。

## 应用自行维护看板与规则 {#application-owned-dashboards-and-rules}

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
```

CI 执行上述检查。通用协议检查以官方真实组件和合成输入验证：零用户启动、读写隔离、网格 Metrics 认证、原生计数、采样前 spanmetrics、错误/慢 Trace 留存、LogsQL/TraceQL、诊断采集，以及扩展空目录启动和配置热加载。隔离网络无宿主端口，通知发送关闭，脚本结束清理自己的容器、卷和镜像标签。Tempo 的独立检查继续验证全新存储及重启后读取持久化块。

部署后核对实际入口与凭证，再观察代表性应用的链路、导出失败/拒收、新鲜度、CPU/RSS、磁盘增速和保留期回收。这些是协议与生命周期检查；生产吞吐和资源预算需要各应用按自身验收手册测量。Tempo 仍与 Manager 共享磁盘，保留时间与写入速率上限并非磁盘配额，既有存储和内存保护需要持续保留。
