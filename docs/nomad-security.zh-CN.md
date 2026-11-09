# Nomad 安全默认值与自动升级 {#nomad-security-defaults-and-automatic-upgrades}

首次执行 `luma bootstrap` 和 `luma node join` 就会配置 ACL、双向 TLS、节点引入凭据和持久网络规则。安装程序验证实际运行配置后才报告节点成功。用户不需要另外执行一组 `luma security` 命令。

本版同时加密 Control 管理的数据，并通过 Nomad 加密 Variables 和 allocation 身份分发应用密钥。新安装在 bootstrap 时启用；现有安装在控制面和全部节点 Agent 升级后自动迁移。包版本更新不等于迁移完成，需要等待系统更新页显示全部检查通过。

已有集群按正常方式升级即可：

1. 在管理节点执行 `luma update manager`，或使用 Dashboard 的「升级 Control」。
2. 通过 `luma update fleet`、Dashboard 节点更新，或各节点上的 `luma update` 更新全部节点 Agent。
3. 在系统更新页面查看「集群安全升级」。只有「全部验证通过」表示迁移完成；软件版本对齐和安全验证分别显示。

管理节点安装独立的 `luma-nomad-security.service`。它等待已注册节点的新版 Agent 就绪，收集配置和身份，检查应用权限与现有存储，再执行持久化的迁移步骤。Control 替换、Nomad 重启和进程中断不会丢失已记录的步骤。离线节点会明确列出；未知作业、身份冲突或不安全配置会阻断并保留原因。修复原因后重新执行普通管理节点更新即可重新检查、继续迁移。

## 升级过程中会发生什么 {#upgrade-behavior}

- 自动路径覆盖 Luma 管理的单服务端集群：Linux 管理节点、Linux/macOS 客户端、Nomad `global` 区域、`dc1` 数据中心、`/opt/nomad/data` 数据目录。
- 从 1.9.x 起逐个次版本升级：**1.10.5 → 1.11.3 → 2.0.7 → 2.1.1**，跳过已完成的版本。每一轮先服务端、再全部客户端，验证身份和健康后进入下一轮。四个平台的官方压缩包 SHA-256 固定在源码中。
- 每轮服务端升级前保留并检查私有 Raft 快照；配置和原二进制也保留在管理目录。不会尝试直接降级 Nomad。
- 每次修改节点前检查集群已登记服务；修改后要求副本数和任务连续健康 30 秒，有 Nomad HTTP/TCP 探针的服务还要通过最新探针检查。节点 ready 本身不能推进迁移。副本丢失、任务反复退出或探针失败会阻止后续节点变更，等待十分钟仍未恢复则明确报错并列出服务。已完成的宿主操作单独持久化，等待或恢复检查不会重复下载、重启；最终完成前也要通过业务健康检查。未配置就绪探针的服务仍需业务自身的可用性验证。
- 先预配各组件的独立 ACL 凭据和证书，再切换 RPC TLS，最后启用服务端 ACL/严格 TLS及全部客户端的最终配置。临时 `rpc_upgrade_mode` 必须关闭才能通过最终验证。
- 迁移应用权限时，运行作业必须与 Control 保存的部署制品相符。名称和 `luma.managed` 标签都不是信任依据。宿主挂载在实际节点核对真实路径；数据源、只读属性和存储归属保持原值。
- Linux bind 挂载校验宿主真实路径；macOS Docker 在虚拟机内运行，自动路径只接受无驱动选项的本地命名卷。无法证明真实来源的 macOS bind 挂载会明确阻断，不能用 Mac 上的路径检查冒充虚拟机内的校验。
- 普通节点默认只允许 Docker 驱动，关闭 privileged 与任意宿主卷。已有合法存储节点获得受控存储配置。以后首次部署持久化应用时，正常部署流程会验证唯一存储归属节点并自动配置卷权限。
- Control 的可信渲染流程会为普通应用记录权限范围，因此修改镜像、命令或环境变量不需要另行手工审核。提权、设备、危险命名空间、根目录、容器引擎套接字及宿主凭据路径不会因应用名称或旧授权而获得许可。系统引导任务保留完整作业授权。
- 业务应用保持原部署工作流。Traefik 和可选 Observe 的 Nomad 读取组件会更新凭据挂载并检查对应版本的 allocation；Observe 还要通过实际读取 Nomad 的指标检查。

迁移正在应用配置时，Control 暂停新的作业变更，避免部署与权限清单交叉。自定义额外 HCL/JSON 片段、外部 ACL/CA、非默认命名空间等情况会明确阻断；程序不会静默覆盖这些配置。

## 凭据、网络与验收 {#boundaries-and-verification}

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

## 中断与回滚 {#recovery-and-rollback}

先查看系统更新页面的失败步骤，及宿主上的 `journalctl -u luma-nomad-security`。私有 `/etc/luma/nomad/upgrade.json` 保存执行位置；不要把这个目录、Raft 快照或完整作业制品贴到公共工单。

Control 暂时不可达或节点离线会在原步骤等待。Agent 任务因进程中断或超时丢失时，使用相同请求有限重试；宿主私有操作记录可恢复二进制/配置已替换但服务尚未重启的阶段。消费者更新先持久化旧版本和目标版本，再提交并核对实际版本，重复恢复不会反复重启已更新的消费者。

