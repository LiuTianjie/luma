# Dashboard shadcn 逐页核查记录

2026-09-12 · v0.1.359 · 现有 `base-nova` / Base UI / Lucide 配置。

## 核查范围与结论

全部业务 TSX、共享 UI 模块和业务 CSS 已按文件和渲染分支检查。下方 73 项清单包括页面、子页、表单、详情、弹层、错误和空状态，不只覆盖导航菜单。各组由独立 agent 修复，另由只读审计 agent 交叉检查，主任务整合共享组件并复验。

最终源码审计没有未解决的可见原生控件、自制组件外观、裸状态颜色、缺失的 Select/Dropdown 分组或表单标签关联。布局容器、数据驱动图形、终端画布和第三方 Grafana 内部不属于 shadcn 替代对象；它们外围的控件、状态与弹层均纳入核查。

## 修复内容

- `styles.css` 从 6301 行减至 218 行，移除旧组件外观及全局覆盖，保留语义主题和页面/画布布局。
- 页面统一使用 Card、Table、Field、Select、Checkbox、ToggleGroup、Alert、Empty、Skeleton、Dialog/Sheet；图表使用 Chart/Recharts，通知使用 Base UI Toast。
- Button 恢复当前 preset 的尺寸、背景和阴影；Calendar 恢复原生日期表格，并修复暗色选中今天的悬停颜色冲突。
- 修复长镜像摘要覆盖相邻列、长日志/标识符溢出、窄屏表格键盘横滚、表单标签与验证属性遗漏。
- 修复路径切换保留旧滚动位置；移动导航选页后关闭侧栏；隐藏 Tooltip 不再抢占 Esc；折叠导航保留可访问名称。
- 镜像删除保留一次操作完成删除与回收、实测释放量、旧响应隔离及无二次全量扫描的行为。

## 验证与边界

