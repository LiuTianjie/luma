# Luma 使用手册 {#how-to-use-luma}

本文是 Luma 首个公开版本的使用手册。

Luma 以五个核心概念组织功能：

```text
node / region / exposure / egress / service
```

Luma Control 运行于 Manager，负责登录令牌、节点注册、DNS 同步、jobspec 渲染和 Nomad 部署调用，底层编排器为 HashiCorp Nomad。执行 `luma login` 后，客户端无需 Docker、SSH、Cloudflare 或 Nomad 凭据即可运行 `luma deploy`。Tailscale 用于控制面网络及家庭服务的可选中继；Cloudflare 提供 DNS 和可选隧道；Egress Gateway 仅用于拉取镜像、安装依赖和服务访问外网等出站流量。

## 1. 安装 CLI {#1-install-the-cli}

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

安装程序在 `~/.local/share/luma/venv` 创建独立虚拟环境，在 `~/.local/bin/luma` 写入命令，并按需将该目录加入 Shell 配置。可立即使用完整路径；简写 `luma` 需打开新 Shell 或执行 `exec $SHELL -l`。

安装特定标签：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.358 sh
```

从检出目录进行本地开发：

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

卸载本地 CLI：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

默认保留 `~/.luma.config.json` 和 `~/.config/luma`。如需一并删除本地配置和登录 context：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

这不会删除服务器上的 Docker、Nomad、Traefik、Luma Control、应用或 `/opt/luma`。

没有 `python3` 时，安装程序会显示 macOS 或 Ubuntu/Debian 的安装命令。客户端 Docker 是可选的，仅用于部署前校验渲染的 jobspec。

创建 `.env`：

```bash
cp .env.example .env
$EDITOR .env
```

Luma 自动加载 `.env`，Shell 已导出变量优先，方便 CI 或单次命令覆盖本地值。

## 2. 配置 `luma.yaml` {#2-configure-lumayaml}

`luma.yaml` 是 Luma 唯一需要的项目配置文件。

```yaml
project: example

providers:
  dns:
    type: cloudflare
    zone: example.com
    zoneId: ""
    apiTokenEnv: CLOUDFLARE_API_TOKEN
    edgeTarget: 203.0.113.10
    recordType: A
    ttl: 1
    proxied: false

nodes:
  manager-1:
    host: manager-1
    publicIp: 203.0.113.10
    region: cn
    roles:
      - nomad-server
      - edge
      - egress

defaults:
  exposure: cn-edge
  registry: ghcr.io/liutianjie
  stackRoot: stacks
  routesRoot: routes
  egressNetwork: egress
  entrypoint: websecure
  certResolver: letsencrypt
  engine: nomad
  images:
    egressGateway: docker.1panel.live/metacubex/mihomo:latest
