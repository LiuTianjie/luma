# Agent Skill (AI 助手技能)

Luma 提供了一个专为 AI 编码助手（如 Codex、Cursor、Antigravity 等）设计的 Agent Skill。通过安装此 Skill，AI 助手可以直接读取并掌握 Luma 的 YAML 部署清单格式与规则，帮助你生成、校验和优化部署配置。

## 目录结构

该 Skill 位于仓库的 [skills/luma-deployment-yaml](file:///Users/turning4th/infra-stacks/skills/luma-deployment-yaml) 目录下：
- **SKILL.md**：定义了 Skill 的元数据、工作流、Luma Manifest 核心规则与最佳实践。
- **references/manifest-reference.md**：包含单服务 manifest、Compose sidecar、storage、remove 行为和 review checklist 的完整字段参考。

---

## 安装步骤

### 自动安装（推荐）

你可以直接通过以下单行命令将 Skill 克隆并安装到你的本地 AI 助手技能目录中：

```bash
mkdir -p ~/.codex/skills && \
git clone --depth 1 https://github.com/LiuTianjie/luma.git /tmp/luma-repo && \
rm -rf ~/.codex/skills/luma-deployment-yaml && \
cp -R /tmp/luma-repo/skills/luma-deployment-yaml ~/.codex/skills/ && \
rm -rf /tmp/luma-repo
```

> [!NOTE]
> 安装完成后，建议重启你的 AI 客户端或编辑器，以确保本地 Skill 能够被正确加载和激活。

---

## 使用场景

安装 Skill 后，你可以在与 AI 助手对话时，直接使用如下指令：

### 1. 生成部署清单
> **对话示例**: "帮我生成一个部署 Node.js 服务的 Luma YAML 文件，服务需要部署在 `cn` 区域，公网域名是 `api.example.com`，容器端口 `3000`，并且限制 `512M` 内存。"

### 2. 校验与审阅配置
> **对话示例**: "检查一下我的 `luma.compose.yml` 是否符合 Luma 的格式与调度规范。"

### 3. 生成 Docker Compose 与 Luma 旁车
> **对话示例**: "我有一个现有的 `docker-compose.yml` 服务，帮我初始化并编写一个 `luma.compose.yml`，将 `pg-data` 存储卷绑定到 `cn-nfs` 存储类。"

---

## 核心校验规则

Skill 内部包含了对以下核心规则的验证，AI 助手在为您编写清单时会自动遵循：
1. **域与端口约束**：如果 `exposure` 设置为公开入口（如 `cn-edge`、`external-edge` 等），则必须包含 `domain` 和 `port`。
2. **节点固定语义**：`node` 必须使用 `luma node join --name` 的 Luma 节点名；控制面会渲染成 Nomad 的 `meta.luma_node_name` 约束，不依赖 Docker hostname。
3. **端口语义**：`port` 是容器内部端口；`tailscale-relay` / `tcp-relay` 的 `publishPort` 是目标节点 host 端口，需避免与本机已有服务冲突。
4. **存储类防呆设计**：防止在 sidecar 中直接定义非空的 `storageClasses`（该配置需在控制面侧通过 `luma storage set` 声明），以防止意外修改全局存储基础设施。
5. **Region 调度匹配**：确保服务所声明的调度 `region` 与其引用的 `storageClass` 所拥有的网络可达 `regions` 匹配。
6. **私有镜像边界**：registry token 不进 YAML；使用 `luma registry login`。运行时 `proxy: true` 不等于镜像拉取代理。
