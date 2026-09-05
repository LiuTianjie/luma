# Dashboard 功能迁移与验收清单

本清单以重构前的 Git HEAD（v0.1.299）与本轮工作树为对照。用于记录现有能力的新入口及验证边界，不代表所有操作已在生产执行。路由均相对 `/dashboard`；对象名按单个 URL segment 编码。

## 验证口径

- **代码核对**：对照旧入口、事件处理器和新页面挂载条件，确认调用链保留；不等同于 API 或浏览器实测。
- **SSR**：`node --test dashboard-src/tests/infrastructure.cjs` 的 4 项渲染行为测试通过；现有业务 panel 在该测试中替身化，仅证明分区选择和页面语义。
- **真实运行待验**：需在新版 Manager 的浏览器/API 环境实际验证；发布负责人补充结果。涉及真实发布、重启、回滚、清理、通知应选定测试对象，不能用读取页面成功替代。

## 功能对应表

| 能力 | 原入口 | 新入口 | 当前证据 | 真实运行验收 |
|---|---|---|---|---|
| 登录、退出、主题、语言、刷新、侧栏收起 | 全局外壳 | 全局外壳 | 代码核对 | 登录/退出、切换、刷新错误与恢复 |
| 集群概况、风险原始信息、临时隐藏与恢复 | 总览 | `/`，按对象归组的诊断 | 代码核对；隐藏仍是浏览器限时隐藏 | 展开证据、隐藏、恢复、对象跳转 |
| 应用列表搜索、状态/区域筛选 | 应用 | `/apps` | 代码核对 | 各筛选组合、空结果、刷新保留 |
| 应用详情与访问地址 | 应用弹窗 | `/apps/:stack/overview` | 代码核对 | HTTP/TCP 地址语义、多服务应用 |
| 服务、实例状态、资源、存储、诊断 | 应用弹窗 | `/apps/:stack/services` 和 `/apps/:stack/services/:service`；应用概览保留卷与诊断 | 代码核对 | 实例完整性、资源和诊断对照 |
| 应用日志、服务/实例选择、暂停/继续、复制/下载 | 日志弹窗 | `/apps/:stack/logs`；全局 `/observe/logs` | 代码核对，复用 `ServiceLogsModal` inline | 真流、切实例、断线恢复、复制下载 |
| 容器 Shell | 应用服务按钮 → 弹窗 | 服务列表/详情 → `/terminal/service/:fullName` | 代码核对；inline SSR | 鉴权、输入、输出、resize、退出 |
| 节点 Shell | 节点矩阵 → 弹窗 | 节点矩阵/详情 → `/terminal/node/:name` | 代码核对；inline SSR | 真实节点连接、连接中切页、离开释放 |
| 配置读取、manifest/Compose 切换 | 应用弹窗 | `/apps/:stack/config` | 代码核对 | 配置读取失败/重试、完整配置 |
| 更新已有应用 | 临时 React 状态更新视图；Git 更新跳历史 | `/apps/:stack/edit`；Git 更新进度保留在应用页面 | 代码核对 | 刷新恢复、Git/镜像/Compose 更新 |
| 应用重启 | 应用列表/详情 | 应用列表/详情 | 同一确认及 replacement allocation 检查保留 | 测试应用重启、结果和错误 |
| 版本列表与回滚 | 应用详情内嵌区域 | `/apps/:stack/versions` | 代码核对 | 版本读取、受保护对象、测试回滚 |
| 创建应用：Git、镜像、YAML、模板、Compose | 创建页与独立构建页 | `/create` 来源选择；`/builds`、`/create/image`、`/create/yaml`、`/create/templates` | 代码核对；原表单/模板组件保留 | 各来源校验和测试部署，原高级选项 |
| 构建/部署历史筛选、分页、详细事件 | `/deployments` | `/deployments` 与 `/deployments/:kind/:id` | 代码核对 | 深链刷新、历史/事件翻页、筛选保持 |
| 构建取消、按原参数重试、重试来源 | 历史详情 | 历史详情 | 原处理器保留 | 测试构建取消/重试、准确跳转新记录 |
| 节点列表、Agent/终端就绪状态 | `/fleet` 混合长页 | `/fleet` 默认矩阵 | 代码核对；SSR | 全部节点/终端可用性 |
| 节点完整属性、关联服务 | 节点详情抽屉 | `/fleet/nodes/:name` | 代码核对；属性 SSR 保留 0/false | 名称编码、刷新、已移除对象 |
| 加入节点指令 | 节点页顶部 | `/fleet/join` | 代码核对；SSR | 复制失败反馈、命令域名 |
| 区域创建/管理 | 节点页顶部 | `/fleet/regions` | 原 `RegionPanel` 保留；SSR | 区域管理实际 API |
| 流量路径、拓扑、相关网络操作 | 节点页底部 | `/fleet/network` | 原 `TrafficPaths`/`NodeTopology` 保留；SSR | 拓扑绘制、网络操作 |
| Manager/Agent 升级与任务进度 | 节点页混排 | `/fleet/maintenance`；设置链接至此 | 原 `SystemUpdatePanel` 保留；SSR | 当前版本、升级状态、任务进度 |
| 存储类、卷、绑定、消费服务、警告 | `/storage` | `/storage` | 原 `StoragePanel` 保留；SSR | 卷/类/绑定数量对照 |
| 容量、增长、策略、清理预览、受保护引用、任务/宽限期 | `/observe?tab=storage` | `/storage/governance` | 原完整 `StorageGovernancePanel` 保留；SSR | 预览可读性、保护引用、测试清理 |
| 镜像查询/分页、详情、引用保护 | `/registry` | `/registry`、`/registry/image?image=...` | 代码核对 | 跨页镜像详情、缺失/错误状态 |
| 镜像保留策略、删除预览、GC/恢复队列 | Registry 混排及删除弹窗 | `/registry/policy`、`/registry/delete`、`/registry/cleanup` | 原 API 处理器保留；清理用预览中的选中项 | 测试镜像预览、删除、恢复/GC；刷新清理页要求重选 |
| 节点/服务历史指标、时间窗、采样断点 | `/observe?tab=metrics` 全量铺陈 | `/observe/metrics` 对象选择；应用 `/apps/:stack/metrics` | 代码核对；按选中对象加载历史 | 对象、时间窗、无数据与断点 |
| 告警事件、确认、时间线 | 可观测性告警 Tab | `/observe`、`/observe/incidents/:id` | 代码核对 | 真实事件、确认、详情刷新 |
| 告警规则新增/编辑/删除 | 规则 Tab 内联表单 | `/observe/rules`、`/observe/rules/:id`、`/observe/rules/new` | 代码核对 | 规则保存、选择对象、校验、删除 |
| 飞书渠道、密钥保留、测试通知 | 通知 Tab 内联表单 | `/observe/channels`、`/observe/channels/:id`、`/observe/channels/new` | 代码核对 | 保存、脱敏、显式测试发送和错误 |
| Secret/Registry 凭据管理 | 凭据页面侧表单 | `/settings/secrets`、`/settings/registries`、各自 `/new` | 代码核对 | 新增、轮换、删除、密钥不回显 |
| LAE 用户、租户、应用、放置状态 | 独立 LAE | `/lae` | 原页面未修改，侧栏保留 | 与旧版相同页面实际 API |

