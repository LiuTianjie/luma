# 控制面存储与恢复 {#control-storage-and-recovery}

Luma Control 在单个 Manager 使用本地 SQLite，作为 Control 状态及索引化运维记录的权威存储。目录必须位于支持 SQLite 锁和 WAL 的本地文件系统，不支持 NFS，不提供多活动 Manager。

新的 `luma bootstrap manager` 自动直接初始化 SQLite，无中间 `control.json`、独立数据库服务、连接字符串或迁移命令。使用 Python 标准库 `sqlite3`；CLI 安装器创建虚拟环境，Control 镜像使用 Python 基础镜像。

默认数据库为 `/opt/luma/control/control.sqlite3`。可在 Control 进程环境设置 `LUMA_CONTROL_STATE_DIR` 更改目录。`control.sqlite3-wal`、`control.sqlite3-shm` 是 SQLite 运行时辅助文件，不能独立备份。

## 本地维护命令 {#local-maintenance-commands}

在 Manager 使用与 Control 相同的已安装版本和状态目录执行。命令操作本地文件，无需远程管理令牌：

```bash
python -m luma.control.maintenance status
python -m luma.control.maintenance check
python -m luma.control.maintenance backup /secure-backups/control-2026-09-05.tar.gz
python -m luma.control.maintenance restore /secure-backups/control-2026-09-05.tar.gz --destination /srv/luma-control-recovery
```

备份目标必须是状态目录之外的新路径。恢复目标必须不存在，不能覆盖活动 Control。`status` 报告完整性、schema 和行数；`check` 检查活动数据库，`check --database PATH` 检查指定副本。同时检查退出状态和完整性结果。成功归档会写最近备份回执供存储清单使用，但其时间不证明已复制到 Manager 之外或完成恢复演练。归档及验证元数据应保持私有。

## 数据增长与保留含义 {#what-grows-and-what-retention-means}

最近 N 次构建列表是查询结果，不是存储策略。构建元数据、事件文本、源码归档、镜像和应用数据生命周期不同，需分别管理。

| 数据 | 位置与生命周期 |
| --- | --- |
| Control 配置及运维记录 | Manager SQLite；查询分页不删除数据。 |
| 资源历史 | 状态目录下独立有界 `metrics-history.json`，API 报告可用区间与保留期。 |
| 生成的 Nomad job 和 Traefik 路由 | 按应用/服务名保存于 Manager，更新应用替换其当前生成文件。 |
| Builder 源码快照与分析/构建证据 | 内容寻址存储，通常 `/var/lib/luma/builder/snapshots`，相同内容可共享 digest。 |
| Builder 临时检出和凭据 | 任务工作目录，正常结束清理，崩溃可能遗留。 |
| BuildKit/Trivy 缓存 | Builder 本地缓存，独立于 Control 历史和 Registry blob。 |
| Registry 镜像 | 仓库存储，按自身策略执行保护删除和垃圾回收。 |
| 应用卷与数据库 | 应用存储，Control 历史保留或数据库备份均不会备份它们。 |

删除构建记录不会自动删除镜像、源码归档或应用卷。反之，镜像被删后，即使历史仍可搜索，也可能无法重新部署旧构建。

## 历史查询与经审查的保留策略 {#history-queries-and-reviewed-retention}

控制台历史页和 `luma service history` 以游标分页查询构建/部署尝试，列表返回摘要，详情与步骤单独读取。`luma history NAME` 仍读取用于回滚的 Nomad job 版本。重试创建新的 attempt ID，通过 `retryOf`/`retryRootId` 关联，保留失败尝试与事件。旧全局 100 构建、200 部署、300 构建事件限制不再管理这些记录。Agent 进度与 LAE Builder 任务仍独立保留。

即使关闭控制台，Manager 清单快照也约每小时刷新，页面显示采样时间与覆盖范围。

在存储治理页查看实测清单，调整历史策略后创建预览。默认摘要 90 天、详细事件 14 天、审查宽限 24 小时。修改策略不会自动删除。清理计划仅保存标识和指纹，不备份被删载荷；需要恢复时应另有已验证备份。

预览最多记录 1000 个候选和估计序列化字节数。结果标记 `truncated` 时，应用已审查批次后再次预览。宽限期结束后需显式应用该计划。删除前重新检查终态、重试/当前部署引用及指纹；活动和被引用记录受保护。计划达到可执行时间后 7 天过期。策略变更或计划过期需新预览；预览后改变的记录跳过并报告。按年龄可能仅删旧详细事件，或连同摘要一起删除。

相同计划也涵盖超过 `summaryDays`（默认 90 天）的 resolved/closed 告警及最终发送记录。活动事件和未完成发送受保护，应用结果单独报告告警历史清理。不是自动后台删除。

以下 API 需要管理令牌：

| 端点 | 操作 |
| --- | --- |
| `GET /v1/governance/inventory` | Manager 实测文件、记录数、未知外部存储和最近计划。 |
| `GET /v1/governance/policy` | 读取保留策略。 |
| `POST /v1/governance/policy` | 设置整数 `summaryDays`、`detailDays`、`graceHours`。 |
| `POST /v1/governance/history/preview` | 持久化可审查清理计划。 |
| `GET /v1/governance/history/plans/PLAN_ID` | 重读候选、日期与结果。 |
| `POST /v1/governance/history/apply` | 宽限后应用 `{ "planId": "PLAN_ID", "confirmed": true }`。 |

载荷回收估计不保证实际磁盘缩小。SQLite 通常复用空闲页，删除后文件大小可能不变，不会自动对活动数据库执行 VACUUM。

## Builder 与 Registry 容量 {#builder-and-registry-capacity}

