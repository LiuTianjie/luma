# Nomad 安全默认值与证书维护 {#nomad-security-defaults-and-automatic-upgrades}

`luma bootstrap` 和 `luma node join` 在首次安装时配置 ACL、双向 TLS、节点引入身份和持久化网络防护。已启用的 Control 数据加密和应用密钥隔离继续生效。

一次性集群安全迁移已退出日常升级流程。`luma update manager`、节点更新和系统更新页面不再触发、等待或展示旧迁移；历史迁移状态也不再阻塞部署。普通升级不会重新执行 Nomad 分阶段升级、应用密钥迁移或历史清理。

`luma-nomad-security.service` 仅负责节点证书签发与续期，沿用原服务名和私有身份记录。它按当前登记的节点维护证书，离线节点恢复后再续期；证书维护失败会记录并重试，不会重开已完成的迁移。

## 凭据、网络与验收 {#boundaries-and-verification}

Manager 更新通过加密存储接口合并 Control 配置，不再扫描全部历史或对正在使用的数据库执行 VACUUM。已启用的存储加密配置继续用于新写入的数据。

| 内容 | 位置与边界 |
| --- | --- |
| Nomad 管理 token、CA 私钥、升级日志状态与备份 | `/etc/luma/nomad`，仅宿主管理员可读，不在 Control 的 `/opt/luma` 挂载树内 |
| Control / Traefik / exporter / logs 凭据 | `/opt/luma/security/nomad/<role>.d`；消费者只读挂载自己的目录，Job JSON 不含 token 或私钥值 |
| 节点证书和只读 token | 节点本地 `/etc/luma/nomad`；经已认证的 Luma 通道安装，证书续期任务只传 CSR 和公开证书 |
| 请求与 Nomad 变更审计 | `/opt/luma/control/security-audit.jsonl`；记录实际 socket peer、凭据指纹、请求标识和作业摘要，不记录秘密请求体 |

服务凭据不会在 24 小时后自动过期。显式轮换采用先安装新凭据、验证消费者、再撤销旧 accessor 的流程。证书有效期一年，宿主服务提前 30 天续期；离线节点回归后经自己的 Agent 通道恢复，即使旧 Nomad 证书已过期也不回退明文。

新版 Agent 会通过正常 DNS 和受验证的 HTTPS 自动移除旧 `insecure`/`resolveIp` 设置。验证失败时停止领取任务并提示修复证书或 DNS；旧 Agent 不会收到新 Nomad 读取凭据。Control 和 Nomad 请求均不跟随重定向转发认证信息。

Nomad HTTP 仅监听 loopback 和本机 Tailscale 地址；RPC/Serf 使用 Tailscale 地址。Linux 使用独立 nftables 表，在 Docker/CNI 的 NAT/accept 前检查，覆盖 IPv4、IPv6 和转发路径。macOS 使用 PF anchor 并重新检测 Tailscale 接口。启动前必须成功应用规则。共享 tailnet 网段不是 HTTP 或 RPC 的全量白名单。

macOS 安装与定时防火墙刷新共用互斥锁，避免同时写入 PF。内核返回 `DIOCADDRULE: Resource busy` 时只做有限重试；其他错误仍立即阻止继续，应用确认只会在规则成功加载后写入。

最终验证包括运行版本、原节点身份、匿名读取作业列表、详情和历史版本的 ACL 拒绝、角色权限、HTTP/RPC TLS、客户端证书验证、关闭 TLS 过渡模式、严格节点引入、实际驱动配置、卷权限、监听地址、配置摘要与已应用网络规则。作业详情和历史版本可能包含业务 Env，因此必须单独验证拒绝；缺少凭据或验证失败均不能退回匿名请求。

Control 仍是受信任的基础设施管理组件：它保留既有 Docker 管理能力。Nomad Community 的 submit 权限本身也不是通用的沙箱或准入控制。不得把 Control 管理 token、宿主 root 权限或 Nomad 管理凭据交给不受信任的应用。

仅回滚作业不能恢复缺少当前凭据挂载或存储节点限制的旧版本。需要回到旧镜像时，通过正常 Luma 部署流程重新部署，以保留现有防护。

