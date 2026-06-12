# Luma Deployment YAML

Luma 的部署文件不是 Docker Compose。它是一个更小的 service manifest，用户只描述服务入口、镜像、区域和少量运行参数。`luma deploy` 会把它提交给控制面，控制面再生成 Swarm stack、同步 DNS、配置 Traefik 路由、触发 Portainer。

## 最小公开服务

国内公开服务通常写成这样：

```yaml
name: status
image: traefik/whoami:latest
region: cn
exposure: cn-edge
domain: status.example.com
port: 80
replicas: 1
```

部署：

```bash
luma deploy status.yaml
```

结果：

- Cloudflare 写入 `status.example.com` 记录；
- Traefik 为 `Host(status.example.com)` 创建 HTTPS 路由；
- Swarm 部署 `status` 服务；
- 请求转发到容器内 `port: 80`。

## 字段参考

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `name` | 是 | string | 服务名。Luma 会转成 slug，用于 stack/service/router 名称。 |
| `image` | 是 | string | 容器镜像，例如 `ghcr.io/acme/api:1.0.0`。`latest` 或未带 tag 会在部署时解析成 `name@sha256:...` 再部署。 |
| `region` | 是 | `cn` / `global` / `home` | 服务运行区域。 |
| `node` | 否 | string | 指定 Luma 节点名，也就是 `luma node join --name` 的值。用于把服务钉到某台机器；控制面会解析成 Swarm NodeID 后调度，仍会同时加 region 约束。 |
| `exposure` | 否 | 见下方 | 访问方式。新文件必须显式表达公开、隧道或内部访问语义。 |
| `domain` | 公开服务必填 | string | 用户访问的域名。 |
| `port` | 公开服务必填 | integer | 容器内部监听端口，不是云服务器安全组端口。 |
| `replicas` | 否 | integer | 副本数，默认 `1`，必须大于等于 `1`。 |
| `env` / `environment` | 否 | map | 环境变量，会写进 Swarm service。 |
| `command` | 否 | string/list | 覆盖容器启动命令。 |
| `constraints` | 否 | string[] | 追加 Swarm placement 约束。Luma 会自动加 region 约束。 |
| `labels` | 否 | string[] | 追加 service labels。公开 Traefik labels 会自动生成。 |
| `networks` | 否 | string[] | 追加 external overlay networks。公开 Traefik 服务会自动加入 public network。 |
| `proxy` | 否 | boolean | 服务运行时是否需要走 egress proxy。为 `true` 时会自动加入 egress 网络和代理环境变量。调度仍按 `region`。不是镜像拉取代理。 |
| `resources` | 否 | map | 透传到 Swarm `deploy.resources`，用于限制 CPU/内存。支持 `limits` 和 `reservations`。 |
| `healthcheck` | 否 | map | 透传到 Swarm service `healthcheck`。公共 HTTP 服务建议探测本地端口，例如 `http://127.0.0.1:<port>/healthz`。 |
| `publishPort` | tailscale-relay / tcp-relay 可用 | integer | task 节点上的 host mode 暴露端口，默认等于 `port`。如果目标节点已有本机服务占用该端口，必须显式换一个不冲突的 `publishPort`。 |
| `relay` | tailscale-relay 可选 | map | 覆盖 Tailscale relay 上游。默认跟随 Swarm 实际运行 task 所在的 home 节点自动推导。 |
| `tcp` | tcp-relay 可选 | map | TCP relay 高级上游覆盖。正常情况不需要填写；入口由 `publishPort` / `port` 自动派生。 |
| `tunnel` | cloudflare-tunnel 可用 | map | Cloudflare Tunnel token env 等设置。 |
| `dns` | 否 | map | 保留给 DNS 相关扩展。 |
| `portainer` | 否 | map | 保留给 Portainer API 相关扩展。 |
| `stackPath` | 否 | string | 覆盖生成 stack 路径。通常不用。 |
| `routePath` | 否 | string | 覆盖 tailscale route 文件路径。通常不用。 |

## exposure 选择

| exposure | region | 是否需要 domain/port | 适合场景 |
| --- | --- | --- | --- |
| `cn-edge` | `cn` | 是 | 国内公开 Web/API，走国内 Traefik 和备案域名。 |
| `external-edge` | `global` | 是 | 海外公开服务，例如外网 API 网关、低频海外工具。 |
| `tailscale-relay` | `home` | 是 | 家里服务通过国内 Traefik + Tailscale 暴露。 |
| `tcp-relay` | 任意 | 是 | 数据库等原生 TCP 服务，公网端口独占，走 Traefik TCP -> task host port。 |
| `cloudflare-tunnel` | 通常 `home` | 是 | 家里/私有服务通过 Cloudflare Tunnel 暴露。 |
| `none` | `cn` / `global` / `home` | 否 | 内部任务、worker、队列消费者，不直接公开。 |