```

直接运行实际需要的命令：

```bash
luma bootstrap manager --domain luma.example.com
```

缺少本地值时，Luma 先提示填写，写入 `~/.luma.config.json` 后继续；工作节点执行 `luma node join ...` 时也如此。`.env` 和导出变量仍可用于覆盖。存在 `CLOUDFLARE_API_TOKEN` 而无 `providers.dns` 时，初始化和 `luma update manager` 根据 Control 域名推导 Cloudflare zone，在安装 `/opt/luma/luma.yaml` 前写入提供方配置。没有边缘 DNS 目标时，交互初始化询问 `LUMA_DNS_EDGE_TARGET`；非交互更新使用已配置边缘节点公网 IP 或已有的该变量。

相关键见[密钥与凭据](secrets.md)。Manager 常用值：

| 变量 | 用途 |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | 创建或更新 Control 与服务 DNS 的 Cloudflare 令牌。 |
| `LUMA_DNS_EDGE_TARGET` | 未配置边缘目标时，DNS 应指向的公网 IP 或域名。 |
| `TRAEFIK_ACME_EMAIL` | Traefik 申请 HTTPS 证书的 Let's Encrypt 邮箱。 |
| `EGRESS_SUBSCRIPTION_URL` | 可选代理订阅，供镜像拉取和 `proxy: true` 服务使用。 |
| `TAILSCALE_AUTHKEY` | 私网工作节点、家庭节点或 tailscale-relay 的可选认证密钥。 |
| `LUMA_SUDO_PASSWORD` | sudo 需要密码时的可选备用值。 |
| `LUMA_CONTROL_IMAGE` | 可选开发或固定版本的 Control API 镜像。 |

不要将密钥提交到 Git。

## 3. 初始化第一个节点 {#3-bootstrap-the-first-node}

对于同时运行 Nomad server、Traefik 和出网代理的公共服务器：

```bash
luma bootstrap manager --domain luma.example.com
```

该操作会：

- 安装 Docker 与 Compose；
- 安装 Tailscale，设置 `TAILSCALE_AUTHKEY` 时自动登录；
- 按需安装并启动 Nomad server agent；
- 应用节点 `meta`（region / luma_node_name / ingress / egress）；
- 创建 `/opt/luma/stacks`、`/opt/luma/routes`、`/opt/luma/control`、`/opt/luma/egress-gateway`；
- 部署 Traefik；
- 部署 Luma Control；
- 配置含 `egress` 角色时部署出网服务；
- 配置 UFW，放行 SSH、80、443、tailnet 上的 Nomad 端口 4646/4647/4648，并禁止 7890 入站。

Manager 需要代理拉取 Control 镜像时，执行含 egress 的配置前先设置 `EGRESS_SUBSCRIPTION_URL`。中国大陆 Manager 使用默认 GHCR 镜像时不应使用 `--skip-egress`。初始化实时输出 `[start]`、`[ok]`、`[fail]`。某步失败后，修复再重跑，或运行对应层的定向修复命令。

初始化时跳过了 Tailscale 登录：

```bash
luma tailscale connect
```

出网被跳过或需要修复：

```bash
luma egress setup
```

只有可直连 Control 仓库，或 `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` 指向可拉取仓库时，才在首次初始化明确跳过出网：

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

初始化输出管理令牌和节点加入令牌。客户端使用管理令牌：

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

新增服务器使用节点加入令牌：

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

加入后 Manager 自动记录区域与 Luma 节点 `meta`。`--name` 是服务清单中的节点名，写入 Nomad client `meta.luma_node_name` 并用于固定调度，避免 OrbStack 的 `orbstack` 等通用 Docker 主机名冲突。Nomad 身份是稳定 UUID，同名重新加入仍可保持固定服务有效。

`--region home` 节点必须先连接 Tailscale，才能访问 tailnet 上的 Manager。未连接时，`luma node join` 会将 `TAILSCALE_AUTHKEY` 视为必需并在注册前询问。也可先 `luma tailscale connect` 填写密钥并连接，而不尝试 Nomad 注册。

## 4. 连接 Cloudflare {#4-connect-cloudflare}

```bash
luma cloudflare connect --zone example.com
```

命令验证令牌、查找 zone 并将 `providers.dns.zoneId` 写回 `luma.yaml`。尽量在初始化前执行。如果之后才连接 Cloudflare，应刷新 Manager 配置和 Control 状态；当前权威存储是 `/opt/luma/control/control.sqlite3`，旧 `control.json` 仅用于迁移。

`cn-edge` 服务的 DNS 默认指向已配置边缘节点的公网 IP，可在服务中覆盖：

```yaml
dns:
  target: 203.0.113.10
```

## 5. 修复或刷新出网 {#5-repair-or-refresh-egress}

```bash
luma egress setup
```

`single-node` 初始化默认执行此步骤，除非使用 `--skip-egress`。出网被跳过、失败或订阅需修复时，可直接运行。它下载订阅，提取最小 Mihomo 配置，写入 `/opt/luma/egress-gateway/config.yaml`，部署 `egress_mihomo` 并配置 Docker daemon 代理：

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

服务运行时代理按服务显式启用。声明 `proxy: true`，无需手写出网网络或默认代理变量，除非要覆盖它们：

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
```

