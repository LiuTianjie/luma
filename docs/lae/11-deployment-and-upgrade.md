# LAE 部署、升级与回退手册

## AI Agent Builder 凭据交接

staging bundle 的 `LAE_AGENT_CONTROLLER_TOKEN` 由 Luma secret 注入平台侧
`agent-controller`；同一 bundle 的 `builder-agent-ai.env` 必须通过 root-only
通道复制到 `builder`，并作为 `setup-lae-builder.sh` 的
`--agent-controller-env-file` 参数安装。它会持久化为
`/etc/luma/lae-builder-ai.env`（`0600`），通过 systemd drop-in 注入
`luma-node-agent`，不会被后续 builder setup 重写。

模型 provider 使用 provider-agnostic 的
`LAE_AGENT_LLM_BASE_URL`、`LAE_AGENT_LLM_API_KEY`、`LAE_AGENT_LLM_MODEL`，
并由 Luma secret 注入；当前 staging 将 ARK Key/Model 映射到这三个通用变量。启用 AI 后 runner
需要 bridge egress 访问 HTTPS controller，生产应在宿主防火墙或 egress
proxy 收紧为 controller-only。

staging 当前允许 Builder 通过 HTTPS + scoped static token 访问独立 controller
域名，并在 controller 内使用全局限流、并发上限和 provider circuit breaker。
该 static token 只允许预发布：生产必须将用户入口保持在 LAE API，由 API broker
签发 task-bound 短期凭据；controller 使用 private ingress 或 mTLS，并在 edge 增加
速率限制，不直接暴露为普通公共服务。

本轮外部模型增强仅作为 staging 能力：controller 加载版本化 LAE Knowledge Pack，且只接收确定性结构化
拓扑、依赖、脚本/文件名、端口和环境变量名，不发送源码正文或环境变量值，
但 production 在具备版本化用户 consent、披露记录与 audit event 前必须保持关闭且
fail-closed。生产 sidecar 因此不公开 controller；后续通过 API broker/private
ingress 完成 consent-bound task credential 后才能启用。

> 状态：真实 Luma staging 已完成 import；最终产品 E2E 与故障恢复验收仍在收尾
> 日期：2026-07-11
> 安全边界：本文不包含任何 secret 值，也不表示仓库当前已经部署到生产。

## 1. 当前结论

LAE 的发布不是一次 `compose up`，而是四个有顺序的发布单元：

1. 固定 Git commit，发布与它绑定的 Luma Control `sha-<short-sha>` 镜像；
2. 先升级 manager 的 CLI/Control，再升级 Builder/Runtime 节点 agent；
3. 安装 LAE service principal、broker、plan-signing 和 runtime placement 配置；
4. 由 Luma Builder 从同一 Git ref 构建 LAE 平台镜像，并用显式 staging sidecar import/deploy。

租户应用实际落点由 Luma 内部决定。Staging 当前通过
`LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON=["manager","tecent"]` 做正向准入；
manager 还必须显式标记 runtime，单有 allowlist 不足以绕过 control-plane policy。租户只看到
`cn | global`、route 和状态，不会看到 node、IP、候选池或 failure domain。
生产必须换成至少两个专用 runner，并继续保持这条可见性边界。

本文所有写操作都要求已批准的 staging 变更窗口。当前集群以 `manager` 为唯一
控制面，且 manager 本身也显式承担 staging runtime；`aly` 是已过时、必须跳过的
历史名称，不属于本轮升级目标。平台 staging 明确落在 `lab`，租户 runtime 候选为
`manager + tecent`，
经 `tailscale-relay` 发布并使用 `builder-registry-nfs` 独立 path。专用 production
storage class 和 runner pool 仍是门禁；
未关闭时不要把 staging 步骤改名后当作 production 发布。

截至 2026-07-11，本轮代码候选为 Luma `0.1.171`（719/719），LAE gate 为
351 passed/23 skipped；当前 live fleet 仍为 `0.1.170`。平台 9 个 task、TLS 与
基础 probes 健康，真实注册/token/CLI/template/analysis 已跑；tenant Runtime
deploy/lifecycle 最终 E2E 仍待完成，因此不代表 production-ready。

## 2. 发布输入与不变量

开始前在变更记录中填写以下非敏感值：

