# Nomad 安全默认值与自动升级

首次执行 `luma bootstrap` 和 `luma node join` 就会配置 ACL、双向 TLS、节点引入凭据和持久网络规则。安装程序验证实际运行配置后才报告节点成功。用户不需要另外执行一组 `luma security` 命令。

本版覆盖 Nomad 与节点入口加固，尚未完成业务密钥体系整改：SQLite 中的业务秘密仍未加密，现有 secret scope 只是配置范围，尚无按应用身份授权的独立秘密分发。具有 Nomad 作业读取权限的身份仍能读取作业 Env。短期令牌目前用于节点引入，业务长期凭据短期化、按实际凭据去重与批量轮换仍未实现。

已有集群按正常方式升级即可：

1. 在管理节点执行 `luma update manager`，或使用 Dashboard 的「升级 Control」。
2. 通过 `luma update fleet`、Dashboard 节点更新，或各节点上的 `luma update` 更新全部节点 Agent。
3. 在系统更新页面查看「集群安全升级」。只有「全部验证通过」表示迁移完成；软件版本对齐和安全验证分别显示。

管理节点安装独立的 `luma-nomad-security.service`。它等待已注册节点的新版 Agent 就绪，收集配置和身份，检查应用权限与现有存储，再执行持久化的迁移步骤。Control 替换、Nomad 重启和进程中断不会丢失已记录的步骤。离线节点会明确列出；未知作业、身份冲突或不安全配置会阻断并保留原因。修复原因后重新执行普通管理节点更新即可重新检查、继续迁移。

## 升级过程中会发生什么

- 自动路径覆盖 Luma 管理的单服务端集群：Linux 管理节点、Linux/macOS 客户端、Nomad `global` 区域、`dc1` 数据中心、`/opt/nomad/data` 数据目录。
- 从 1.9.x 起逐个次版本升级：**1.10.5 → 1.11.3 → 2.0.7 → 2.1.1**，跳过已完成的版本。每一轮先服务端、再全部客户端，验证身份和健康后进入下一轮。四个平台的官方压缩包 SHA-256 固定在源码中。
- 每轮服务端升级前保留并检查私有 Raft 快照；配置和原二进制也保留在管理目录。不会尝试直接降级 Nomad。
- 先预配各组件的独立 ACL 凭据和证书，再切换 RPC TLS，最后启用服务端 ACL/严格 TLS及全部客户端的最终配置。临时 `rpc_upgrade_mode` 必须关闭才能通过最终验证。
- 迁移应用权限时，运行作业必须与 Control 保存的部署制品相符。名称和 `luma.managed` 标签都不是信任依据。宿主挂载在实际节点核对真实路径；数据源、只读属性和存储归属保持原值。
- Linux bind 挂载校验宿主真实路径；macOS Docker 在虚拟机内运行，自动路径只接受无驱动选项的本地命名卷。无法证明真实来源的 macOS bind 挂载会明确阻断，不能用 Mac 上的路径检查冒充虚拟机内的校验。
- 普通节点默认只允许 Docker 驱动，关闭 privileged 与任意宿主卷。已有合法存储节点获得受控存储配置。以后首次部署持久化应用时，正常部署流程会验证唯一存储归属节点并自动配置卷权限。
- Control 的可信渲染流程会为普通应用记录权限范围，因此修改镜像、命令或环境变量不需要另行手工审核。提权、设备、危险命名空间、根目录、容器引擎套接字及宿主凭据路径不会因应用名称或旧授权而获得许可。系统引导任务保留完整作业授权。
- 业务应用保持原部署工作流。Traefik 和可选 Observe 的 Nomad 读取组件会更新凭据挂载并检查对应版本的 allocation；Observe 还要通过实际读取 Nomad 的指标检查。

迁移正在应用配置时，Control 暂停新的作业变更，避免部署与权限清单交叉。自定义额外 HCL/JSON 片段、外部 ACL/CA、非默认命名空间等情况会明确阻断；程序不会静默覆盖这些配置。

## 凭据、网络与验收

| 内容 | 位置与边界 |
| --- | --- |
| Nomad 管理 token、CA 私钥、升级日志状态与备份 | `/etc/luma/nomad`，仅宿主管理员可读，不在 Control 的 `/opt/luma` 挂载树内 |
| Control / Traefik / exporter / logs 凭据 | `/opt/luma/security/nomad/<role>.d`；消费者只读挂载自己的目录，Job JSON 不含 token 或私钥值 |
| 节点证书和只读 token | 节点本地 `/etc/luma/nomad`；经已认证的 Luma 通道安装，证书续期任务只传 CSR 和公开证书 |
| 公共迁移进度 | `/opt/luma/security/nomad/upgrade-status.json`，由管理权限 API 和系统更新页面展示 |
| 请求与 Nomad 变更审计 | `/opt/luma/control/security-audit.jsonl`；记录实际 socket peer、凭据指纹、请求标识和作业摘要，不记录秘密请求体 |

