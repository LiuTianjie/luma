# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

**Luma**：面向 HashiCorp Nomad 的轻量自托管部署控制面。Python CLI（`luma`）从任意已认证的客户端运行，通过 Luma Control 渲染并提交 Nomad job，用 Traefik 做 HTTP/HTTPS/TCP 入口，用 Cloudflare 管理 DNS。Python 包名为 `luma-infra`，发布到 PyPI。

调用链：`客户端 -> Luma Control -> Nomad API -> Nomad client -> docker driver -> container`

## 常用命令

开发环境用仓库内的 `.venv`（不是 conda base，base 里没有依赖）。运行时支持 Python 3.9（家庭节点用 macOS 自带 Python），**测试需要 3.10+**。

```bash
# 开发环境
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
npm ci

# PR、镜像和包发布共用门禁：版本同步、CLI 参考、ruff、模板校验、Python/Dashboard 测试、构建、空白
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# 全部 Python 测试（根 tests/ 用 unittest，没有 pytest）
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# 单个测试
.venv/bin/python -m unittest tests.test_nomad_render.NomadRenderTests.test_public_cn_service_gets_traefik_labels

# Lint（只开 F 与 E9；tests/ruff.toml 把测试目标版本设为 py310）
.venv/bin/python -m ruff check luma tests scripts observe

# 修改 CLI 参数/帮助后重新生成中英文参考；CI 用 --check 防漂移
.venv/bin/python scripts/generate-cli-reference.py

# 校验 templates/ 下的 manifest 都能渲染成 Nomad jobspec
./scripts/validate-stacks.sh

# 仓库内运行 CLI（避免 shell 把 luma/ 包目录当成命令）
.venv/bin/luma <command>
./scripts/luma <command>

# 文档站（需要 Markdown==3.8.2）：构建并检查链接、锚点和中英文一致性
.venv/bin/python scripts/build-site.py --output /tmp/luma-site && .venv/bin/python scripts/check-site.py /tmp/luma-site
```

Dashboard（React + Vite + TypeScript，源码在 `dashboard-src/`）：

```bash
npm run dev:dashboard        # 本地开发
npm run build:dashboard      # 构建（产物输出到 luma/assets/dashboard，git 忽略、CI 构建）
npm run typecheck:dashboard  # 类型检查
```

## 版本管理

版本号**绝不要手改**，统一用脚本：

```bash
python scripts/bump-version.py --check    # 校验各文件版本是否一致
python scripts/bump-version.py --minor    # 或 --major / --set x.y.z
```

`scripts/bump-version.py` 维护 `pyproject.toml`、`luma/__init__.py` 的版本号，并同步 `README*.md`、`docs/**/*.md`、`luma/cli/*.py` 和 `skills/` 中的版本引用（如 `luma-infra==`、`LUMA_INSTALL_REF=v`、`luma-control:v`、`--install-ref v`）。

## 核心模型

用户面向的概念只有五个（详见 `docs/concepts.md`）：

- `node`：加入 Nomad 集群并安装 Luma agent 的机器（manager / worker / home）。
- `region`：调度边界（`cn` / `global` / `home` / 自定义），决定服务**跑在哪**。
- `exposure`：流量**怎么进**，共六种（`cn-edge` / `external-edge` / `tailscale-relay` / `tcp-relay` / `cloudflare-tunnel` / `none`）。region 与 exposure 强绑定：cn-edge⇒cn、external-edge⇒global、tailscale-relay⇒home。全部模式见 `docs/exposure-model.md`。
- `egress`：出站代理（镜像拉取 + `proxy: true` 服务的运行时 HTTP/HTTPS 代理）。
- `service`：一个 Luma YAML manifest 描述的部署单元。

## Token 模型

用户可见 token 只有两种：**管理 token**（`LUMA_DEPLOY_TOKEN`，客户端/dashboard/CI 用）和**节点加入 token**（`luma node join` 用）。节点 agent 凭据是内部的，Control 只存 hash，不要让用户复制或管理它们。Luma 从不保存 sudo 密码或主机凭据（`LUMA_SUDO_PASSWORD` 只作为单次运行的环境变量兜底，不再提示或写入配置）。

## 代码结构

### CLI：`luma/cli/` 包

- `parser.py`：`build_parser()`，按 `COMMAND_GROUPS` 分组的命令树与帮助格式（`LumaHelpFormatter`），未知命令给出 did-you-mean。改参数从这里入手，然后重新生成 CLI 参考。
- `main.py`：`main()` 与 `COMMANDS` 分发表；统一加载环境：显式 `--env-file` 严格加载，否则只从 `./.env` 读取 Luma 自己的配置项（`luma/envfile.py` 的 `LUMA_SETTING_NAMES`），应用变量不会被隐式注入。KeyboardInterrupt → 130；非 `LumaError` 异常打印简短报告（`LUMA_DEBUG=1` 看 traceback）。
- 命令实现按领域拆分：`session.py`（init/login/context/doctor/version/validate）、`deploy.py`（deploy/compose）、`builds.py`（import/build/workflow）、`apps.py`（status 与 `luma app ...`）、`settings.py`（secret/registry/git-provider/storage/region）、`manager.py`（bootstrap/update/manager ...）、`nodes.py`（node ...）、`common.py`（共享 helper）。
- `__main__.py` 必须保留：安装器 shim 以 `python -m luma.cli` 启动。
- 命令树不考虑旧命令兼容，但 Control 上已记录的 workflow 会被当前解析器重放：`deploy`/`compose deploy` 保留隐藏的 `--engine`，`replay_argv` 去掉旧记录中的前置 `--env-file .env`。

