# 01. 产品范围与体验设计

## 1. 产品原则

LAE 的核心承诺是：**用户只需要提供代码或静态产物，平台负责判断、解释、收集必要配置、部署并持续呈现真实状态。**

设计遵守以下优先级：

1. 支持范围内的成功率高于“看起来什么都能部署”。
2. 真实系统状态高于伪进度和乐观文案。
3. 恢复路径与成功路径同等重要。
4. 默认隐藏 region、node、Nomad、Traefik、storageClass 等基础设施概念。
5. Web、CLI 和 Agent Skill 共用同一套 API 与状态机，不各自实现一套逻辑。
6. 艺术感来自空间、材质、节奏和因果关系，不使用泛化的 AI 紫色渐变、聊天气泡或无意义发光。

## 2. 用户与角色

### 2.1 MVP 用户模型

- 一个注册用户自动创建一个 personal tenant。
- 用户是 personal tenant 的 `owner`。
- 数据层从第一天包含 `tenant_id`，但 MVP 控制台暂不开放组织、邀请和成员管理。
- 超级管理员不属于普通 tenant，通过 Luma 管理入口和 LAE admin API 工作。

### 2.2 预留角色

| 角色 | 能力 |
| --- | --- |
| owner | 账户、支付、token、应用和删除 tenant |
| admin | 应用、成员、token、用量，不可删除 tenant |
| developer | 部署、环境变量、日志、重启，不可支付 |
| billing | 套餐、订单、发票，只读应用信息 |
| viewer | 只读应用、部署、日志和用量 |

## 3. 信息架构

### 3.1 公共站

- 首页：一句承诺、即时演示、模板湖面、CLI 示例、支持边界。
- 模板：可交互的模板探索与一键部署入口。
- 定价：Lite / Pro / Ultra，明确限制和不支持项。
- 文档：Web、CLI、API、Agent Skill、框架适配说明。
- Status：平台状态、历史事故和维护窗口。
- 登录 / 注册 / 邮箱验证 / 找回。

### 3.2 用户控制台

- **Home**：新用户进入“创建第一个应用”；老用户看到健康摘要、最近部署和用量风险。
- **New Deployment**：来源 → 诊断 → 环境变量 → 确认 → 部署。
- **Applications**：应用状态、稳定域名、当前版本、来源、最近部署。
- **Application Detail**：
  - Overview
  - Deployments / Versions
  - Source / Check updates
  - Environment Variables
  - Logs / Metrics
  - Settings / Suspend / Restart / Delete
- **Deployments**：跨应用操作记录、失败恢复和继续观看。
- **Integrations**：GitHub App、私有 Git、Webhook。
- **Deploy Tokens**：创建、命名、scope、到期、最近使用、撤销。
- **Usage & Billing**：entitlement、用量、订单、支付、续费。
- **Account & Security**：邮箱、会话、登录记录和账户删除。

### 3.3 超级管理员

在现有 Luma Dashboard 增加 `LAE` 一级区域，但数据仍来自 LAE admin API：

- Tenants / Users
- Apps / Deployments / Operations
- Plans / Entitlements / Usage / Payments
- Abuse / Suspensions / Audit
- Capacity / Build workers / Runner pools
- Artifact / Registry / Garbage collection
- Platform health / SLO / Incidents

Luma 节点、storage、registry 和 terminal 等基础设施页面不对普通 LAE 用户开放。

## 4. 核心用户旅程

### 4.1 邮件注册

1. 用户输入邮箱并接受服务条款。
2. 服务端统一返回“若邮箱可用，验证邮件已发送”，避免账户枚举。
3. 用户点击 magic link 或填写验证码。
4. 后端原子创建 `user`、personal `tenant`、owner membership、Lite entitlement。
5. 自动创建一枚默认 deploy token；只在本次页面显示明文，用户可复制或立即撤销。
6. 进入首次部署引导，不先展示空白 dashboard。

验证码需要短时有效、单次使用、按邮箱/IP/设备限流。注册完成后 deploy token 才生效。

