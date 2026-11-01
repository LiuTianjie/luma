# AI Agent Skills {#ai-agent-skills}

Luma 提供三个面向 AI 编码助手的 Skill，包含项目专用的部署与可观测工作流、参考资料和校验规则。Skill 安装在助手中，不安装到运行中的集群。

## 部署工作流 {#deployment-workflows}

[`luma-deployment-yaml`](../skills/luma-deployment-yaml/) 帮助生成和审查单服务清单与 Compose 旁车配置，选择区域与暴露方式，配置本地存储，并排查构建和部署问题。

对于已有应用，它会沿用记录的构建与部署方式。更改工作流时应说明差异并确认；仅安装 Skill 不会更新 CLI 或 Control。密钥使用 `${DATABASE_PASSWORD}` 等引用，不以明文写入 YAML。

示例指令：

> 检查这个 Compose 项目，生成 Luma 旁车配置并校验。沿用现有部署方式，持久化数据使用本地存储。

## 应用可观测 {#application-observability}

[`luma-observe`](../skills/luma-observe/) 帮助部署和维护可选的独立可观测栈，涵盖 Traefik 请求指标、Nomad 实例失败、OTLP 采集和通知发送。

该 Skill 区分 observe 的 vmalert/Alertmanager 规则与 Control 的控制台应用告警预设，并说明 Grafana 登录认证、遥测保留策略和通知配置。

安装 Skill 不会自动安装可观测栈。可观测栈需要单独部署；应用链路追踪还需要对应的 OpenTelemetry 运行时插桩。

示例指令：

> 为这个集群部署 luma-observe，验证应用指标和实例失败告警，并检查通知发送结果。

## 日志分析 {#log-investigation}

[`luma-logs`](../skills/luma-logs/) 使用 `luma app alerts`、`luma app log-query`、运行事件与部署历史分析应用错误。通过 Luma 已认证的 API 查询，不需要 Agent 提取凭证、绕过 Grafana 或直连日志存储。接口说明见[历史日志查询](log-investigation.md)。

示例指令：

> 分析这个应用最近一小时的报错，区分重试与最终失败，并给出原因、业务影响和日志证据。

## 安装与自动升级 {#installation}

直接安装当前 CLI 自带的完整 Skill 套件：

```sh
luma skills install --agent codex
luma skills install --agent claude
# 其他助手：记录自定义安装目录，后续同样自动升级
luma skills install --path /absolute/path/to/assistant/skills
luma skills status --format json
```

`luma update` 和安装脚本会自动刷新已安装的套件；通过 pip／uv 升级后，下一次运行普通 Luma 命令也会补齐同步。Skill 与 CLI 来自同一个安装包和版本，不再另外下载可能超前的 `main`。

默认识别 Codex、Claude 目录中的旧手工安装，接管后补齐套件内的新 Skill；没有安装过 Luma Skill 的助手不会被自动写入。支持 `CODEX_HOME`、`CLAUDE_CONFIG_DIR` 和已登记的自定义目录。提权安装时以目标用户身份同步其默认目录；非默认目录请先通过 `skills install` 登记。

每个 Skill 的 `.luma-managed.json` 记录配套版本与文件哈希。升级发现本地修改时，先把旧目录备份到助手技能目录旁的 `luma-skill-backups`，再安装新版本，并输出备份路径。未修改的托管文件直接替换，不反复产生备份；Skill 内已移除的旧文件也会清除。符号链接目录会保留并明确提示。权限等同步失败不会使 CLI 升级失败，但会显示同步未完成，解决后可运行：

```sh
luma skills sync
# 当前版本也有本地修改时，主动恢复配套内容，同样先备份
luma skills sync --force
```

`status` 只读显示配套状态和本地修改。同版本本地修改在下次套件更新或显式 `--force` 前保持原样；建议把长期定制放在独立指令中，或从备份重新应用。开发源码目录不在普通命令运行时自动同步，需要显式运行 `skills sync`。

wheel 和源码包都包含完整 Skill 及其引用文件。安装 Skill 不会升级 Control，也不会部署可观测栈；升级后请重新加载 Skill 或新开助手对话。

## 参考文档 {#references}

- [部署 YAML](deployment-yaml.zh-CN.md)
- [Compose 与本地存储](compose-storage.zh-CN.md)
- [可观测](observability.zh-CN.md)
- [部署工作流记录](../skills/luma-deployment-yaml/references/deployment-workflow.md)