Luma 将代理附加到服务，在对应变量未设置时注入 `HTTP_PROXY=http://egress_mihomo:7890` 和 `HTTPS_PROXY=http://egress_mihomo:7890`。调度仍按 `region`。

后续刷新订阅输出：

```bash
luma egress refresh
```

## 6. 创建服务 {#6-create-a-service}

交互模式：

```bash
luma service new
```

手动清单：

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

固定到单台机器时，添加 `node`，使用 `luma node join --name` 指定的名称：

```yaml
region: home
node: home-mac-mini
```

## 7. 部署 {#7-deploy}

默认部署流程：

```bash
luma deploy app.yaml
```

应用已有项目 `.env` 时，可作为应用作用域密钥传入：

```bash
luma deploy app.yaml --env .env
```

Luma 仅导入清单引用的变量，按服务 `name` 保存，在 Manager 渲染时解析 `${NAME}`。不同应用可复用 `DATABASE_URL` 等名称而不冲突。

清单提交到已登录 Control 端点。Manager 在 `/opt/luma` 渲染文件、同步 DNS，通过 Nomad HTTP API 创建或更新 job，并探测 cn-edge/external-edge 的公共路由。

`luma deploy` 输出客户端和 Control 每一步进度。路由经验证、在监视目录外暂存后原子发布。探测 `/` 时，应用 404 表示已到达应用但可能没有根页面；Traefik 默认 `404 page not found` 代表缺少 router。路由不健康（缺少 router 或短暂 502/503/504）时，会重建一次 allocation 并复测，再判失败。单服务和 Compose 给冷镜像获取 30 分钟、滚动进度 40 分钟，客户端默认等待 3000 秒。可按需覆盖：

```bash
luma deploy app.yaml --timeout 3600
```

重复部署即更新。同一 `name` 对应同一 Nomad job（ID 为服务 slug），重跑会重写 jobspec 并更新 job。Nomad 保留上一版，可通过 `luma rollback app` 或控制台版本页回退。这只回滚运行 job，不改 Git/清单；生产应固定镜像标签或 digest。更改 `name` 会创建不同 job。

无副作用预览：

```bash
luma deploy app.yaml --dry-run
```

提交到 Control 并在 Manager 渲染/写入文件，但跳过 DNS 同步和 Nomad 部署：

```bash
luma deploy app.yaml --skip-dns --skip-orchestrator
```

按部署名称删除服务或 Compose 应用：

```bash
luma service remove app
```

Control 使用最近成功部署的记录清单，删除公共服务 DNS，注销并清除 Nomad job，删除 `/opt/luma/stacks/<region>/<service>/<service>.nomad.json`、`/opt/luma/stacks/compose/<name>/<name>.nomad.json` 等生成文件，以及 tailscale-relay 的 `routes/<service>.yml`。同一命令支持单服务和 Compose。可先预览或按需保留 DNS：

```bash
luma service remove app --dry-run
luma service remove app --skip-dns
```

## 8. 入口模式 {#8-exposure-modes}

`cn-edge`:

```text
user -> Cloudflare DNS -> CN Traefik -> cn service
```

适合中国大陆公共服务。

`tailscale-relay`:

```text
user -> Cloudflare DNS -> CN Traefik -> Tailscale -> home service
```

适合低频家庭服务，同时提供统一公网域名体验。

`cloudflare-tunnel`:

```text
user -> Cloudflare -> cloudflared -> service
```

适合不依赖中国大陆边缘节点的家庭服务。

`external-edge`:

```text
user -> Cloudflare DNS -> global edge -> global service
```

适合需要外网访问和公共端点的海外服务。

`none`:

没有公共入口，适合工作任务和内部服务。

## 9. 诊断 {#9-diagnose}

```bash
luma doctor
```

每项失败检查都包含具体修复命令或环境变量。

## 10. 首次真实冒烟测试 {#10-first-real-smoke-test}