### 4.2 首次 Web 部署

1. 用户从湖面模板或“从代码开始”进入。
2. 选择：HTML、ZIP、GitHub、私有 Git、模板。
3. 缺少 Git 凭据时，当前步骤内打开授权抽屉；授权完成后回到原上下文。
4. 服务端创建 source request；文件上传可直接形成不可变 revision，Git/模板由 Luma builder 拉取并解析为 commit + snapshot digest 后形成 revision。
5. Luma builder 在隔离任务中运行 LAE Agent runner，完成确定性诊断并保存 DeploymentPlan/BuildPlan。
6. 控制台展示识别出的应用类型；若为 Compose，则同时展示服务拓扑、公开/内部入口、依赖、构建方式、端口、命名卷、环境变量、阻塞项、风险和预计配额。
7. 用户按应用/服务分组补齐必填变量；secret 值保存后不可回显。
8. 用户确认应用名称、稳定随机域名和部署计划。
9. 后端预留配额并创建 durable operation。
10. Luma builder 按已签名 BuildPlan 构建/扫描/推送镜像；平台生成并保存 manifest，再调用 Luma 等待调度、路由和验证。
11. 成功后显示 URL、复制、打开应用、查看日志；应用进入列表。

任何时候离开页面都不取消任务；重新进入时从事件 cursor 继续。

### 4.2.1 Compose 部署特有步骤

- Agent 检测 `compose.yml`、`compose.yaml`、`docker-compose.yml`，也可以为模板生成 Compose。
- UI 使用拓扑图而不是 YAML 作为主视图，明确每个 service 的 image/build、依赖、HTTP 入口、volume 和资源。
- 用户选择 primary HTTP service；其他公开 HTTP service 由平台分配附加随机域名。
- `exposure:none` 的数据库、cache、队列和 worker 不产生公网入口。
- 出现 `tcp-relay`、host port、host network、privileged、宿主 bind 或 Docker socket 时诊断直接阻塞，并解释替代方式。
- 部署前显示 volume 是否有备份、删除时是否保留、套餐将占用多少存储。
- 环境变量或 volume plan 未确认时进入 `NEEDS_INPUT`，不能把未解析值交给 Luma。

### 4.3 CLI / 用户自己的 Agent 部署

1. 用户在 Web 中取得 deploy token，或用 `lae login` 完成人机登录。
2. `lae inspect .` 只做打包清单与诊断，不部署。
3. `lae deploy . --format ndjson` 创建或更新应用，并持续输出机器可解析事件。
4. 网络断开后使用 `lae operation watch <id> --cursor <n>` 继续。
5. 命令退出码区分鉴权、需要输入、不支持、配额、构建、部署和平台故障。

Skill 不得把 token 写进仓库或 prompt；支付只能生成待确认链接，不允许 Agent 自主购买。

### 4.4 检查 Git 更新

1. 用户点击 `Check for updates`，或 CLI 调用 `lae apps inspect-update`。
2. LAE 让 Luma builder 获取当前 ref、解析新 commit 和不可变 snapshot，并创建 source revision。
3. builder 中的 Agent runner 重新诊断，将结果与当前 active plan 做结构化 diff。
4. 如果端口、构建方式、环境变量、资源或安全级别变化，进入 `NEEDS_INPUT`。
5. 如果只有源码变化且策略未变，允许一键部署。
6. 新版本通过 readiness 和公网验证后才切换 active deployment。
7. 失败时保留上一健康版本。

Webhook 自动部署后置；第一版以用户主动触发为主。

### 4.5 Suspend / Resume / Delete

- **Suspend**：停止运行资源，保留 app、域名、配置、secret、source 和可恢复版本；访问域名返回明确的 suspended 页面。
- **Resume**：优先复用最后一个合格 image digest 和 manifest，重新部署并验证。
- **Delete**：两阶段删除。立即下线并进入 7–30 天软删除，之后异步清理 artifact、image 引用、secret 和日志。
- **Restart**：只重启当前 allocation，不重新构建，不改变版本。

