# How To Use Luma

This is the operating manual for the first public version of Luma.

Luma keeps five concepts visible:

```text
node / region / exposure / egress / service
```

Luma Control runs on the manager node and owns login tokens, node registration, DNS sync, jobspec rendering, and Nomad deployment calls. The orchestrator underneath is HashiCorp Nomad. After `luma login`, `luma deploy` can be run from a client that does not have Docker, SSH access, Cloudflare credentials, or Nomad credentials. Tailscale is a control-plane network and a relay option for home services. Cloudflare is the DNS provider and optional tunnel provider. Egress Gateway is only for outbound traffic such as pulling images, installing dependencies, or running services that need external network access.

## 1. Install The CLI

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
~/.local/bin/luma preflight
```

This creates a private venv at `~/.local/share/luma/venv`, writes a `luma` command at `~/.local/bin/luma`, and adds `~/.local/bin` to your shell profile when needed. Use `~/.local/bin/luma` immediately, or open a new shell / run `exec $SHELL -l` before using the shorter `luma` command.

Install a specific tag:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | LUMA_INSTALL_REF=v0.1.151 sh
```

For local development from a checkout:

```bash
./scripts/install-luma.sh
. .venv/bin/activate
```

To uninstall the local CLI:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

The default uninstall keeps `~/.luma.config.json` and `~/.config/luma`. To remove local config and login contexts too:

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

This does not remove Docker, Nomad, Traefik, Luma Control, deployed services, or `/opt/luma` from a server.

If `python3` is missing, the installer prints the package command for macOS or Ubuntu/Debian. Local Docker is optional; it is only used to validate rendered jobspec files before deployment.

Create `.env`:

```bash
cp .env.example .env
$EDITOR .env
```

Luma loads `.env` automatically. Shell exports win over `.env`, so CI or one-off commands can override local values.

## 2. Configure `luma.yaml`

`luma.yaml` is the only project config file Luma needs.

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

Run the command you actually need:

```bash
luma bootstrap manager --domain luma.example.com
```

If local values are missing, Luma asks for them first, writes `~/.luma.config.json`, then continues. On worker nodes, the same happens during `luma node join ...`. `.env` and exported environment variables still work for local overrides. If `CLOUDFLARE_API_TOKEN` is configured but `providers.dns` is missing, bootstrap and `luma update manager` infer the Cloudflare zone from the control domain and write the provider config before installing `/opt/luma/luma.yaml`. If no edge DNS target is configured, interactive bootstrap asks for `LUMA_DNS_EDGE_TARGET`; non-interactive update uses the configured edge node public IP or an existing `LUMA_DNS_EDGE_TARGET`.

The relevant keys are explained in [secrets.md](secrets.md). The common manager values are:

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Cloudflare DNS token used to create/update control and service records. |
| `LUMA_DNS_EDGE_TARGET` | Public IP or DNS name that Cloudflare records should point to when no edge target is already configured. |
| `TRAEFIK_ACME_EMAIL` | Let's Encrypt account email used by Traefik for HTTPS certificates. |
| `EGRESS_SUBSCRIPTION_URL` | Optional proxy subscription URL for image-pull proxying and `proxy: true` services. |
| `TAILSCALE_AUTHKEY` | Optional auth key for private worker joins, home nodes, or tailscale-relay services. |
| `LUMA_SUDO_PASSWORD` | Optional fallback when sudo requires a password. |
| `LUMA_CONTROL_IMAGE` | Optional development/pinned control API image. |

Do not commit secrets.

## 3. Bootstrap The First Node

For a single public server that runs the Nomad server, Traefik, and egress:

```bash
luma bootstrap manager --domain luma.example.com
```

This does:

- installs Docker and Compose;
- installs Tailscale and logs in when `TAILSCALE_AUTHKEY` is set;
- installs and starts the Nomad server agent if needed;
- applies node `meta` (region / luma_node_name / ingress / egress);
- creates `/opt/luma/stacks`, `/opt/luma/routes`, `/opt/luma/control`, and `/opt/luma/egress-gateway`;
- deploys Traefik;
- deploys Luma Control;
- deploys egress when the profile has the `egress` role;
- configures UFW for SSH, 80, 443, the Nomad ports (4646/4647/4648) on the tailnet, and blocks inbound 7890.

Set `EGRESS_SUBSCRIPTION_URL` before running an egress profile when the manager needs a proxy to pull the configured control image. Mainland managers using the default GHCR control image should not use `--skip-egress`. Bootstrap prints live step logs with `[start]`, `[ok]`, and `[fail]` markers. If one step fails, either re-run bootstrap after fixing the issue or run the focused repair command for that layer.

If Tailscale login was skipped during bootstrap:

```bash
luma tailscale connect
```

If egress was skipped or needs repair:

```bash
luma egress setup
```

To intentionally skip egress during first bootstrap, only do this when the control image registry is directly reachable, or when `LUMA_CONTROL_IMAGE` / `defaults.images.lumaControl` points at a registry the manager can pull:

```bash
luma bootstrap manager --domain luma.example.com --skip-egress
```

