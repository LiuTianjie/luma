# 发布 {#release}

Luma 可以直接分发给用户，无需克隆仓库。

## 统一源码验证 {#shared-source-validation}

拉取请求、Python 包发布和 Control 镜像发布使用同一套源码检查。安装 Python 测试依赖和控制台依赖后运行：

```bash
python -m pip install -e ".[test]"
npm ci
bash scripts/check-luma.sh
```

检查涵盖版本同步、生成的 CLI 参考、根目录下全部 `unittest` 测试、控制台行为测试、类型检查、构建和空白字符。修改 CLI 参数后，运行 `python scripts/generate-cli-reference.py` 并提交更新后的参考文档。LAE 使用独立工作区和 CI（`cd lae && make check`）。这些检查验证源码和构建产物，不代表已验证线上集群。两个发布工作流还会在发布镜像或包之前，拒绝与包版本不匹配的发布标签。

## 推荐发布流程 {#recommended-release}

1. 提交需要通过 `luma version` 区分的代码前，先更新包版本：

```bash
python scripts/bump-version.py
```

如果不适合递增补丁版本，可使用 `--minor`、`--major` 或 `--set 0.2.0`。只验证而不修改文件：

```bash
python scripts/bump-version.py --check
```

2. 将仓库推送到 GitHub。
3. 使用 GitHub Actions 构建并发布 Control API 镜像：

```bash
git push origin main
```

`Build Control Image` 工作流发布以下镜像：

- 从 `main` 发布 `ghcr.io/liutianjie/luma-control:latest`
- 从 `main` 发布 `ghcr.io/liutianjie/luma-control:main-<sha>`
- 从 `v*` 标签发布 `ghcr.io/liutianjie/luma-control:<tag>`
- 对任意分支或标签执行获授权的 `workflow_dispatch` 时，发布 `ghcr.io/liutianjie/luma-control:sha-<7-char-sha>`

手动发布标签绑定具体提交，不会给功能分支添加 `latest`，也不会改变现有的 `main-*` 或发布标签渠道。候选引用推送后再运行工作流，并验证已完成运行的 `headSha` 与准备安装的提交一致：

```bash
BRANCH=codex/lae-foundation
git fetch origin "$BRANCH"
FULL_SHA="$(git rev-parse "origin/$BRANCH^{commit}")"
SHORT_SHA="$(printf '%s' "$FULL_SHA" | cut -c1-7)"

gh workflow run control-image.yml --ref "$BRANCH"
gh run list --workflow control-image.yml --branch "$BRANCH" \
  --event workflow_dispatch --limit 5 \
  --json databaseId,headSha,status,conclusion

RUN_ID=<database-id-for-the-matching-head-sha>
gh run watch "$RUN_ID" --exit-status
test "$(gh run view "$RUN_ID" --json headSha --jq .headSha)" = "$FULL_SHA"
test "$(gh run view "$RUN_ID" --json conclusion --jq .conclusion)" = success

CONTROL_IMAGE="ghcr.io/liutianjie/luma-control:sha-$SHORT_SHA"
docker buildx imagetools inspect "$CONTROL_IMAGE"
```

不要只因某次运行最新就选择它；分支并发和重新运行会使这个判断失效。`workflow_dispatch` 可通过 `--ref` 指定非默认分支，但工作流本身必须已存在于仓库默认分支，供 GitHub Actions 使用。

## 候选 Manager 升级与回滚 {#candidate-manager-upgrade-and-rollback}

### 首次从 JSON 升级到 SQLite {#first-json-to-sqlite-upgrade}

首次使用 SQLite 的版本需要一个短暂的 Control 维护窗口。开始更新前，暂停新的部署和构建请求，等待正在运行的构建结束。记录当前 Control 任务定义与镜像、CLI 安装引用、入口基线，并对 `/opt/luma/control`、`/opt/luma/luma.yaml` 及[Control 存储](control-storage.md)中说明的外部配置进行私密备份。

新版 CLI 在角色识别和镜像准备阶段只读取旧配置，不进行导入。预取 Control 镜像后，安装器仅停止 `luma-control` Nomad 任务，等待其分配实例确认终止。出现未知、丢失或仍在运行的实例、Nomad 查询失败或超时，都会中止导入。此次存储切换不会停止 Nomad、Traefik 或应用任务。

旧写入进程退出后，安装器在同级 `control-pre-sqlite-*` 目录保存私密检查点，包含最终旧状态、Control 任务定义及 Luma 配置。随后导入最新 JSON，仅合并安装字段，并以 `AutoRevert=false` 启动新版 Control。待完成切换标记会在更新失败重试时保留这一保护。全新安装及后续 SQLite 到 SQLite 的更新无需为迁移停止 Control；正常兼容更新保留自动回滚。