MVP 不把 “Stop” 映射为 `remove`，避免用户误以为配置与域名被删除。

## 5. 页面和交互规格

### 5.1 视觉概念：Quiet Current / 静水引擎

风格关键词：静水、矿物、磨砂金属、深墨蓝、月光、受控流动、精密工具。

不采用：霓虹紫蓝、过量玻璃拟态、满屏粒子、机器人头像、AI 星星、无意义 3D、emoji 作为图标。

建议设计 tokens：

| Token | 建议 |
| --- | --- |
| Background | 深墨蓝黑，不用纯黑 |
| Surface | 低透明矿物灰，边界清晰，少量 blur 只用于层级 |
| Primary text | 冷白，保证 4.5:1 对比 |
| Muted text | 蓝灰，正文仍满足可读性 |
| Accent | 低饱和翡翠/青绿，只用于主动作和运行态 |
| Warning | 琥珀，不用黄绿混淆 |
| Error | 暗朱红 + 图标/文本，不只靠颜色 |
| Type | 自托管可变 sans + tabular mono；正文最小 16px |
| Spacing | 4/8px 基线，页面节奏 16/24/32/48 |
| Radius | 中等、统一，不使用随机大圆角 |

具体字体在实现前做中英文混排样张测试；默认可使用自托管 `Inter/Geist + IBM Plex Mono` 或等价开源组合，避免运行时依赖 Google Fonts。

### 5.2 模板湖面

模板不是规则网格卡片，而是一组漂浮在静水空间中的抽象 SVG 标记：

- 每个标记有稳定位置、轻微呼吸和低幅漂移。
- 指针靠近时，只影响最近 1–3 个标记，产生局部水纹和空间避让。
- hover/focus 展开模板名称、类型、预计资源和部署时间；点击/tap 是唯一主动作。
- 点击后使用 shared element transition，把标记变成部署工作台的 source badge。
- 移动端不依赖 hover，降级为可滑动轨道和可见标签。
- `prefers-reduced-motion` 下使用静态拓扑布局；键盘 Tab 顺序与视觉顺序一致。
- 低端设备、后台标签页和 save-data 模式停用连续动画。

水面背景使用 CSS/SVG 或轻量 Canvas，不把 Three.js 作为 MVP 依赖。性能预算要求动画只修改 transform/opacity，首屏不因动效阻塞可交互时间。

### 5.3 部署工作台

桌面布局：

- 左侧：部署阶段河道和当前节点。
- 中间：当前阶段的主要内容（诊断、env 表单、部署状态）。
- 右侧或底部抽屉：真实日志、生成计划、manifest 预览和帮助。
- 顶部保持 app/source/commit/operation ID，可复制并用于客服定位。

移动端按阶段纵向排列，日志使用全屏 sheet，不做三栏压缩。

### 5.4 部署动画

动效必须由真实事件驱动：

| 真实阶段 | 视觉表达 |
| --- | --- |
| Upload / Builder fetch | source 标记进入河道，解析 commit/snapshot 后产生一次波纹 |
| Analyze | 扫描线经过可解释的检查节点 |
| Needs input | 动画停止，表单从当前节点展开 |
| Luma builder | 河道按每个 service 的 queued/build/push/digest 真实事件推进，不显示伪百分比 |
| Scan / Plan | 产物被收束成带 digest 的 artifact |
| Deploy | artifact 进入 Luma 节点，展示调度/启动/路由 |
| Verify | 从内部 health 到公网 URL 两级确认 |
| Success | 路径汇聚为稳定 URL 和健康状态 |
| Failure | 仅失败节点出现局部扰动，同时展示恢复动作 |

微交互 150–300ms，复杂 shared transition 不超过约 400ms。长任务可以持续展示 elapsed time，但不播放无限装饰动画。所有动画可被中断，UI 不因动画锁住操作。

Compose 部署时，主河道在 build 阶段分成逐服务支流，完成后再汇聚到 Nomad/route/verify。支流只反映真实并行任务；内部服务在 topology 中可见但不绘制公网出口，多个 HTTP route 分别验证。

