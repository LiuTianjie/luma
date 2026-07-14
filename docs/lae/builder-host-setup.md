# LAE Builder 主机准备与验收

> 状态：Luma Control `0.1.233` 已 live，当前 `builder` agent 为 `0.1.228`；既有构建链路已完成真实 `--check`、平台 import、四服务 Compose tenant build 和 clean-room CLI/Skill template build。配置幂等、隔离/压力/故障与恢复证据未全部关闭，本文不构成 production-ready 证明。

LAE Builder v2 只在显式准备过的 Ubuntu 专用节点上启用。仓库默认不会让普通 Luma 节点宣告 `builder-analyze-v1` 或 `builder-build-v1`：只有操作员执行准备脚本、所有运行时门槛通过、node agent 重新加载环境后，能力探测才可能成功。

## 1. 脚本边界

入口为 `scripts/setup-lae-builder.sh`，只支持 Linux amd64，并强制要求：

- 主机存在 `ubuntu` 用户且 UID 必须为 `1000`；
- Docker CLI 与 rootless daemon 均不低于 `29.0.0`；脚本不会下载或升级 Docker Engine；
- `dockerd-rootless-setuptool.sh` 已由与宿主 Docker 版本匹配的 rootless extras 包提供；
- runner 必须用完整的 `repository@sha256:<64 hex>` 传入，tag 会被拒绝；
- registry pull host 与 push host 均由操作员显式传入，不包含协议、路径或用户信息；
- BuildKit release asset 的 SHA-256 必须由操作员通过独立可信发布流程取得并显式传入。缺失时 fail closed，脚本不会把“刚下载后现算出的 hash”当作信任根；
- 脚本不接受 deploy token、Git token、registry password 或任何 source credential。

准备脚本会：

1. 安装 Ubuntu 上的 rootless 运行依赖，并核验现有 Docker 版本；
2. 为 `ubuntu` 配置 subordinate UID/GID、linger、rootless Docker 用户服务；
3. 用固定 release 和校验链安装 BuildKit、Syft、Trivy、Cosign、Crane；
4. 启动 rootless BuildKit 用户服务，准备 Trivy DB，并将 digest 固定的 analyzer runner 拉入 rootless Docker；
5. 写入不含秘密的 `/etc/default/luma-node-agent`，重启 node agent；
6. 以追加记录方式写本机 setup audit，并写当前二进制/配置 hash manifest；
7. 立即执行与 `--check` 相同的完整验收，任何门槛不满足都返回非零。

写给 node agent 的最终键名与当前 executor 一致：`LUMA_BUILDER_TASKS_ENABLED`、`LUMA_BUILDER_ANALYZE_IMAGE_DIGEST`、`LUMA_BUILDER_ANALYZE_DOCKER_HOST`、`LUMA_BUILDER_SNAPSHOT_ROOT`、`LUMA_BUILDER_WORK_ROOT`、`LUMA_BUILDER_EXTERNAL_REGISTRIES_JSON`、`LUMA_BUILDER_BUILD_ENABLED`、`LUMA_BUILDER_BUILDKIT_ADDR`、`LUMA_BUILDER_REGISTRY_PULL_HOST`、`LUMA_BUILDER_REGISTRY_PUSH_HOST`、`LUMA_BUILDER_REGISTRY_INSECURE`、`LUMA_BUILDER_ALLOW_ANONYMOUS_REGISTRY` 和 `LUMA_BUILDER_TRIVY_CACHE_DIR`。Control 私有 plan-signing key 不属于 Builder host，尤其不得把 inline `LUMA_LAE_PLAN_SIGNING_KEYS_JSON` 写进此文件；Control 使用独立、`0600` regular file 形式的 `LUMA_LAE_PLAN_SIGNING_KEYS_FILE`。

node agent 仍以 root 创建、拉取、快照化源码，immutable snapshot store 始终保持 `root:root 0700`，不会递归 chown 给 daemon。只有马上运行 analyzer 时，executor 才先后完成 socket `lstat`、runtime directory owner、Linux `SO_PEERCRED` daemon UID/GID、Docker `SecurityOptions=rootless` 和本地 runner repo digest 证明；随后仅把当次 disposable task workspace 交给该 UID/GID。source/input 最终为 owner-only `0500/0400`，output 为 `0700`，bind mount 仍分别声明 readonly/readonly/read-write；容器内 UID 0 只映射到已验证的非 root daemon UID，且仍有 `--cap-drop ALL`、只读 rootfs、无网络和资源限制。runner 完成后 output 收紧为 `0700/0600`，发现 symlink、异常 owner 或 ownership syscall 失败都会 fail closed，部分修改会回滚，最终整个临时 task 目录由 root 清理。

公开镜像解析还可显式配置 `LUMA_BUILDER_EXTERNAL_RESOLVER_PROXY` 与 `LUMA_BUILDER_EXTERNAL_RESOLVER_NO_PROXY`。它们只注入短生命周期、无凭据目录的 `crane digest` 进程；executor 不继承 node agent ambient proxy，不修改 rootless/rootful Docker daemon，也不把代理传入 analyzer 容器。