The bootstrap output includes a management token and a node join token. Use the management token on client machines:

```bash
luma login https://luma.example.com --token <management-token>
luma context list
```

Use the node join token on additional servers:

```bash
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

The manager records the node's region and Luma node `meta` automatically after the node joins. `--name` is the Luma node name used in service manifests. Luma writes it to the Nomad client `meta.luma_node_name` and uses it for pinned scheduling, so generic Docker hostnames such as OrbStack's `orbstack` do not collide. The Nomad node identity is a stable UUID, so a rejoin under the same name keeps pinned services valid.

For `--region home`, the node must be connected to Tailscale before it can reach a manager address on the tailnet. If the node is not connected yet, `luma node join` treats `TAILSCALE_AUTHKEY` as required and asks for it before registering the node. You can also run `luma tailscale connect` first to fill the key and connect Tailscale without attempting a Nomad enroll.

## 4. Connect Cloudflare

```bash
luma cloudflare connect --zone example.com
```

The command verifies the token, finds the zone, and writes `providers.dns.zoneId` back to `luma.yaml`.
Run this before `luma bootstrap manager` when possible. If you connect Cloudflare afterward, rerun manager bootstrap so `/opt/luma/luma.yaml` and `/opt/luma/control/control.json` are refreshed.

For `cn-edge` services, DNS defaults to the public IP of the configured edge node. A service can override this with:

```yaml
dns:
  target: 203.0.113.10
```

## 5. Repair Or Refresh Egress

```bash
luma egress setup
```

Bootstrap already runs this for `single-node` unless `--skip-egress` is used. Run it directly when egress was skipped, failed, or the subscription needs repair. It downloads the subscription, strips it into a minimal Mihomo config, writes it to `/opt/luma/egress-gateway/config.yaml`, deploys `egress_mihomo`, and configures Docker daemon proxy:

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

Service runtime proxy is opt-in per service. Declare `proxy: true` in the service manifest; do not hand-write the egress network or default proxy env unless you need to override them:

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
```

Luma attaches the egress proxy to the service and injects `HTTP_PROXY=http://egress_mihomo:7890` plus `HTTPS_PROXY=http://egress_mihomo:7890` when those env vars are not already set. Scheduling still follows the service `region`.

Refresh subscription output later:

```bash
luma egress refresh
```

## 6. Create A Service

Interactive mode:

```bash
luma service new
```

Manual manifest:

