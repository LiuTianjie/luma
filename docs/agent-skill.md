# Agent Skill (AI 助手技能)

Luma 提供了一个专为 AI 编码助手（如 Codex、Cursor、Antigravity 等）设计的 Agent Skill。通过安装此 Skill，AI 助手可以直接读取并掌握 Luma 的 YAML 部署清单格式与规则，帮助你生成、校验和优化部署配置。

## 目录结构

该 Skill 位于仓库的 [skills/luma-deployment-yaml](file:///Users/turning4th/infra-stacks/skills/luma-deployment-yaml) 目录下：
- **SKILL.md**：定义了 Skill 的元数据、系统提示词以及 Luma Manifest 的标准字段规范与最佳实践。
- **rules/**：包含具体的 YAML 结构校验与调度规则。

---

## 安装步骤

### 自动安装（推荐）

你可以直接通过以下单行命令将 Skill 克隆并安装到你的本地 AI 助手技能目录中：

```bash
mkdir -p ~/.codex/skills && \
git clone --depth 1 https://github.com/LiuTianjie/luma.git /tmp/luma-repo && \
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
> **对话示例**: "我有一个现有的 `docker-compose.yml` 服务，帮我初始化并编写一个 `luma.compose.yml`，将 `pg-data` 存储卷绑定到 `home-nfs` 存储类。"

---

## 核心校验规则

Skill 内部包含了对以下核心规则的验证，AI 助手在为您编写清单时会自动遵循：
1. **域与端口约束**：如果 `exposure` 设置为公开入口（如 `cn-edge`、`external-edge` 等），则必须包含 `domain` 和 `port`。
2. **存储类防呆设计**：防止在 sidecar 中直接定义非空的 `storageClasses`（该配置需在控制面侧通过 `luma storage set` 声明），以防止意外修改全局存储基础设施。
3. **Region 调度匹配**：确保服务所声明的调度 `region` 与其引用的 `storageClass` 所拥有的网络可达 `regions` 匹配。
