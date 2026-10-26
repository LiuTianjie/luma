# Compose 部署与本地存储 {#compose-deployments-and-local-storage}

持久化数据属于运行应用的节点。新的控制台模板使用本地存储，无需 NFS 服务器或存储类。Control 在部署区域中选择一个就绪节点（或使用指定节点），在提交任务前记录数据归属，后续部署固定到该节点。归属节点不可用时会阻止调度，而不会在其他节点启动一个空数据库。

多服务 Compose stack 中的服务一起运行在同一节点，只有一个 allocation。无挂载的单服务 stack 可以在所属 region 内运行多个 allocation。只读配置挂载和 Docker socket 不产生持久化数据归属。可写本地挂载需要稳定的节点位置。带持久化挂载的原生服务要求 `replicas: 1`；持久化工作负载不使用可能让两个写入进程同时操作同一目录的金丝雀晋级。

## 副本数 {#replicas}

Luma 将 Docker Compose 的 `services.<name>.deploy.replicas` 与 sidecar 的
`services.<name>.replicas` 解析为 Nomad group `Count`。两处任选其一；同时声明时
必须一致。Compose `scale` 同样受支持，并且必须与其他副本声明一致。默认值为 `1`。
副本数必须为正整数；零、布尔值、浮点数、null 和冲突值会在校验阶段报错，导入校验
也执行相同规则。`deploy.mode` 只支持 `replicated`。

多个副本要求恰好一个服务、没有 Compose volume 挂载，也没有 sidecar volume 声明。
只读、匿名及历史 NFS 挂载也在限制范围内，目前尚不支持这些挂载的多副本语义。
多服务 stack 中每个服务必须为一个副本，即使所有服务声明的副本数相同也不能扩容。
同组服务共享 loopback；复制整个组会同时复制数据库和调度器。需要独立扩容的 worker
应拆为独立部署，通过可跨节点访问的地址连接依赖。

```yaml
# docker-compose.yml
services:
  worker:
    image: registry.example.com/worker:1
    deploy:
      replicas: 6
      resources:
        reservations: {memory: 2048M}
        limits: {memory: 2048M}
```

```yaml
# luma.compose.yml
name: worker-pool
region: render
services:
  worker:
    exposure: none
    replicas: 6
```

部署前需创建自定义 region。不设 `node` 时，Nomad 可将各 allocation 分配到该 region
内任一符合条件的节点，不保证均匀分布。显式节点锁定会约束所有副本。资源预留和容器内
进程数均按**每副本**计算；六个各预留 2 GiB 的 allocation 共需 12 GiB 可调度内存，
物理空闲内存不等于可调度余量。

多副本服务支持 bridge 网络和 `exposure: none`，或使用动态端口的 `cn-edge` /
`external-edge`。host 网络、relay 暴露、`publishPort`、Compose `ports` 和
`container_name` 会被拒绝。多副本的 `deploy.placement`、`deploy.update_config`
和 `deploy.rollback_config` 也会报错：调度使用 Luma region/node，更新使用 Luma
管理的滚动策略。更新保留 `MaxParallel: 1`、健康检查、自动回滚与 `stop_grace_period`；
多副本不额外启动 canary。提交版本的全部所需 allocation 健康后才算部署成功。

修复必须升级 **Control**，仅升级 CLI 无法改变旧服务端渲染的 jobspec。升级本身不会
重新提交已有应用。升级后先预览保存的配置、确认 `Count`，再按应用原有流程主动重新
部署，才会应用副本数。在此之前，已有应用保持原状。

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

Mac/OrbStack 的多服务或公网 Compose 为每个 allocation 创建独立 Docker 网络空间，并固定整组节点。`publishPort` 只映射明确暴露的服务，同组名称仍解析到私有 loopback。单服务内部 worker 保持普通 Docker 网络，可以不固定节点。见 [Mac Compose 网络](region-public-ingress.zh-CN.md#mac-compose-networking)。

Linux 上的普通 Compose 组共享 Nomad 桥接网络命名空间，同组服务名解析到 `127.0.0.1`。需要采集主机回环监听端点（Traefik 指标、本地 Nomad HTTP）的 stack，可以为**每个**服务设置 `network_mode: host`。同组混用 host 和 bridge 会被拒绝。使用主机网络的 Compose 要求 `exposure: none`，且不能设置 `publishPort`；它面向 [`luma-observe`](../observe/) 等内部采集器，而非公开应用。

## 部署与更新 {#deploy-and-update}

```bash
luma login https://luma.example.com --token <management-token>
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --format ndjson
```

复用同一部署名会更新已有任务及存储归属。更改部署名会创建另一个部署。Control 预览会解析已保存的数据归属和当前候选节点；部署时还会检查已有 Nomad 任务及实例，以验证旧部署的数据归属。离线执行 `compose render` 必须显式指定本地节点，因为它无法持久化调度决策。

应用设置保留在标准 Compose 文件中。旁车清单承载区域、暴露方式、路由及存储配置。路由字段参见[部署 YAML](deployment-yaml.md)。

新旧生成任务中的挂载差异（包括增删服务）只产生 `Runtime storage mounts` 警告，不再阻止部署。卷配置中的存储后端、节点或路径变更，以及与已保存本地存储归属不同的显式 `node`，会产生 `Storage placement` 警告，部署按新的位置继续。这些警告都不会迁移数据：请检查服务是否仍使用预期数据；旧数据仍有用时，按下面的迁移步骤操作。未显式指定节点时，重新部署仍留在已保存的归属节点。

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

该命令不会停止服务或复制数据。本地目标要求显式指定部署节点或本地节点。`adopted: true` 表示数据已验证，并消除后端变更警告。设置不同的 `node` 会迁移部署并给出 `Storage placement` 警告；如果旧节点上仍有数据，请先复制。

## 旧版 NFS 兼容 {#legacy-nfs-compatibility}

迁移数据期间，已有 `storageClass` 引用仍可使用。注册的类仍由 Manager 管理；提交的旁车清单中若包含非空 `storageClasses`，会被拒绝。`luma storage list`、`check`、`apply` 和 `remove` 继续管理这些旧类。本地存储无需存储类。

检查完所有运行中和已停止的部署之前，不要移除存储类、卸载卷或关闭 NFS 服务器。没有运行实例的应用仍可能依赖旧数据。离线节点的数据应保留原位，直到能够检查该节点。

## 移除与恢复 {#removal-and-recovery}

`luma app remove <name>` 删除应用任务和路由，默认保留数据。迁移期间避免使用 `--delete-storage`。删除部署也可能删除已保存的数据归属，因此删除前应保存其固定节点配置，并在恢复应用时使用。

本地存储不提供跨节点故障转移。应恢复原节点，或从经过验证的备份恢复，并显式同步调度位置。备份应独立于应用所在磁盘保存。
