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
