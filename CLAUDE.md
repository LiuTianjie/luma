# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

Luma 是一个面向 HashiCorp Nomad 的轻量自托管部署控制面。Python CLI（`luma`）从任意已认证的客户端运行，通过 Luma Control 渲染并提交 Nomad job，用 Traefik 做 HTTP/HTTPS/TCP 入口，用 Cloudflare 管理 DNS。Python 包名为 `luma-infra`，发布到 PyPI。

调用链：`客户端 -> Luma Control -> Nomad API -> Nomad client -> docker driver -> container`

## 常用命令

开发环境用仓库内的 `.venv`（不是 conda base，base 里没有依赖）：

```bash
# 安装开发环境（创建 .venv，可编辑安装）
./scripts/install-luma.sh
. .venv/bin/activate

# 跑全部测试（项目用 unittest，没有 pytest）
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# 跑单个测试
.venv/bin/python -m unittest tests.test_nomad_render.NomadRenderTests.test_public_cn_service_gets_traefik_labels

# 校验示例和模板 manifest 是否都能渲染成 Nomad jobspec
./scripts/validate-stacks.sh

# 仓库内运行 CLI（避免 shell 把 luma/ 包目录当成命令）
.venv/bin/luma <command>
./scripts/luma <command>
```

Dashboard（React + Vite + TypeScript，源码在 `dashboard-src/`）：

```bash
npm run dev:dashboard        # 本地开发
npm run build:dashboard      # 构建
npm run typecheck:dashboard  # 类型检查
```

## 版本管理

版本号同步散落在多个文件，**绝不要手改**，统一用脚本：

```bash
python scripts/bump-version.py --check    # 校验各文件版本是否一致
python scripts/bump-version.py --minor    # 或 --major / --set x.y.z
```

`scripts/bump-version.py` 维护 `pyproject.toml`、`luma/__init__.py`、`luma/assets/pyproject.toml` 三处版本号，并同步 `README.md`、`docs/` 与 `luma/cli.py` 里的版本引用（如 `luma-infra==`、`git tag v`、`luma-control:v`、`--install-ref v`）。改版本后这几处必须保持一致，否则 `--check` 会失败。

## 核心模型

用户面向的概念只有五个，理解它们是读懂代码的前提：

- `node`：加入 Nomad 集群并安装 Luma agent 的机器（manager / worker / home）。
- `region`：调度边界（`cn` / `global` / `home`），决定服务**跑在哪**。`region: cn` 的服务只调度到 `region=cn` 的节点。
- `exposure`：流量**怎么进**（`cn-edge` / `external-edge` / `tailscale-relay` / `cloudflare-tunnel` / `none`）。
- `egress`：出站代理能力（镜像拉取 + `proxy: true` 服务的运行时 HTTP/HTTPS 代理）。
- `service`：一个 Luma YAML manifest 描述的部署单元。

`region` 和 `exposure` 是两个正交维度，不要混淆。region 职责划分见 `docs/architecture.md`。

## Token 模型

只有两种用户可见 token：**管理 token**（`LUMA_DEPLOY_TOKEN`，客户端/dashboard/CI 用，登录+部署+管理 secret/registry/storage/node）和**节点加入 token**（`luma node join` 用）。节点 agent 凭据是内部的，Control 只存 hash，不要试图让用户复制或管理它们。

## 代码结构与架构

`luma/` 是 Python 包，按职责拆分模块。关键的几个大文件：

- `luma/cli.py`（~2500 行）：argparse 命令分发入口。所有子命令在这里注册（`bootstrap`、`node`、`deploy`、`compose`、`secret`、`registry`、`storage`、`update` 等），命令实现是 `cmd_*` 函数。改 CLI 行为从这里入手。
- `luma/control/server.py`（~4200 行）：Control API 服务端（标准库 `http.server`，无 Web 框架）。运行在 manager 上、容器内（见 `Dockerfile.control`）。处理部署、DNS 同步、节点 agent 任务派发、状态查询，并以 NDJSON 流式返回部署事件。
- `luma/control/client.py` / `state.py` / `context.py`：客户端 HTTP 封装（`ControlClient`，强制 https）、服务端状态持久化、登录上下文管理。
- `luma/bootstrap.py`：manager 引导与 `luma update`，分层安装 Docker/Nomad/Traefik/Control/egress，每层可单独重跑修复。
- `luma/agent.py`（~770 行）：节点 agent，跑在每个加入的节点上，执行 Control 派发的本地任务（NFS、volume、容器统计等），能力随 OS 不同（见 `node_agent_capabilities`）。
- `luma/render.py` / `compose.py` / `service.py` / `nomad_render.py`：两条部署路径的渲染核心。`service.py` + `render.py` 处理原生 Luma manifest（`luma deploy`）；`compose.py` 处理 compose sidecar 路径（`luma compose deploy`），最终都渲染成 Nomad jobspec。
- `luma/cloudflare.py` / `nomad_api.py` / `egress.py` / `storage.py` / `registry.py`：各外部系统的集成封装。
- `luma/local.py` / `remote.py`：本地与远程命令执行器（`LocalExecutor`）。

### 两条部署路径

代码里 deploy 逻辑成对出现，改一条通常要同步看另一条：

1. **原生 manifest**：`luma deploy app.yaml` → `cmd_deploy` → `render_stack`（`render.py`）→ Control。
2. **compose sidecar**：`luma compose deploy` → `cmd_compose_deploy` → `render_compose_stack`（`compose.py`）→ Control。

两者都通过 `ControlClient` 把 manifest 文本发给 Control，Control 端再渲染并经 Nomad HTTP API 部署，部署过程以 NDJSON 事件流回传。`--dry-run` 在客户端本地渲染、不联系 Control。

### 资产打包

`luma/assets/`（Dockerfile.control、dashboard 产物、核心 Nomad job 模板）通过 `pyproject.toml` 的 `package-data` 打进 Python 包，运行时用 `luma/assets.py` 的 `asset_path()` / `asset_text()` 读取。

## 测试约定

- 用标准库 `unittest`，**不要引入 pytest**。
- `tests/test_nomad_render.py` / `tests/test_render.py`：渲染逻辑（manifest/compose → Nomad jobspec、Traefik route、storage、tailscale route）。
- `tests/test_productization.py`：bootstrap、agent、cloudflare、control server 等更宽的集成行为，常用 `unittest.mock` 与临时 `ThreadingHTTPServer`。
- 改渲染或部署逻辑时，优先在这两个文件里加用例。

## 文档

`docs/` 下文档大多为中文，是设计意图的权威来源。改对应模块前先读相关文档：`architecture.md`（region 模型）、`deployment-yaml.md` / `compose-storage.md`（manifest 字段）、`exposure-model.md`、`secrets.md`、`bootstrap.md`、`release.md`（发布流程）。`site/` 是 GitHub Pages 静态站，由 `.github/workflows/pages.yml` 部署。
