# Luma 控制台设计规范 {#luma-console-design}

控制台对齐 Cloudflare 当前控制台及其公开设计系统 Kumo。2026-10-31 对照 Kumo 的 `theme-kumo.css` token 以及 `LayerCard`、`ResourceListPage`、`Sidebar`、`Table`、`Badge`、`Button` 和 Flow 源码审阅。以下规则适用于所有工作空间：应用、部署记录、可观测、基础设施和设置。

## 参考来源与组件选择 {#sources-and-component-decision}

- [Kumo](https://github.com/cloudflare/kumo) 是 Cloudflare 基于 Base UI 的公开 React 组件库，其[资源列表布局](https://github.com/cloudflare/kumo/blob/main/packages/kumo/src/blocks/resource-list/resource-list.tsx)约束页面宽度并统一标题、说明和资源内容。
- [Flow](https://kumo-ui.com/components/flow/) 把有向关系画成点阵画布上的卡片。Luma 保留 Cytoscape 与 Dagre 处理图交互，只用样式对齐 Flow 的外观。
- Kumo 使用 [Phosphor](https://phosphoricons.com/) 图标。Luma 把用到的图标内置到 `dashboard-src/src/components/icons.ts`，由 `scripts/generate-dashboard-icons.mjs` 从 `@phosphor-icons/core`（MIT）生成。新增图标时在该脚本里加映射后重新运行；`check-luma.sh` 会以 `--check` 校验。
- [cf-ui](https://github.com/cloudflare/cf-ui) 已于 2021 年归档，不适合作为新增依赖。

保留现有 React 19、Base UI、Tailwind 4 和 shadcn，通过共享语义 token 和组件吸收参考设计；不为外观引入第二套组件或图框架。

## 通用规则 {#shared-rules}

- 表面由后到前：内容背后是灰色 `--canvas`；侧栏、顶栏和控件是白色 chrome（`--background`）；内容层是白色 `--card`，带细边框和 `shadow-xs`；表头底带和代码用 `--muted`。`--border` 用于分隔层，`--hairline` 用于层内行分隔。
- 状态色是指示色（`--success`、`--warning`、`--info`、`--destructive`：圆点、进度条、图标）。状态浅底上的文字用对应的 `*-foreground` 配 `*-soft`。白字只放在 `--primary` 和 `--destructive-fill` 上。两个主题的所有文字配色都不低于 4.5:1；半透明填充要先合成到实际背景上再计算。
- 侧栏包含身份（Logo 与集群 ID）、打开 ⌘K 命令面板的“搜索或跳转”、按分组排列且二级页面在原位展开的工作空间、舰队健康行和折叠按钮。收起后的窄栏用浮层打开二级页面。顶栏包含面包屑、同步状态、文档链接和账户菜单（外观、语言、退出）。没有全局刷新按钮。每个目的地在导航中只有一个入口。
- 主内容最大 1400px，横向留白响应式变化，章节间距 24px。终端工作区使用完整可用尺寸。
- 每页一个标题，操作右对齐；标题不带图标或 eyebrow，因为侧栏和面包屑已经标明工作空间。页头指标是一行安静的带标签计数，不做成看似可点击的胶囊。应用详情页显示对象名，下方为 line 标签页。
- 列表页把搜索和筛选放在列表上方的工具栏行，不加表单标签（每个控件保留无障碍名称）。只装一张表格的列表卡片不再重复标题。
- 卡片里单独放一张表格时贴边显示：浅色表头底带、细行分隔，首尾单元格与卡片留白对齐。键值表使用 `TableHead scope="row"` 形成固定宽度的标签列。行操作用 ghost 按钮加更多菜单。
- 关键数字用 `StatTile`：小标签、大数值、弱化说明，可选进度条。表格里的 CPU、内存和磁盘用 `UsageMeter`，只在超过 75% 和 90% 时着色。
- 状态徽标统一来自 `StatePill`/`stateVariant`。健康和未知状态用安静的圆点样式，让一列运行中的对象保持平静；警告和失败用填充浅底。缺失值显示为 `—`。
- 主按钮使用 Kumo 的强调样式（只向下加深的顶光渐变、内高光、更深的描边）。次要操作是白底描边按钮；行内删除是红色描边，对话框里的最终确认是实心红色。
- 页面级导航标签页（应用详情、存储、网络）使用带下划线的 `line` 变体；卡片内的视图切换保留分段控件。
- 日期、容量和百分比统一经 `format.ts`（`formatDateTime`、二进制单位的 `formatBytes`、`formatPercent`）。应用内链接使用 `spaLink`，带修饰键的点击仍可新开标签页。
- 网络页分开路由与节点拓扑。搜索和入口筛选同时作用于图与表格。图节点是点阵画布 `topology-grid` 上带细边框的白色卡片；图颜色先合成为不透明值再交给 Cytoscape。
- 保留明暗主题、键盘和窄屏导航。不要为了填满空状态而加入 API 诊断或无依据的健康结论。

## 验证边界 {#validation-boundary}

本地开发数据用于布局与交互检查，不证明生产健康，也不改变真实路由、证书或部署。

## 公开网站 {#public-website}

GitHub Pages 沿用中性底色、8px 卡片、细边框和蓝色操作。宣传页使用更大标题，嵌入控制台示意保持紧凑。中英文首页内容对应，示意使用示例域名并明确标注示例数据，不声称生产健康。

仓库 Markdown 构建为有导航、表格和目录的独立文档页。构建发布流程见[网站维护](website.zh-CN.md)。