先使用参考节点：

```bash
luma doctor
luma bootstrap manager --domain luma.example.com
luma egress setup
luma deploy examples/public-cn-service.yaml
```

然后检查：

```bash
nomad job status
curl -I https://whoami.example.com
```

仓库开源前，轮换所有曾粘贴到聊天或日志中的令牌与订阅 URL。

## 11. Build And Deploy From Repository (可插拔) {#11-build-and-deploy-from-repository}

默认的 `luma deploy` 只部署已经构建好的镜像。如果想直接从 GitHub/Gitea 仓库的源码构建并上线，用 `luma import`：它在集群里的**构建节点**上 clone 仓库、自动发现 `.luma.yml` 或 `luma.compose.yml` 这类部署文件、按 Dockerfile/Compose `build:` 构建镜像、推送到集群内自托管 registry，再走正常部署链路。这一整套是**可插拔的**——不用它，集群和现有部署不受任何影响；用它，只需下面这套一次性接入。

### 接入 SOP（已部署好 Luma 的前提下） {#sop-luma}

假设你已经走完上面 1–10 节，集群里 manager 正常、至少有一个 worker 节点、`luma login` 能用。接入「从 Git 仓库构建部署」分四步：

**Step 1 — 选一个构建节点并装好 buildx。** 构建在某个 Luma 节点上跑，需要 `docker buildx`（Linux 节点通常随 Docker 一起就有；跨架构构建还需要 `qemu`/`binfmt`）。节点 agent 会自动 advertise `docker-build` 能力，可以这样确认：

```bash
luma node list                 # 找到要用作构建节点的节点名，例如 build-1
```

如果该节点没有 buildx，先在节点上安装；装好后 agent 会自动带上 `docker-build` 能力。

**Step 2 — 起一个集群内 registry。** 构建出的镜像要有地方存，并让其它区域的节点能拉。一条命令搞定（部署 registry 服务 + 给非 manager 的就绪 Linux 节点配 `insecure-registries`）：

```bash
luma registry serve --node build-1
```

它会把 `registry:2` 部署到 `build-1`（默认 `5000` 端口、带持久化卷、仅 Tailscale 内网可达），并遍历非 manager 的就绪 Linux 节点配置 `insecure-registries`，让它们能经 Tailscale 内网从这个 registry 拉镜像。构建节点本机推送走 `localhost:5000`，跨节点拉取走 `<build-1-tailscale-host>:5000`。

从 `0.1.162` 起，CLI 会先完成 Docker daemon 配置、再创建 registry allocation；重复写入相同的 `insecure-registries` 也不会重启 Docker。这个顺序避免首次启用 registry 时由 Docker 重启打断刚创建的 Nomad CNI 网络。

可选 flag：

- `--port <n>`：registry 监听端口，默认 `5000`。
- `--storage-class <name>`：registry 数据卷用的 storageClass，默认 `local`（本地节点卷）；要把镜像数据放到 NFS 等共享存储时指定已声明的 storageClass。
- `--image <ref>`：registry 镜像，默认 `registry:2`。
- `--name <svc>`：服务名，默认 `luma-registry`。
- `--timeout <seconds>`：等待部署响应的秒数，默认 `1800`。

> manager 节点会被跳过：Control 跑在 manager 的容器里，重启它的 docker 会杀掉 Control 自己。如果 manager 也要跑从该 registry 拉取的服务，手动在 manager 的 `/etc/docker/daemon.json` 加 `insecure-registries` 并重启 docker。

**Step 3 —（私有仓库才需要）保存 Git provider token。** 公开仓库跳过这步。私有 GitHub/Gitea 仓库：

```bash
printf '%s' "$GITEA_TOKEN" | luma git-provider set gitea lin \
  --base-url https://gcode.example.com \
  --username lin \
  --token-stdin

luma git-provider repos gitea:lin
```

