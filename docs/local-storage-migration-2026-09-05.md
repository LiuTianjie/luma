# 本地存储迁移盘点（2026-09-05）

这是迁移前的只读盘点，不是线上迁移完成记录。依据 Control 保存配置、Nomad 当前任务与分配、manager/aly/builder 的 Docker volume 和内核挂载核对。未修改线上配置、停止服务或删除数据。

| 应用 | 应用节点 | 实际存储 | 后续动作 |
| --- | --- | --- | --- |
| vibecheck | manager | manager 自身导出的 NFS，`/srv/luma-lae-runtime/vibecheck/{pg-data,scan-data}` | 备份并停止写入后直挂原目录；PostgreSQL 目录必须保留 UID/GID 70:70、0700 权限 |
| word2pdf | manager | aly 的 NFS，`/srv/luma/word2pdf/{results,redis}` | 检查容量、备份，停止所有生产者/消费者及 Redis 后一致复制到 manager，再切换本地目录 |
| 旧 LAE 应用 `lae-574ac370fe00786a1745d0143646` | tecent，当前无 allocation | builder 上的租户 pg-data/app-data NFS | 核查旧数据和持久化 volumeRef，再明确迁移路径与节点归属；无运行实例不代表数据可删除 |
| luma-registry | builder | 实际是本地 Docker 卷 `luma-registry-data`；保存配置仍引用 NFS class | 去除多余的 class 引用，保持卷名及节点不变 |
| agent-pool | manager | `/srv/agent-pool/postgres` 本地 bind | 保留 |
| lae-platform | manager | `/srv/luma/lae/production` 下本地 bind | 保留 |
| codex-gitea、kato | lab | 本地命名卷 | 保留原卷名和节点 |
| luxe-monitor | tecent | 本地命名卷 | 保留原卷名和节点 |
| granary | blg，离线 | 原本地 MySQL 命名卷 | 恢复节点后核查；禁止在其他节点初始化空库 |

manager 当时的 4 个活跃 NFS 挂载属于 vibecheck 和 word2pdf。历史 Docker NFS volume 对象仍存在，不能仅按是否运行判断可删。现存类包括 `blg-registry-nfs`、`builder-registry-nfs`、`cn-nfs`、`lae-runtime-manager`；全部依赖核清前保留。

上线顺序：

1. 发布并验证 Control 与节点 Agent 新版，确保已有目录权限保留逻辑生效。
2. 将 LAE 新应用默认环境配置改为 `LUMA_LAE_RUNTIME_STORAGE_CLASS=local`；既有 volumeRef 继续保留原后端。
3. 分别保存当前 job/config、备份与健康基线，再执行获准停机的单应用切换。不要同时切两个应用。
4. 移除旧的 `initialize: empty`，在校验数据后使用 `adopted: true`。核对实际运行 mount、数据库记录和业务功能。
5. 核清旧 LAE、离线节点与历史卷依赖后，才能移除 class 或停止 NFS 服务。源数据保留用于回滚；恢复旧 job 前必须考虑切换后新增写入。

新应用的存储策略与迁移步骤见 [compose-storage.md](compose-storage.md)。上述实际路径是迁移源，不能未经检查替换成默认的新空路径。

## 执行结果（同日约 23:20 CST）

用户授权逐个停机迁移，并明确清理无用的旧 LAE 应用。执行前重新核对发现 vibecheck 已由其他更新切到本地 bind（Nomad v32），因此本次未重启或修改它。

- Control 已上线 `0.1.305`，源码为 `9e5730b2575be37e3557e11f1f507b5f589f3d0d`；内部镜像为 `100.66.177.70:5000/luma-control:sha-9e5730b`，已核对镜像摘要和公网健康。manager、aly、builder、lab、tecent 的 Agent 均回报 `0.1.305`。
- LAE 新应用默认配置已设置 `LUMA_LAE_RUNTIME_STORAGE_CLASS=local`。
- word2pdf：保存 v13 job 和 Control 配置；Redis SAVE 成功后停止整个应用，确认旧 allocation 完成。将 aly 的 results/redis 打包复制到 manager 的 `/srv/luma/data/word2pdf/{results,redis}`，核对 SHA-256、tar 内容比较和 numeric ownership。重新部署为 Nomad v15，本地 bind 已从实际 Redis 容器确认。迁移前后 Redis 均为 0 个键；原目录及备份保留。公开 `/healthz` 的全部依赖正常，上传最小 DOCX 的公开转换测试返回 HTTP 200 和有效 PDF（9987 bytes）。此测试不等同于复杂文档版式验收。
- word2pdf 工作区的 `luma.compose.yml` 同步为本地目录和 `adopted: true`，去掉 `initialize: empty`；保留该工作区原有未提交修改，未替用户提交其他文件。
- luma-registry 原本即使用 builder 本地命名卷；仅修正保存配置的多余 NFS class 引用，保持 `luma-registry-data` 和运行任务不变。
- 旧 LAE 应用 `app_01KXEVCCVT2Q017SC3XR739EBM` 在当前平台数据库中已不存在。已清理其孤立 Nomad job、两个专属 DNS、生成文件、两个 volumeRef 及 hostname 绑定，并将 runtime 记录标记 deleted。builder 专属目录只有空卷目录（16K），归档后删除；tecent 的专属 NFS Docker volume 对象已移除。其他历史应用的归档数据未删除。
- `lae-runtime-manager`、`cn-nfs`、`builder-registry-nfs`、`blg-registry-nfs` 四个类已从 Control 移除。在线 manager、aly、builder、tecent 的 NFS 服务均为 inactive/disabled，exports 为空；lab 本来未运行 NFS。最终当前 Nomad job 的 NFS mount 数量为 0。

备份：manager `/opt/luma/backups/local-storage-20260905/` 保存升级前 Control SQLite 快照、配置、job 和 word2pdf 数据归档；各处理节点 `/opt/luma/backups/nfs-retirement-20260905/` 保留 exports 配置。word2pdf 的原 aly 数据仍在。旧 LAE 的目录归档位于 builder `/srv/luma-retired-backups/lae-unused-20260905.tar.gz`，不再导出。

边界：`blg` 节点离线且 SSH 连接关闭，无法停用它本机可能残留的 NFS 服务；其存储声明已退役，未移动其本地数据库数据。历史未运行的 code-server/旧 LAE 配置与旧 volumeRef 仍可含已退役或早已不存在的 class 名称；它们不是当前运行中的 NFS 挂载，恢复时需要先核对原数据并明确迁移，不能直接初始化新卷。NFS 兼容解析代码保留用于识别这些旧配置。
