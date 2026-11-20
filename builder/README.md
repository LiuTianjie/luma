# luma-build

独立的、可插拔的远程 BuildKit 构建队列服务与无状态算力 Runner 集群（架构地位同 [`observe/`](../observe/)）。

`luma-build` 作为一个普通应用部署在 Luma 之上，与 `luma-control` **完全解耦**且**天然复用 Luma 的身份与 Scoped Secret 体系**：
- **浏览器共用登录态**：配置 `LUMA_CONTROL_URL` 后，打开 Builder 会复用 Luma 控制面的 Cookie／Passkey 会话；未登录时先在控制面登录，再自动返回。控制面通过已部署的 `luma-build` 域名或 `LUMA_BUILD_URL` 确定唯一允许回跳的 HTTPS 地址。一次性票据绑定浏览器校验码，Builder 只持有 HttpOnly Cookie，不接触控制面的 Cookie 或管理 Token；控制面退出、撤销或过期后，Builder 后续请求立即失效。两端需同时升级到支持浏览器 SSO 的版本。
- **CLI 身份复用**：CLI 继续使用本机 `luma login`（`~/.config/luma/current-context`）的 Token，由 `POST /v1/auth/login/verify` 验证。独立部署或恢复访问可在 Builder 登录页选择「使用管理 Token」（也可访问 `/?login=manual`）。Builder 的「退出」只退出当前 Builder 会话，控制面会话保持有效。
- **凭据统一用 `luma secret --scope luma-build` 管理 + 读写权限分离**：
  - **ACR Push 写凭据 & Git Token**：直接作为 `luma-build` 服务的 Luma Scoped Secrets 注入该容器（集群内其他节点与其他应用不可见），单次构建租约达成瞬间按需从内存下发给 Runner，任务结束立即销毁；
  - **ACR Pull 只读凭据**：通过 `luma registry login` 存在 `luma-control` 供业务运行节点安全拉镜像，严格遵循最小权限分离。
- **Runner 零控制面耦合**：任何 Linux 主机、macOS（OrbStack / Docker Desktop）或临时按量云主机只需配置 `LUMA_BUILD_URL` + `LUMA_RUNNER_TOKEN` 即可接入编译池，**无需 `luma node join`，无需安装 Nomad**。
- **双层 BuildKit 缓存加速**：
  1. **L1 本地热盘缓存 + 亲和调度**：Runner 持久化本地 BuildKit 守护进程缓存（内置 `gcpolicy` 默认保留 `20 GiB` 自动 LRU 淘汰），服务端优先把同一仓库派给上次编译它的同架构 Runner（默认 `3s` 亲和窗口）；
  2. **L2 远端 Registry 缓存（`--cache-from` / `--cache-to`）**：溢出到新 Runner 时自动从 OCI Registry 拉取层缓存；所有项目共享单个 `luma-buildcache` 仓库按 Tag 隔离（不占阿里云 ACR 个人版 300 仓库配额）；家庭慢上行机器可设 `--cache-mode read-only` 只拉缓存不回推。

---

## 目录结构

```text
builder/
├── pyproject.toml          # 独立 Python 包 (CLI: luma-build)
├── Dockerfile.server       # luma-build 队列调度服务端镜像
├── Dockerfile.runner       # Linux 即插即用 Runner 容器镜像 (内置 docker-cli + buildx + git)
├── docker-compose.yml      # 本地或 Compose 部署编排
├── luma.compose.yml        # Luma Compose 部署 sidecar
├── .env.example            # Luma Scoped Secrets 环境变量配置模板
├── luma_build/
│   ├── server.py           # Starlette ASGI API + Luma SSO + DeployWorker + /metrics
│   ├── browser_auth.py     # Luma Control 浏览器 SSO 票据校验与会话 Cookie 管理
│   ├── dashboard.py        # 内嵌 Web 控制台页面与交互逻辑
│   ├── store.py            # SQLite WAL 状态机、Luma Scoped Env 凭据读取、分支 Supersede
│   ├── scheduler.py        # 原生架构硬校验（禁 QEMU 慢模拟）、缓存亲和与权重选路
│   ├── runner.py           # 薄封装层，转发到 `luma.builder_runner` 跨平台 BuildKit 执行器
│   ├── runner_http.py      # Runner 与服务端心跳、租约及日志上报 HTTP 封装
│   ├── deployer.py         # Luma SSO 验签缓存 & 对接 Luma Control 标准公开部署 API
│   ├── registry_naming.py  # 两级命名空间（如阿里云 ACR）扁平化 & HTTP/HTTPS 协议判定
│   ├── crypto.py           # 动态凭据与部署 .env 的 Fernet 加密
│   ├── errors.py           # Builder 统一异常类型定义
│   └── cli.py              # luma-build 命令行工具（自动读取 ~/.config/luma 登录态）
└── tests/                  # 独立单元测试套件
```

---