```bash
REPO=https://github.com/LiuTianjie/luma.git
BRANCH=codex/lae-foundation
FULL_SHA=<verified-40-character-git-commit>
SHORT_SHA="$(printf '%s' "$FULL_SHA" | cut -c1-7)"
CONTROL_IMAGE="ghcr.io/liutianjie/luma-control:sha-$SHORT_SHA"
CONTROL_URL=https://luma.itool.tech
LAE_API_URL=https://lae-api-staging.itool.tech
STAGING_SIDECAR=lae/deploy/luma/luma.compose.staging.itool.yml
```

必须同时满足：

- `FULL_SHA` 已推到远端，Control workflow 的 `headSha` 与之完全相等；
- manager 和 fleet 安装同一个 `FULL_SHA`，不使用移动中的 branch 安装 CLI；
- Control 使用 `CONTROL_IMAGE`，不使用 `latest`；
- 平台 import 使用固定不再移动的 ref。正式 release 直接使用 `v*` tag；
  预发布可创建并保留 `staging/<short-sha>` tag；
- Analyzer 使用完整 `repository@sha256:...`，Worker 与 Control 两端逐字相同；
- 构建配置固定为 target pull `100.66.177.70:5000`、builder-local push
  `localhost:5000`，二者不能互换；
- production sidecar `lae/deploy/luma/luma.compose.yml` 不参与 staging import。

预发布 ref 示例（这是 Git 写操作，只在候选 commit 已评审后执行）：

```bash
CANDIDATE_REF="staging/$SHORT_SHA"
git tag --annotate "$CANDIDATE_REF" "$FULL_SHA" \
  --message "LAE staging candidate $FULL_SHA"
git push origin "refs/tags/$CANDIDATE_REF"
test "$(git rev-list -n 1 "$CANDIDATE_REF")" = "$FULL_SHA"
```

候选 tag 在变更关闭前不得移动或删除。不要用 `--force` 覆盖它。

## 3. 本地和 CI 门禁

在仓库根目录执行，不部署：

```bash
python scripts/bump-version.py --check
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_control_image_workflow.py'
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_import_compose_sidecar.py'
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_productization.py'
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_lae_luma_deploy_assets.py'

docker compose -f lae/deploy/luma/docker-compose.staging.yml config --no-interpolate
.venv/bin/luma compose validate "$STAGING_SIDECAR" --import-mode --format json
bash -n scripts/setup-lae-builder.sh
sh -n lae/deploy/luma/docker/api-entrypoint.sh \
  lae/deploy/luma/docker/artifact-init.sh \
  lae/deploy/luma/docker/worker-entrypoint.sh \
  lae/deploy/luma/smoke-images.sh
git diff --check
```

`compose validate --import-mode` 已在 Repository Import 语义下注入构建结果，并在
输出中包含 storage validation。不要额外用 `luma storage check` 校验这些仍含
`build:` 的 sidecar；该命令没有 `--import-mode`，会在 image 注入前失败。
`scripts/setup-lae-builder.sh` 必须用 `bash -n`；其余列出的容器脚本用 `sh -n`。

再检查真实集群只读状态：

```bash
.venv/bin/luma version --control-url "$CONTROL_URL"
.venv/bin/luma status --format json
.venv/bin/luma doctor
.venv/bin/luma storage list --format json
.venv/bin/luma registry list --format json
.venv/bin/luma build config
```

任一检查发现历史 `aly` 仍参与 placement、manager node agent/runtime role 不
ready、`lab`/`builder`/`tecent` 不 ready、registry host 为空、
`builder-registry-nfs` 或 `lae-staging-runtime-nfs` 不可用时停止。不要通过删除
node pin、改成 unmanaged volume 或扩大 runtime allowlist 让验证变绿。

构建配置必须逐字为以下值。`registry-host` 面向 `lab`、`manager`、`tecent` 等
target 拉取镜像；`push-host` 面向 builder 本机推送，不能都写成 Tailscale 地址，
也不能都写成 localhost：

```bash
.venv/bin/luma build config \
  --node builder --default-node builder \
  --registry-host 100.66.177.70:5000 \
  --push-host localhost:5000
.venv/bin/luma build config
```

## 4. 发布不可变 Control 镜像

手动 workflow 能从任意授权 branch/tag 构建，但只有匹配 `headSha` 的 run
可以使用：

