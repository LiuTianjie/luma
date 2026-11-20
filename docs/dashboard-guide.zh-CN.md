# Luma 控制台指南 {#luma-console-guide}

控制台地址为 `https://<control-domain>/dashboard/`。可以用 Passkey 登录，也可以在可信设备上用管理令牌登录。控制台与 CLI 使用同一个 Control API。见[登录与 Passkey](#sign-in-and-passkeys)。

## 首次安装后的第一小时 {#first-hour-after-bootstrap}

1. 打开 `/dashboard/`，粘贴 bootstrap 给出的管理 Token。随后在 **设置 → 登录与 Passkey** 添加一个 Passkey，之后登录就不再需要 Token。
2. 在 **应用 → 创建应用** 选择 **hello-world 首装验证**。它是内部服务（`exposure: none`），不需要额外 DNS、Tailscale 或 Registry。
3. **首次安装** 页只补集群可选项：Cloudflare zone、Tailscale、egress、内部 Registry。跳过可选项不等于安装失败。
4. hello-world 健康后再创建 `cn-edge` / `external-edge` 的公开服务。

## 登录与 Passkey {#sign-in-and-passkeys}

浏览器从不保存管理令牌。无论用 Passkey 登录，还是一次性粘贴令牌，Control 都会为 Control 域名设置会话 Cookie（`HttpOnly`、`Secure`、`SameSite=Strict`），服务端只保存它的哈希。退出登录、连续 2 小时无活动或登录满 24 小时（以先到者为准）时会话结束。CLI 和 CI 继续使用管理令牌，不受影响。

用令牌登录后，可在 **设置 → 登录与 Passkey** 添加第一个 Passkey；也可以给尚未登录的浏览器生成一次性链接：

```bash
luma passkey enroll --name "MacBook Touch ID"
```

链接会打开 `/dashboard/enroll`，只能使用一次，15 分钟后失效。完成后该浏览器直接登录，因此要像对待密码一样对待这个链接。Passkey 绑定 Control 域名（WebAuthn RP ID）：请通过 `https://<control-domain>/dashboard/` 打开控制台，不要用 IP 地址或其他主机名。已登录浏览器发出的写操作必须来自这个 Origin，否则 Control 会拒绝。

**设置 → 登录与 Passkey** 列出 Passkey 和浏览器会话。删除 Passkey 会结束用它登录的会话。**要求 Passkey 登录**开启后，浏览器不能再用管理令牌登录，已有的令牌会话会立即结束。开启前至少需要一个 Passkey；删除最后一个 Passkey 时该设置会自动关闭。

恢复操作不依赖浏览器：

```bash
luma passkey require off
luma passkey list
luma passkey remove <id>
luma passkey sign-out
```

`luma passkey sign-out` 结束所有浏览器会话，或用 `--session <id>` 只结束一个。管理令牌仍是最高权限凭据：持有它的人都能执行这些命令，所以令牌泄露时仍需轮换令牌。

Passkey 校验在 Control 镜像中运行，镜像安装了 `control` 依赖组（固定版本的 `webauthn` 包，需要 Python 3.10+）。缺少该依赖的 Control 仍提供令牌登录，并显示 Passkey 不可用。嵌入的 Grafana（`/grafana`）受同一套登录保护：默认情况下 Traefik 对每个请求询问 Control（`/v1/auth/forward`），只有带有效控制台会话或管理令牌的请求才会放行，否则返回 401。Grafana 内部仍是匿名 Viewer，控制台无需再次登录。由于放行由 Control 决定，Control 不可用时登录保护的 Grafana 入口也无法访问；在 Manager 上仍可通过 `127.0.0.1:3100` 调试 Grafana。**可观测 → 观测设置** 提供可选的[对外分享开关](observability.zh-CN.md#external-dashboard-sharing)，仅放行已在 Grafana 主动分享的看板，不会开放其他 Grafana API。

## 选择工作空间 {#choose-a-workspace}

| 工作空间 | 功能 |
| --- | --- |
| 总览 | 查看需要关注的应用和节点。 |
| 应用 | 查看服务、副本、访问地址、实时日志、资源指标、配置、密钥和版本历史（安装 Luma Observe 后解锁 Grafana 看板，安装 Luma Eye 后解锁前端监控）。 |
| 部署记录 | 跟踪部署历史；安装 Luma Build 后同时解锁远端 Git 构建记录与 Builder 控制台直达入口。 |
| 监控与告警 / 可观测 | 原生提供告警事件、基于节点 Agent 的资源指标、告警规则与通知渠道。安装 Luma Observe 后自动扩展采集状态、应用监控与看板分享；安装 Luma Eye 后自动扩展前端监控。 |
| 基础设施 | 管理节点（含加入新节点和清理节点磁盘）、区域、网络路由、存储和镜像。 |
| 插件中心 | 按需一键安装、配置与卸载官方扩展插件（Luma Build、Luma Observe、Luma Eye），统一接入 Control 单点登录并自动托管内部密钥。见[插件与统一登录](addons.zh-CN.md)。 |
| 设置 | 管理密钥、镜像仓库凭据、Git 凭据、登录（Passkey 与浏览器会话）和系统维护（Control 与 agent 升级）。 |
| 首次安装 | 配置并验证核心集群依赖。 |

插件中心、设置和首次安装位于侧边栏的“平台管理”分组。创建应用和从 Git 构建归属于“应用”。控制台界面会根据已安装插件自动显隐：未装插件时界面保持极简，直接访问插件路由时展示统一安装引导而非报错。顶部栏显示当前位置。外观、语言和退出操作位于侧边栏。桌面端侧边栏收起为图标时，带二级页面的工作空间会弹出菜单。选择外观或语言后，偏好菜单会关闭。桌面端的折叠按钮位于侧边栏头部；窄屏通过顶部栏打开导航。

## 创建应用 {#create-an-application}

打开 **应用 → 创建应用**，选择配置来源：

- **Git 仓库**：选择已连接的提供方或输入仓库 URL，在当前页面提交。Builder 排队后由匹配架构的空闲 Runner 领取，镜像复用 Builder 已配置的镜像仓库（例如阿里云 ACR）。无需选择构建节点或 Registry。
- **容器镜像**：配置镜像、区域、入口、资源和环境变量。
- **YAML 文件**：直接编辑部署清单。
- **应用模板**：搜索单服务或 Compose 模板，再调整配置。

构建日志仍在当前页面展示。所有 Runner 忙碌也可以提交，任务在独立 Builder 中等待；关闭页面只停止跟随日志，不取消构建。Control 保存的 Git 账户用于浏览仓库列表；Builder 按仓库 URL 匹配自己保存的 Git 凭据。

选择模板不会立即部署。提交前应检查并校验配置。手动编辑 YAML 后，校验与部署将以当前 YAML 为准；恢复表单前会确认是否丢弃这些编辑。

字段定义见[部署 YAML](deployment-yaml.zh-CN.md)，多服务和持久卷行为见 [Compose 与存储](compose-storage.zh-CN.md)。

## 查看应用 {#inspect-an-application}

应用详情中的标签页保持同一个应用上下文，可切换概览、服务与实例、日志、指标、自定义看板、配置和版本历史。

- **概览**：显示运行状态、部署位置、访问地址、存储和诊断（安装 Luma Observe 后自动展示顶部采集状态条）。
- **日志**：未安装 Luma Observe 时默认直接展示 Control 原生提供的**实时日志**（支持筛选、暂停、自动折行、复制和下载）；安装 Luma Observe 后自动解锁按当前应用筛选的 VictoriaLogs 日志检索与 Tempo 链路追踪。运行事件与拉取诊断独立于应用日志输出。
- **看板**（安装 **Luma Observe** 后显示）：展示带有 `luma-app:<application-name>` 标签的 Grafana 看板及 OTel 任务观测板。配置与交付方式见[应用看板说明](observe-applications.zh-CN.md#link-dashboards-to-an-application)。
- **前端监控**（安装 **Luma Eye** 后显示）：按应用名或公网域名自动匹配对应的 Luma Eye 项目，展示实时 UV/PV、客户端报错、无崩溃会话率，并支持一键 SSO 直达 Eye 完整控制台。
- **配置**：显示已登记的部署来源、更新时间和复制操作。有多个配置文件时才显示文件切换。
- **版本**：区分加载失败、暂无历史和回滚失败。回滚需要确认，并会改变当前部署。

路由配置为内部访问，并不代表该应用在所有私网中都不可达。修改入口前，请先检查入口类型与节点位置。

## 查看完整路由 {#follow-a-route}

打开 **基础设施 → 网络 → 路由**。可按域名、应用、节点或地址搜索，也可按入口类型筛选。选择路由后，会聚焦经过共享节点到达目标的完整配置路径。“节点拓扑”标签页单独展示集群关系。

路由视图结合配置与运行实例之间的关系，**不是逐跳连通性测试**。DNS、证书、路由和服务健康状态可能各自出错，请使用相应诊断或路由检查功能排查。

## 指标与通知 {#metrics-and-notifications}

资源图表将数值与历史曲线放在一起，提供时间轴和采样提示。采样缺口会保留断点，不会用虚构数据补齐。当前快照与最近历史采样的时间和值可能不同。

告警事件、规则和通知渠道分别有独立页面。告警引擎摘要位于事件页；通知渠道显示配置和发送历史，排队并不代表发送成功。保存渠道不会自动发送测试消息。

内置资源监控与可选的应用级可观测组件有不同的要求。区别和部署说明见[可观测](observability.zh-CN.md)。

## 预览与真实集群 {#preview-versus-a-live-cluster}

官网控制台示意使用示例名称与数值。本地开发数据可用于检查布局和交互，但官网示意图和本地预览都不能证明生产环境健康。请在自己的 Control 实例上验证实际行为。
## 应用密钥 {#application-secrets}

进入应用 → 选择应用 → 密钥，即可管理该应用作用域的密钥。在详情页内新增或轮转，新值只写不回显。配置中引用的已保存全局密钥会单独标记来源；修改共享值使用“管理全局值”，也可设置应用专用覆盖值，保留其他应用的配置。部署配置读取失败时，仅展示已保存的应用密钥。

保存密钥只更新 Control 存储，已有实例不会自动获得新值；需要更新或重新部署应用使其生效。保存成功不代表外部凭据已经轮转，也不代表运行中的应用已经使用新值。
