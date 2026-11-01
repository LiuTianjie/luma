# 应用日志分析 {#application-log-investigation}

使用 `luma app log-query` 查询保留的历史日志，使用 `luma app logs` 查看当前 Nomad 实例的日志尾部和实时流。历史查询依赖 luma-observe 已有的 VictoriaLogs 服务，无需增加部署组件。

```sh
luma app alerts my-app --format json
luma app log-query my-app --since 1h --errors --format json
luma app log-query my-app --since 2026-11-01T08:00:00Z --until 2026-11-01T09:00:00Z --task worker --contains 'request-id' --format json
luma app log-query my-app --cursor 'NEXT_CURSOR' --format json
```

## API {#api}

`GET /v1/logs/query` 与其他 Control 读取接口使用相同的 deploy-token 或控制台会话认证。VictoriaLogs 仍仅监听 manager 回环地址；客户端不需要 Grafana 凭证，也不需要直接访问存储。

| 参数 | 含义 |
| --- | --- |
| `app` | 必填，精确的应用／栈 ID |
| `task`, `allocation` | 可选，精确的任务和实例 ID |
| `stream` | 可选，`stdout` 或 `stderr` |
| `since`, `until` | Unix 秒数、含时区的 RFC3339，或相对当前时间的时长；默认最近一小时 |
| `contains`, `exclude` | 字面子串过滤，不支持正则或任意 LogsQL |
| `errors` | `true` 启用固定的错误文本启发式匹配 |
| `limit` | 1–500，默认 100 |
| `cursor` | 不透明的下一页游标；仅携带 `app`、`cursor` 和可选的 `limit` |

响应包含 `source`、`app`、规范化的 `query`、绝对时间 `range`、`entries`、`returned`、`page: {limit, hasMore, nextCursor}`、`warnings` 和 `updatedAt`。每条记录包含 `observedAt`、`line`、`task`、`allocationId`、`stream`、`node`、`app` 和 `truncated`，不重复提供旧式 `logs` 数组或逐行游标。`returned` 是当前页条数，不是匹配总数。常见凭证格式通过共享日志样本清理器脱敏；任意个人数据不会自动脱敏。

每次查询限定一个应用、最多 24 小时、四个并发后端读取、五秒存储查询时间和 8 MiB 响应。游标偏移上限为 10,000；超过后需缩小时间范围或过滤条件。无效参数、存储故障和超时会明确报错，不会伪装成成功的空结果。

分页固定查询时间窗口，并按观察时间及次级排序字段确定顺序，但不是不可变的存储快照。延迟采集和保留策略可能改变分页内容，同时间戳批次不保证原始行顺序。相同日志行会保留，不做去重。时间过滤使用采集器观察时间，不是消息内嵌的应用时间戳。

读取异常堆栈上下文时，移除文本／错误过滤，在观察时间附近查询相同任务和实例。未被采集或已过期的日志无法由此接口恢复。保留周期由 observe 部署配置，CLI 不负责配置。

`luma app alerts APP` 封装 `GET /v1/alerting/incidents?app=APP`，支持 `limit`、`cursor` 和可选的 `status`，保留已有事件 ID、证据摘要和链接。事件已恢复不代表业务任务已恢复。

[luma-logs Skill](../skills/luma-logs/SKILL.md) 提供 Agent 工作流。查询编译器使用文档定义的 [LogsQL 排序与偏移管道](https://docs.victoriametrics.com/victorialogs/logsql/#sort-pipe)。