## 路由与终端专项核对

`App` 在通用 `AppRoutes` 之前解析 `objectRoutes.ts`。节点详情使用 `/fleet/nodes/:name`，不会把名为 `join`、`regions`、`network` 或 `maintenance` 的节点误认为管理页面。例如名为 `network` 的节点详情是 `/fleet/nodes/network`。未知对象有明确不存在提示。

旧 `/apps/:stack` 仍解析为应用概览；旧 `/observe?tab=storage|metrics|alerts|rules|notifications` 有重定向兼容。旧一级页面 `/builds`、`/create`、`/storage`、`/registry` 可直接访问，侧栏减项并未删除这些路由。

inline Shell 不挂载 `OverlayShell`，不声明模态，不全局截获 Escape。WebSocket 仍通过 `/v1/terminal/browser`，仍发送鉴权、resize、input、close；卸载会关闭已连接或尚在连接的 socket，旧事件回调不会写入已销毁终端。刷新页面会重新建立会话，不承诺恢复原交互进程。真实终端的输入输出尚须运行验证。

## 不能混淆为本轮已有能力的内容

- 旧 Dashboard 代码没有节点 drain/eligibility/标签编辑表单；只有状态显示和部署时节点/区域放置约束。本轮保留这些既有能力，不能宣称已新增节点调度控制。
- 旧容器 Shell 只传 service 标识，没有指定 allocation 的选择器。本轮 Shell 保持该协议。**日志**具有实例选择，不能将日志能力误称为 Shell 能力。
- 应用详情仍通过已有 Dashboard 聚合快照定位对象；不等于改成完全独立的对象详情 API。
- SSR 不能证明流式日志、Shell、告警发送或升级操作成功。版本发布成功也不能替代这些验收。

## 审计发现与修复

已修复非法基础设施子路径无提示、镜像详情加载/缺失/错误混淆、新详情链接修改键被拦截、交付页缺少直接 Git 构建入口。另修复跨应用日志服务选择、旧详情响应覆盖、详情刷新以及系统服务返回路径。

发布前共享门禁：1185 项 Python 测试、19 项 mjs 和 38 项 cjs 前端测试通过，TypeScript 与生产资源构建通过。浏览器已验证应用/服务页、终端独立页面路由、配置读取与更新硬刷新；开发服务使用本地测试数据，不将其计作真实 Manager 运行验证。