```bash
gh workflow run control-image.yml --ref "$CANDIDATE_REF"
gh run list --workflow control-image.yml \
  --event workflow_dispatch --limit 10 \
  --json databaseId,headSha,status,conclusion

RUN_ID=<database-id-for-the-matching-head-sha>
gh run watch "$RUN_ID" --exit-status
test "$(gh run view "$RUN_ID" --json headSha --jq .headSha)" = "$FULL_SHA"
test "$(gh run view "$RUN_ID" --json conclusion --jq .conclusion)" = success
docker buildx imagetools inspect "$CONTROL_IMAGE"
```

`workflow_dispatch` 的 topic branch/tag 只得到 `sha-<7-char-sha>`；它不会获得
`latest`。`main` 和 `v*` 的现有 tag 语义保持不变。完整 Luma release 细节见
[Luma Release](../release.md)。

## 5. 生成并安装 staging 配置包

先为 tenant volume 注册 staging 专用定义；数据实际落在 builder NFS 的独立 path，
允许 manager 与 tecent 挂载。这是 staging 选择，不是 production storage：

```bash
luma storage set lae-staging-runtime-nfs \
  --node builder --path /srv/luma \
  --region cn \
  --eligible-node manager --eligible-node tecent
```

再得到已发布且 Builder 可拉取的 Analyzer 完整 digest，并生成一次性 bundle：

```bash
umask 077
BUNDLE_DIR="$HOME/lae-staging-bundle-$SHORT_SHA"
ANALYZER_IMAGE_DIGEST=<registry/repository@sha256:64-hex-digest>
CLUSTER_ID=<cluster-id-from-luma-status>

python lae/deploy/luma/generate-staging-bundle.py \
  --output-dir "$BUNDLE_DIR" \
  --analyzer-image-digest "$ANALYZER_IMAGE_DIGEST" \
  --cluster-id "$CLUSTER_ID" \
  --runtime-storage-class lae-staging-runtime-nfs \
  --runtime-node manager \
  --runtime-node tecent
```

脚本只打印文件名，不打印 secret；目录为 `0700`、文件为 `0600`，且已存在时
拒绝覆盖。禁止 `cat`、日志上传或提交 bundle。通过批准的加密通道把目录复制到
manager 后，只安装 Control 需要的文件：

```bash
BUNDLE_DIR="$HOME/lae-staging-bundle-$SHORT_SHA"
sudo install -d -o root -g root -m 0700 /opt/luma/control
sudo install -o root -g root -m 0600 \
  "$BUNDLE_DIR/lae-builder.token" \
  "$BUNDLE_DIR/lae-runtime.token" \
  "$BUNDLE_DIR/credential-broker.token" \
  "$BUNDLE_DIR/object-broker.token" \
  "$BUNDLE_DIR/lae-admin.token" \
  "$BUNDLE_DIR/lae-builder-principals.json" \
  "$BUNDLE_DIR/lae-runtime-principals.json" \
  "$BUNDLE_DIR/lae-plan-signing.json" \
  /opt/luma/control/
sudo install -o root -g root -m 0600 \
  "$BUNDLE_DIR/lae-control.env" \
  /opt/luma/control/control.env

sudo stat -c '%a %U:%G %F %n' \
  /opt/luma/control/control.env \
  /opt/luma/control/lae-builder-principals.json \
  /opt/luma/control/lae-runtime-principals.json \
  /opt/luma/control/lae-plan-signing.json
```

`lae-control.env` 只有 endpoint、file path、digest 和 policy，不含 token 值；安装
后的固定路径是 `/opt/luma/control/control.env`。它不是 shell script，只允许
Luma `CONTROL_JOB_ENV_ALLOWLIST` 中的严格 `NAME=value` 行，禁止 `source`、`export`、
注释、重复键和 shell 展开。`luma update manager`/Control refresh 会自动安全读取该
文件，因此后续升级不再依赖某个终端是否手工导入环境变量。文件必须是 root-owned
regular file、不得是 symlink，权限只能为 `0400` 或 `0600`；否则更新 fail closed。
当前命令进程中显式提供的 allowlist 环境变量优先于持久文件，可用于受控轮换；两种
来源都会经过同一项级校验，任何非白名单变量都不会进入 Control Nomad Job。

## 6. 升级顺序

先记录 manager 回退点：

```bash
PREVIOUS_JOB_VERSION="$(nomad job inspect -json luma-control | jq -er .Version)"
PREVIOUS_CONTROL_IMAGE="$(nomad job inspect -json luma-control | \
  jq -er '.TaskGroups[] | select(.Name == "luma-control") | .Tasks[] | select(.Name == "luma-control") | .Config.image')"
PREVIOUS_INSTALL_REF=<known-good-release-tag-or-full-commit>
nomad job history -p luma-control
```