## 快速配置指南

### 1. 用 Luma 原生凭据体系配置并部署 `luma-build`

可以通过 `luma addon install builder` 直接安装预构建的 GHCR 镜像，或从源码自定义构建。复制 `builder/.env.example` 为 `builder/.env`，填入 OCI Registry（如 `ghcr.io`、`registry.example.com` 或阿里云 ACR）的 Push 读写账密与 Git Token，部署时 Luma 会自动将它们存为 **`luma-build` 专属的 Scoped Secrets**：

```bash
# 1) 在 Luma Control 保存 OCI Registry 的【只读 Pull 凭据】（供集群运行节点拉镜像）
# 通用 OCI Registry（如 ghcr.io / registry.example.com）：
printf '%s' "$REGISTRY_PULL_PASSWORD" | luma registry login \
  registry.example.com \
  --username "$REGISTRY_PULL_USERNAME" --password-stdin

# 若使用阿里云 ACR，将 registry 地址替换为你的实例域名，例如：
# crpi-xxxxxxxx.cn-beijing.personal.cr.aliyuncs.com

# 2) 安装预构建 Addon（推荐）或本地源码构建并部署 luma-build 服务
luma addon install builder
# 或本地源码构建并注入 scoped secrets：
# luma build local builder --platform linux/amd64 --env builder/.env

# 后续若需单独轮换某个密钥，直接用标准 luma secret 命令即可：
# printf '%s' "$NEW_GITEA_TOKEN" | luma secret set GITEA_TOKEN --scope luma-build --value-stdin && luma app restart luma-build
# 或使用 --value：
# luma secret set GITEA_TOKEN --scope luma-build --value "$NEW_GITEA_TOKEN" && luma app restart luma-build
```

### 2. 本地绑定一次地址（直接使用 `luma builder`，自动复用 `luma login` 身份）

直接打开浏览器访问 `https://build.example.com` 即可进入内置的 **Luma 同风格 Web 控制台**（查看构建时间线、实时流式日志、网页提交构建与管理 Runner）；命令行同样已内置进 `luma builder`，并会自动从当前 `luma login` 的 Control 上下文发现已部署的 Builder 地址（如需覆盖可运行 `luma builder use`）：

```bash
luma builder use https://build.example.com
luma builder status
```

### 3. 在任意机器接入 Runner（即插即用）

```bash
luma builder token create x86-builder --weight 200 --cache-mode read-write
luma builder token create arm64-builder --weight 100 --cache-mode read-only
```

- **Linux 机器（容器方式一行启动）**：
  ```bash
  docker run -d --name luma-runner --restart=on-failure --network=host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v luma-runner-state:/var/lib/luma-runner \
    -e LUMA_BUILD_URL="https://build.example.com" \
    -e LUMA_RUNNER_TOKEN="lbr_xxx" \
    -e LUMA_RUNNER_CACHE_KEEP_GB="30" \
    ghcr.io/liutianjie/luma-build-runner:v0.2.76
  ```

- **macOS 机器（如 Apple Silicon / `arm64-builder` 跑 OrbStack 原生 ARM64 构建，直接用 `luma` CLI）**：
  ```bash
  LUMA_RUNNER_PROXY="http://127.0.0.1:7890" \
  luma runner join --url https://build.example.com --token lbr_xxx --cache-mode read-only --cache-keep-gb 25
  ```

首次 `luma runner join` 验证成功后默认启动后台服务；后续用 `luma runner start / stop / restart / status / logs --follow` 管理。已有 runner 用原 token 重新 join，无需新建名称；原 token 丢失时按原名称执行 `luma builder token create NAME --rotate`。

控制台「删除」或 `luma builder token delete NAME` 会移除注册记录；新版 runner 收到 token 失效响应后自动退出。完整的旧 runner 迁移、删除和系统服务说明见[构建 Runner](../docs/runners.zh-CN.md)。容器的 `on-failure` 策略支持异常重启，主机开机自启请使用 `luma runner join`。

### 4. 提交构建与部署

```bash
luma builder submit acme/my-app --ref main --env .env
luma builder submit https://git.example.com/org/service.git --ref v1.2.0 --no-deploy
luma builder list
```

---

## 本地运行测试

```bash
PYTHONPATH=builder .venv/bin/python -m unittest discover -s builder/tests -p 'test_*.py'
.venv/bin/python -m ruff check builder
```

## 独立发版

使用 `python scripts/bump-builder-version.py` 更新 builder 版本，提交后推送 `builder-v0.2.76` tag。`Publish Builder` CI 发布 GitHub Release 包制品和 `ghcr.io/liutianjie/luma-build-server:v0.2.76`、`ghcr.io/liutianjie/luma-build-runner:v0.2.76`，不自动部署。先升级 Luma/Control，再由操作员部署 builder；详见[发版指南](../docs/maintainers/release.zh-CN.md#independent-builder-release)。
