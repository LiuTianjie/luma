# 自定义 Region 的公网入口 {#public-ingress-for-a-custom-region}

`render` 等自定义 Region 可以保留现有节点和调度归属，同时允许选定的 HTTP 服务使用 `tailscale-relay`。公网访问路径为：

```text
Client -> public HTTPS / Traefik -> Tailscale -> render node -> application
```

先给 Region 开启入口能力，再由应用选择该入口。`exposure: none` 的 worker 和数据库不会因此公开。修改入口不会改变 Region 的出站策略，也不会重新部署已有 job；继续使用现有 Nomad 架构。

## 升级集群 {#upgrade-the-cluster}

CLI、Control 和节点 Agent 使用同一已发布版本。先等该版本的 Python 包与 Control 镜像流水线成功，再安装。只升级 CLI 不会改变服务端的校验和 jobspec 渲染。升级前确认 Manager 有足够容量容纳 Control 的 4 GiB 预留和其他系统服务，升级后按[控制面内存](operations.zh-CN.md#control-memory)核对实际 job。

1. 在控制台「设置 → 系统维护」保存当前公网路由基线，记录旧版本 ref 和 Control 镜像，按 [Control 存储](control-storage.zh-CN.md#backup-scope)完成私有备份。
2. 升级本地 CLI，并更新 CI 的版本：

```bash
luma update --install-ref v0.2.17
python -m pip install --upgrade "luma-infra==0.2.17"
```

第一条升级本地 Luma 安装；如果本机已加入集群，也会刷新 Agent。第二条是 Python CI 环境的等效依赖固定方式，不需要为了执行两条命令而安装进无关的系统 Python。

3. 在系统维护中填写上述版本 ref 和镜像 `ghcr.io/liutianjie/luma-control:v0.2.17`。先通过已配置的 Builder 准备镜像，再升级 Control，保留已核验的内部 Registry digest 和操作记录。Manager 升级也会刷新其 CLI 与 Agent；重连后检查自动路由哨兵结果。
4. 从同一页面升级其余节点，或执行：

```bash
luma update fleet --install-ref v0.2.17 --all
luma node list
luma version
```

每个节点必须用新心跳报告目标版本，任务排队或安装程序退出都不等于升级完成。离线或失败节点需要修复重试，不能计入「全部成功」。Manager 已在第 3 步处理，`--include-manager` 留给明确的修复场景。

新 Control 会在 `luma version` / `/v1/health` 中报告 `region-exposures-v1` 和 `macos-compose-network-v1`。Compose 网络无需新增 Agent 协议，可信生命周期任务随 Control 生成的 jobspec 下发。已有 job 保持旧网络，直到主动重新部署。仅靠 CLI 恢复 Manager 或回退兼容版本，见[发布指南](maintainers/release.zh-CN.md#candidate-manager-upgrade-and-rollback)。

## 开启 render 入口 {#enable-render-ingress}

对已存在的 Region：

```bash
luma region update render --exposure tailscale-relay
luma region list
```

结果应包含 `none,tailscale-relay`。`--exposure` 替换允许的入口列表；要保留其他模式，重复传入该参数。`none` 始终保留。不传 `--egress` 就保持当前出站策略。内置 Region 仍然只读。控制台「节点 → 区域」也可开启 HTTP 中继。

新 Region 要先创建，再加入节点：

```bash
luma region create render --egress direct --exposure tailscale-relay
```

Region 配置只是允许某种入口；目标节点仍需 Tailscale、Docker、就绪的 Nomad client 和可用的内部 Registry。Traefik 与 DNS 继续使用现有公网边缘节点。应用端口只需能被边缘节点经 tailnet 访问，无需给 Mac 配置路由器公网端口转发。

## 原生 API 清单 {#native-api-manifest}

Mac 上的原生单服务 API 继续使用 Docker host 网络。填写应用真实监听端口，省略 `publishPort`。测试和生产同机时，使用不同监听端口。

```yaml
name: api-test
image: registry.example.com/api:release
region: render
node: mac-worker-1
exposure: tailscale-relay
domain: app-test.example.com
port: 18080
replicas: 1
env:
  PORT: "18080"
healthcheck:
  test: [CMD, curl, -f, http://127.0.0.1:18080/healthz]
  interval: 15s
  timeout: 5s
```

镜像、域名、启动命令/环境变量和健康路径要替换为应用实际契约。`PORT` 只是示例，不会自动改变不读取该变量的应用。保留应用鉴权。路由通过稳定节点名和其记录的 Tailscale 地址生成。

```bash
luma validate luma.api.test.yml
luma deploy luma.api.test.yml --dry-run
```

检查预览后，按应用已记录的镜像、本地构建或仓库导入方式部署。源码清单里的占位镜像不能代替目标 revision 的实际构建。

## Mac Compose 网络 {#mac-compose-networking}

Mac 上的多服务或公网 Compose 栈，需要固定整组运行节点；Control 选定的本地存储归属节点也可提供此约束。Luma 在 OrbStack 内为每个 allocation 创建私有 Docker 网络空间，同组服务名解析到其私有 loopback，只发布明确声明的端口。应用容器不会获得 Docker socket。Linux 仍走原有 Nomad CNI 网络。

```yaml
# compose.yml
services:
  api:
    image: registry.example.com/api:release
    environment:
      PORT: "8080"
      DATABASE_URL: postgres://app:${DB_PASSWORD}@database:5432/app
  database:
    image: postgres:17
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: app
    volumes:
      - app-test-db:/var/lib/postgresql/data
volumes:
  app-test-db: {}
```

```yaml
# luma.compose.yml
name: app-test
compose: compose.yml
region: render
services:
  api:
    node: mac-worker-1
    exposure: tailscale-relay
    domain: app-test.example.com
    port: 8080
    publishPort: 18080
  database:
    exposure: none
```

此配置将 `mac-worker-1:18080` 转到 API 容器的 `8080`。第二套部署可保留 `port: 8080`，改用 `publishPort: 18081`；部署名必须不同，独立数据也必须使用不同卷名。裸 Docker 卷名会保持原名，不会自动加部署前缀。`database:5432` 仅供同一 allocation 内的服务访问；独立部署的 worker 需要单独配置可达的私网数据库地址，Compose 服务名不跨 allocation。

发布端口的 Compose 栈在固定节点上只允许一个副本。单服务内部 worker 池仍可使用 Region 调度和多副本。新 Mac 路径支持 `none`、`tailscale-relay`、`tcp-relay`；所有服务显式 `network_mode: host` 仍只用于内部服务。不要给上面的公开服务示例补 Compose `ports` 或 host 网络。

辅助镜像默认是 `python:3.13-alpine`，由 Builder 单独缓存到内部 Registry。需要固定其他兼容 Python 镜像时，修改 Manager 的 `defaults.images.composeNetwork`。Docker 在 VM 的通配地址发布声明端口，让 OrbStack 能转到 macOS；限制直接局域网访问由宿主机防火墙负责。自动清理只删除该 allocation 所有的网络容器，重启和替换通过 Nomad 生命周期任务重建。

```bash
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
```

## 验收与回退 {#verify-and-roll-back}

先检查目标 allocation，再检查边缘节点到目标端口、DNS、公网 HTTPS。验收真实健康接口、带鉴权的 API，以及应用支持的 SSE 流和文件下载 HTTP Range。同时从 tailnet 外和边缘节点验证；allocation 就绪不能证明公网已通。由对象存储直接提供的文件继续使用其原有访问地址。

```bash
curl --fail --show-error https://app-test.example.com/healthz
curl --no-buffer --max-time 15 https://app-test.example.com/events
curl --fail --show-error -D - -H 'Range: bytes=0-1023' \
  https://app-test.example.com/files/example.bin -o /dev/null
```

按实际应用替换路径和鉴权。持续 SSE 在限定时间后超时是正常现象。在迁移有状态应用之前，重建可丢弃的测试 allocation，确认路由恢复且另一套部署仍可访问。

应用发布失败时，先恢复记录中的上一 revision。关闭 Region 中继前，先移除或重新部署所有使用该入口的应用：

```bash
luma region update render --exposure none
```

只要保存的原生或 Compose 部署仍使用中继，包括部分失败记录，Control 就会拒绝关闭。回退 Control 不会改写已有 job 和应用数据；保留匹配的 job 制品、旧安装 ref 和镜像。

维护者可用独立 Nomad 二进制和本机已有的 Python 镜像复现 Mac 联调：

```bash
python scripts/check-macos-compose.py --nomad /path/to/nomad --image python:3.13-alpine
```

它检查 Luma 部署健康门禁、端口映射、同组名称解析、两套栈隔离、SSE、Range、清理与重建，结束后删除自身的 job、容器和临时 Agent 数据。它不会连接当前配置的线上集群，也不能代替该集群的公网 DNS/TLS 验收。
