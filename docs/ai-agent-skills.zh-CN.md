# AI Agent Skills {#ai-agent-skills}

Luma 提供两个面向 AI 编码助手的 Skill，包含项目专用的部署与可观测工作流、参考资料和校验规则。Skill 安装在助手中，不安装到运行中的集群。

## 部署工作流 {#deployment-workflows}

[`luma-deployment-yaml`](../skills/luma-deployment-yaml/) 帮助生成和审查单服务清单与 Compose 旁车配置，选择区域与暴露方式，配置本地存储，并排查构建和部署问题。

对于已有应用，它会沿用记录的构建与部署方式。更改工作流时应说明差异并确认；仅安装 Skill 不会更新 CLI 或 Control。密钥使用 `${DATABASE_PASSWORD}` 等引用，不以明文写入 YAML。

示例指令：

> 检查这个 Compose 项目，生成 Luma 旁车配置并校验。沿用现有部署方式，持久化数据使用本地存储。

## 应用可观测 {#application-observability}

[`luma-observe`](../skills/luma-observe/) 帮助部署和维护可选的独立可观测栈，涵盖 Traefik 请求指标、Nomad 实例失败、OTLP 采集和通知发送。

安装 Skill 不会自动安装可观测栈。可观测栈需要单独部署；应用链路追踪还需要对应的 OpenTelemetry 运行时插桩。

示例指令：

> 为这个集群部署 luma-observe，验证应用指标和实例失败告警，并检查通知发送结果。

## 安装 {#installation}

在本地仓库目录中，将两个 Skill 文件夹复制到助手的用户级技能目录。Codex 使用：

```bash
mkdir -p ~/.codex/skills/luma-deployment-yaml ~/.codex/skills/luma-observe
cp -R skills/luma-deployment-yaml/. ~/.codex/skills/luma-deployment-yaml/
cp -R skills/luma-observe/. ~/.codex/skills/luma-observe/
```

Claude Code 使用：

```bash
mkdir -p ~/.claude/skills/luma-deployment-yaml ~/.claude/skills/luma-observe
cp -R skills/luma-deployment-yaml/. ~/.claude/skills/luma-deployment-yaml/
cp -R skills/luma-observe/. ~/.claude/skills/luma-observe/
```

其他助手请通过其支持的 Skill 安装方式，使用上方链接的源码目录。安装后新开对话。更新 Luma 时，应从匹配版本的仓库同步 Skill，保持技能说明与安装版本一致。

## 参考文档 {#references}

- [部署 YAML](deployment-yaml.md)
- [Compose 与本地存储](compose-storage.md)
- [可观测](observability.md)
- [部署工作流记录](../skills/luma-deployment-yaml/references/deployment-workflow.md)