## 2. 固定工具链与校验链

| 工具 | 固定版本 | 校验方式 |
| --- | --- | --- |
| BuildKit / buildctl | `v0.31.1` | 操作员必须传 `buildkit-v0.31.1.linux-amd64.tar.gz` 的明确 SHA-256 |
| Syft | `v1.46.0` | 固定 checksum file SHA-256，再从已验证 checksum file 取 asset SHA-256 |
| Trivy | `v0.72.0` | 同上 |
| Cosign | `v3.1.1` | 同上 |
| Crane | `v0.21.7` | 同上 |

固定 checksum file hash 已直接写在脚本中。每次 setup 都重新下载 release asset、验证后再安装；不会因为 PATH 中“碰巧已有同名命令”而跳过供应链校验。tar 成员先做路径穿越检查，最终安装版本和二进制 SHA-256 都会再次核验并写入 manifest。

Trivy vulnerability DB 本身是更新数据，不伪装成固定工具版本。脚本从 `ghcr.io/aquasecurity/trivy-db:2` 刷新 DB，检查 `metadata.json`，并把 metadata SHA-256 写入审计证据。生产上仍需用定时任务刷新 DB，并对 freshness/下载失败告警。

## 3. 首次准备

每次 LAE 候选先在 Builder 使用版本化的
`scripts/build-lae-agent-runner.sh` 从完整 Git commit 构建并推送 Analyzer。脚本固定
`linux/amd64`、启用 provenance/SBOM、校验 Buildx metadata digest，并只输出
`repository@sha256:...`；它拒绝 branch、短 SHA、带凭据 URL 和非法 repository。
构建后使用 `scripts/update-lae-builder-runner.sh` 预拉到 rootless Docker，并原子更新
node-agent allowlist。普通发布不需要重跑整套首次 setup。

必须先把含 `EnvironmentFile=-/etc/default/luma-node-agent` 的当前 Luma 版本安装到目标节点，再执行：

```bash
sudo scripts/setup-lae-builder.sh \
  --runner-image '<registry>/<repository>@sha256:<64-hex-digest>' \
  --registry-host '<pull-host>:<port>' \
  --registry-push-host '<push-host>:<port>' \
  --buildkit-sha256 '<buildkit-release-asset-sha256>' \
  --external-resolver-proxy 'http://<operator-egress-host>:<port>' \
  --external-resolver-no-proxy 'localhost,127.0.0.1,100.64.0.0/10' \
  --buildkit-egress-proxy 'http://<operator-egress-host>:<port>' \
  --buildkit-egress-no-proxy 'localhost,127.0.0.1,10.0.0.0/8,100.64.0.0/10,<builder-registry-host>' \
  --registry-insecure
```

说明：

- 仅当内部 registry 确实是 HTTP 或使用不受信任 TLS 时传 `--registry-insecure`。该开关同时约束 pull/push host，并写入 rootless Docker、BuildKit 与 node-agent policy；
- pull/push host 必须分别从当前 Control build config 读取，不能暗中互相推导；它们可以不同，也可以在 registry 直接绑定 Builder Tailscale 地址时相同；
- rootless Docker/BuildKit 中的 `localhost` 是各自的网络命名空间，不是 Builder 宿主机；脚本会拒绝 loopback registry host。Builder 自建 registry 必须使用 Builder 的局域网或 Tailscale 地址；
- 默认允许解析 `docker.io` 与 `ghcr.io` 的外部基础镜像。要收窄或替换，重复传小写、按字典序排列且不重复的 `--external-registry HOST`；
- 大陆 Builder 无法直连公开 registry 时，`--external-resolver-proxy` 只负责 `crane digest`，`--buildkit-egress-proxy` 只负责专用 rootless BuildKit 拉公开基础镜像；两者都不得改用户 Compose、系统 Docker daemon、rootless Docker daemon或 analyzer 网络；
- `--buildkit-egress-no-proxy` 必须覆盖内部 registry 及内网/Tailscale 网段，确保构建产物直推 Builder registry，不绕公网代理；
- rootless BuildKit 需要访问显式 push endpoint，因此专用 Builder 主机不得在该 endpoint 暴露非必要敏感服务。进一步的 Builder 出口/主机防火墙隔离仍是 staging 上线门槛。

脚本只为 rootless Docker 管理本次指定的 insecure registry 条目：它用 `/var/lib/luma/builder/rootless-docker-managed-registries.json` 记录自己拥有的条目，更新时移除旧的 managed 值并保留运维人员原有的其他 Docker daemon 配置。

当前共享 staging 的实时参数是：pull host 与 push host 均为 `100.66.177.70:5000`，内部 registry 使用 insecure HTTP，平台镜像构建使用 direct 网络。旧的 `localhost:5000` push endpoint 已无监听并会连接失败；发布前必须以 `luma build config` 的当前值为准。target node 仍不得使用 Builder loopback。