最多三台支持下载缓存的客户端并行准备后续版本包，安装仍保持服务端先升、客户端逐台升。准备阶段复用已认证的 preflight 通道，要求 Agent 声明下载缓存能力；旧 Agent 继续使用串行路径，不需要为了新增参数先替换 Control。预下载不会重启 Nomad、修改配置或恢复尚未完成的宿主操作。版本包保存在 root 管理的缓存目录，每次使用都重新核对固定的官方 SHA-256；中断或未校验的文件不会成为缓存。

下载先使用已配置的代理，连接失败或文件校验失败后尝试直连；两条线路都必须通过官方校验，否则保留原二进制并明确报错。下载、配置、身份或权限检查失败后，修复原因并重新运行 `luma update manager`，继续原迁移记录。不要删除 ACL 引导文件、创建 bootstrap reset 文件、重新生成 CA、关闭 ACL 或 TLS来恢复访问。

Nomad 不支持原地二进制降级。确需恢复旧集群时，在独立、可信的宿主和隔离网络中按对应版本恢复已经验证的 Raft 快照、应用数据、配置与凭据，再检查作业、节点和路由后切换。Raft 快照不包含应用卷数据，配置备份也不等于业务备份。

已确认获得过宿主 root 的机器需要从可信系统重建，并轮换曾暴露的凭据。软件升级能部署防护，不能证明旧宿主已清除入侵。

这些防护不会让已经暴露的凭据失效。确认迁移完成、接收新密钥的宿主可信后，仍需轮换旧凭据。

## 密钥加密与应用身份 {#encrypted-storage-and-application-identities}

Control SQLite schema 2 使用 AES-256-GCM 加密配置、部署记录、队列请求和历史 payload，告警与回调凭据也使用同一套认证加密。密文绑定具体记录；缺少密钥或校验失败时停止读写，不回退明文。存量迁移先原子提交密文，再 checkpoint、VACUUM，清理旧 SQLite 页和 WAL。普通配置与历史接口隐藏已保存的值；编辑同一个应用配置时，保留的 `<redacted>` 字段会继续使用原值。

恢复键保存在 `/etc/luma/secrets/control-data.key`，位于 Control 的 `/opt/luma` 挂载和应用卷之外。Control 通过受验证的 TLS 和自己的 Nomad 角色，从 `luma/control/storage-key` 获取运行所需的键。`/opt/luma/security/nomad/secrets.json` 只保存键标识，不包含键本身。恢复键和该 profile 应独立私密备份；仅复制加密数据库不足以完成灾难恢复。Control 仍是能够管理和解密这些凭据的受信任组件。

每个任务只从自身 namespace 下的 `nomad/jobs/<job>/<group>/<task>` 取得渲染给它的值。Nomad 内置工作负载身份授权原生 secret hook，Luma 不创建跨任务的 job/group 共享秘密路径。作业 Env、镜像仓库认证和业务启动参数保存引用；原生 hook 在内存解析。业务容器不会取得 Control 恢复键或通用 Nomad 读取令牌。Nomad 以干净环境启动，避免把宿主环境变量带给无关应用；已有未知工作负载 ACL 授权会阻止自动迁移并给出诊断。

应用运行时需要在自身内存、环境中使用明文，宿主 root 或 Docker 管理权限仍能读取。普通 Nomad 作业读取身份无权读取 Variables；可读作业历史和 allocation 保存引用，allocation 接口不返回签名身份。密钥在原生 env 解析前做 JSON 引号编码，保留引号、换行、Unicode 和字面量插值字符。

迁移依次替换应用，逐一等待准确版本健康；单副本应用可能有短暂重启窗口。宿主服务加密归档原状态、受管旧制品和备份，移除旧版本标签及明文作业历史，再回收退休 allocation。迁移期间暂时只保留一个版本，清理后恢复保留六个受保护版本，支持正常回滚；旧作业源文件也用空 source 替换。历史 Raft 日志、文件系统快照和 Luma 管理目录外的备份不会被追溯擦除，已经暴露的旧凭据仍需轮换。

回滚前校验被引用的密钥版本仍然存在，避免先替换健康作业再启动失败。schema 1 的旧 Control 无法打开 schema 2 数据库；自动回退到迁移前 Control 镜像或明文作业被禁用。节点离线、应用启动失败或旧 allocation 未清理完都会保持迁移未完成。

在可信隔离宿主恢复受管 `.enc` 备份时，先恢复私有键和 profile，再使用已安装 Luma 的解释器执行：`sudo /opt/luma-cli/bin/python -m luma.secret_storage decrypt-backup SOURCE.enc NEW_DESTINATION`。校验成功后才创建目标文件，禁止覆盖已有文件。恢复出的 SQLite 备份可能仍有加密行，需要继续保留对应恢复键。

节点引入令牌有效期十分钟。默认 allocation 身份保留在 Nomad 内部，不将其宣传为短期业务凭据；第三方密钥仍使用服务商规定的有效期。本版不会替用户轮换服务商账号，也不会自动把所有长期凭据转换成动态凭据。

参考：[Nomad 升级说明](https://developer.hashicorp.com/nomad/docs/upgrade/upgrade-specific)、[Nomad 升级流程与降级限制](https://developer.hashicorp.com/nomad/docs/upgrade)、[Nomad 官方发行包](https://releases.hashicorp.com/nomad/)。