### 5.5 状态和反馈

- 不展示没有计算依据的百分比。
- `NEEDS_INPUT` 是暂停，不是失败。
- 错误必须包含：发生阶段、可理解原因、用户能做什么、平台是否已自动清理、operation ID。
- 失败页面保留原始日志入口，但默认先给结构化摘要。
- 按钮异步提交后立即 disabled，并在 100ms 内提供反馈。
- 销毁、撤销 token、删除 app 必须二次确认；可恢复删除提供 Undo。
- Toast 只做短反馈，关键状态必须留在页面中。

## 6. 应用列表与详情

### 6.1 列表字段

- 应用名、状态、随机域名。
- 当前 source（upload / provider / repo / ref）。
- 当前版本 commit 或 artifact digest 短串。
- 最近成功部署时间和最近一次失败。
- 资源/套餐占用。
- `Open`、`Deploy update`、`Restart` 三个常用动作；危险动作进入 More。

状态不只显示颜色，还包含图标和文本：`Active / Deploying / Degraded / Suspended / Failed`。

### 6.2 应用详情

- Overview：健康、URL、source、版本、配额、最近事件。
- Services：Compose 拓扑、逐服务 desired/observed state、image digest、资源、内部/公开入口和 restart；单服务应用也使用同一模型显示一个 service。
- Deployments：不可变版本时间线、diff、回滚和事件重放。
- Environment：变量名、类型、作用域、更新时间；secret 只可替换或删除。
- Logs：实时/历史、时间范围、搜索、下载；明确 retention。
- Metrics：CPU、memory、requests、errors，提供文本摘要和数据表。
- Settings：应用名、source、suspend、delete；域名只读。

## 7. 模板系统

模板不是前端写死列表，而是版本化 registry：

- `template_id`、版本、标题、描述、分类、图标资产。
- source 类型和不可变 commit/digest。
- adapter、默认变量 schema、资源 profile、健康检查。
- 支持套餐、地区和 feature flag。
- 维护者、风险等级、最后验证时间、自动化 smoke test。

首批建议：

1. Blank HTML / Personal page。
2. React + Vite static。
3. Vue + Vite static。
4. Astro static content site。
5. FastAPI HTTP service。
6. Express/Hono HTTP service。
7. Uptime Kuma Compose（命名卷、公开 UI + 内部依赖）。
8. n8n Compose（worker/queue/volume 能力稳定后开放）。
9. App + PostgreSQL Compose 示例（明确“用户自管数据库”与备份策略）。
10. Web + Admin 双 HTTP Compose 示例（两个稳定随机域名，演示 primary/additional route）。

每个模板每天或每次依赖更新后在 staging Luma 上真实构建部署；验证失败的模板自动下架，不继续展示“一键部署”。

## 8. 可访问性与性能验收

- 正文对比度至少 4.5:1，图形/大字至少 3:1。
- 全流程可键盘操作；focus ring 明确，dialog 关闭后焦点回到触发点。
- 交互目标至少 44×44px，移动端不依赖 hover。
- 表单使用可见 label，错误位于字段旁，并在提交后聚焦首个错误。
- `prefers-reduced-motion` 提供等价静态版本。
- 实时事件使用 `aria-live=polite`，不能每条日志抢占读屏。
- 首屏背景动效延迟加载；CLS < 0.1；低端设备自动降低粒子/波纹密度。
- 375、768、1024、1440px 四档验收；浏览器缩放 200% 不丢功能。

## 9. 产品指标

- 注册到首次成功部署的转化率。
- 支持范围内首次部署成功率。
- 静态上传从提交到 active 的 p50/p95。
- 诊断进入 `NEEDS_INPUT` 后的完成率。
- 失败后自助恢复率和重复失败率。
- 7/30 日活跃应用、成功更新次数。
- CLI/Skill 部署占比和非交互成功率。
- 每 100 次部署的人工支持请求数。
- 跨租户数据泄漏和凭据泄漏必须始终为 0。