BuildKit 出口是 Builder 专用 user service 的显式环境，不修改任何 Docker daemon。变更 setup 参数会重写 unit 并重启该 BuildKit；若出现 `short read`、`unexpected EOF`、`ECONNRESET` 或依赖下载异常，先检查 `luma-buildkit.service` 的实际环境、内部 registry 是否命中 `NO_PROXY`，不要只重试 import。代理只解决 Builder 基础镜像/构建网络，不会被写进用户 Compose。

## 4. `--check` 验收

`--check` 使用与 setup 完全相同的显式信任输入，但不安装包、不写持久配置、不拉镜像、不刷新 DB、不重启服务。它会在 Builder work root 中创建并删除一个真实的 rootless bind probe，用同一 digest runner 读取 `0500/0400` source/input 并写入 `0700` output，以证明宿主路径 ownership 与 mount 语义不是纸面配置：

```bash
sudo scripts/setup-lae-builder.sh --check \
  --runner-image '<registry>/<repository>@sha256:<64-hex-digest>' \
  --registry-host '<pull-host>:<port>' \
  --registry-push-host '<push-host>:<port>' \
  --buildkit-sha256 '<buildkit-release-asset-sha256>' \
  --registry-insecure
```

成功必须同时满足：

- 固定版本命令全部可执行，manifest 中的安装后二进制 hash 与当前文件一致；
- Docker socket 和 BuildKit socket 均属于 UID 1000，Docker 明确报告 rootless，BuildKit 有可用 worker；
- buildctl 支持 `--attest`；
- Trivy DB metadata 合法且与 manifest hash 一致；
- runner 的本地 `RepoDigests` 精确包含请求的 digest；
- pull/push registry `/v2/` 端点均可用；当前 Builder v2 只支持 anonymous internal registry，返回 401 会 fail closed；
- systemd unit 已引用 `/etc/default/luma-node-agent`，env 与本次显式参数完全一致；
- audit manifest 中的 runner、BuildKit asset digest、工具 binary hash、Trivy DB metadata hash 和 env hash 全部一致。

## 5. 生成状态与审计位置

| 路径 | 内容 | 是否包含秘密 |
| --- | --- | --- |
| `/etc/default/luma-node-agent` | Builder capability、rootless socket、registry policy、runner digest、工作目录 | 否 |
| `/var/log/luma/lae-builder-setup.log` | setup 开始/安装/配置/结果的追加式本机审计 | 否 |
| `/var/lib/luma/builder/toolchain-manifest.env` | 工具版本、binary SHA-256、DB/env hash、runner/registry policy | 否 |
| `/var/lib/luma/builder/rootless-docker-managed-registries.json` | 脚本管理的 insecure registry host 集合 | 否 |
| `/home/ubuntu/.config/systemd/user/luma-buildkit.service` | rootless BuildKit 用户服务 | 否 |
| `/home/ubuntu/.config/buildkit/buildkitd.toml` | BuildKit registry transport policy | 否 |

真正的 Git/object/registry 凭据仍必须由 Builder task lease 短期兑换，不能写入以上任一路径。

## 6. 尚需 staging 关闭的证据

历史 `--check` 已证明固定工具链、rootless socket、runner digest、registry endpoint 和 bind probe 在当时一致；当前 Control 已把 pull/push 都配置为 `100.66.177.70:5000`，必须用这组实时参数重跑并归档新的 `--check`。脚本落库与单次 `--check` 仍不等于 Builder 已可公开承载用户代码，至少还要完成并保存：

1. setup 首跑与二次幂等重跑的完整输出，并归档本次及后续 `--check` 的 audit/manifest；
2. rootless Docker/BuildKit peer UID、cgroup CPU/内存/PID/磁盘限制和并发压测；尤其要验证 root node-agent 创建的 `0700` 单任务目录能以最小范围 ACL/chown 提供给 UID 1000 的 analyzer bind mount，不能把整棵源码目录改成 world-readable；
3. 多服务 Compose 构建、SBOM、Trivy 扫描、provenance、push、digest pull 的端到端任务证据；
4. registry 不可达、错误 BuildKit checksum、runner tag/错误 digest、Trivy DB 缺失时均 fail closed；
5. Builder 到 manager/Nomad API/cloud metadata/其他租户网络的拒绝证据，以及允许的 Git/基础镜像出口清单；
6. rootless 用户服务重启、宿主重启、node-agent 重启后的自动恢复；
7. Trivy DB 定时刷新与 freshness 告警；
8. registry 从 anonymous 迁移到短期 credential broker 后，删除 `LUMA_BUILDER_ALLOW_ANONYMOUS_REGISTRY=1` 的迁移验证。

因此当前默认状态仍是：**普通节点不启用 Builder；只有操作员显式运行 setup 且验收通过的专用节点才启用本机 Builder env。当前 staging 已真实执行 import 并通过一次 Builder `--check`，但公开多租户 Builder 的剩余门禁未清零，本能力不能标记为 production-ready。**