规则：

- `exposure: cn-edge` 必须配 `region: cn`。
- `exposure: external-edge` 必须配 `region: global`。
- `exposure: tailscale-relay` 必须配 `region: home`。若未提供 `relay.host`/`relay.url`，控制面会在部署后根据实际 running task 所在节点自动推导上游。
- `exposure: tcp-relay` 的 Traefik TCP entrypoint 由 `publishPort` 或 `port` 自动生成。普通 MySQL 不能可靠使用 SNI 分流，因此当前实现按端口独占转发。
- 公开服务必须提供 `domain` 和整数 `port`。
- `public` 已移除；请使用 `exposure`。

## 常用模板

### 国内公开 API

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
env:
  NODE_ENV: production
  DATABASE_URL: ${DATABASE_URL}
```

## 环境变量和 Secret

普通非敏感配置可以直接写在 manifest 里：

```yaml
env:
  NODE_ENV: production
  LOG_LEVEL: info
```

敏感值不要写明文。先把 secret 存到控制面：

```bash
luma secret set DATABASE_URL
luma secret set OPENAI_API_KEY
luma secret list
```

然后在 YAML 里引用：

```yaml
env:
  DATABASE_URL: ${DATABASE_URL}
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

部署时，客户端只提交 manifest。Luma Control 会从控制面 secret store 读取这些变量，并作为 Portainer stack environment 传入。`luma secret list` 只显示 key，不显示 value。

如果缺少引用的变量，部署会失败并提示：

```text
missing deployment secrets: DATABASE_URL. Run: luma secret set <NAME>
```

## 私有镜像仓库

镜像拉取凭证不要写进 manifest，也不要作为容器环境变量传给业务服务。先在控制面保存 registry credential：

```bash
luma registry login ghcr.io --username <user> --password-stdin
luma registry list
```

然后 manifest 仍然只写镜像：

```yaml
image: ghcr.io/acme/private-api:1.0.0
```

部署时 Luma 会从 image 推断 registry host，使用匹配的凭证预拉取镜像，并把 registry 关联到 Portainer/Swarm stack，让被调度的节点可以拉取私有镜像。`luma registry list` 只显示 registry host 和 username，不显示 password/token。

常见 GitHub 场景：GitHub Actions 把应用镜像推到私有 GHCR，同一个仓库还可以用 GitHub Pages 发布文档或营销页。Luma 只需要 GHCR 的 registry credential 来拉运行时镜像，不需要把 GitHub token 写进 manifest，也不影响 GitHub Pages 的静态站点发布。

私有 registry 的镜像拉取和服务运行时 `proxy: true` 是两条路径。`proxy: true` 只给容器里的出站 HTTP/HTTPS 请求注入代理；镜像拉取走 Docker daemon。如果 `curl https://<registry>/v2/` 能返回 registry 的 `401`，但 `docker pull` 报 EOF/timeout，优先检查 `docker info` 里的 HTTPProxy/HTTPSProxy/NO_PROXY，并确保私有 registry host 在 Docker daemon 的 `NO_PROXY` 中。

### 海外 worker

```yaml
name: fetch-worker
image: ghcr.io/acme/fetch-worker:1.0.0
region: global
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
  OPENAI_BASE_URL: https://api.openai.com/v1
```

渲染后会自动带上：

```yaml
placement:
  constraints:
    - node.labels.region == global
```

### 指定部署到某个节点

如果服务必须固定在某台机器上，例如有本地磁盘状态、只想跑在家里的 Mac mini、或临时调试某个 worker，可以使用 `node`：

```yaml
name: home-db
image: postgres:16
region: home
node: mac-mini-gaojiu
exposure: none
volumes:
  - home_db_data:/var/lib/postgresql/data
```

控制面部署时会同时保留 region 约束，并把 Luma 节点名解析成真实 Swarm NodeID 约束：

```yaml
placement:
  constraints:
    - node.labels.region == home
    - node.labels.luma.node.id == 3ve5sy2mn3n16a7yhu9tavhrm
```

`node` 使用的是 Luma 节点名，不是 Docker hostname。这个区别对 OrbStack 很重要：多台 Mac 的 Docker hostname 可能都叫 `orbstack`，但 Luma 会用 `luma.node.id` 指向唯一的 Swarm NodeID，避免服务跑到错误机器。

如果节点离开 Swarm 后用同一个 Luma 节点名重新 join，它会获得新的 Swarm NodeID。控制面会刷新该节点的 `luma.node.id` 标签，并更新 Luma 管理的固定节点服务约束；不用手工把 Docker hostname 写进 manifest。

### 普通服务使用 storageClass

单服务 manifest 也可以把任意 named volume 交给控制面注册的 storageClass。`volumes` 仍然是容器挂载声明；顶层 `storage` 只描述这些 named volume 应该落到哪个基础设施存储服务的哪个子目录：

