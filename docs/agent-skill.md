# Agent Skill (AI 助手技能)

Luma 和 LAE 各提供一个给 AI 编码助手（Codex、Cursor、Claude Code 等）用的 Skill。

- **`luma-deployment-yaml`**：超管/运维侧。生成、校验部署 YAML，沿用服务端记录的构建部署方式，并处理部署差异确认。
- **`lae-deploy`**：租户/产品侧。只通过 `lae` CLI 诊断和部署；不要用它去调 Luma management API。

本仓库 checkout 里的副本是权威来源。把 Skill 拷到助手的用户级 skills 目录后，新对话才会稳定加载。

Luma 与 LAE 技能独立维护。下面的默认安装只更新 Luma 技能，不安装、删除或更新 LAE 技能。

## 目录结构

[`skills/luma-deployment-yaml`](../skills/luma-deployment-yaml)：

- **SKILL.md**：工作流、region/exposure、Builder Registry、local build、Compose/storage、CI。
- **references/manifest-reference.md**：完整字段表、import/build 规则、rollback checklist。
- **references/deployment-workflow.md**：自动检查、无记录放行、差异确认、成功记录与版本兼容边界。

[`lae/skills/lae-deploy`](../lae/skills/lae-deploy)：

- **SKILL.md**：认证、inspect、env、deploy、lifecycle、支付人机边界。
- **references/cli-contract.md** / **policy.md**：CLI 命令契约与策略。
- **references/knowledge-pack.json**：与 Controller 同源的版本化 Knowledge Pack。

## 安装

用户级目录：

| 助手 | Skills 目录 |
| --- | --- |
| Claude Code / Cursor | `~/.claude/skills` |
| Codex | `~/.codex/skills` |

从本仓库 checkout 安装（推荐，保证和当前代码一致）：

```bash
repo="/path/to/infra-stacks"
for dest in ~/.claude/skills ~/.codex/skills; do
  mkdir -p "$dest/luma-deployment-yaml"
  cp -R "$repo/skills/luma-deployment-yaml/." "$dest/luma-deployment-yaml/"
done
```

没有本地 checkout 时，从 GitHub `main` 安装：

```bash
tmp="$(mktemp -d)"
git clone --depth 1 https://github.com/LiuTianjie/luma.git "$tmp/luma"
for dest in ~/.claude/skills ~/.codex/skills; do
  mkdir -p "$dest/luma-deployment-yaml"
  cp -R "$tmp/luma/skills/luma-deployment-yaml/." "$dest/luma-deployment-yaml/"
done
rm -rf "$tmp"
```

在 Codex 里也可以说：

```text
Install the skill from https://github.com/LiuTianjie/luma/tree/main/skills/luma-deployment-yaml
```

安装后重启对应助手，或新开一轮对话。

## 使用场景

### luma-deployment-yaml

1. 生成部署清单  
   「帮我生成一个部署 Node.js 服务的 Luma YAML，`region: cn`，域名 `api.example.com`，端口 `3000`，内存 `512M`。」
2. 校验 Compose sidecar  
   「检查一下我的 `luma.compose.yml` 是否符合 Luma 调度和存储规范。」
3. 从现有 Compose 写 sidecar  
   「已有 `docker-compose.yml`，帮我写 `luma.compose.yml`，把 `pg-data` 绑到 `cn-nfs`。」
4. 回滚准备度  
   「review 这个部署文件，确认镜像 tag、存储和 Compose sidecar 是否适合生产回滚。」
5. 沿用构建部署方式
   「按上次的方式部署这个应用；没有记录就正常部署，出现差异先告诉我，确认后再继续。」

### lae-deploy

「使用 lae-deploy skill 部署这个应用。先做 capability/doctor/whoami，再 inspect；只在结果为 `deployable` 时部署。token、Git secret、环境变量和支付确认都由我在本机输入。」

## luma-deployment-yaml 核心规则

部署时优先沿用服务端记录。更新后的 CLI 会在 `import`、`build local/retry`、`deploy`、`compose deploy` 前自动检查：无记录正常继续，成功后自动建立；一致直接继续；不同才显示差异并要求用户确认。非交互调用会先停止，只有用户明确同意该变化后才能使用 `--accept-workflow-change`。不能通过覆盖记录、更换应用名或绕到其他部署入口来跳过确认。失败与 dry-run 不覆盖成功记录。

技能更新不代表运行中的 CLI/Control 已升级。部署前若兼容性不明，检查 CLI 帮助和 Control 的 `deployment-workflow-v1` 能力；检查不可用不等于没有记录。具体命令与边界见 [Deployment Workflow Records](../skills/luma-deployment-yaml/references/deployment-workflow.md)。

1. **域与端口**：公开 `exposure` 必须有 `domain` 和 `port`。
2. **节点固定**：`node` 使用 `luma node join --name` 的 Luma 节点名，不要用 Docker hostname。
3. **端口语义**：`port` 是容器端口；`tailscale-relay` / `tcp-relay` 的 `publishPort` 是目标节点 host 端口。
4. **存储类**：sidecar 不要定义非空 `storageClasses`；用 `luma storage set` 在控制面注册，sidecar 只引用名称。
5. **region 与存储**：服务 `region` 必须落在所引用 `storageClass` 的可达 regions 内。
6. **镜像与 secret**：registry token 不进 YAML；用 `luma registry login`。`proxy: true` 不是镜像拉取代理。有 Builder Registry 时，预构建镜像先拷进内部 registry 再部署。
7. **回滚**：`luma rollback` 是 Nomad job 运行态回滚，不回写 Git/manifest，也不恢复卷数据。