在 manager 的 `/opt/luma/luma.yaml` 中保留其现有 canonical node name，并在该
node 的 `roles` 中显式加入 `runtime`；不要把 hostname 强行改写成公共产品字段。
Luma placement 会把该 manager record 映射为稳定内部 alias `manager`，但仍要求
runtime role。随后确认 manager 已安装第 5 节的 Control 持久环境文件再升级。
manager 更新会替换 Control allocation，浏览器 terminal/SSH 转发可能随旧 allocation
断开，所以推荐显式使用安装配置和 detached transaction：

如果 manager 当前 CLI 为 `0.1.168` 或更早、`update manager --help` 还没有
`--detach`，先用候选 tag 的完整 ref 只升级本机 CLI；不要用旧 `main` installer 加
40 字符 SHA：

```bash
curl -fsSL \
  "https://raw.githubusercontent.com/LiuTianjie/luma/refs/tags/$CANDIDATE_REF/scripts/install-luma.sh" \
  | LUMA_INSTALL_REF="refs/tags/$CANDIDATE_REF" sh
luma version --local
```

确认本机 CLI 是本轮 `0.1.171` 候选后再执行：

```bash
export LUMA_CONTROL_IMAGE="$CONTROL_IMAGE"
/home/tao/.local/bin/luma --config /opt/luma/luma.yaml update manager \
  --install-ref "$FULL_SHA" --domain luma.itool.tech --detach

# 命令会打印 0600 log/status 路径；使用该次输出的精确路径，status 为 0 才算成功。
tail -f <printed-log-path>
cat <printed-status-path>

luma version --control-url "$CONTROL_URL"
curl --fail --silent --show-error "$CONTROL_URL/v1/health"
nomad job status luma-control
```

如果 `/opt/luma/control/control.json` 和相关 state 只有 root 可读，普通用户启动的
`--detach` 仍没有足够权限。此时不要 chmod state，也不要复制到用户目录；应由 root
在 manager host 启动一个 host-owned transaction，例如：

```bash
sudo --preserve-env=LUMA_CONTROL_IMAGE systemd-run \
  --unit="luma-manager-update-$SHORT_SHA" --collect \
  /home/tao/.local/bin/luma --config /opt/luma/luma.yaml update manager \
  --install-ref "$FULL_SHA" --domain luma.itool.tech

sudo systemctl status "luma-manager-update-$SHORT_SHA"
sudo journalctl -u "luma-manager-update-$SHORT_SHA" -f
```

`systemd-run` 事务由 host systemd 持有，Control/terminal 重启不会终止它。可执行文件
路径必须使用 manager 上已验证的本轮候选 CLI 实际路径；不要默认改用历史
`/root/.local/bin/luma`。root transaction 完成后还要确认 node-agent 的 systemd
`ExecStart` 仍指向预期的完整安装，且 `luma version --local` 可加载全部依赖。该
root-owned 路径与 `--detach` 二选一，不要同时嵌套。

健康响应的 `capabilities` 必须包含
`repository-compose-sidecar-v1`。如果 `aly` 仍作为历史 Luma 注册出现，确认 Control
已是本版本后才删除该 stale record；如果它已不在节点清单中则直接跳过。新版本会
识别它与 manager 共用的旧 node ID，只删除 stale record，绝不会 drain manager。
旧 Control 上禁止执行这一步，也不要把 `aly` 当成可 SSH/升级的真实节点。

```bash
luma node remove aly
luma status --format json
nomad node status -json | jq '[.[] | select(.Status == "ready") | .ID]'
```

必须确认 manager 仍 ready/eligible、agent ready 且 `aly` 不再出现在 Luma 注册
节点中。随后把同一 Git commit 更新到非 manager agent。

从 `0.1.168` 或更早版本第一次 bootstrap fleet 时有一个兼容性边界：旧 agent 会
从 `main` 取得 installer，再把 40 字符 SHA 当成 branch archive，最终 404。第一次
必须把不可变 staging tag 写成完整 tag ref，让旧 installer 正确取 archive：

```bash
LEGACY_BOOTSTRAP_REF="refs/tags/$CANDIDATE_REF"
luma update fleet --install-ref "$LEGACY_BOOTSTRAP_REF" \
  --timeout 900 --format json
```

