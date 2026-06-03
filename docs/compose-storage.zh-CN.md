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

### 注册 NFS 共享存储
在客户端执行以下命令注册一个名为 `home-nfs` 的存储类，并指定其所在的物理节点及路径：

```bash
luma storage set home-nfs \
  --node home-nas \
  --path /srv/luma \
  --region cn \
  --region home
```

- `home-nfs` 为部署引用的存储类名称。
- `--node` 绑定存储服务的 Luma 节点名。
- `--path` NFS 导出的物理根路径。
- `--region` 限制可以使用该存储类的业务区域。

### 注册外部独立 NFS 服务器
如果是外部已有的 NFS 服务，可以使用 `--external` 参数：

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

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
    storageClass: home-nfs
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
* `volumes.<name>.storageClass`：引用的控制面存储类名称（例如 `home-nfs`）。
* `services.<name>.exposure`：暴露入口，如 `cn-edge`、`tailscale-relay`、`none` 等。

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

### 第一步：部署存储卷组件
对于托管的 NFS 存储，在部署依赖它的服务之前，必须先应用存储挂载：

```bash
luma storage apply luma.compose.yml
```

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