同一个 provider 可以保存多个账户，例如 `github:personal`、`github:work`、`gitea:lin`。Token 只写不回显，构建任务被 builder node-agent lease 时才注入 `git clone`，不写入部署文件或 agent task state。

**Step 4 — 在仓库里放 Luma 部署文件。** 单服务用普通 `.luma.yml` / `luma.yml` service manifest，并用 `build` 块代替 `image`：

```yaml
name: myapp
region: cn
exposure: cn-edge
domain: myapp.example.com
port: 8080
build:
  context: .
  dockerfile: Dockerfile
  platform: linux/amd64
```

Compose 仓库用 `luma.compose.yml` 指向标准 `docker-compose.yml`。Repository Import 会构建 Compose 里带 `build:` 的服务，推送到 builder registry，然后把这些服务改写成 `image:` 再部署：

```yaml
# luma.compose.yml
name: my-stack
compose: docker-compose.yml
region: cn
services:
  web:
    exposure: cn-edge
    domain: myapp.example.com
    port: 8080
```

支持的 Compose sidecar 文件名包括 `luma.compose.yml`、`.luma.compose.yml`、`*.luma.compose.yml`、`*.compose.luma.yml`、`docker-compose.luma.yml`。如果本地 Compose 还只有 `build:`、没有最终 `image:`，用 import 模式校验：

```bash
luma compose validate --import-mode luma.compose.yml
```

### 导入并部署 {#_1}

```bash
luma import https://github.com/acme/myapp --build-node build-1
```

GitHub 仓库也可以用短写：

```bash
luma import acme/myapp --build-node build-1
```

短写会展开成 `https://github.com/acme/myapp.git`。Gitea/self-hosted Git 用保存的 provider 账户或完整 clone URL。

使用保存的 Git provider 账户：

```bash
luma import --provider-id gitea:lin --repository acme/myapp --build-node build-1 --env .env
```

CLI 流式回传 clone → build → push → deploy 每一步。命令行可覆盖单服务 `.luma.yml`：

```bash
luma import https://github.com/acme/myapp \
  --ref release \
  --region cn --exposure cn-edge --domain myapp.example.com --port 8080
```

如果 Builder 排队或构建较慢，也可以直接用当前电脑的 Docker Buildx 构建：

```bash
cd myapp
luma build local . --env .env
```

这个命令从本地 Git `origin` 识别项目（没有 origin 时传
`--repo-url https://github.com/acme/myapp.git`），先向 Control 申请项目构建租约，
再把本地构建结果推到 Luma 内部 registry，最后沿用正常部署链路。镜像仍写入
`acme/myapp` 这一项目命名空间，不会因为改走本地构建而产生另一套项目。
本机需要能访问 `build.registryHost`；registry 开启鉴权时，先执行对应的
`docker login`。单服务与 Compose 都支持，也可以传 `--compose-sidecar`、
`--platform`、`--context`、`--dockerfile`。
如果本机已有带镜像加速或多架构能力的 Buildx builder，可以传
`--builder <名称>` 直接复用；本地拉取镜像和 Dockerfile 网络访问需要代理时，
可以传 `--proxy <地址>`，内部 registry 会自动保留在 `NO_PROXY`。

本地构建和 Builder 构建都会以实际部署目标为准选择容器架构：固定到
Mac/ARM 节点时构建 `linux/arm64`，目标区域同时存在 amd64 和 arm64 节点时
构建多架构镜像。显式 `--platform` 只能用于覆盖全部目标架构，不能把镜像
强制构建成与部署节点不兼容的架构。

Control 支持 `build-queue-v1` 时，新 CLI 自动使用同项目 FIFO 队列：远程
import / build retry 提交后排队；本地构建可以并行构建和上传，**上传完成后**
才将部署加入同一队列。顺序按服务端接受排队请求的先后计算，不按本地构建
开始时间计算。同一项目一次执行一个队列任务；不同项目可使用其它执行槽位，
但仍受已有 Builder 容量和运行时部署锁约束。前一个成功、失败或取消后，
后一个继续，不自动覆盖或取消旧任务。

