# Luma 进度交接 {#luma-progress-handoff}

> 2026-07-12 的历史快照（`0.1.200` / `lab` 上的 LAE 9 任务）。以下运行数据不是当前状态。面向用户的命令见 `README.md`、`docs/how-to-use-luma.md`、`docs/bootstrap.md`。LAE 状态见 `docs/lae/08-implementation-status.md` 和 `docs/lae/13-final-handoff.md`。

日期：2026-07-12

本文是截至该日期的控制面实现简要交接。

## 当时的运行快照 {#current-live-snapshot}

- Control 和 Manager agent 运行 `0.1.200`，6 个在线非 Manager agent 运行 `0.1.198`；之后变更仅涉及 Manager 重启协调/渲染。`0.1.200` 产品化测试 461/461 通过。离线 `blg` 仍为 `0.1.175`，等待重连前不操作。
- `manager` 是唯一控制面，`aly` 是旧名称。LAE 平台在 `lab` 运行 9 任务组，租户运行时仅准入 `manager + tecent`，面向租户的 API 不暴露放置位置。
- LAE staging job 版本 23 使用精确 commit `2f2afd2ab84926d058cbff53c51a86e8816324a9` 构建镜像，9 个任务与 Web/API/Agent/产物探测健康。
- 两个真实 FastAPI 租户应用在 `tecent` 运行，新模板启动已完成 Agent 诊断、Builder 构建、运行时部署、随机域名发布与有效 TLS，无需用户提供 Luma 清单。
- `0.1.190` 按部署隔离 Nomad 服务和 Traefik router 名；`0.1.192` 使用运行节点 `luma_tailscale_ip` 注册上游；`0.1.196` 增加 BuildKit 超时取消和标签/包版本检查；`0.1.198` 刷新可信静态适配器并保留运维配置；`0.1.200` 等待替换 allocation，并根据存储部署与实际节点协调路由/DNS/公网探测。真实 `linkshell-gateway` 重启 4.83 秒完成，有路由、DNS 和 HTTP 200 证据，随后 12/12 公网探测成功。定向 LAE Web 路由检查通过，但长时探测仍有少量 404/502/超时，尚不能证明零停机。
- 更新检查的语义摘要已排除单次尝试的 snapshot/plan ID。两次独立检查得到相同计划；重新部署后再查，source、plan、aggregate 均为 `changed=false`。
- 真实 HTML 上传完成隔离扫描、Agent 诊断、可信静态构建、SBOM/Trivy 门禁、运行时部署，以及随机 `*.itool.tech` 的 6 项公网内容检查；临时应用随后成功删除。ZIP 仍是独立必测案例。
- 生产仍受专用 runner/core 池、完整来源与生命周期矩阵、Docker/CNI/协调故障注入、真实 SMTP、支付、隔离、备份和恢复演练阻塞。

## 当时的方向 {#current-direction}

Luma 使用自托管控制面，取代 SSH 驱动部署：

- 首个完整节点在本机执行 `luma bootstrap manager --domain luma.example.com`。
- 工作节点在各服务器执行 `luma node join https://luma.example.com --token <node-join-token> --region cn|global|home --name <node-name>`。
- 客户端仅需 `luma login` 和 `luma deploy`，无需 Docker、SSH、Cloudflare 或 Nomad 凭据。
- 编排器为 HashiCorp Nomad，部署直接调用 Nomad HTTP API。
- Control 管理令牌、节点注册/meta、DNS、jobspec、镜像拉取回退、部署状态和 Nomad API 编排。

## 主流程 {#main-flow}

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
luma bootstrap manager --domain luma.example.com
luma login https://luma.example.com --token <management-token>
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
luma deploy app.yaml
luma doctor
```

## 重要默认值 {#important-defaults}

- Control API 镜像：`ghcr.io/liutianjie/luma-control:latest`。
- Manager 运行根目录：`/opt/luma`。
- 客户端 context 根目录：`~/.config/luma`。
- 公共入口：Traefik 80/443。
- Nomad 控制端口：4646 HTTP API、4647 RPC、4648 Serf，仅在 tailnet 放行。
- 默认出网代理：本地 `127.0.0.1:7890` 的 Mihomo，禁止公网入站。

## 修复命令 {#repair-commands}

直接在待修复服务器执行：

```bash
luma bootstrap manager --domain luma.example.com
luma egress setup
luma egress refresh
luma tailscale connect
```

## 发布流程 {#release-flow}

```bash
docker build -f Dockerfile.control -t ghcr.io/liutianjie/luma-control:v0.1.0 .
docker tag ghcr.io/liutianjie/luma-control:v0.1.0 ghcr.io/liutianjie/luma-control:latest
docker push ghcr.io/liutianjie/luma-control:v0.1.0
docker push ghcr.io/liutianjie/luma-control:latest
git tag v0.1.0
git push origin main --tags
```

用户可固定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.0 sh
```

## 验证 {#validation}

发布前使用以下检查：

```bash
python -m unittest discover -s tests
python -m compileall luma
./scripts/validate-stacks.sh
sh -n scripts/install-luma.sh
git diff --check
```