在执行上述受控更新之前，不要对仍在运行的旧 Manager 执行新的维护命令或手动调用 `load_state()`，这些入口可能初始化 SQLite。如果导入或启动失败，更新器不会自动重启写入 JSON 的镜像。修复报告的问题后，重试兼容版本。

**下方仅回滚任务的命令只适用于状态格式相同的版本之间。切换到 SQLite 后，绝不能直接回退到写入 JSON 的 Control。** 旧版本会读取已经冻结的 JSON，丢失后续状态变更。要恢复到 SQLite 之前的版本，先停止新 Control，单独保留其 SQLite 目录，再将检查点中的旧 `control` 目录及配套配置恢复到新目录。确认没有新写入进程运行后，让旧任务指向恢复后的状态（或替换已停止 Manager 的状态目录），安装已记录的旧 CLI，并运行保存的旧任务定义。这会恢复到检查点时刻，舍弃之后的 Control 变更；恢复客户端前，需要核对检查点之后发生的应用操作。如果必须保留后续变更，应优先使用修复后的 SQLite 兼容版本。

### 兼容版本升级 {#compatible-release-upgrades}

CLI 源码归档和绑定提交的 Control 镜像应使用同一完整提交。`install-luma.sh` 的 `LUMA_INSTALL_REF` 支持完整的 40 位 Git 提交，因此 Manager 和集群更新无需依赖可变的分支名。修改 Manager 之前，在变更记录中保存当前 Nomad 任务版本和镜像：

```bash
PREVIOUS_JOB_VERSION="$(nomad job inspect -json luma-control | jq -er .Version)"
PREVIOUS_CONTROL_IMAGE="$(nomad job inspect -json luma-control | \
  jq -er '.TaskGroups[] | select(.Name == "luma-control") | .Tasks[] | select(.Name == "luma-control") | .Config.image')"
nomad job history -p luma-control
```

还应将已验证可用的 Git 安装引用（通常是当前发布标签）记录为 `PREVIOUS_INSTALL_REF`。然后在 Manager 上更新候选版本。若此次发布的 Control 支持 LAE，请在同一个 shell 中保留已导出的 LAE Control 环境变量：

```bash
FULL_SHA=<verified-40-character-commit>
SHORT_SHA="$(printf '%s' "$FULL_SHA" | cut -c1-7)"
CONTROL_IMAGE="ghcr.io/liutianjie/luma-control:sha-$SHORT_SHA"

export LUMA_CONTROL_IMAGE="$CONTROL_IMAGE"
luma update manager --install-ref "$FULL_SHA" --domain luma.itool.tech

luma version --control-url https://luma.itool.tech
curl --fail --silent --show-error https://luma.itool.tech/v1/health
nomad job status luma-control
```

当前 Manager 更新流程保留 Control 状态和用户任务，但会协调防火墙 TCP 中继端口、带 `edge` 角色 Manager 上的 Traefik、watchdog、已安装配置与状态，以及 `luma-control` Nomad 任务。它不会重启 Docker 或 Nomad、执行出口设置，或重新部署用户应用。应将其视为控制面维护变更，在更新后检查通过之前，保留上述变更前记录。

兼容升级使用 Nomad `AutoRevert`，但运维人员仍须验证公共健康端点和实际运行镜像。如果新 Control 实例不健康，立即恢复之前的 Nomad 任务：

```bash
nomad job revert luma-control "$PREVIOUS_JOB_VERSION"
nomad job status luma-control
curl --fail --silent --show-error https://luma.itool.tech/v1/health
```

首次回滚只恢复 Control 任务定义，不会恢复本地安装的 CLI。服务恢复后，将 CLI 和镜像一起恢复到记录的版本：

```bash
PREVIOUS_INSTALL_REF=<known-good-tag-or-40-character-commit>
export LUMA_CONTROL_IMAGE="$PREVIOUS_CONTROL_IMAGE"
luma update manager --install-ref "$PREVIOUS_INSTALL_REF" --domain luma.itool.tech
```

如果候选 CLI 本身无法执行回滚，先重新安装已知可用的 CLI，再重新刷新 Manager：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | \
  LUMA_INSTALL_REF="$PREVIOUS_INSTALL_REF" sh
export LUMA_CONTROL_IMAGE="$PREVIOUS_CONTROL_IMAGE"
~/.local/bin/luma update manager --install-ref "$PREVIOUS_INSTALL_REF" \
  --domain luma.itool.tech
