# 托管安装与本地诊断 {#managed-installation-and-local-diagnosis}

> v0.1.317 引入了首批托管安装加固。候选环境准备相互隔离。后续变更新增独立节点 agent 切换监督器，新进程无法证明自身是目标运行时时会回滚命令 shim。Manager 更新获取 Control 维护租约，拒绝并发部署/构建，并要求镜像已缓存。本文不代表已完成的全节点迁移流程。

## 安装身份 {#installation-identity}

托管安装在 `<runtime>/luma-installation.json` 记录实际运行时、源码目录、安装根目录、命令目录、所有者 UID、版本及依赖策略。CLI 和 agent 更新优先读取正在运行的 Python 环境（`sys.prefix`），而非 root PATH 中无关命令。常规更新拒绝冲突的显式安装路径。目录别名会规范化，但不会把虚拟环境 Python 可执行文件解析成系统解释器。准备更新后，通过稳定命令 shim 重新执行，不使用保留的旧 Python/源码。

历史用户安装（`~/.local/share/luma/venv`）保留原用户 home 和命令目录；其他既有 venv（如 `/opt/luma-cli`）保留自身根目录与 `bin`。仓库 `.venv` 仍属开发安装，不会被静默接管。这是基于运行时的兼容，不是完整的任意路径/服务接管向导。模糊安装仍需规划中的显式接管和服务路径协调流程。

新托管运行时直接在 `<install-root>/releases/candidate.*/{src,venv}` 最终路径准备，不覆盖活动源码/venv。依赖安装和运行时验证通过后才原子替换命令 shim。候选准备独立于调用者 umask，移除组/其他用户写权限，并在发布前通过 agent 身份验证器回读安装记录。既有环境诊断保持只读，不安全权限会失败。托管包安装失败不回退到源码加残留依赖。安装级锁拒绝并发准备，但不是持久化分布式维护租约，也不保证服务健康。

旧环境会保留。正常更新不要移动 venv（入口可能包含绝对路径）、删除旧安装或手动重定向运行中的服务。首版未包含自动候选保留/清理。

## 依赖策略 {#dependency-policy}

托管安装忽略主机/全局/用户/site pip 配置和继承的 `PIP_*`，也防止外部 Python 模块路径补入候选环境缺失的依赖。不会修改系统 `pip.conf`。

支持的显式输入：

| 输入 | 行为 |
| --- | --- |
| `LUMA_PIP_INDEX_URL` | 单一 HTTPS 包索引，默认 `https://pypi.org/simple`。 |
| `LUMA_PIP_WHEELHOUSE` | 已存在的本地绝对目录，使 pip 禁用索引访问。 |
| `LUMA_PIP_CA_BUNDLE` | 已存在的 CA bundle 绝对文件路径，仍执行正常证书验证。 |

成功安装会保存有效策略，后续 CLI/agent 更新复用。显式 Luma 输入覆盖保存策略，不隐式增加公共索引或 `trusted-host` 回退。拒绝带凭据或查询字符串的 URL、非 HTTPS 索引和缺失本地路径。尚不支持需认证的包索引流程。进程代理设置与包索引选择分开。

已安装 CLI 可为下次更新选择批准的依赖源：

```sh
LUMA_PIP_INDEX_URL=https://packages.example.com/simple luma update --install-ref <release-tag>
```

对于已从**同一**目标版本下载的初始化脚本：

```sh
LUMA_INSTALL_REF=<release-tag> \
LUMA_PIP_INDEX_URL=https://packages.example.com/simple \
sh ./install-luma.sh
```

`LUMA_PIP_WHEELHOUSE` 仅让 **pip** 离线；初始化/源码归档仍需网络或独立提供源码。Wheelhouse 必须包含目标 Python/平台的构建后端和全部传递依赖，不只是 Luma wheel。缺少 wheel/构建依赖会让候选准备失败，不切换索引。

要停用保存的 wheelhouse 或自定义 CA，请在此次更新显式传入空的 `LUMA_PIP_WHEELHOUSE` 或 `LUMA_PIP_CA_BUNDLE`。新环境安装并启动前，活动进程保持原环境。

## 只读诊断 {#read-only-diagnosis}

```sh
luma doctor --local
luma doctor --local --format json
```

报告实际运行时、检测到的安装、有效依赖源、被忽略的 pip 环境变量**键名**、隔离依赖导入和 `pip check`。不包含网络请求、凭据、服务修改或状态写入。即使旧元数据让 `pip check` 通过，导入失败仍非零退出。严重损坏到无法启动 CLI 的环境不能使用此入口，独立恢复仍是规划中的能力。

Control 与节点健康使用 `luma doctor` / `luma doctor --deep`。本地诊断健康不证明 agent 正在运行、服务管理器正常或部署成功。

## 节点加入验收 {#node-join-acceptance}

Control 未返回 agent 凭据时，`luma node join` 不再报告完成。本地服务安装后，最多等待 60 秒，让 Control 确认使用新凭据认证的新鲜心跳/租约。只读 `/v1/node-agent/readiness` 限定对应节点凭据，轮询不能令节点就绪。重新签发凭据会使加入验证失效，即使旧心跳仍新鲜。

凭据缺失、就绪超时或旧 Control 不支持端点时，CLI 返回失败与可操作原因，不删除已注册节点。修复连接/升级 Control 后重试常规加入。应先发布兼容 Control 端点，再使用新加入客户端。这是加入验证，不是规划中的持久化加入操作、完整角色能力预检或更新 nonce 验证。

## 节点 agent 切换 {#node-agent-cutover}

托管安装发布新命令 shim 后，写入 `<install-root>/lifecycle/target.json` 并保留旧 shim。`update-luma` 启动独立 systemd/launchd 监督器（`python -m luma.node_lifecycle switch`），不从运行中任务内部重启 agent。监督器重启节点 agent，等待新进程证明是目标运行时（或新 agent 记录切换 nonce）；证明始终未出现时恢复旧 shim。

这尚不是持久化分布式维护租约；无 systemd-run/launchd 的主机不能使用监督流程。混合版本 agent 未发现 `target.json` 时，沿用进程内服务刷新。

## 尚未覆盖 {#not-yet-covered}

切换中途监督器崩溃恢复、公开接管/修复、控制台生命周期 UI、稳定版本默认解析、Builder/Registry 验证、Manager 维护门禁和 observe/备份标准化仍由 `standard-path-remediation-plan-2026-09-09.md` 跟踪。不能仅凭候选准备测试就判断可安全进行真实升级。