确认每个 agent 已到 `0.1.171` 后，新的 installer 会从与安装 ref 相同的位置启动，
后续 fleet 更新必须恢复使用完整 SHA：

```bash
luma update fleet --install-ref "$FULL_SHA" --timeout 900 --format json
luma status --format json
```

这一步不部署应用，但会更新所有 ready、支持 fleet update 的非 manager 节点，
因此仍需变更窗口。协议相关的最小升级集合是 `manager`、`builder`、平台节点
`lab` 和 runtime `manager + tecent`；为了避免下一次调度/故障转移落到旧 agent，
推荐升级全部 online/ready 节点。`aly` 是历史名称，明确跳过，不应让它导致 fleet
change 失败。

必须确认 `builder` 更新成功。显式 sidecar import 有三层保护：CLI 在 build 前检查
Control capability，Control 要求 Builder 回显同一路径，Builder 在 clone 后拒绝
absolute path、`..`、非规范路径、缺失文件和 symlink escape。任何一层版本过旧，
import 都应失败，不允许自动发现 production sidecar。

## 7. 导入并部署 LAE staging

`luma import` 会真实 clone、build、push 和 deploy，没有 preview-only 模式。
本轮真实 staging 已用该链路完成 import；重跑仍是写操作，只能在前述门禁全部
通过后执行。当前 builder 直连外网比 manager egress 稳定，因此 staging 命令必须
显式声明 `--proxy-mode direct`，不能依赖 `auto` 或 shell 中临时 unset proxy：

```bash
luma import "$REPO" \
  --ref "$CANDIDATE_REF" \
  --build-node builder \
  --compose-sidecar "$STAGING_SIDECAR" \
  --env "$BUNDLE_DIR/lae-platform-staging.env" \
  --proxy-mode direct \
  --format ndjson \
  --timeout 3600
```

