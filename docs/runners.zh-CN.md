# 构建 Runner {#build-runners}

`luma builder` 管理独立的 `luma-build` 队列，`luma runner` 负责当前机器上的加入与进程管理。Runner 需要 Docker、Buildx 和 Git，无需 Nomad 或 `luma node join`。Linux 使用 systemd，macOS 使用系统级 launchd 服务，并保留执行命令用户的 Docker 环境。

Control 的 **创建应用 → Git 仓库** 表单向同一个队列提交，并在原页面跟随构建日志，无需选择集群构建节点或配置内部 Registry。Control 自动读取 `luma-build` 应用的公网域名（Compose 为 `server` 服务）；Builder 部署在其他地方时，在 Control 设置 `LUMA_BUILD_URL`。Builder 仍需配置目标镜像仓库（例如 ACR）、Git 凭据和 Control 部署目标。

## 加入并启动 {#join-and-start}

在操作员机器上，绑定构建服务地址，并复用已有 Luma 登录态创建 token：

```bash
luma builder use https://build.example.com
luma builder token create my-runner --cache-mode read-write
```

在实际执行构建的机器上运行：

```bash
luma runner join --url https://build.example.com --token lbr_xxx
luma runner status
luma runner logs --follow
```

Join 先检查本机 Docker、验证 token，不会领取排队中的构建任务。通过后才保存配置并默认启动后台服务。关闭 SSH 或终端后进程继续运行，开机自动启动，异常退出自动重启。安装和管理系统服务时，系统可能要求输入 sudo 密码。macOS 上仍需 OrbStack 或 Docker Desktop 引擎运行，因此可能需要用户先登录系统。

可以用 `--token-stdin` 从标准输入读取 token，或使用环境变量 `LUMA_RUNNER_TOKEN`。服务将其保存在 `/opt/luma/runner/config.json`，只允许 runner 的系统用户和 root 读取；系统服务命令中只有配置路径，不包含 token。首次 join 保存执行命令用户的 home、Docker context 和代理设置；同一用户重新 join 会刷新环境变量，start/restart 复用已保存环境。BuildKit 元数据默认保存在 `~/.luma-runner`，可用 `--state-dir` 修改。

## 日常命令 {#daily-commands}

```bash
luma runner stop
luma runner start
luma runner restart
luma runner status --format json
luma runner logs --lines 100
luma builder token list
```

`stop` 停止本机服务并关闭开机自启，保留配置；`start` 复用配置启动并恢复开机自启；`restart` 重启进程。空闲长轮询会在 stop/restart 时中断，无需等待服务端的 15 秒长轮询返回。每台机器管理一份 runner 服务，重复 join 更新这份服务，不会新增另一份。构建期间 stop/restart 会取消本机构建；不希望中断时先等待构建结束。

macOS 服务日志写入 `/opt/luma/runner/logs/runner.log`，按 10 MiB 轮转，保留 5 份备份；Linux 使用 journald。`luma runner logs --follow` 可跨日志轮转继续跟随。

`runner status` 查看的是**本机服务**，`builder token list` 查看的是**服务端观察到的心跳**，在线阈值为 90 秒。服务进程运行不等于网络、认证正常，需要结合日志和远端列表判断。`luma runner run` 明确表示调试或容器使用的前台运行；旧命令 `luma builder runner` 保留为前台兼容入口。

## 恢复启动失败的 Linux 服务 {#recover-a-failed-linux-service}

Luma 0.2.81 修复了旧版 Runner 服务生成的 `WorkingDirectory` 引号。如果 systemd 报 `WorkingDirectory= path is not absolute`，先升级该 Runner 机器的 CLI，再执行 `luma runner start`；它会重写服务定义，保留已保存的 token 和参数。命令从哪个目录执行不影响结果。Linux 日志通过 journald 读取（`luma runner logs`），不使用 macOS 日志文件路径。服务命令失败时会显示具体操作和脱敏后的诊断。

## 迁移已加入的 Runner {#migrate-existing-runners}

原有注册记录和名称继续有效。先升级 runner 机器上的 Luma CLI，并部署新版 builder 服务；对旧前台命令按 Ctrl-C（或停止旧 Docker 容器），再用**原 token** 执行 `luma runner join`。无需新建另一个名称。手动安装的 `luma-build-runner.service` 会在验证成功后替换为 CLI 管理的服务。

如果原 token 丢失，在操作员机器上按原名称轮换：

```bash
luma builder token create my-runner --rotate
```

然后在该 runner 机器上用新 token 执行 `luma runner join`。轮换保留名称和 ID，旧 token 立即失效。有自定义权重、并发数和缓存策略时，轮换命令需同时传入这些参数。旧进程需升级到本次 runner 版本，才能在旧 token 被拒绝后自动退出。

## 删除 Runner {#delete-a-runner}

在 builder 控制台点击**删除**，或运行：

```bash
luma builder token delete my-runner
```

删除会把注册记录和 token 从列表移除，同时取消正在该 runner 上构建的任务，以及明确固定给它的排队任务。已完成的构建历史保留，已构建完成、等待部署的镜像继续走部署队列。新版 builder 启动时会清理历史已吊销记录。旧 `token revoke` 命令和 API 保留为删除兼容别名，不再产生吊销记录。

新版 runner 收到 HTTP 401/403 认证拒绝后，会取消本地工作并正常退出，systemd、launchd 和本文的 Docker 重启策略不会反复拉起它。网络超时和 HTTP 5xx 仍会重试。删除后需要重新接入时，创建新 token 再 join。删除不会清理机器上的 BuildKit 缓存，也不会卸载 Luma。

## 缓存与容器 {#cache-and-containers}

家庭 Mac 上传带宽较小时，可使用只读远端缓存：

```bash
luma runner join --url https://build.example.com --token lbr_xxx --cache-mode read-only --cache-keep-gb 25
```

独立 builder 的缓存亲和等待默认 3 秒（`LUMA_BUILD_AFFINITY_SECONDS`），与 Control 自身的构建调度不同。本地 BuildKit 缓存默认 20 GiB。`--workers` 控制本机工作槽位，token 的 `--concurrency` 限制服务端派发并发数。

Linux 也可以用独立 runner 镜像，以容器方式运行：

```bash
docker run -d --name luma-runner --restart=on-failure --network=host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v luma-runner-state:/var/lib/luma-runner \
  -e LUMA_BUILD_URL=https://build.example.com \
  -e LUMA_RUNNER_TOKEN=lbr_xxx \
  ghcr.io/liutianjie/luma-build-runner:v0.2.76
```

Docker 的 `on-failure` 策略不会在 Docker 引擎或主机重启后自动启动容器。需要主机开机自启时使用 `luma runner join`。如果要求删除后进程保持停止，不要使用 `--restart=always`。

## 升级顺序 {#upgrade-order}

Luma/Control 与独立 builder 分别发版。先升级控制面和 Luma CLI，再部署对应 builder 版本，最后迁移或重启 runner 进程。`runner join` 依赖 builder 的 `/v1/runner/join` 接口，旧服务会收到明确的升级提示。单独升级 Control 不会部署 builder，也不会重启独立 runner。见[发版指南](maintainers/release.zh-CN.md#independent-builder-release)与 [builder 部署文件](../builder/README.md)。