```yaml
name: home-db
image: postgres:16
region: home
exposure: none
volumes:
  - pg-data:/var/lib/postgresql/data
storage:
  pg-data:
    storageClass: db-storage
    path: home-db/pg-data
    accessMode: ReadWriteOnce
```

`storageClass` 本身由 manager 维护，例如：

```bash
luma storage set db-storage \
  --node home-nas \
  --path /srv/luma \
  --region home
```

`storageClass` 是统一的存储服务引用。无论挂载目标是 PostgreSQL/MySQL 数据目录、上传目录还是普通应用状态目录，Luma 都按同一套 storage service 解析和挂载；它只校验 storageClass 是否存在、region/node 是否允许、跨 Region 是否可达。

### 需要代理的 worker

如果服务运行时需要通过 Luma egress proxy 访问外网，声明 `proxy: true`。不要为了使用默认代理手写 `networks: [egress]` 或 `HTTP_PROXY` / `HTTPS_PROXY`；Luma 会自动渲染这些字段。如果你显式写了同名 env，Luma 会保留你的值。

`proxy: true` 只管容器自己的出站 HTTP/HTTPS 请求，和服务如何被访问是两件事。比如 `region: home` + `exposure: tailscale-relay` + `proxy: true` 是有效组合：用户入站流量走公网 Traefik -> Tailscale -> home task，容器访问外网时走 `egress_mihomo`。

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
```

渲染后会自动带上：

```yaml
environment:
  HTTP_PROXY: http://egress_mihomo:7890
  HTTPS_PROXY: http://egress_mihomo:7890
networks:
  - egress
placement:
  constraints:
    - node.labels.region == cn
```

### 小机器资源限制

如果 manager 只有 2c2g，并且业务服务也部署在 manager 上，建议给每个非核心服务显式设置资源边界。`limits` 是硬上限，`reservations` 用于 Swarm 调度时预留资源：

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
resources:
  limits:
    cpus: "0.50"
    memory: 512M
  reservations:
    cpus: "0.10"
    memory: 128M
```

### 家里内部服务

```yaml
name: backup-job
image: ghcr.io/acme/backup-job:1.0.0
region: home
exposure: none
replicas: 1
```

### 家里服务通过 Tailscale Relay 暴露

```yaml
name: home-panel
image: ghcr.io/acme/home-panel:1.0.0
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
```

默认情况下，Luma Control 会在服务部署后查看 Swarm task 实际运行在哪些 home 节点，并把 route 上游指向这些节点的 host port。若服务必须固定到某台机器，再显式指定 `node`：

```yaml
node: home-mac-mini
```

也可以手动覆盖完整上游 URL：

```yaml
relay:
  url: http://home-1.your-tailnet.ts.net:8080
```

### 公开 TCP 服务

服务 manifest：

```yaml
name: granary-db
image: mysql:8.4.9
region: home
node: lab
exposure: tcp-relay
domain: granary-db.itool.tech
port: 3306
publishPort: 3306
replicas: 1
```

Luma 会把 DNS 指到公网 edge，自动确保 Traefik 监听 `tcp-3306` entrypoint，并写入 Traefik TCP route。`domain` 用于 DNS；普通 MySQL 连接无法提供 HTTP Host 或可靠起始 SNI，所以同一个发布端口一次只应给一个 TCP 服务使用。
`publishPort` 是目标 task 节点上的宿主机端口。如果同一台机器已有本机容器或非 Luma 服务占用 `3306`，请选择其它端口并同步调整客户端连接端口或入口配置。

### Cloudflare Tunnel 服务

```yaml
name: home-tool
image: ghcr.io/acme/home-tool:1.0.0
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

## 生成前检查清单

- 域名是否是用户真正要访问的入口。
- `port` 是否是容器内部监听端口，而不是公网端口。
- `region` 和 `exposure` 是否匹配。
- 公开服务是否填写了 `domain` 和 `port`。
- 镜像是否带 tag；`latest`/未带 tag 会在部署时解析成 digest，但生产回滚仍建议使用固定版本 tag。
- secret 不要直接写明文，优先写 `${ENV_NAME}`。
- worker 默认使用 `exposure: none`，不要给它配公网域名。
- home 节点不要承载核心高频公网服务。

## 验证命令

```bash
luma validate service.yaml
luma deploy service.yaml --dry-run
```

`validate` 会校验 manifest 并输出渲染后的 stack。`deploy --dry-run` 不会提交控制面，只展示会生成什么。若本地校验无法读取控制面的节点或 storageClass 信息，JSON 输出会带 `validationMode: "degraded"` 和 `warnings`，文本输出会打印 `[warn]`，表示这次校验没有覆盖真实集群放置/存储可达性。