CLI 显示任务 ID、队列位置和等待的任务。`--timeout` 到期或客户端退出只停止
等待，已接受的任务仍由服务端执行；用 `luma build logs <id>` 查状态，
`luma build cancel <id>` 取消尚未开始的任务。排队请求持久化，Control 重启后
保留；正在执行的任务明确标记中断，不自动重放可能已经生效的部署，重试前
先检查运行时。尚未上传完成的本地构建仍依赖调用者电脑。

环境变量按任务独立保存在私有 Control 状态中，不进入公开构建历史，任务结束
后清理队列载荷。旧 Control 仍按原来的活动构建互斥规则处理，需要同时升级
CLI 和 Control 才能使用队列。直接部署预构建镜像的 `deploy` / `compose deploy`
继续使用已有同步锁。Control 仍为本地上传分配唯一 tag，并校验镜像属于项目
预留的 registry 路径。

构建节点来自控制面声明的 builder 节点；通常不用传 `--build-node`，只有需要临时覆盖到另一个已声明 builder 时才传。单服务 import 还可用 `--context`（build 上下文目录，默认 `.`）、`--dockerfile`（默认 `Dockerfile`）、`--registry-host`（其它节点拉取用的 registry 主机，默认 `<build-node>:5000`）覆盖仓库里的 `build:` 字段。对 Compose import，`--region` 会覆盖 sidecar 的 region；`--exposure`、`--domain`、`--port` 是单服务覆盖项，会被忽略并打印 warning。Compose 的服务级路由请写在 `luma.compose.yml` 的 `services:` 里。`luma import` 默认等待 `3600` 秒的 build+deploy 响应，用 `--timeout <seconds>` 覆盖。

预声明 builder 节点和内部 registry 默认值，之后 import/build 就能省掉 `--build-node`：

```bash
luma build config --node build-1 --default-node build-1 \
  --registry-host <build-1-tailscale-host>:5000 \
  --push-host <build-1-tailscale-host>:5000
```

`--node` 可重复声明多个 builder；`--default-node` 是 `luma import` 缺省用的 builder；`--registry-host` 是 target node 拉镜像的地址，`--push-host` 是 BuildKit 推镜像的地址。两者都必须是 BuildKit 容器和所有目标节点可达的 Builder Tailscale endpoint；不要使用已移除且在 BuildKit namespace 内含义错误的 `localhost:5000`。不带参数运行 `luma build config` 只打印当前配置和各 builder 的就绪/能力表。

构建历史和失败日志用 CLI 查看：`luma build list`（打印 ID/状态/节点/provider/仓库/ref）、`luma build logs <id>`（某次构建的分步日志）。修好凭据或配置后 `luma build retry <id>` 重跑整条 build+deploy；`retry` 也接受 `--env .env` 重新提供 scoped secrets、`--timeout`（默认 3600）。

dashboard 的「创建应用」页顶部也有「仓库导入」入口：选择 Git provider、账户、仓库和 ref；或手填 URL。进度实时显示。

> CN 节点的 `git clone` 会自动走 manager 的 egress 网关（`http://<manager-host>:7890`），和镜像拉取、节点 join 用的是同一个出口；`global` 节点直连不走代理。代理出口由控制面的 egress 配置决定，无需在 `luma import` 上单独指定。

### 升级已部署的应用 {#_2}

**升级 = 改完代码、推到 GitHub，再跑一次同样的 `luma import`。**

```bash
luma import https://github.com/acme/myapp --build-node build-1
```

原理：每次构建按 git commit 打 tag（`<registry>:5000/acme/myapp:<git-sha>`），注入 manifest 的是这个不可变的 sha 标签；而 Nomad job id 来自 `.luma.yml` 的 `name`。所以同名 + 新 SHA = 对同一个 Nomad job 做滚动更新，旧版本自动保留。这和普通 `luma deploy` 的「同名即更新」是同一条链路。

升级特定分支/Tag：

