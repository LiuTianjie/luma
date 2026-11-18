# luma-build

独立的、可插拔的远程 BuildKit 构建队列服务与无状态算力 Runner 集群（架构地位同 [`observe/`](../observe/)）。

`luma-build` 作为一个普通应用部署在 Luma 之上，与 `luma-control` **完全解耦**且**天然复用 Luma 的身份与 Scoped Secret 体系**：
- **零二次登录（SSO 身份复用）**：操作员与 CLI 直接复用本机 `luma login`（`~/.config/luma/current-context`）的登录态，`luma-build` 通过 `luma-control` 只读接口 `POST /v1/auth/login/verify` 验签，无需维护额外的管理员密码。
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
├── luma.yml                # Luma 单服务部署 manifest 模板
├── .env.example            # Luma Scoped Secrets 环境变量配置模板
├── luma_build/
│   ├── server.py           # Starlette ASGI API + Luma SSO + DeployWorker + /metrics
│   ├── store.py            # SQLite WAL 状态机、Luma Scoped Env 凭据读取、分支 Supersede
│   ├── scheduler.py        # 原生架构硬校验（禁 QEMU 慢模拟）、缓存亲和与权重选路
│   ├── runner.py           # 跨平台 BuildKit 执行器（Linux 容器 / 宿主机 / macOS OrbStack）
│   ├── deployer.py         # Luma SSO 验签缓存 & 对接 Luma Control 标准公开部署 API
│   ├── registry_naming.py  # 阿里云 ACR 两级命名空间自动扁平化 & HTTP/HTTPS 协议判定
│   ├── crypto.py           # 动态凭据与部署 .env 的 Fernet 加密
│   └── cli.py              # luma-build 命令行工具（自动读取 ~/.config/luma 登录态）
└── tests/                  # 独立单元测试套件
```

---

## 快速配置指南

### 1. 用 Luma 原生凭据体系配置并部署 `luma-build`

复制 `builder/.env.example` 为 `builder/.env`，填入阿里云 ACR Push 读写账密与 Git Token，部署时 Luma 会自动将它们存为 **`luma-build` 专属的 Scoped Secrets**：

```bash
# 1) 在 Luma Control 保存阿里云 ACR 的【只读 Pull 子账号】（供集群运行节点拉镜像）
printf '%s' "$ACR_PULL_PASSWORD" | luma registry login \
  crpi-xxxxxxxx.cn-beijing.personal.cr.aliyuncs.com \
  --username "$ACR_PULL_USERNAME" --password-stdin

# 2) 本地构建并部署 luma-build 服务（同 observe/ 部署方式；ACR Push 密码与 Git Token 自动存为 scoped secrets）
luma build local builder --platform linux/amd64 --env builder/.env

# 后续若需单独轮换某个密钥，直接用标准 luma secret 命令即可：
# luma secret set GITEA_TOKEN "new-token" --scope luma-build && luma app restart luma-build
```

### 2. 本地绑定一次地址（直接使用 `luma builder`，自动复用 `luma login` 身份）

直接打开浏览器访问 `https://build.example.com` 即可进入内置的 **Luma 同风格 Web 控制台**（查看构建时间线、实时流式日志、网页提交构建与管理 Runner）；命令行同样已内置进 `luma builder`：

```bash
luma builder use https://build.example.com
luma builder status
```

### 3. 在任意机器接入 Runner（即插即用）

```bash
luma builder token create bd-runner --weight 200 --cache-mode read-write
luma builder token create m4 --weight 100 --cache-mode read-only
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

- **macOS 机器（如 `m4` / `air3` 跑 OrbStack 原生 ARM64 构建，直接用 `luma` CLI）**：
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

## 本集群维护入口

`bash builder/deploy.sh` 使用本机 `luma.local.compose.yml` 和 Control 的全局 ACR 配置。
本地 sidecar 定义实际域名；示例 `build.example.com` 不作为本集群默认入口。
首次显式修改 scoped secrets 时传 `--env builder/.env`，日常发布复用已有作用域。
Registry host/namespace 不固定在脚本中。运行镜像迁移和实际服务升级须在发布时执行；
仅提交脚本不会升级 Builder、Control 或 Runner。

CLI 0.2.81 起，脚本省略 Registry 参数时，工作流校验会读取 Control 的实际默认值，与已记录的显式 ACR 地址/命名空间比较，并固定本次上传目标。默认值相同不再误报为 `None`；真正改变地址或命名空间仍需确认。
