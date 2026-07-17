# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

本仓库包含两层产品：

1. **Luma**：面向 HashiCorp Nomad 的轻量自托管部署控制面。Python CLI（`luma`）从任意已认证的客户端运行，通过 Luma Control 渲染并提交 Nomad job，用 Traefik 做 HTTP/HTTPS/TCP 入口，用 Cloudflare 管理 DNS。Python 包名为 `luma-infra`，发布到 PyPI。
2. **LAE（Luma Application Engine，`lae/` 目录）**：建在 Luma 之上的多租户 ToC 应用托管平台（面向普通用户和 AI Agent）。Luma 是基础设施控制面/超管底座；LAE 负责用户/租户/应用/诊断/构建/部署任务/配额/计费/审计。设计与现状的权威来源是 `docs/lae/`（尤其 `08-implementation-status.md` 完成度表、`13-final-handoff.md` 交接现状、`07-open-decisions.md` 已确认决策）。

Luma 调用链：`客户端 -> Luma Control -> Nomad API -> Nomad client -> docker driver -> container`

LAE 调用链：`用户/CLI/Skill -> LAE API（FastAPI）-> PostgreSQL（operation/outbox）-> lae-worker -> lae-luma-adapter -> Luma Control（/v1/builder/* 与 /v1/lae/runtime/*）-> Nomad`

LAE 核心边界：用户与用户级 token 永远不能直接调 Luma management API，lae-worker 是唯一允许调 Luma 的产品服务；用户只选 region（cn/global），看不到节点/IP；AI 生成 manifest candidate，确定性校验器终审。

## 常用命令

开发环境用仓库内的 `.venv`（不是 conda base，base 里没有依赖）：

```bash
# 安装开发环境（创建 .venv，可编辑安装）
./scripts/install-luma.sh
. .venv/bin/activate

# 跑全部测试（根 tests/ 用 unittest，没有 pytest）
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
npm run build:dashboard      # 构建（产物输出到 luma/assets/dashboard，git 忽略、CI 构建）
npm run typecheck:dashboard  # 类型检查
```

LAE（`lae/` 是独立 workspace：uv + Python 3.12 + pnpm + Node 22，有自己的 CI `.github/workflows/lae-ci.yml`）：

```bash
cd lae
make contracts   # 校验 JSON Schema 契约
make test        # LAE 自己的测试（pytest 风格；根仓库"不引入 pytest"的约定不适用于 lae/）
make check
```

## 版本管理

版本号同步散落在多个文件，**绝不要手改**，统一用脚本：

```bash
python scripts/bump-version.py --check    # 校验各文件版本是否一致
python scripts/bump-version.py --minor    # 或 --major / --set x.y.z
```

`scripts/bump-version.py` 维护 `pyproject.toml`、`luma/__init__.py`、`luma/assets/pyproject.toml` 三处版本号，并同步 `README.md`、`docs/` 与 `luma/cli.py` 里的版本引用（如 `luma-infra==`、`git tag v`、`luma-control:v`、`--install-ref v`）。改版本后这几处必须保持一致，否则 `--check` 会失败。

## 核心模型

Luma 用户面向的概念只有五个，理解它们是读懂代码的前提：

- `node`：加入 Nomad 集群并安装 Luma agent 的机器（manager / worker / home）。
- `region`：调度边界（`cn` / `global` / `home`），决定服务**跑在哪**。`region: cn` 的服务只调度到 `region=cn` 的节点。
- `exposure`：流量**怎么进**，共六种（`cn-edge` / `external-edge` / `tailscale-relay` / `tcp-relay` / `cloudflare-tunnel` / `none`）。region 与 exposure 有强绑定：cn-edge⇒cn、external-edge⇒global、tailscale-relay⇒home。
- `egress`：出站代理能力（镜像拉取 + `proxy: true` 服务的运行时 HTTP/HTTPS 代理）。
- `service`：一个 Luma YAML manifest 描述的部署单元。

`region` 和 `exposure` 是两个正交维度，不要混淆。region 职责划分见 `docs/architecture.md`，exposure 全模式见 `docs/exposure-model.md`。

LAE 在此之上引入 tenant / application / operation / deployment / placement 语义（见 `docs/lae/`）。

## Token 模型