服务凭据不会在 24 小时后自动过期。显式轮换采用先安装新凭据、验证消费者、再撤销旧 accessor 的流程。证书有效期一年，宿主服务提前 30 天续期；离线节点回归后经自己的 Agent 通道恢复，即使旧 Nomad 证书已过期也不回退明文。

新版 Agent 会通过正常 DNS 和受验证的 HTTPS 自动移除旧 `insecure`/`resolveIp` 设置。验证失败时停止领取任务并提示修复证书或 DNS；旧 Agent 不会收到新 Nomad 读取凭据。Control 和 Nomad 请求均不跟随重定向转发认证信息。

Nomad HTTP 仅监听 loopback 和本机 Tailscale 地址；RPC/Serf 使用 Tailscale 地址。Linux 使用独立 nftables 表，在 Docker/CNI 的 NAT/accept 前检查，覆盖 IPv4、IPv6 和转发路径。macOS 使用 PF anchor 并重新检测 Tailscale 接口。启动前必须成功应用规则。共享 tailnet 网段不是 HTTP 或 RPC 的全量白名单。

最终验证包括运行版本、原节点身份、匿名读取作业列表、详情和历史版本的 ACL 拒绝、角色权限、HTTP/RPC TLS、客户端证书验证、关闭 TLS 过渡模式、严格节点引入、实际驱动配置、卷权限、监听地址、配置摘要与已应用网络规则。作业详情和历史版本可能包含业务 Env，因此必须单独验证拒绝；缺少凭据或验证失败均不能退回匿名请求。

Control 仍是受信任的基础设施管理组件：它保留既有 Docker 管理能力。Nomad Community 的 submit 权限本身也不是通用的沙箱或准入控制。不得把 Control 管理 token、宿主 root 权限或 Nomad 管理凭据交给不受信任的应用。

仅回滚作业不能恢复缺少当前凭据挂载或存储节点限制的旧版本。需要回到旧镜像时，通过正常 Luma 部署流程重新部署，以保留现有防护。

## 中断与回滚

先查看系统更新页面的失败步骤，及宿主上的 `journalctl -u luma-nomad-security`。私有 `/etc/luma/nomad/upgrade.json` 保存执行位置；不要把这个目录、Raft 快照或完整作业制品贴到公共工单。

Control 暂时不可达或节点离线会在原步骤等待。Agent 任务因进程中断或超时丢失时，使用相同请求有限重试；宿主私有操作记录可恢复二进制/配置已替换但服务尚未重启的阶段。消费者更新先持久化旧版本和目标版本，再提交并核对实际版本，重复恢复不会反复重启已更新的消费者。

下载先使用已配置的代理，失败后尝试直连；仍失败则保留原二进制并明确报错。下载、配置、身份或权限检查失败后，修复原因并重新运行 `luma update manager`，继续原迁移记录。不要删除 ACL 引导文件、创建 bootstrap reset 文件、重新生成 CA、关闭 ACL 或 TLS来恢复访问。

Nomad 不支持原地二进制降级。确需恢复旧集群时，在独立、可信的宿主和隔离网络中按对应版本恢复已经验证的 Raft 快照、应用数据、配置与凭据，再检查作业、节点和路由后切换。Raft 快照不包含应用卷数据，配置备份也不等于业务备份。

已确认获得过宿主 root 的机器需要从可信系统重建，并轮换曾暴露的凭据。软件升级能部署防护，不能证明旧宿主已清除入侵。

本次修复不改变业务秘密在 Control 数据库和运行时 Env 中的存储方式，也不会让已暴露的旧秘密失效。磁盘加密和工作负载秘密分发需要独立设计；解密能力仍由同一宿主 root 掌握时，单独加密数据库不能消除宿主失陷的风险。

参考：[Nomad 升级说明](https://developer.hashicorp.com/nomad/docs/upgrade/upgrade-specific)、[Nomad 升级流程与降级限制](https://developer.hashicorp.com/nomad/docs/upgrade)、[Nomad 官方发行包](https://releases.hashicorp.com/nomad/)。
