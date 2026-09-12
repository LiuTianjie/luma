# Compose 部署与本地存储 {#compose-deployments-and-local-storage}

持久化数据属于运行应用的节点。新的控制台模板使用本地存储，无需 NFS 服务器或存储类。Control 在部署区域中选择一个就绪节点（或使用指定节点），在提交任务前记录数据归属，后续部署固定到该节点。归属节点不可用时会阻止调度，而不会在其他节点启动一个空数据库。

一个 Compose stack 中的服务一起运行在同一节点。只读配置挂载和 Docker socket 不产生持久化数据归属。可写本地挂载需要稳定的节点位置。带持久化挂载的原生服务要求 `replicas: 1`；持久化工作负载不使用可能让两个写入进程同时操作同一目录的金丝雀晋级。

## Compose 示例 {#compose-example}

保持应用文件为标准格式：

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pg-data:/var/lib/postgresql/data
volumes:
  pg-data: {}
```

在 Luma 旁车清单中使用本地路径：

```yaml
# luma.compose.yml
name: app-stack
compose: docker-compose.yml
region: cn
services:
  postgres:
    exposure: none
volumes:
  pg-data:
    local:
      path: /srv/luma/data/app-stack/pg-data
```

通过 Control 部署时，`local.node` 可选。路径跟随部署节点，不单独选择存储节点。要显式选择首次部署节点，设置 `services.postgres.node`。互相冲突的节点绑定会被拒绝。Control 将解析后的节点绑定和本地卷节点保存到部署配置。已有目录保留所有权和权限，包括 PostgreSQL 数据目录的严格权限。

控制台为新的 Compose 卷生成 `/srv/luma/data/<deployment-slug>/<volume-name>` 路径。不同数据集应使用不同路径。也支持直接使用 Docker 命名卷，部署会固定到其归属节点；更新时保留现有卷名。

## 原生服务示例 {#native-service-example}

```yaml
name: app-postgres
image: postgres:17
region: cn
exposure: none
replicas: 1
volumes:
  - app-postgres-data:/var/lib/postgresql/data
```

Control 记录选中的节点。控制台按部署隔离新原生卷的名称。已有卷名和绑定路径保持不变：在显式迁移数据之前，不允许替换同一挂载目标的数据源。节点重命名或注册信息丢失时，必须先核对已有数据归属，再执行部署。


## 主机网络 {#host-networking}

普通 Compose 组共享 Nomad 桥接网络命名空间，同组服务名解析到 `127.0.0.1`。需要采集主机回环监听端点（Traefik 指标、本地 Nomad HTTP）的 stack，可以为**每个**服务设置 `network_mode: host`。同组混用 host 和 bridge 会被拒绝。使用主机网络的 Compose 要求 `exposure: none`，且不能设置 `publishPort`；它面向 [`luma-observe`](../observe/) 等内部采集器，而非公开应用。

## 部署与更新 {#deploy-and-update}

```bash
luma login https://luma.example.com --token <management-token>
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --format ndjson
```

复用同一部署名会更新已有任务及存储归属。更改部署名会创建另一个部署。Control 预览会解析已保存的数据归属和当前候选节点；部署时还会检查已有 Nomad 任务及实例，以验证旧部署的数据归属。离线执行 `compose render` 必须显式指定本地节点，因为它无法持久化调度决策。

应用设置保留在标准 Compose 文件中。旁车清单承载区域、暴露方式、路由及存储配置。路由字段参见[部署 YAML](deployment-yaml.md)。

## LAE 运行时 {#lae-runtime}

`LUMA_LAE_RUNTIME_STORAGE_CLASS` 未设置或为 `local` 时，LAE 默认使用内部本地存储。宿主机路径根据已认证的租户和应用身份生成，位于 `/srv/luma/data/lae/tenants/` 下；API 调用者不能提供任意宿主机路径。卷绑定会在数据准备和任务提交前记录归属节点 ID。重试和后续发布继续使用该节点。

即使全局默认值改变，已有 LAE 卷引用仍保留记录的存储后端。将环境变量设为 `local` 不会迁移旧 NFS 数据。迁移期间仍支持显式指定此前配置的 NFS 类。

## 迁移现有 NFS 数据 {#migrate-existing-nfs-data}

更换后端不会自动复制数据。先检查实际运行的挂载：旧配置可能与早期渲染器实际挂载的内容不同。记录应用节点、源目录或 Docker 卷、数字用户与组归属、权限、数据大小及当前任务和配置。

1. 在现有应用节点准备目标目录并检查可用空间。
2. 创建可恢复的备份。最终复制前停止所有写入进程。适用时使用数据库原生备份与恢复；复制正在使用的数据库目录无法得到一致备份。
3. 复制时保留数字用户与组归属和权限；如果 NFS 从应用节点本身导出目录，也可复用同一宿主机目录。
4. 验证数据和权限，配置本地目标，并在该卷上设置 `adopted: true`。移除旧的 `initialize: empty` 确认。
5. 重新部署，检查应用与数据库健康状况，并读取有代表性的数据进行验证。保留原始数据及任务和配置以便回滚。发生新写入后，回滚还需处理这些新增数据。

目标配置示例：

```yaml
volumes:
  pg-data:
    local:
      node: manager
      path: /srv/luma/data/app-stack/pg-data
    adopted: true
```

输出手动迁移计划：

```bash
luma storage migrate luma.compose.yml \
  --volume pg-data --from-node old-storage-node --from-volume old-docker-volume
```

该命令不会停止服务或复制数据。本地目标要求显式指定部署节点或本地节点。`adopted: true` 表示数据已验证，不允许将已确定的本地数据归属移到另一个节点。迁移归属节点需要运维人员管理数据迁移，并同步归属记录。

## 旧版 NFS 兼容 {#legacy-nfs-compatibility}

迁移数据期间，已有 `storageClass` 引用仍可使用。注册的类仍由 Manager 管理；提交的旁车清单中若包含非空 `storageClasses`，会被拒绝。`luma storage list`、`check`、`apply` 和 `remove` 继续管理这些旧类。本地存储无需存储类。

检查完所有运行中和已停止的部署，以及 LAE 绑定之前，不要移除存储类、卸载卷或关闭 NFS 服务器。没有运行实例的应用仍可能依赖旧数据。离线节点的数据应保留原位，直到能够检查该节点。

## 移除与恢复 {#removal-and-recovery}

`luma service remove <name>` 删除应用任务和路由，默认保留数据。迁移期间避免使用 `--delete-storage`。删除部署也可能删除已保存的数据归属，因此删除前应保存其固定节点配置，并在恢复应用时使用。

本地存储不提供跨节点故障转移。应恢复原节点，或从经过验证的备份恢复，并显式同步调度位置。备份应独立于应用所在磁盘保存。