用户可见 token 只有两种：**管理 token**（`LUMA_DEPLOY_TOKEN`，客户端/dashboard/CI 用，登录+部署+管理 secret/registry/storage/node）和**节点加入 token**（`luma node join` 用）。节点 agent 凭据是内部的，Control 只存 hash，不要试图让用户复制或管理它们。

LAE 另有三类**服务端 principal**（不是用户 token，全部从 0600 文件加载、互斥校验、不可与管理 token 混用）：builder 面 principal（lae-worker 调 `/v1/builder/*`）、runtime 面 principal（调 `/v1/lae/runtime/*`，带 audience+scope）、plan 签名密钥（HMAC 验 signedBuildPlan）。

## 代码结构与架构

`luma/` 是 Python 包，按职责拆分模块。关键的几个大文件：

- `luma/cli.py`（~3700 行）：argparse 命令分发入口。所有子命令在这里注册（`bootstrap`、`node`、`deploy`、`compose`、`secret`、`registry`、`storage`、`update`、`import`、`build`、`doctor` 等），命令实现是 `cmd_*` 函数。改 CLI 行为从这里入手。
- `luma/control/server.py`（~17700 行）：Control API 服务端。**实际是 Starlette ASGI + uvicorn**（入口 `create_app()`/`serve()`），同时保留旧的 `ThreadingHTTPServer` + `ControlHandler` 同步栈（测试仍在用）。**每个端点在两套栈里各注册一次（`do_GET`/`do_POST` 与 `_asgi_authenticated_get`/`_asgi_authenticated_post`），改端点必须两处同步改**。运行在 manager 上、容器内（见 `Dockerfile.control`）。处理部署、DNS 同步、节点 agent 任务派发、状态查询、builder task API、LAE runtime API，并以 NDJSON 流式返回部署事件。所有部署路径被全局 `_DEPLOY_LOCK` 串行化。
- `luma/control/client.py` / `state.py` / `context.py` / `secrets.py` / `metrics.py` / `resources.py`：客户端 HTTP 封装（`ControlClient`，强制 https）、服务端状态持久化（`control.json`，每次心跳全量重写，metrics 因此拆独立文件）、登录上下文、secret 渲染、指标历史、镜像/registry 解析。
- `luma/bootstrap.py`（~2000 行）：manager 引导与 `luma update`，分层安装 Docker/Nomad/Traefik/Control/egress，每层可单独重跑修复。
- `luma/agent.py`（~4400 行）：节点 agent，跑在每个加入的节点上。**反向长轮询模型**：agent 轮询 Control 的 `/v1/node-agent/lease` 领任务，Control 不主动连节点；凭据只在 lease 时注入内存 payload、绝不持久化；终态上报无限重试直到 Control 确认。能力随 OS 不同（见 `node_agent_capabilities`），含 NFS、volume、镜像 mirror/cache、buildx 构建、终端、自更新等动作。
- `luma/render.py` / `service.py` / `compose.py` / `nomad_render.py`：渲染核心。`service.py`/`compose.py` 负责 manifest schema 加载校验；`nomad_render.py`（~1500 行）是唯一的 jobspec 渲染器，CLI 与 Control 共用（保证 dry-run 与实际部署产物一致），也渲染 traefik/egress/control 三个核心基础设施 job；`render.py` 只剩路径计算 + relay/tcp 的 Traefik file-provider 路由 YAML。
- **Builder 子系统**（LAE 构建面，跑在 builder 节点）：`builder_tasks.py`（closed-schema 任务协议，analyze-source / build-plan 两种 kind）、`builder_executor.py`（源码分析：rootless Docker 沙箱 + pinned digest runner 镜像 + 确定性 snapshot）、`builder_build_executor.py`（rootless BuildKit 构建 + SBOM + Trivy 扫描 + provenance）、`credential_broker.py`（短期凭据单次兑换，TTL≤300s，fail-closed）、`artifact_leases.py`（分析产物一次性内存 lease）、`gitops.py`（shallow clone，凭据经 askpass 不进 argv/env）。
- **LAE runtime 面**：`lae_runtime.py`（runtime 边界协议 + 部署绑定的内存 secret lease）、`lae_placement.py`（region-only 内部 placement：allowlist 准入 + 拓扑脱敏投影）、`lae_admin_proxy.py`（dashboard 超管只读代理）。
- `luma/cloudflare.py` / `nomad_api.py` / `nomad_node.py` / `egress.py` / `storage.py` / `registry.py`：各外部系统的集成封装。`nomad_api.py` 的 `deploy_to_nomad` 是部署正确性核心（用 JobModifyIndex 严格关联本次 rollout）。
- `luma/local.py` / `remote.py`：本地与远程命令执行器。

