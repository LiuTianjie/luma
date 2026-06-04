# Compose 部署与持久化存储 (Compose & Storage)

Luma 支持通过一个旁车配置文件 `luma.compose.yml` 部署标准的 `docker-compose.yml` 多服务应用。

使用该模式可以保持你的 `docker-compose.yml` 文件完全标准（可直接用于本地开发），同时将所有 Luma 特有的部署配置（如 Region 调度、公开域名入口、存储卷绑定等）隔离在旁车清单中。

---

## 前置条件

- 控制面已通过 `luma bootstrap manager` 初始化。
- 各节点已通过 `luma node join --region <cn|global|home>` 加入集群。
- 客户端已通过 `luma login` 登录并保存本地上下文。

---

## 1. 注册与声明存储服务

在 Luma 中，存储服务（如 NFS 共享存储）是控制面管理的共享基础设施。你需要先在控制面注册存储类，之后在各应用的旁车清单中通过名称引用它们。

> [!NOTE]
> 旁车文件里的 `volumes.<name>.storageClass` 并不是声明 NFS 服务本身，只是说明 Compose 里的该命名卷（named volume）要挂载到对应存储类的指定子目录。具体的 NFS 物理节点、导出路径及网络拓扑，由控制面保存的 Storage 状态决定。

### 推荐：注册 Manager 作为首个托管 NFS 存储
如果是单节点或刚开始使用，建议将 Manager 主机作为第一块 NFS 存储。注册名为 `cn-nfs` 的存储类：

```bash
luma storage set cn-nfs \
  --node <manager-swarm-hostname> \
  --path /srv/luma \
  --region cn
```

- `cn-nfs` 为部署引用的存储类名称。
- `--node` 指定拥有并导出该存储路径的 Luma 节点名（在此处为 Manager 的 Swarm 主机名，可通过 `luma status` 查看）。
- `--path` NFS 导出的物理根路径（也是主机的持久化数据目录）。
- `--region` 限制可以使用该存储类的业务区域。

对于本地控制节点上的托管 NFS，`storage set` 会自动准备宿主机：按需安装 NFS server/client 包、创建导出目录、写入 NFS export、启动宿主机 NFS 服务，并清理旧版本留下的 `luma-storage-*` 存储栈。这个过程不会删除已有数据。如果目标节点不在当前 Luma Control 进程本地，命令会失败，不会保存 pending 的存储类；此时请注册外部 NFS，或在能准备该宿主机的控制节点上执行。

### 进阶：注册独立的专用 NFS 存储（如局域网 NAS）
```bash
luma storage set home-nfs \
  --node home-nas \
  --path /srv/luma \
  --region cn \
  --region home
```

### 注册外部独立 NFS 服务器（非托管）
如果使用的是外部已有的非 Luma 托管的 NFS 服务，可以使用 `--external` 参数：

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

### 统一存储服务

StorageClass 是基础设施服务。任何 Compose 命名卷都走同一个 StorageClass 模型，包括 PostgreSQL/MySQL 这类数据库容器的数据目录。Luma 不再按应用类型给卷分类，也不要求数据库卷先声明类型标签或执行探针。

存储服务只需要注册一次：

```bash
luma storage set db-storage \
  --node storage-node \
  --path /srv/luma-db \
  --region home \
  --region cn
```

之后任何命名卷都可以引用这个存储服务，并选择自己的子目录：

```yaml
volumes:
  nextcloud-data:
    storageClass: db-storage
    path: nextcloud/nextcloud-data

  nextcloud-db:
    storageClass: db-storage
    path: nextcloud/nextcloud-db
```

Luma 仍然会校验存储拓扑：storageClass 必须存在，允许的 region/node 必须匹配服务调度位置，跨 Region 的托管存储节点必须有可达的 `tailscaleIP`。这些检查只判断“这个存储服务能不能从目标节点挂载”，不再关心消费它的是数据库、文件服务、缓存还是其它应用类型。

---

## 2. 编写旁车清单 (luma.compose.yml)

使用 `luma compose init` 命令基于已有的 `docker-compose.yml` 初始化旁车配置文件：

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
```

编辑生成的 `luma.compose.yml`，将服务暴露方式和存储卷映射绑定到控制面的存储类中：

```yaml
name: app-stack
compose: docker-compose.yml
region: cn

volumes:
  pg-data:
    storageClass: cn-nfs
    path: postgres/pg-data
    accessMode: ReadWriteOnce

services:
  app:
    exposure: cn-edge
    domain: app.example.com
    port: 3000
