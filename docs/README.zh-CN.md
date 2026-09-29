# Luma 文档 {#luma-documentation}

第一次使用 Luma？按顺序读完前两篇，其余指南按需查阅。[English](README.md)

## 从这里开始 {#start-here}

1. [快速上手](getting-started.zh-CN.md)：安装 CLI、初始化 Manager、部署第一个应用、加入更多节点。
2. [核心概念](concepts.zh-CN.md)：节点、区域、暴露方式、出站代理和服务，以及它们如何协作。

## 指南 {#guides}

| 指南 | 内容 |
| --- | --- |
| [部署应用](deploying.zh-CN.md) | 镜像部署、本地构建、仓库导入、Compose、部署方式记录与 CI |
| [日常运维](operations.zh-CN.md) | 日志、历史、回滚、重启、删除、升级与节点维护 |
| [Compose 与存储](compose-storage.zh-CN.md) | 多服务应用与持久卷 |
| [密钥与凭据](secrets.zh-CN.md) | 令牌、应用密钥与镜像仓库凭据 |
| [暴露模型](exposure-model.zh-CN.md) | 每种入口方式的要求与示例 |
| [出站代理](egress-gateway.zh-CN.md) | 镜像拉取与 `proxy: true` 服务使用的出站代理 |
| [可观测](observability.zh-CN.md) | 指标、告警与可选的 observe 栈 |
| [控制台指南](dashboard-guide.zh-CN.md) | Web 控制台 |
| [AI Agent 技能](ai-agent-skills.zh-CN.md) | 让编码助手使用 Luma 部署的 Skill |
| [故障排查](troubleshooting.zh-CN.md) | 已知故障与处理方法 |

## 参考 {#reference}

| 参考 | 内容 |
| --- | --- |
| [部署 YAML 参考](deployment-yaml.zh-CN.md) | 服务 manifest 的全部字段 |
| [CLI 参考](luma-cli-reference.zh-CN.md) | 由 CLI 自动生成的全部命令与参数 |
| [Control 存储与恢复](control-storage.zh-CN.md) | Manager 的 SQLite 状态、保留策略、备份与恢复 |
| [安装生命周期](installation-lifecycle.zh-CN.md) | 各机器上安装与升级的校验方式 |
| [初始化配置档](profiles.zh-CN.md) | `luma bootstrap --profile` 使用的角色组合 |

## 维护者 {#for-maintainers}

[发布流程](maintainers/release.zh-CN.md) · [网站维护](maintainers/website.zh-CN.md) · [控制台设计](maintainers/dashboard-design.zh-CN.md)