```

4. 为 `luma-infra` 项目完成一次 PyPI Trusted Publishing 配置：

- 所有者：`LiuTianjie`
- 仓库：`luma`
- 工作流：`pypi.yml`
- 环境：`pypi`

分发包名为 `luma-infra`；安装后的控制台命令仍为 `luma`。

5. 创建标签，发布带版本的镜像、GitHub 归档和 PyPI 包：

```bash
git tag v0.1.360
git push origin main --tags
```

`Publish Python Package` 工作流构建 wheel 和 sdist，运行 `twine check`，并通过 OIDC 使用 `pypa/gh-action-pypi-publish@release/v1` 发布。不要保存长期有效的 `PYPI_API_TOKEN` 密钥。

6. CI 用户使用以下方式安装：

```bash
python -m pip install "luma-infra==0.1.360"
```

交互式使用仍可采用：

```bash
export LUMA_INSTALL_REF=v0.1.360
curl -fsSL "https://raw.githubusercontent.com/LiuTianjie/luma/$LUMA_INSTALL_REF/scripts/install-luma.sh" -o /tmp/install-luma.sh && sh /tmp/install-luma.sh
```

安装器下载该标签的 GitHub 归档，在 `~/.local/share/luma/releases/` 下准备隔离运行环境，安装 Python 包，写入 `~/.local/bin/luma`，并在需要时将 `~/.local/bin` 加入用户的 shell 配置。

### 从控制台升级已发布版本 {#roll-out-a-published-release-from-dashboard}

包发布和 Control 镜像工作流均成功后，打开控制台 → 基础设施 → 节点 → 系统维护。使用不可变标签作为发布引用，并选择相同标签的 Control 镜像。支持的操作顺序是：

1. 记录公共路由基线；
2. 确认更新 Control；已声明的 Builder 首先通过受管出口代理将外部镜像复制到内部镜像仓库，并验证摘要；
3. 镜像准备成功后，由页面启动 Manager 更新并自动重新连接；
4. 验证更新后的自动路由检查结果；
5. 仅更新报告的 agent 版本与目标版本不同的非 Manager 节点；
6. 在同一页面重试失败或中断的镜像、Manager 或节点操作。

Control 持久化镜像准备和集群操作。镜像准备使用 Builder 的 `control-image-mirror-v1` 能力及配置的 `registryHost` / `pushHost`，代理不会暴露给浏览器。Manager 更新运行于独立的临时 systemd 单元，因此刷新 Manager 节点 agent 不会终止由它启动的更新。只有安装器执行完毕，且新心跳报告目标发布版本后，集群节点才被视为更新成功。CLI 更新命令保留用于应急和首次接入；常规发布无需使用。

用户可以卸载本地 CLI：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh
```

使用 `--purge` 同时删除本地配置和登录上下文：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/uninstall-luma.sh | sh -s -- --purge
```

卸载脚本只处理本机 CLI，不会移除服务器运行组件，例如 Docker、Nomad、Traefik、Luma Control、已部署服务或 `/opt/luma`。

默认 Control 镜像为 `ghcr.io/liutianjie/luma-control:latest`。若要完全固定初始化版本，请在初始化 Manager 前，在 `luma.yaml` 中设置：

```yaml
defaults:
  images:
    lumaControl: ghcr.io/liutianjie/luma-control:v0.1.360
```

## 最新版本渠道 {#latest-channel}

早期测试可以安装 `main`：

```bash
curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh
```

这种方式方便，但可复现性弱于标签。正式使用应优先选择版本标签。

CI 应优先使用固定版本的 PyPI 包：

```bash
python -m pip install "luma-infra==0.1.360"
```

## 自定义托管地址或派生仓库 {#custom-host-or-fork}

代码托管于其他位置时，可使用以下环境变量：

```bash
curl -fsSL https://example.com/install-luma.sh | \
  LUMA_REPO_URL=https://github.com/acme/luma \
  LUMA_INSTALL_REF=v0.1.360 \
  sh
```

使用 `LUMA_ARCHIVE_URL` 可完全绕过 GitHub 归档 URL 约定。

## PyPI 包检查 {#pypi-package-checks}

创建标签之前，在本地验证包：

```bash
rm -rf dist
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m venv /tmp/luma-package-test
. /tmp/luma-package-test/bin/activate
python -m pip install dist/*.whl
luma version --local
```

包中包含运行时 stack 模板和控制台静态资源。一行安装命令仍可用于本地预检、创建 venv 和配置 PATH。Linux DNS 修复等主机级变更由 Manager 初始化或节点加入流程负责，单独安装 CLI 不会执行这些变更。