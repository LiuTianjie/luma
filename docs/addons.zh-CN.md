# 可插拔插件与统一单点登录 {#pluggable-addons-unified-sso}

Luma Control 本身是一个精简、完整的自托管容器部署控制面：**零插件状态下**即可完成节点管理、镜像/Compose/YAML 部署、作用域密钥、部署历史、实时容器日志、基于节点 Agent 的资源指标以及核心告警。[English](addons.md)

当集群需要多 Runner 远端 Git 构建、全栈可观测或前端埋点与崩溃追踪时，可以在控制台 **平台管理 → 插件中心** 或通过 `luma addon` 按需启用官方插件。所有插件共享**同一套 Luma Control 身份认证**（Passkey 浏览器单点登录与 `luma login` CLI 令牌），并全自动托管内部密钥。

## 官方插件 {#official-addons}

| 插件 | 应用栈 | 建议资源与节点固定策略 | 鉴权方式 |
| --- | --- | --- | --- |
| **Luma Build** (`builder`) | `luma-build` | ≥ 0.5 GiB 内存（SQLite 队列，实际编译由外部 Runner 执行；建议固定节点） | Control SSO (`control-sso`) |
| **Luma Observe** (`observe`) | `luma-observe` | 建议 Manager ≥ 8 GiB 内存（13 个可观测组件；固定部署在 Manager） | Control `forwardAuth` (`/grafana`) |
| **Luma Eye** (`eye`) | `luma-eye` | ≥ 1.5 GiB（`node-edge`）/ 2 GiB（`all-in-one`）；含 ClickHouse 与 SQLite 卷，**必须固定节点** | Control SSO (`control-sso`) |

## 核心底座与插件扩展槽（按能力动态显隐） {#core-vs-addon-ui-capabilities}

Luma 控制台会根据集群实际运行的插件（`luma.addon/v1` 中的 `uiCapabilities`）自动调整导航和页面能力。未安装的插件不会在侧边栏占位置或展示报错面板；一旦安装成功，对应的扩展槽会自动激活。

| 控制台位置 | Control 核心底座（0 插件） | 启用 `Luma Build` 后解锁 | 启用 `Luma Observe` 后解锁 | 启用 `Luma Eye` 后解锁 |
| --- | --- | --- | --- | --- |
| **侧边栏监控区** | **监控与告警**（4 个原生页面）：告警事件、资源指标、告警规则、通知渠道 | — | 自动升级为 **可观测**：新增采集状态、应用监控（Grafana/日志/链路）、看板分享 | 新增 **前端监控**（`/observe/eye`）项目总览 |
| **部署记录与创建应用** | 部署时间线、容器镜像 / YAML / 模板创建 | 解锁从 Git 仓库构建（`/builds`）、远端构建重试及 Builder 控制台入口 | — | — |
| **应用详情 Tab** | 概览、服务与实例、告警、实时日志、指标、配置、密钥、版本与回滚 | — | 新增概览采集状态条、**看板** Tab（Grafana + 任务板）、**日志**内解锁历史日志检索与 Tempo 链路、合并 Alertmanager 告警 | 新增 **前端监控**（`eye`）Tab，按应用名或域名自动关联 Eye 项目 |
| **告警规则预设** | 7 项原生 Agent/Control 规则（`node.*`、`app.memory`、`task.queue_age`） | 解锁 `build.failed` | 解锁 11 项 HTTP、Nomad、日志匹配及 Taskiq/Celery 规则 | — |

## 统一 Control 鉴权 {#unified-control-authentication}

Luma Control 作为所有插件的统一身份提供方（IdP）：

1. **浏览器单点登录（`control-sso`，用于 Luma Build 和 Luma Eye）**：打开插件控制台时自动跳转至 `https://<control-domain>/dashboard/addon-login?addon=<id>&challenge=...`，复用已登录的 Passkey/控制台会话签发一次性 PKCE 票据，换取绑定目标域名受众的不透明会话（`las_...`）。在 Control 退出登录或卸载插件时，相关插件会话立即吊销。
2. **ForwardAuth（`forward-auth`，用于 Luma Observe）**：Traefik 通过 Control 的 `/v1/auth/forward` 校验 `/grafana` 请求，直接复用同一控制台会话或管理令牌，无需维护第二套 Grafana 账号。
3. **CLI 零额外 Token**：`luma builder` 与 `luma eye` 自动复用当前 `luma login` 凭据，并从 Control 自动发现插件地址。

```bash
luma addon list
luma builder status
luma eye status
luma eye portfolio --since 24h
```

## 首次配置与密钥自动化 {#first-time-configuration-secret-automation}

