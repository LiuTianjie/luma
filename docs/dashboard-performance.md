# Dashboard 性能修复与验收（2026-09-12）

## 线上基线

使用现有 CLI 登录上下文进行 12 次只读请求，线上版本为 0.1.355。
完整脱敏记录见 `dashboard-performance-baseline.json`。

| 接口 | 两次端到端耗时 | 解压后响应体 |
| --- | --- | --- |
| health | 2.89 / 2.44 秒 | 522 B |
| dashboard | 3.45 / 4.53 秒 | 108 KB |
| control-resources | 2.08 / 1.84 秒 | 12 KB |
| history（50 条） | 3.52 / 3.86 秒 | 19 KB |
| registry inventory（50 条） | 10.36 / 6.43 秒 | 128 KB |
| alerting overview | 5.54 / 2.40 秒 | 163 B |

这些数据包含连接与网络开销；健康接口自身已有明显耗时，不能把整段时间
归因于服务器计算。没有刷新扫描、部署、升级或其他线上写入。
下述新代码尚未部署，不能用这些基线声称线上已提速。

## 页面数据契约

沿用 `/v1/dashboard` 并增加 scope，旧客户端不带参数仍得到完整响应。
同步 HTTP 和生产 ASGI 路由均传递相同参数；旧同步 Registry 路由也补齐分页。

| 页面 | scope / 独立接口 | 查询边界 |
| --- | --- | --- |
| 总览 | overview | 节点、服务摘要和诊断；省略任务详情、路由、存储；保留资源统计以维持内存告警 |
| 应用列表 | applications | 以应用为单位分页，默认 50，上限 200；q/status/region 在全清单筛选；全局计数与选项独立于当前页 |
| 应用详情、编辑 | application + app | 只查询指定作业及其 allocations，保留存储、任务、配置回退所需字段 |
| 旧服务详情及服务终端 URL | full | 保留跨应用解析服务归属的兼容行为 |
| 创建入口 | 无 | 来源选择无需集群快照 |
| 创建表单 | deploy | 节点、区域、存储类、构建配置，无服务作业查询 |
| 构建入口、加入节点、区域管理、维护、节点终端 | nodes | 无服务与路由扫描 |
| 节点舰队图 | fleet | 节点与服务放置摘要；保留任务节点用于计数 |
| 节点详情 | full | 保留节点上服务与资源明细的兼容行为 |
| 网络拓扑 | network | 节点、服务放置、流量路径；无资源统计查询 |
| 存储卷 | storage | 只返回卷、服务关联和存储类 |
| 存储治理、Registry、凭据、发布历史、LAE | 无 | 使用各自独立接口 |
| 应用监控、日志、告警事件/规则/渠道 | directory | 节点与应用身份目录；Nomad 仅一次作业列表读取，无作业详情和 allocations |
| 资源指标 | metrics | 节点与服务指标；历史通过有界 batch 接口读取 |
| 设置向导 | setup | readiness 与配置，无服务查询 |

局部响应不会填造空的全局健康结果，侧边栏只在数据完整的 scope 显示舰队健康。
应用列表查询变化保留已挂载内容与输入焦点；切换应用身份时清除旧对象内容。
列表筛选有 250 ms 防抖，并重置页码。列表发起更新时保留确认框与进度；配置缺失
时先按需取得完整应用，再生成回退草稿，避免摘要缺少存储字段而产生错误更新。

## 后端与请求调度

- Nomad 静态作业配置缓存按 endpoint、ACL 摘要、namespace、job ID 和修订号隔离。
  修订变更、删除、TTL 到期会失效，查询失败不回退旧配置。只保存摘要用静态字段，
  不保存环境变量或凭据。最多 256 条、单条 128 KiB、TTL 300 秒。
- 作业列表及 allocations 每次实时查询；定向详情仅查询当前应用。
  每次 Nomad 读取默认 5 秒，补充查询共享 10 秒预算，最多 8 个并发，预算耗尽后
  不继续派发队列；已在途读取仍受单次超时约束。不同客户端之间没有全局请求合并。