`lae/` 是独立 monorepo workspace（可迁出仓库）：`apps/api`（FastAPI 租户 API）、`apps/web`（Next.js 控制台）、`services/worker`（唯一调 Luma 的编排器）、`services/agent-runner`（确定性分析器，沙箱内跑）、`services/agent-controller`（candidate 校验+签名）、`packages/python/lae-store`（PostgreSQL 持久层 + Alembic migrations）、`packages/python/lae-luma-adapter`（面向 Luma 的唯一边界，http/fake 双实现）、`packages/contracts`（JSON Schema 契约，唯一协议源）、`deploy/luma`（LAE 自身的 Luma 部署资产）。

### 三条部署路径

改一条通常要同步看其他条：

1. **原生 manifest**：`luma deploy app.yaml` → `cmd_deploy` → Control `/v1/deployments` → `render_nomad_job`。`--dry-run` 完全本地渲染、不联系 Control。
2. **compose sidecar**：`luma compose deploy` → `cmd_compose_deploy` → Control `/v1/compose-deployments` → `render_compose_job`。所有 compose service 渲染成**一个 group 的多个 task**（共享网络命名空间、同节点）。dry-run 也需要 Control 在线（拉 storage class 与节点记录做校验，与原生不同）。不支持 cloudflare-tunnel。
3. **LAE runtime**：LAE worker → Control `/v1/lae/runtime/deployments` 直收结构化 manifest，内部复用 `render_compose_job`，叠加 placement 准入与 Nomad Variables 注入 secret。不走 CLI。

前两条都通过 `ControlClient` 把 manifest 文本发给 Control，Control 端渲染并经 Nomad HTTP API 部署，部署过程以 NDJSON 事件流回传。

### 资产打包

`luma/assets/` 通过 `pyproject.toml` 的 `package-data` 打进 Python 包，运行时用 `luma/assets.py` 的 `asset_path()` / `asset_text()` 读取。内容：`Dockerfile.control`（与仓库根的不同：assets 版无 dashboard 构建阶段）、`pyproject.toml`（bump-version 同步的三处版本号之一）、`dashboard/`（vite 产物，git 忽略、CI 构建）。**注意：assets 里没有 Nomad job 模板**——traefik/egress/control 核心 job 由 `nomad_render.py` 代码生成；`assets/stacks/core/` 是 Swarm 时代的空目录残留。

## 测试约定

- 根 `tests/` 用标准库 `unittest`，**不要引入 pytest**（`lae/` 子 workspace 例外，它有自己的测试与 CI）。
- `tests/test_nomad_render.py` / `test_render.py` / `test_nomad_compose.py`：渲染逻辑（manifest/compose → Nomad jobspec、Traefik route、storage、tailscale route）。
- `tests/test_productization.py`（~16000 行）：bootstrap、agent、cloudflare、control server 等宽集成行为，常用 `unittest.mock` 与临时 `ThreadingHTTPServer`。
- `tests/test_builder_*.py`（7 个）与 `tests/test_lae_*.py`（5 个）：builder 任务协议/执行器/凭据 broker 的安全边界，LAE runtime API/placement。注意 `test_lae_runtime_api.py` 用 `starlette.testclient` 走 ASGI 栈，`test_builder_tasks_security.py` 故意直接测 handler 不走 HTTP。
- 改渲染或部署逻辑时，优先在渲染测试文件里加用例；改 Control 端点记得两套 HTTP 栈都要覆盖。

## 文档

`docs/` 下文档大多为中文，是设计意图的权威来源。改对应模块前先读相关文档：`architecture.md`（region 模型）、`deployment-yaml.md` / `compose-storage.md`（manifest 字段）、`exposure-model.md`（六种 exposure）、`secrets.md`（token/secret 模型）、`bootstrap.md`、`operations.md`（rollback/restart/remove 语义）、`release.md`（发布流程）。**LAE 相关一律以 `docs/lae/` 为准**。`site/` 是 GitHub Pages 静态站，由 `.github/workflows/pages.yml` 部署。