```bash
luma import https://github.com/acme/myapp --build-node build-1 --ref v2.1.0
```

要点：

- 镜像钉死在 git SHA，不是 `:latest`，所以两次构建之间即使 registry 里 `:latest` 变了，已部署的旧版本也不会漂移。
- `name` 不变才是升级；改了 `.luma.yml` 里的 `name` 会创建一个新应用，而不是升级旧的。
- 改了 `domain`/`region`/`port` 等也通过重跑 import 生效（或用对应 CLI flag 覆盖）。

### 回滚 {#_3}

因为每个历史版本的 jobspec 钉的是各自的 `:<git-sha>`，registry 默认不回收旧镜像，所以回滚拉得回原镜像字节：

```bash
luma history myapp                 # 看版本列表
luma rollback myapp                # 回到上一个版本
luma rollback myapp --to-version <N>
```

dashboard 的 Applications → Versions 也能做同样的回滚。注意这是 Nomad job 版本回退（拉回那个版本钉的镜像），不会改 Git、不会回滚数据库迁移或卷数据。

### 跨架构注意 {#_4}

构建节点是 arm64（比如 Mac mini）而目标运行节点是 amd64 时，必须让构建产出 `linux/amd64`。`.luma.yml` 的 `build.platform` 默认就是 `linux/amd64`，但构建节点要装好 `qemu`/`binfmt` 才能跨架构构建。

### 取消接入 {#_5}

不想再用时，删掉 registry 服务即可（`luma service remove luma-registry`），已经在跑的服务不受影响。`insecure-registries` 配置留在各节点的 docker daemon 里是无害的。

## 12. 通过 Dashboard 升级 Luma {#12-dashboard-luma}

日常升级不需要 SSH manager，也不需要在每台节点运行命令。使用管理 Token 登录 `https://<control-domain>/dashboard/fleet`，在「升级中心」完成整条流程：

1. 填写不可变 release tag（例如 `v0.1.175`）。标准 tag 会自动建议同 tag 的 Control 镜像；自定义分支或 commit 需要明确填写已发布镜像。
2. 点击「检查全部公网路由」保存升级前基线。HTTP 成功、重定向和鉴权拒绝都能证明路由已发布；404、网关错误和连接失败会明确标红，但不会悄悄阻断管理员操作。
3. 第一次点击「升级 Control」会展示影响和基线结果，第二次确认后，Builder 先通过受管出网代理把外部 Control 镜像缓存到内部 registry 并校验 digest。该任务可恢复、有明确进度，避免 manager 的 Docker daemon 在替换窗口直接依赖 GHCR。
4. 内网镜像就绪后才启动 Control 更新。更新运行在独立 systemd 单元中，不会因 node agent 自身重启而被中断。
5. Control 替换期间页面自动重连。完成后自动重新检查全部公网路由，并展示有界日志；无需手动重启应用。
6. 点击「更新未对齐节点」。操作会只选择版本落后的非 manager 节点，并持久化逐节点状态。安装完成后，节点必须以目标版本重新心跳，才会显示成功。
7. 失败或中断的镜像准备、Control 或节点任务都会保留原因，可在同一页面重试。关闭页面、刷新浏览器或 Control 重启都不会丢失操作结果。

首次接入一个早于托管更新能力的历史 agent 时，Dashboard 会明确显示缺失的 capability；只需对该历史节点做一次 CLI 更新。之后的发布均可在升级中心完成。

## 13. 应用级可观测（luma-observe） {#13-luma-observe}

Dashboard 里的 CPU/内存是节点样本，不是应用是否可用。HTTP 5xx、延迟、Nomad 失败/重启和节点水位走独立的 [`observe/`](../observe/) 栈，和 Traefik/Control 同机、只绑 loopback。部署该栈后 Control 会把它钉到 manager、把 Grafana 挂到控制面域名的 `/grafana`，告警规则里会出现 p95 / 5xx / 失败 alloc 预设。详见 [Observability](observability.md)。