`--compose-sidecar` 是仓库内 POSIX 相对路径，不是 manager 本地路径。不能与
`--manifest` 同时使用。显式路径不存在或不是合法 Luma Compose sidecar时，
Builder 必须失败，不能回退到 `lae/deploy/luma/luma.compose.yml`。
`direct` 是本次 staging 的已验证选择，不是所有环境的永久默认；如果 production
builder 只能经 egress 出网，应保留 `auto` 并验证 BuildKit 容器里的当前
proxy/`NO_PROXY`。出现旧 `aly` proxy、base pull EOF、内部 HTTP registry 被代理
或 HTTPS 探测时，按 [运维 SOP 3.2](./10-operations-troubleshooting-sop.md#32-repository-importbuildkit-或内部-registry-失败)
处理，不要盲目重复 import。

部署后依次验证：

```bash
luma history lae-platform-staging --format json
luma status --format json

curl --fail --silent --show-error https://lae-api-staging.itool.tech/health/live
curl --fail --silent --show-error https://lae-api-staging.itool.tech/health/ready
curl --fail --silent --show-error \
  https://lae-artifacts-staging.itool.tech/minio/health/ready
curl --fail --silent --show-error --output /dev/null \
  https://lae-staging.itool.tech/
```

然后按 [实施状态](./08-implementation-status.md) 和
[运维 SOP](./10-operations-troubleshooting-sop.md) 完成 email、HTML/ZIP、
public/private Git、单 HTTP、Compose 多 HTTP、volume、环境变量、update-check、
stop/restart/rollback/delete、CLI/Skill、无容量、节点故障、路由/TLS 和恢复 E2E。
Nomad allocation `running` 或单个 HTTP 200 都不是完整验收。

update-check 的验收必须读取终态 Operation 中闭合的 `updateCheck`，确认
`baselineAvailable`、`sourceChanged`、`deploymentPlanChanged`、`changed` 和
baseline/candidate digest 一致；它不能自动切换 current deployment。Luma Dashboard
的 LAE“调度位置”视图也已实现，但必须在本节部署后验证其候选、preferred node 和
实时 Nomad allocation 关联，不能把代码测试视为 staging 证据。

## 8. 回退矩阵

| 失败点 | 首个动作 | 数据边界 |
| --- | --- | --- |
| Control rollout 不健康 | `nomad job revert luma-control "$PREVIOUS_JOB_VERSION"` | 只回退 Control job spec/image，不回退 CLI 或 state 文件 |
| Builder 未回显 sidecar | 停止 import，完成 agent update 后重试 | 不允许绕过检查或改名 production sidecar |
| 平台新版本健康失败 | 回退 `lae-platform-staging` 到记录的上一 Nomad version | PostgreSQL migration/volume 数据不会随 job 自动回退 |
| 首次 staging 部署失败且无旧版本 | 移除失败 deployment，默认保留 storage | 禁止普通回退使用 `--delete-storage` |
| 路由/DNS 短暂切换 | 等待 Operation 和 health grace，再判断 | 不因单次 502 重复 import |

Control 紧急回退：

```bash
nomad job revert luma-control "$PREVIOUS_JOB_VERSION"
nomad job status luma-control
curl --fail --silent --show-error "$CONTROL_URL/v1/health"
```

恢复服务后，再把本地 CLI 和 Control 镜像一起回到已记录版本：

```bash
export LUMA_CONTROL_IMAGE="$PREVIOUS_CONTROL_IMAGE"
/home/tao/.local/bin/luma --config /opt/luma/luma.yaml update manager \
  --install-ref "$PREVIOUS_INSTALL_REF" --domain luma.itool.tech --detach
```

平台已有健康旧版本时：

```bash
luma history lae-platform-staging --format json
PREVIOUS_PLATFORM_VERSION=<verified-healthy-nomad-version>
luma rollback lae-platform-staging \
  --to-version "$PREVIOUS_PLATFORM_VERSION" --format json
```

首次部署没有旧版本时：

```bash
luma service remove lae-platform-staging
```

默认 remove 保留受管数据。不要添加 `--delete-storage`。涉及 migration 的失败先
停止写入并按数据库恢复计划处理，不能把容器回滚描述成数据库回滚。

## 9. 从 staging 到 production

只有以下条件都有证据时，才创建 production change：

- 专用 `lae-core` 和至少两个专用 cn runner 已注册、隔离、压测；
- `lae-cn-postgres`、`lae-cn-artifacts`、registry storage、PITR/object backup
  与 restore drill 通过；
- Builder rootless、egress、临时盘、SBOM/扫描和 Analyzer digest 门禁通过；
- wildcard DNS/TLS、随机 `*.itool.tech`、多 route 和 abuse controls 通过；
- 真实 SMTP 已接入；payment 在 provider 未完成前继续 `disabled`，不能把 mock
  带到 production；
- 完整 staging E2E、故障注入、回滚、取消、GC 与审计通过。

Production 发布使用正式 `v*` Git tag、版本化 Control image或完整 image digest，
以及 `lae/deploy/luma/luma.compose.yml`。不得把
`luma.compose.staging.itool.yml` 的 `lab`、`builder-registry-nfs` path、
`tailscale-relay`、Mailpit、manager runtime opt-in 或 mock billing
复制为生产默认。

因此当前 production 的明确硬阻塞是：专用 `lae-core`、至少两个专用 runtime
runner、独立且完成恢复演练的 PostgreSQL/artifact/registry storage、真实 SMTP，
以及可用的微信/支付宝等真实 payment provider。provider 未就绪时可以保持
`disabled`，但不能把 mock 解释为生产支付能力。

## 10. 后续升级的固定流程

每次升级都按同一顺序执行：

1. 固定 commit、版本和变更范围；
2. 跑 CI/单元/集成/镜像/manifest 门禁；
3. 发布不可变 Control 与 Analyzer/平台镜像；
4. 记录 Control、平台、数据库和 storage 回退点；
5. staging 先升级 manager，再升级 agents，再显式 sidecar import；
6. 完成 API、浏览器、CLI/Skill、lifecycle 和恢复验证；
7. 批准后生产灰度；
8. 观察 SLO/错误/队列/容量，最后关闭 change；
9. 保留 active/rollback image、plan、volume 和数据库恢复点，之后才允许 GC。

任何一步失败都停在当前层回退，不用 full bootstrap 修复普通应用发布，也不通过
暴露节点信息、扩大 token 权限或关闭校验换取“成功”。

每次资源变更还必须 render/plan 检查 `artifact-init`。本轮真实 Nomad 运行证明
`128M` reservation 会 OOM；当前不可下调的配置是
`reservations.memory: 256M`、`limits.memory: 512M`。Nomad 以 reservation 作为
实际内存申请、以 limit 作为 `memory_max`，只提高 limit 不能修复过低 reservation。