- 类型检查、生产构建、1430 项 Python 测试、106 项 Dashboard 测试通过。
- 30 个实际路由 × 桌面/窄屏、明暗及中英文组合共 120 次集成渲染检查，无运行错误、页面级横向溢出或无名称按钮；默认开发数据不能证明所有异常分支。
- 各组另用隔离数据验证提交、取消、验证失败、读写错误、加载、空态、分页、筛选、标签页、展开、复制、键盘、焦点与日期选择。外壳另验移动导航关闭、主题选择、Esc 关闭顺序、路径滚动复位、长面包屑和折叠导航名称。
- 会修改数据的浏览器场景全部使用本地 fixture。用户已登录的线上页面只读对照不代表新版已在生产生效。
- 无运行时消费者的 DetailDrawer、非内联终端用隔离组件验证；旧 useOverlay 已删除。共享 storageClass 兼容分支仅声明源码核查，没有声称实际部署验证。
- Grafana iframe 内部保持第三方界面，Cytoscape/xterm 保留专用渲染器。Base UI Tooltip 作为补充视觉提示，触发控件本身具有完整名称，见[官方用法](https://base-ui.com/react/components/tooltip#usage-guidelines)。

## 页面和状态清单

下列各项均完成源码核查；浏览器按上述分组场景验证，不把清单条目数等同于穷举测试次数。

| 分组 | 页面或分支 | 路径或入口 | 核查状态 |
| --- | --- | --- | --- |
| overview_entry | 总览 | `/` | data, no diagnostics, dismissed diagnostics, refresh error, loading |
| overview_entry | 未登录 | `/` | missing token, invalid token, login pending, error |
| overview_entry | 未知顶层路由 | `/does-not-exist` | not found |
| applications | 应用列表 | `/apps` | data, empty, filtered empty, loading, load error, stale data |
| applications | 应用详情 overview | `/apps/granary/overview` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 services | `/apps/granary/services` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 logs | `/apps/granary/logs` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 metrics | `/apps/granary/metrics` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 config | `/apps/granary/config` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 secrets | `/apps/granary/secrets` | data, missing object, loading, empty, error, long names |
| applications | 应用详情 versions | `/apps/granary/versions` | data, missing object, loading, empty, error, long names |
| applications | 应用内单服务详情 | `/apps/granary/services/granary_frontend` | data, missing service, long values |
| delivery_forms | 更新应用 | `/apps/granary/edit` | config pending, config error, missing app, service mode, compose mode |
| delivery_forms | 创建来源 | `/create` | normal, narrow |
| delivery_forms | 镜像创建 | `/create/image` | service, form, validation errors, preview pending, preview failure, deploy pending, deploy failed, deploy succeeded |
| delivery_forms | YAML 创建 | `/create/yaml` | service YAML, compose YAML, invalid YAML, source import, dirty source, preview |
| delivery_forms | 模板入口 | `/create/templates` | service category, compose category, selected template |
| delivery_forms | Compose 表单 | `/create/templates` | selected compose, invalid service, storage warnings, preview result |
| delivery_forms | Git 构建来源 | `/builds` | provider loading, no providers, provider error, repository empty, refs empty, build running, build failure, build success |
| delivery_forms | 手动 Git URL | `/builds` | form, invalid input, preview, running, error |
| delivery_history | 交付记录列表 | `/deployments` | data, empty, filtered empty, loading, error with cached rows, more loading |
| delivery_history | 交付详情 deployment | `/deployments/deployment/deploy-1` | succeeded, running, failed, interrupted, missing, events empty, events expired, events truncated, more error |
| delivery_history | 交付详情 build | `/deployments/build/build-1` | succeeded, running, failed, interrupted, missing, events empty, events expired, events truncated, more error |
| fleet | 节点列表 | `/fleet` | ready, drain, offline, terminal unavailable, empty, long names |
| fleet | 加入节点 | `/fleet/join` | token available, token unavailable, copy success, copy failure |
| maintenance_setup | 区域管理 | `/fleet/regions` | list, empty, create, invalid, save error, delete blocked |
| maintenance_setup | 系统维护 | `/fleet/maintenance` | loading, load error, idle, image preparing, manager running, fleet running, sentinel failed, reconnecting, completed |
| fleet | 路由与流量图 | `/fleet/network` | normal, empty, filtered empty, certificate issue, retry success/error, long domains |
| fleet | 节点拓扑 | `/fleet/network` | normal, empty, filtered, selected |
| fleet | 未知基础设施子页 | `/fleet/unknown` | not found |
| delivery_history | 节点详情 | `/fleet/nodes/cn-edge` | normal, missing, no services, resource data unavailable |
| delivery_history | 独立服务详情 | `/services/granary_frontend` | normal, missing, long config, runtime not ready |
| terminal_logs | 节点终端 | `/terminal/node/cn-edge` | connecting, connected, closed, error, unavailable |
| terminal_logs | 服务终端 | `/terminal/service/granary_frontend?stack=granary` | connecting, connected, closed, error, unavailable |
| registry_storage | 卷与存储类 | `/storage` | normal, empty, warnings, long mount paths |
| registry_storage | 容量与回收 | `/storage/governance` | loading, error, no builder, history plan, expired plan, blocked plan, builder running, inventory, preview, quarantine, restore, purge completed |
| registry_storage | 镜像列表 | `/registry` | normal, initial loading, scan pending, error, filter empty, selected, partial page, notice |
| registry_storage | 未知镜像子页 | `/registry/unknown` | unsupported section |
| registry_storage | 旧清理链接兼容 | `/registry/cleanup` | redirected/list compatibility |
| registry_storage | 镜像保留策略 | `/registry/policy` | off, recommend, enforce, validation, save pending, save error |
| registry_storage | 镜像详情 | `/registry/image?image=granary%2Ffrontend%40sha256%3Aeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` | ready, loading, pending, missing, error |
| registry_storage | 删除镜像确认 | `/registry/delete` | no selection after refresh, selected, blocked, referenced risk, busy elapsed, success, error |
| observability | 告警事件 | `/observe` | loading, load error, no evaluator, stale evaluator, empty, firing, resolved, silenced |
| observability | 告警详情 | `/observe/incidents/101` | loading, missing/error, events empty, firing, resolved |
| observability | 应用观测 | `/observe/apps` | iframe loading, iframe loaded, no apps, fullscreen error |
| observability | 日志检索 | `/observe/logs` | iframe loading, iframe loaded, fullscreen error |
| observability | 资源指标 | `/observe/metrics` | no targets, node target, service target, history loading, too few samples, history error, data gap |
| observability | 规则列表 | `/observe/rules` | loading, empty, data, error |
| observability | 新规则 | `/observe/rules/new` | preset loading, no presets, form, validation, save pending, save error |
| observability | 编辑规则 | `/observe/rules/cpu-home` | form, missing/error, save pending |
| observability | 通知渠道及投递记录 | `/observe/channels` | loading, empty, data, error, delivery retry |
| observability | 新增通知渠道 | `/observe/channels/new` | form, validation, save pending, save error |
| observability | 编辑通知渠道 | `/observe/channels/feishu-ops` | form, missing/error, secret configured |
| maintenance_setup | 首次安装 | `/setup` | missing, configured, error, save pending, validation result |
| settings_lae | 设置 secrets | `/settings/secrets` | loading, data, empty, error, cached refresh |
| settings_lae | 设置 registries | `/settings/registries` | loading, data, empty, error, cached refresh |
| settings_lae | 设置 git | `/settings/git` | loading, data, empty, error, cached refresh |
| settings_lae | 设置 storage | `/settings/storage` | loading, data, empty, error, cached refresh |
| settings_lae | 设置 maintenance | `/settings/maintenance` | loading, data, empty, error, cached refresh |
| settings_lae | 新增/轮换 secrets | `/settings/secrets/new` | form, invalid, save pending, save error |
| settings_lae | 新增/轮换 registries | `/settings/registries/new` | form, invalid, save pending, save error |
| settings_lae | 新增/轮换 git | `/settings/git/new` | form, invalid, save pending, save error |
| settings_lae | LAE applications | `/lae` | loading, empty, error, populated, large counts, long ids |
| settings_lae | LAE placements | `/lae` | loading, empty, error, populated, large counts, long ids |
| settings_lae | LAE users | `/lae` | loading, empty, error, populated, large counts, long ids |
| settings_lae | LAE tenants | `/lae` | loading, empty, error, populated, large counts, long ids |
| settings_lae | LAE operations | `/lae` | loading, empty, error, populated, large counts, long ids |
| settings_lae | LAE usage | `/lae` | loading, empty, error, populated, large counts, long ids |
| root | 通用确认弹层 | `/` | default, destructive, long description, pending, error |
| root | 偏好弹层 | `/` | open, closed, collapsed sidebar, mobile |
| terminal_logs | 实时服务日志与运行事件 | `/apps/granary/logs` | connecting, live, retrying, error, empty, paused, no selection, runtime loading, runtime error, pull diagnosis running/done/fail |
| delivery_history | DetailDrawer 兼容导出 | `当前页面或隔离组件` | node, service, open, close |
| terminal_logs | TerminalDrawer 非 inline 模式 | `当前页面或隔离组件` | connecting, connected, error |

## 文件覆盖

55 个业务模块（相对于 `dashboard-src/src`）：

- `App.tsx`
- `AppRoutes.tsx`
- `DetailDrawer.tsx`
- `Sidebar.tsx`
- `components/AlertingPanel.tsx`
- `components/ApplicationLogs.tsx`
- `components/ApplicationManagementPanel.tsx`
- `components/ApplicationProperties.tsx`
- `components/ApplicationSecrets.tsx`
- `components/ConfirmDialog.tsx`
- `components/DateTimePicker.tsx`
- `components/ErrorBanner.tsx`
- `components/LoginPanel.tsx`
- `components/NodeFleetMap.tsx`
- `components/NodeTopology.tsx`
- `components/ObservabilityPanel.tsx`
- `components/ObserveAppsPanel.tsx`
- `components/RegionPanel.tsx`
- `components/ServiceLogsModal.tsx`
- `components/StorageGovernancePanel.tsx`
- `components/StoragePanel.tsx`
- `components/SystemUpdatePanel.tsx`
- `components/TerminalDrawer.tsx`
- `components/Topbar.tsx`
- `components/TopologyFullscreenButton.tsx`
- `components/TrafficPaths.tsx`
- `components/charts.tsx`
- `components/primitives.tsx`
- `deploy/ComposeDeployForm.tsx`
- `deploy/DeployFormFields.tsx`
- `deploy/DeploySummary.tsx`
- `deploy/DeployTemplates.tsx`
- `deploy/DeployWorkspace.tsx`
- `deploy/GithubImportPanel.tsx`
- `deploy/SingleServiceDeployForm.tsx`
- `deploy/StepLog.tsx`
- `deploy/YamlPreviewEditor.tsx`
- `main.tsx`
- `pages/ApplicationsPage.tsx`
- `pages/BuilderPage.tsx`
- `pages/CredentialsPage.tsx`
- `pages/DeployPage.tsx`
- `pages/DeploymentsPage.tsx`
- `pages/LaeAdminPage.tsx`
- `pages/NodesPage.tsx`
- `pages/NotFound.tsx`
- `pages/ObservabilityPage.tsx`
- `pages/OverviewPage.tsx`
- `pages/PageHeader.tsx`
- `pages/PageLoading.tsx`
- `pages/RegistryPage.tsx`
- `pages/ResourceDetailPage.tsx`
- `pages/SetupPage.tsx`
- `pages/StoragePage.tsx`
- `router.tsx`

38 个共享组件（`dashboard-src/src/components/ui`）：

`accordion.tsx`、`alert-dialog.tsx`、`alert.tsx`、`avatar.tsx`、`badge.tsx`、`breadcrumb.tsx`、`button.tsx`、`calendar.tsx`、`card.tsx`、`chart.tsx`、`checkbox.tsx`、`collapsible.tsx`、`combobox.tsx`、`dialog.tsx`、`dropdown-menu.tsx`、`empty.tsx`、`field.tsx`、`input-group.tsx`、`input.tsx`、`label.tsx`、`pagination.tsx`、`popover.tsx`、`progress.tsx`、`scroll-area.tsx`、`select.tsx`、`separator.tsx`、`sheet.tsx`、`sidebar.tsx`、`skeleton.tsx`、`spinner.tsx`、`switch.tsx`、`table.tsx`、`tabs.tsx`、`textarea.tsx`、`toast.tsx`、`toggle-group.tsx`、`toggle.tsx`、`tooltip.tsx`

## 后续复验

遵循 [Dashboard 设计规范](dashboard-design.md)，新增页面使用现有组件、组件变体和语义主题。发布前运行 `bash scripts/check-luma.sh`，并按清单检查受影响状态与窄屏交互。