## 维护与恢复 {#recovery-and-rollback}

证书维护日志通过 `journalctl -u luma-nomad-security` 查看。私有身份与续期记录继续保留在 `/etc/luma/nomad`。离线节点不会阻塞 Control 更新或其他节点的维护。

缺少凭据或证书验证失败时修复对应配置。不要删除 ACL 引导文件、重新生成 CA 或关闭 ACL/TLS 来恢复访问。

## 密钥加密与应用身份 {#encrypted-storage-and-application-identities}

Control SQLite schema 2 使用 AES-256-GCM 加密配置、部署记录、队列请求和历史 payload，告警与回调凭据也使用同一套认证加密。密文绑定具体记录；缺少密钥或校验失败时停止读写，不回退明文。存量迁移先原子提交密文，再 checkpoint、VACUUM，清理旧 SQLite 页和 WAL。普通配置与历史接口隐藏已保存的值；编辑同一个应用配置时，保留的 `<redacted>` 字段会继续使用原值。

恢复键保存在 `/etc/luma/secrets/control-data.key`，位于 Control 的 `/opt/luma` 挂载和应用卷之外。Control 通过受验证的 TLS 和自己的 Nomad 角色，从 `luma/control/storage-key` 获取运行所需的键。`/opt/luma/security/nomad/secrets.json` 只保存键标识，不包含键本身。恢复键和该 profile 应独立私密备份；仅复制加密数据库不足以完成灾难恢复。Control 仍是能够管理和解密这些凭据的受信任组件。

每个任务只从自身 namespace 下的 `nomad/jobs/<job>/<group>/<task>` 取得渲染给它的值。Nomad 内置工作负载身份授权原生 secret hook，Luma 不创建跨任务的 job/group 共享秘密路径。作业 Env、镜像仓库认证和业务启动参数保存引用；原生 hook 在内存解析。业务容器不会取得 Control 恢复键或通用 Nomad 读取令牌。Nomad 以干净环境启动，避免把宿主环境变量带给无关应用；已有未知工作负载 ACL 授权会阻止自动迁移并给出诊断。

应用运行时需要在自身内存、环境中使用明文，宿主 root 或 Docker 管理权限仍能读取。普通 Nomad 作业读取身份无权读取 Variables；可读作业历史和 allocation 保存引用，allocation 接口不返回签名身份。密钥在原生 env 解析前做 JSON 引号编码，保留引号、换行、Unicode 和字面量插值字符。

迁移依次替换应用，逐一等待准确版本健康；单副本应用可能有短暂重启窗口。宿主服务加密归档原状态、受管旧制品和备份，移除旧版本标签及明文作业历史，再回收退休 allocation。迁移期间暂时只保留一个版本，清理后恢复保留六个受保护版本，支持正常回滚；旧作业源文件也用空 source 替换。历史 Raft 日志、文件系统快照和 Luma 管理目录外的备份不会被追溯擦除，已经暴露的旧凭据仍需轮换。

回滚前校验被引用的密钥版本仍然存在，避免先替换健康作业再启动失败。schema 1 的旧 Control 无法打开 schema 2 数据库；自动回退到迁移前 Control 镜像或明文作业被禁用。

在可信隔离宿主恢复受管 `.enc` 备份时，先恢复私有键和 profile，再使用已安装 Luma 的解释器执行：`sudo /opt/luma-cli/bin/python -m luma.secret_storage decrypt-backup SOURCE.enc NEW_DESTINATION`。校验成功后才创建目标文件，禁止覆盖已有文件。恢复出的 SQLite 备份可能仍有加密行，需要继续保留对应恢复键。

节点引入令牌有效期十分钟。默认 allocation 身份保留在 Nomad 内部，不将其宣传为短期业务凭据；第三方密钥仍使用服务商规定的有效期。本版不会替用户轮换服务商账号，也不会自动把所有长期凭据转换成动态凭据。

参考：[Nomad 升级说明](https://developer.hashicorp.com/nomad/docs/upgrade/upgrade-specific)、[Nomad 升级流程与降级限制](https://developer.hashicorp.com/nomad/docs/upgrade)、[Nomad 官方发行包](https://releases.hashicorp.com/nomad/)。