Builder 清单和清理通过所选节点 agent 执行。`POST /v1/governance/builder` 接受 `node`、`operation`，必要时附 `planId` 与 `confirmed: true`；通过 `GET /v1/governance/builder/TASK_ID` 轮询。支持 `inventory`、`preview`、`quarantine`、`restore`、`purge`，控制台展示同样的流程与可执行时间。

范围仅为配置的内容寻址快照存储，包括源码归档、分析产物和构建/外部镜像证据。Manager 不猜测远端磁盘使用；Builder、Registry、BuildKit、Trivy 和应用卷在对应拥有者实测前保持未知。

Builder 清理需要新鲜完整的 Manager 引用清单，且无活动构建任务。所有已知引用受保护，仅旧且无引用内容可候选。显式预览记录文件身份；至少 24 小时后显式隔离未变化候选到可恢复区，再等 24 小时并刷新引用检查后才能最终清除。恢复不覆盖已有文件。计划返回 7 天过期时间与精确可执行时刻。

计划元数据 `.governance.sqlite3` 和隔离区 `.trash` 位于 Builder 快照根目录，属于远端 Builder 数据，不包含在 Manager Control 备份中。

隔离只在同一文件系统移动字节，不释放空间，最终清除才释放。符号链接、未知路径、内容变化、引用过期或活动任务会阻止清理，不扩大删除范围。不清理临时工作区、BuildKit/Trivy 缓存、Registry blob、密钥或应用卷。

Registry 有独立保留和保护删除/GC 流程。默认 `recommend`，保留最近 20 镜像，年龄策略 30 天，恢复窗口 7 天。自动删除需显式 `enforce`；修改 Control 历史策略不会启用它。Manifest 删除和物理 blob GC 分开，保留镜像可能仍使用共享层。

## 已有安装：旧状态导入 {#existing-installations-legacy-state-import}

仅升级已有 JSON 安装使用该兼容流程，新 Manager 不需要，也无需手动迁移命令。首次升级使用 `luma update manager`，预留短维护窗口。读取配置和预拉镜像不会初始化 SQLite。导入前安装器仅停止 `luma-control` job，确认没有 allocation 继续写 JSON，并在状态目录旁保存私有 `control-pre-sqlite-*` 检查点；无法确认停止则中止。首次数据库初始化导入可用 `control.json`，保留 `control.json.pre-sqlite.bak`，在 `control-sqlite-migration.json` 记录校验和与计数。之后数据库是唯一依据，不要编辑旧 JSON，也不要新旧写入版本共用状态目录。

首次迁移后的 Control job 禁用自动回滚到旧 JSON 镜像。滚动失败保留待切换标记，让重试仍受保护。后续普通 SQLite 更新沿用自动回滚。升级前不要直接对活动旧目录执行新数据库/备份命令，它们可能在未隔离旧进程时初始化数据库。

旧 JSON 镜像无法读取迁移数据库。回退需停止新 Control，恢复最终旧检查点、匹配配置和旧 jobspec，另存迁移后 SQLite 目录。这把 Control 状态回到检查点时刻，不撤销之后的应用侧操作。见[发布切换与回滚流程](release.md#first-json-to-sqlite-upgrade)。

导入只保留旧文件中仍存在的记录，无法恢复先前数量限制已删除的构建、部署或指标。已有数据库不会反复被 JSON 覆盖。`control-sqlite-authority.json` 记录数据库身份；切换后数据库缺失、截断或不匹配会停止，即使 JSON 仍存在。应恢复验证备份；删除数据库不会让 Manager 回退 JSON。

## 备份范围 {#backup-scope}

一致性 SQLite 备份必须包含 WAL 中尚未合并的事务。使用支持的备份操作；只复制活动数据库可能漏掉近期提交。不要拼接、编辑或恢复其他快照的 WAL/SHM。

数据库含凭据和私有配置。备份须保持私有，传输或存到 Manager 外时应加密。命令只需报告路径、数量和完整性，不必输出令牌或密钥验证。

Control 归档包含一致性数据库快照及状态目录支持的文件，包括私有 token 和 fleet/prepare/Registry 恢复产物；排除活动 WAL/SHM、锁和临时文件，保留旧导入证据。归档时暂停配置/token 轮换：数据库快照是事务性的，外部文件分别复制。目录外配置文件不会自动包括。

完整 Manager 恢复集还需要归档外的文件：

- Control 服务环境及状态目录外的密钥，例如 LAE principal/签名文件或自定义 metrics token。SQLite 中的渠道 App Secret 已包含，内存 tenant access token 不持久化，重启后重取。
- Manager Luma 配置、生成 job/路由、Traefik 配置和 ACME 证书状态，以及 Git 中期望清单。
- 适合该集群的 Nomad 恢复材料，以及应用卷、数据库、所需 Registry/Builder 数据的独立备份。

每份恢复集应附带日期的路径和权限清单。数据库备份成功仅证明数据库一致性，不证明其他恢复项齐全。

## 恢复或迁移 Manager {#restoring-or-moving-the-manager}

1. 切换权威 Manager 前停止旧 Control 写入，保留数据库与配置为回滚检查点。
2. 验证备份完整性，用 `restore` 恢复到目标 Manager 本地存储上的新且不存在目录；让目标 Control 指向它，按原权限恢复外部配置和私有文件。
3. 启动兼容 Control，重定向客户端/agent 前检查认证、集群/节点身份、保留历史和数据库健康。
4. 检查 Nomad leader/连通性、agent 心跳、入口路由及真实应用端点；数据库完整不代表运行时可用。
5. 目标验证通过前保留原检查点，不要把旧 Manager 作为同集群第二个写入者启动。

生产依赖此流程前，先用隔离恢复副本测试。本文描述实现与操作手册，不声称已完成真实备份、迁移或恢复演练。