新用户安装插件时，不需要手改 YAML，也不需要手动编造内部环境变量：

- **由 Control 全自动托管（0 配置）**：
  - 自动注入 `LUMA_CONTROL_URL`（为 `builder` 自动注入 `LUMA_DEPLOY_TOKEN`）。
  - 首次配置时自动以密码学随机数生成内部密钥（`LUMA_EYE_ADMIN_TOKEN`、`LUMA_EYE_RELAY_SECRET`、`LUMA_EYE_AUTO_KEY`、`LUMA_OBSERVE_READ_TOKEN`、`LUMA_OBSERVE_INGEST_TOKEN`），升级时默认保留（除非显式指定 `--rotate-secrets`）。
  - `LUMA_EYE_AUTO_ORIGIN_SUFFIXES` 留空时自动根据 Eye 域名推导根域后缀（例如 `eye.example.com` → `.example.com,localhost,127.0.0.1`）。
- **凭据一处录入、全平台复用（零重复输入）**：
  - 在 **插件中心** 或 CLI 配置 **Luma Build** 时，可直接选择复用 Control 已保存的镜像仓库凭据与 Git 凭据，无需重复填写密码或 Token。
- **最少必填输入与实时预检**：
  - **Luma Eye**：1 个域名 + 区域 + **必填固定节点**（目标区域仅有 1 台节点时自动选中；多节点时自动预选最合适节点并标注有状态卷提示）。
  - **Luma Observe**：确认 1 个 Manager 区域（自动按已注册 Manager 节点推导）。
  - **Luma Build**：1 个域名 + 镜像仓库（可直接继承 Control 凭据）+ 建议固定节点。
  - 安装弹窗会实时对比插件声明的 `requirements` 与目标节点的真实剩余/总内存，内存偏紧或不足时在部署前给出醒目提醒。
  - 安装完 **Luma Build** 后，若在线构建 Runner 为 0，**插件中心**与 **从 Git 构建** 页面均会直接展示接入引导卡（`luma builder token create` + `luma runner join`）。

## 安装、配置与安全卸载 {#installing-configuring-and-uninstalling}

### 在浏览器中一键安装 {#one-click-install-from-the-browser}

进入控制台 **平台管理 → 插件中心**，点击对应官方插件的 **一键安装**，确认自动预选的固定节点与实时内存预检结果，填写或确认域名后点击 **保存并一键部署**。Control 会直接使用插件清单中声明的官方 GHCR 预构建镜像完成原地部署，无需克隆源码。

### 通过 CLI 安装 {#installing-via-cli}

```bash
# One-click install using official prebuilt GHCR images
luma addon install eye --domain eye.example.com --region cn --node manager --yes
luma addon install observe --region cn --yes
luma addon install builder --domain build.example.com --use-registry ghcr.io --use-git-provider github:my-account --yes
```

如果希望 Luma Eye 部署在独立公网边缘节点以绕开 Manager 带宽（`node-edge` 预设），传入 `--preset node-edge --node <edge-node>`：

```bash
luma addon install eye --preset node-edge --domain eye.example.com --region global --node edge-1 --yes
```

### 从本地源码构建部署（用于二次开发或自定分支） {#building-from-local-source-for-development-or-custom-forks}

传入 `--source <dir>` 即可在复用同一套插件配置与密钥自动化流程的前提下，本地构建并推送镜像：

```bash
luma addon install eye --source ./eye --yes
luma addon install builder --source ./builder --yes
luma addon install observe --source ./observe --yes
```

### 更新配置与安全卸载 {#updating-settings-or-uninstalling-safely}

```bash
luma addon show eye
luma addon configure eye --set retentionDays=14 --apply
luma addon uninstall eye
```

卸载操作会停止插件服务与路由、立即吊销其 Control SSO 会话，并自动隐藏控制台中的对应扩展槽。**持久化数据卷（`eye-data`、`clickhouse-data`、`luma-build-data`）默认保留**，只有显式传入 `--delete-storage` 才会删除。

## 声明式 `luma.addon/v1` 规范 {#declarative-lumaaddonv1-specification}

官方插件清单位于 `luma/assets/addons/*.yaml`（`builder.yaml`、`eye.yaml`、`observe.yaml`）。每个清单声明自身的 `auth` 协议、`uiCapabilities`、`requirements`（最低/建议内存及有状态节点固定策略）、`defaultImages`、部署拓扑 `presets`、分层 `configSchema`（`basic` / `recommended` / `advanced`）以及 `autoSecrets`。第三方扩展可遵循同一规范接入 Control SSO 与声明式配置界面。