```yaml
name: app
image: ghcr.io/me/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

To pin a service to one machine, add `node` with the Luma node name passed to `luma node join --name`:

```yaml
region: home
node: home-mac-mini
```

## 7. Deploy

Default deploy path:

```bash
luma deploy app.yaml
```

If the application already has a project `.env`, pass it as scoped deployment secrets:

```bash
luma deploy app.yaml --env .env
```

Luma imports only variables referenced by the manifest, stores them under the service `name`, and then resolves `${NAME}` placeholders during manager-side render. This lets different applications reuse common names such as `DATABASE_URL` without colliding.

This submits the manifest to the logged-in Luma Control endpoint. The manager renders generated files under `/opt/luma`, syncs DNS, creates or updates the Nomad job through the Nomad HTTP API, and probes the public route for `cn-edge` and `external-edge` services.

`luma deploy` prints client-side progress and each control-plane step. Luma validates generated Traefik file-provider routes, stages them outside the watched routes directory, then atomically publishes the final route file. A public route probe reports the HTTP status from `/`; an application-level `404` means the route reached the application but the app may not serve a root page, while Traefik's default `404 page not found` is treated as a missing router and a failed public route. When the probe reports the route unhealthy (Traefik router not found, or a transient `502`/`503`/`504`), Control recreates the service's allocation once and re-probes before failing the deploy. The client waits up to 1800 seconds by default because first deploys may pull large images on the target node. Override it when needed:

```bash
luma deploy app.yaml --timeout 3600
```

Repeated deploys are updates. The same service `name` maps to the same Nomad job (the job id is the service slug); running deploy again rewrites the generated jobspec and updates that job. Nomad keeps the previous version, so `luma rollback app` or the dashboard's Applications -> Versions action can return to it. This is a running job rollback, not a Git/manifest rewrite; use pinned image tags or digests for production. Changing `name` creates a different job.

Preview without side effects:

```bash
luma deploy app.yaml --dry-run
```

Submit to the control plane, render/write files on the manager, but skip DNS sync and the Nomad deploy step:

```bash
luma deploy app.yaml --skip-dns --skip-orchestrator
```

Remove a service or Compose application by its deployed name:

```bash
luma service remove app
```

Luma Control uses the manifest recorded during the last successful deploy. This deletes the Cloudflare DNS record for public services, deregisters and purges the Nomad job, and deletes generated manager files such as `/opt/luma/stacks/<region>/<service>/<service>.nomad.json`, `/opt/luma/stacks/compose/<name>/<name>.nomad.json`, and `routes/<service>.yml` for `tailscale-relay`. The same command removes single-service and Compose deployments. Preview first or keep DNS when needed:

```bash
luma service remove app --dry-run
luma service remove app --skip-dns
```

## 8. Exposure Modes

`cn-edge`:

```text
user -> Cloudflare DNS -> CN Traefik -> cn service
```

Use this for domestic public services.

`tailscale-relay`:

```text
user -> Cloudflare DNS -> CN Traefik -> Tailscale -> home service
```

Use this for low-frequency home services that should still share the same public domain experience.

`cloudflare-tunnel`:

```text
user -> Cloudflare -> cloudflared -> service
```

Use this for home services that should not depend on the CN edge.

`external-edge`:

```text
user -> Cloudflare DNS -> global edge -> global service
```

Use this for overseas services that need external network access and a public endpoint.

`none`:

No public entrypoint. Use it for workers and internal services.

## 9. Diagnose

```bash
luma doctor
```

Each failed check includes a concrete fix command or environment variable.

## 10. First Real Smoke Test

Use the reference node first:

```bash
luma doctor
luma bootstrap manager --domain luma.example.com
luma egress setup
luma deploy examples/public-cn-service.yaml
```

Then check:

```bash
nomad job status
curl -I https://whoami.example.com
```

Rotate any token or subscription URL that has been pasted into chat or logs before open-sourcing the repository.

## 11. Build And Deploy From Repository (可插拔)

默认的 `luma deploy` 只部署已经构建好的镜像。如果想直接从 GitHub/Gitea 仓库的源码构建并上线，用 `luma import`：它在集群里的**构建节点**上 clone 仓库、自动发现 `.luma.yml` 或 `luma.compose.yml` 这类部署文件、按 Dockerfile/Compose `build:` 构建镜像、推送到集群内自托管 registry，再走正常部署链路。这一整套是**可插拔的**——不用它，集群和现有部署不受任何影响；用它，只需下面这套一次性接入。

### 接入 SOP（已部署好 Luma 的前提下）

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

### 导入并部署

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

构建节点来自控制面声明的 builder 节点；通常不用传 `--build-node`，只有需要临时覆盖到另一个已声明 builder 时才传。单服务 import 还可用 `--context`（build 上下文目录，默认 `.`）、`--dockerfile`（默认 `Dockerfile`）、`--registry-host`（其它节点拉取用的 registry 主机，默认 `<build-node>:5000`）覆盖仓库里的 `build:` 字段。对 Compose import，`--region` 会覆盖 sidecar 的 region；`--exposure`、`--domain`、`--port` 是单服务覆盖项，会被忽略并打印 warning。Compose 的服务级路由请写在 `luma.compose.yml` 的 `services:` 里。`luma import` 默认等待 `2400` 秒的 build+deploy 响应，用 `--timeout <seconds>` 覆盖。

预声明 builder 节点和内部 registry 默认值，之后 import/build 就能省掉 `--build-node`：

```bash
luma build config --node build-1 --default-node build-1 \
  --registry-host <build-1-tailscale-host>:5000 --push-host localhost:5000
```

`--node` 可重复声明多个 builder；`--default-node` 是 `luma import` 缺省用的 builder；`--registry-host` 是**其它节点**拉镜像的地址，`--push-host` 是**构建节点自身**推镜像的地址（通常 `localhost:5000`）。不带参数运行 `luma build config` 只打印当前配置和各 builder 的就绪/能力表。

构建历史和失败日志用 CLI 查看：`luma build list`（打印 ID/状态/节点/provider/仓库/ref）、`luma build logs <id>`（某次构建的分步日志）。修好凭据或配置后 `luma build retry <id>` 重跑整条 build+deploy；`retry` 也接受 `--env .env` 重新提供 scoped secrets、`--timeout`（默认 2400）。

dashboard 的「创建应用」页顶部也有「仓库导入」入口：选择 Git provider、账户、仓库和 ref；或手填 URL。进度实时显示。

> CN 节点的 `git clone` 会自动走 manager 的 egress 网关（`http://<manager-host>:7890`），和镜像拉取、节点 join 用的是同一个出口；`global` 节点直连不走代理。代理出口由控制面的 egress 配置决定，无需在 `luma import` 上单独指定。

### 升级已部署的应用

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

### 回滚

因为每个历史版本的 jobspec 钉的是各自的 `:<git-sha>`，registry 默认不回收旧镜像，所以回滚拉得回原镜像字节：

```bash
luma history myapp                 # 看版本列表
luma rollback myapp                # 回到上一个版本
luma rollback myapp --to-version <N>
```

dashboard 的 Applications → Versions 也能做同样的回滚。注意这是 Nomad job 版本回退（拉回那个版本钉的镜像），不会改 Git、不会回滚数据库迁移或卷数据。

### 跨架构注意

构建节点是 arm64（比如 Mac mini）而目标运行节点是 amd64 时，必须让构建产出 `linux/amd64`。`.luma.yml` 的 `build.platform` 默认就是 `linux/amd64`，但构建节点要装好 `qemu`/`binfmt` 才能跨架构构建。

### 取消接入

不想再用时，删掉 registry 服务即可（`luma service remove luma-registry`），已经在跑的服务不受影响。`insecure-registries` 配置留在各节点的 docker daemon 里是无害的。