```

### 核心字段说明：
* `compose`：指向标准 Compose 文件的相对路径。
* `volumes.<name>.storageClass`：引用的控制面存储类名称（例如 `cn-nfs`）。
* `services.<name>.exposure`：暴露入口，如 `cn-edge`、`tailscale-relay`、`none` 等。

---

## ⚠️ 存储挂载的关键注意事项

在配置和使用 Luma 托管存储时，请务必注意以下三点：

1. **同名卷声明匹配**
   `docker-compose.yml` 里也必须真的有对应的命名卷，并且某个 service 使用它。例如：
   ```yaml
   # docker-compose.yml
   services:
     postgres:
       image: postgres:15
       volumes:
         - pg-data:/var/lib/postgresql/data

   volumes:
     pg-data: {}  # 必须在此声明
   ```

2. **跨 Region 网络与 Tailscale 依赖**
   如果存储服务所在节点（如 `home-nfs` 运行在 `home-nas` 上）与服务运行节点（如 `app` 在 `region: cn`）处于不同的 Region，Luma 会将其解析为跨 Region 托管存储。
   **前提条件**：存储节点必须拥有 `tailscaleIP`（已通过 `luma node join` 启用 Tailscale 连接），否则 render/deploy 会因为无法穿透网络而直接失败。

3. **命名与 Region 语义严谨**
   为保证架构清晰，建议将存储类名称与其实际物理分布及 Region 语义统一。例如对于在 `cn` 区域运行的 Manager 节点存储，建议命名为 `cn-nfs`，并配套注册：
   `luma storage set cn-nfs --node <manager-swarm-hostname> --path /srv/luma --region cn`

---

## 3. 校验与渲染

在真正部署前，可以使用本地校验命令检查配置与存储的正确性：

```bash
# 校验 YAML 格式与结构
luma compose validate luma.compose.yml

# 渲染合并后的 Swarm Compose 文件进行预览
luma compose render luma.compose.yml

# 检查存储类绑定与端点解析计划
luma storage check luma.compose.yml
```

Luma 在校验时会自动根据节点所在的 Region 以及 Tailscale IP 拓扑，动态解析出最合理的 NFS 挂载端点。

---

## 4. 部署存储与服务

### 第一步：准备存储卷目录
对于托管的 NFS 存储，在部署依赖它的服务之前，可以先应用存储挂载计划：

```bash
luma storage apply luma.compose.yml
```

`storage apply` 会解析控制面里的存储类，并创建本次旁车引用到的具体卷目录，例如 `/srv/luma/app-stack/pg-data`。`compose deploy` 在部署应用栈前也会执行同样的准备步骤。

### 第二步：部署 Compose 服务栈
提交旁车和标准 Compose 文件到 Luma Control，控制面会自动渲染生成 Swarm Stack 并执行部署：

```bash
luma compose deploy luma.compose.yml
```

如果更新了代码或旁车配置，直接重新执行上述部署命令即可，同名 Stack 将执行滚动更新。

---

## 5. 数据防丢保护与迁移 (Migration)

Luma 在变更已部署服务的存储卷路径时，为防止数据丢失，默认会进行安全拦截。

### 切换存储后端
如果你将已部署的卷从本地卷切换为共享存储，你必须在旁车清单中明确声明以确认该操作：
- 若属于**全新空白挂载**，配置 `initialize: empty`；
- 若属于**手动迁入已有数据**，配置 `adopted: true`。

### 迁移现有数据
Luma 提供了辅助的数据迁移指令，用于在不同节点和存储卷之间安全复制状态：

```bash
luma storage migrate luma.compose.yml \
  --volume pg-data \
  --from-node home-mac-mini \
  --from-volume pg-data
```

---

## 6. 移除与清理

默认移除 Compose 应用时，Luma 会删除应用 Stack、生成的 route 文件和公网 DNS，但**不会删除存储数据**：

```bash
luma service remove app-stack --dry-run
luma service remove app-stack
```

如果确定要连该应用引用的托管存储子目录一起删除，显式加 `--delete-storage`：

```bash
luma service remove app-stack --dry-run --delete-storage
luma service remove app-stack --delete-storage
```

清理依据来自 control-plane 在上次成功部署时保存的 sidecar/manifest，不依赖执行命令的 client 机器上还有 YAML 文件。Compose 部署只删除旁车清单里 `volumes.<name>.path` 指向的 managed storage 子目录，不删除 storageClass 本身，也不清理 unmanaged/external 存储。普通单服务部署也支持 `--delete-storage`：如果 manifest 的 `storage.<volume>.path` 引用了 managed storage，会删除对应子目录；同时会删除记录 manifest 中声明的 named Docker volume 对象，例如 `data:/data`，但会跳过 bind mount 路径。`--delete-storage` 不能和 `--skip-portainer` 一起使用。