### Control：`luma/control/`

- `server.py`（~14.7k 行）：Starlette ASGI + uvicorn（`create_app()`/`serve()`），只有这一套 HTTP 栈。认证后的路由在 `_asgi_authenticated_get` / `_asgi_authenticated_post` 分发。处理部署、DNS 同步、节点 agent 任务派发、状态查询，以 NDJSON 流式返回部署事件。所有部署路径被全局 `_DEPLOY_LOCK` 串行化。部署步骤错误契约：`_deploy_step` 里的非 `LumaError` 必须原样重抛。
- `state.py` / `database.py` / `history.py`：单 Manager 本地 SQLite/WAL 状态与索引历史。`control.json` 只作为旧版首次迁移输入；迁移后 SQLite 是唯一权威状态。备份恢复见 `docs/control-storage.md`。
- `client.py`（`ControlClient`，强制 https）、`context.py`（登录上下文）、`secrets.py`、`metrics*.py`、`monitoring.py`、`alerting.py`、`build_queue.py`、`workflows.py`、`logs.py`、`operations.py`、`resources.py`（镜像/registry 解析）等按领域拆分。

### 其余模块

- `luma/bootstrap.py`：manager 引导与 `luma update`，分层安装 Docker/Nomad/Traefik/Control/egress，每层可重跑修复。
- `luma/agent.py`：节点 agent。**反向长轮询**：agent 轮询 `/v1/node-agent/lease` 领任务，Control 不主动连节点；凭据只在 lease 时注入内存 payload，绝不持久化；终态上报无限重试直到确认。能力见 `node_agent_capabilities`（NFS、卷、镜像 mirror/cache、buildx 构建、终端、自更新等）。
- 渲染核心：`service.py`/`compose.py`（manifest 加载校验）、`nomad_render.py`（唯一 jobspec 渲染器，CLI 与 Control 共用，也渲染 traefik/egress/control 核心 job）、`render.py`（路径计算 + relay/tcp 的 Traefik file-provider 路由）。
- 集成：`cloudflare.py`、`nomad_api.py`（`deploy_to_nomad` 用 JobModifyIndex 严格关联本次 rollout）、`nomad_node.py`、`egress.py`、`storage.py`、`registry*.py`（`registry_access.registry_host_allows_http` 决定是否允许 HTTP：只对已记录的托管 HTTP registry 和私网/回环地址放行）。
- 安装：`installer.py`（默认安装最新发布 tag）、`scripts/install-luma.sh`、`installation.py`。
- `luma/local.py` / `remote.py`：本地与远程命令执行器。
- `luma/assets/dashboard/`：vite 产物，经 `package-data` 打包，`luma/assets.py` 读取。

### 两条部署路径

1. **原生 manifest**：`luma deploy app.yaml` → `cli/deploy.py` → Control `/v1/deployments` → `render_nomad_job`。`--dry-run` 完全本地渲染。
2. **compose sidecar**：`luma compose deploy` → Control `/v1/compose-deployments` → `render_compose_job`。所有 compose service 渲染成**一个 group 的多个 task**（共享网络命名空间、同节点）。dry-run 需要 Control 在线。不支持 cloudflare-tunnel。

`luma import` / `luma build local` 先构建镜像，再走上面两条路径之一。

## 测试约定

- 根 `tests/` 用标准库 `unittest`，**不要引入 pytest**。
- `tests/__init__.py` 隔离环境：`LUMA_USER_CONFIG`、`LUMA_CONFIG_HOME` 指向临时目录，`LUMA_CONTROL_CONFIG` 默认 `tests/fixtures/cluster.yaml`。测试不得依赖仓库根的 `luma.yaml`、`.env` 或真实 `~/.luma.config.json`。
- mock 要打在**名字被查找的模块**上（例如 `luma.cli.nodes.X` 而不是 `luma.cli.manager.X`）；不要为兼容旧 patch 路径重新导出私有 helper。CLI 分发用 `patch.dict("luma.cli.main.COMMANDS", ...)`。
- Control 端点用 `starlette.testclient.TestClient(create_app())` 测。
- `tests/test_nomad_render.py` / `test_render.py` / `test_nomad_compose.py`：渲染逻辑，改渲染或部署逻辑优先在这里加用例。`tests/test_cli_surface.py`：命令树、帮助与首次使用体验。`tests/test_productization.py`：bootstrap、agent、cloudflare、Control 等宽集成行为。

## 文档

入口是 `docs/README.md`。按阅读顺序：`getting-started.md` → `concepts.md` → `deploying.md` / `operations.md`；参考：`deployment-yaml.md`、`exposure-model.md`、`compose-storage.md`、`secrets.md`、`control-storage.md`、`luma-cli-reference.md`（生成）。维护者文档在 `docs/maintainers/`（发布、网站、控制台设计）。

每篇公开文档都有 `.zh-CN.md` 译文；`scripts/check-site.py` 要求译文的代码块与原文**逐字一致**、标题锚点（`{#id}`）覆盖原文全部锚点。`site/` 是 GitHub Pages 静态站，`scripts/build-site.py` 的 `GUIDES` 决定侧栏，由 `.github/workflows/pages.yml` 部署。

## Local investigation artifacts

Keep page audits, investigation reports, request baselines, validation evidence, and handoff notes in the Git-ignored `.local-notes/` directory. Do not commit them or link to them from published documentation or release notes. Keep `docs/` for maintained product and developer documentation.