- 完整快照资源读取与状态/作业读取并行。总览保留 service-memory 告警语义并有回归测试。
- Registry 缓存读取/策略读取只加载配置快照，不再展开全部构建、任务、发布事件及日志；
  显式刷新继续使用完整历史，保留镜像保护判断。分页只深拷贝所选页和元数据。
- 指标 batch 最多 32 个目标，认证一次、共享一次历史快照和时钟，逐目标返回错误。
  原单目标接口保留，认证也改用轻量配置读取。
- 告警事件/规则/通知页分别请求 2/4/3 组数据，替代每页都请求全部 6 组。
- LAE 按当前标签加载：应用与租户名称为 2 个独立请求，应用表不等待名称补全；
  其他标签首次打开时各 1 个请求。保留每页的数据、错误与加载状态，换 token 清空；
  未加载统计显示未知，运行/失败计数明确标为当前页。
- 共享 Dashboard 请求去重，20 秒超时，导航/换 token 取消并隔离旧响应；
  自动刷新在文档隐藏时暂停，重新可见后恢复。指标刷新保留上次图表。
- 拓扑按实际图形内容生成稳定快照；只改变 CPU/内存数值时不再重跑 Dagre 布局。

## 验证证据

自动测试覆盖分页跨页筛选、摘要/详情字段、诊断兼容、两种路由入口、认证、
缓存修订失效/隔离/删除/并发部署竞争、查询预算、取消及过期响应、Registry 镜像保护。
前端覆盖请求去重、搜索与 scope 选择、指标 batch、真实拓扑模型与布局。

浏览器验收使用本地生产构建、真实 scope handler 与合成的 62 个应用数据，
不连接生产变更接口。已验证第二页显示 51–62，打开 app-061 能显示完整存储，
浏览器返回恢复 offset=50；搜索 app-061 后页码重置、只显示 1 条，输入框持续聚焦。
创建入口无需 Dashboard 请求；告警页实际请求 directory + overview + incidents。
浏览器控制台未记录错误。该验收不代表真实设备/线上交互耗时测量。

可复现合成基准（不能作为线上加速比例）：

- 50 个作业：服务汇总冷缓存 52 次 Nomad 读取、热缓存 2 次；目录 1 次、定向详情热缓存 2 次。
- 32 个指标目标、2.2 MB 历史文件：逐目标读取中位数 769.8 ms，batch 98.0 ms。
  当前指标页面为单选，主要价值是复用快照和提供有界批量能力。
- 20 节点/200 服务拓扑：稳定快照平均约 0.14 ms，单次布局约 53 ms；不变图跳过布局。

排除了“每次读取数据库都会写 schema”的假设：当前版本已在 schema_version 一致时
跳过迁移；40 次真实读取没有写入，也能与未提交写事务并行。未修改数据库结构。

## 发布与后续线上验收

本修复版本为 v0.1.356，配套前后端一起交付（batch 接口需要新后端），保留原版本用于回退。
软件发布与 Manager 升级分开执行；本次软件发布不修改运行中的 Manager。
上线后应在相同网络下复测上述接口，并采集每页请求数量、传输体积、TTFB、首次可操作时间。
再用多标签页、慢 Nomad、节点失联检查负载与错误表现。冷缓存仍可能查询多个作业；
总览和指标仍需完整实时状态，不能把分页体积下降误解为所有冷查询成本已消失。

最终验证命令包括：

```sh
python -m unittest discover -s tests -p 'test_dashboard*.py'
python -m unittest discover -s tests -p 'test_metrics*.py'
python -m unittest discover -s tests -p 'test_nomad*.py'
python -m unittest discover -s tests -p 'test_registry*.py'
python -m unittest discover -s tests -p 'test_productization.py' -k dashboard
python -m unittest discover -s tests -p 'test_productization.py' -k service_stats
node --test dashboard-src/tests/*.cjs tests/dashboard/*.test.mjs
npm run typecheck:dashboard
npm run build:dashboard -- --outDir /tmp/luma-dashboard-performance-build
git diff --check
```

交互验收构建写入临时目录；发布前由 scripts/check-luma.sh 重新生成受 Git 忽略的 Dashboard 资产。
临时浏览器与测试服务已关闭。
