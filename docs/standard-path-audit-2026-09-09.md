# Luma 标准接入路径审计（2026-09-09）

## 范围与判定

审计基线：v0.1.316（467f11c0）。本轮只读核查代码、文档、Control 状态，未修改线上配置。
现场：Control 0.1.316，10 个注册节点中 9 个 ready/0.1.316，blg 离线；29 个应用记录。Builder 和 Registry 由 Control 保存，分别为 builder 与 100.66.177.70:5000；地址是部署实例配置，不是仅因出现私网 IP 就判作硬编码。

目标不是禁止 SSH：首次在主机执行安装、云安全组授权、灾难恢复本来就可能需要主机入口。问题是正常加入、升级、部署与诊断是否需要维护者知道隐藏路径并手工编辑配置。在线 agent 应通过受管 API 执行；agent 不可用时须有本机恢复命令，不应让用户自己拼修复脚本。

本报告不是从零集群的实测认证。除本次 PPT 升级及 manager 升级外，其余为代码审计或当前状态；历史迁移记录不等于当前挂载检查。未全面审计独立 LAE 产品。

## 已确认缺口

### P0：安装身份依赖目录猜测，加入时未建立稳定契约

证据：`luma/agent.py:_install_layout_from_executable/_current_install_layout/_installed_luma_executable` 识别特定 `.local` 目录，再回退 HOME/PATH；`install_node_agent` 写入凭据和轮询参数，但不保存安装身份。`python -m luma.cli` 的入口、`/opt/luma-cli`、sudo 后 HOME 变化并非等价路径。

现场：PPT 的有效环境在 `/opt/luma-cli`，升级却准备 `/root/.local/share/luma/venv`。原先手工改 ExecStart 和 shim 是运维绕行，不是标准入网流程。

修复方向：统一解析并持久化安装身份（解释器、安装根、启动入口、属主、安装模式）；加入前验证并登记，CLI/agent/manager 更新共用。非受管安装需明确 adopt/迁移结果，不能悄悄创建另一份。

### P0：依赖下载策略不属于 Luma 配置契约

证据：`scripts/install-luma.sh` 激活 venv 后直接调用 pip；`luma/agent.py:update_luma_install` 代理失败回退直连只移除代理环境，没有改变 pip 源。`luma/cli.py:cmd_preflight` 只检查 Python/pip/venv 等存在性，不验证依赖下载与安装能力。

现场：PPT root 的 pip 源连接超时；只改 Luma venv 的源后，通过相同 fleet API 成功升级。这个 venv/pip.conf 是本次人为补救，不是产品生成的策略。

修复方向：提供统一、持久化的 Luma 依赖源策略；默认和显式企业源有清楚优先级，加入/升级共用。不能盲目全局改 pip，不能未经配置把私有依赖转交任意公网镜像。报告下载阶段、目标主机（脱敏）、失败类别和恢复动作。

### P0：升级仍原地改动，失败保护不等于事务回滚

证据：安装器先删除并替换 `INSTALL_HOME/src`，在既有 VENV_DIR 创建/安装；验证位于这些操作之后。v0.1.316 只保证验证失败不覆盖 shim/service，不还原此前源代码/包变动。脚本与 agent 的升级函数又分别包含刷新服务职责。

修复方向：候选版本独立目录安装，验证后由一个切换执行器替换入口；新 agent 的版本和心跳确认后完成，失败恢复旧指针。升级须互斥、保存阶段并可恢复。保留 v0.1.316 检查作为内部验证，不再当作完整升级事务。

### P0：加入成功标准低于真实可管理标准

证据：`luma/cli.py:cmd_node` 安装 agent 后直接输出 `Node join complete`；缺少 agentToken 时也可能 skip 后输出完成。注册回滚围住 Nomad 安装，而标签/agent 安装后的失败没有同级状态闭环。`install_node_agent` 的成功只是本机命令成功，非 Control 回连成功。

修复方向：明确 registered/provisioning/verifying/ready/failed 阶段；本次身份的首次新心跳和最小能力验证通过才宣告 ready。失败要保留可恢复记录，重试幂等；不要为了清理注册而误删已经工作的 Nomad 节点。

### P1：Builder/Registry 配置存在，但不是完整初始化向导

证据：`handle_build_config_set` 保存节点与 push/pull host；`docs/how-to-use-luma.md` 要求 BuildKit 容器和所有目标节点均可访问 Registry。`handle_control_image_prepare_start` 有镜像缓存与摘要校验。配置写入本身不等于端到端连通测试。Control image prepare 还依赖 `LUMA_LAE_BUILDER_REGISTRY_INSECURE`，普通 Luma 基础能力和 LAE 命名的进程环境相耦合。

判定：我们的 Builder/Registry 地址已经在 Control，不应再 SSH 改这些声明；但新用户仍需要自行拼齐能力准备。

修复方向：声明 builder/registry 后执行实际 build/push/pull 能力验证，节点加入时验证目标拉取能力。TLS/鉴权/信任配置归到统一 Registry 模型，保留旧环境变量兼容。

### P1：manager 升级安全检查尚依赖调用方编排

证据：`handle_manager_update_start` 校验参数并派发任务，未在这个服务端入口建立与构建队列共享的维护锁、强制活动构建检查或路由前后对照。Dashboard 有升级编排，release 文档另要求操作者检查和保存回滚信息。

现场：本次由助手手工查活动构建、记录 job 版本/镜像、保存路由基线，再调升级 API。镜像预缓存本身已有标准接口，不是缺能力。

修复方向：manager 升级作为统一服务端 operation，持有维护互斥、先准备镜像，再执行检查、切换和验证；CLI/Dashboard 只是同一 operation 的入口。不能只在 UI 弹确认框。

### P1：诊断不足以解释安装失败，本机恢复入口不完整

证据：`_agent_node_diagnostics` 主要提供 Docker、镜像拉取、Nomad/CNI；doctor 对离线节点建议重启或重装。`update_luma_install` 汇总末尾日志，PPT 中首要下载错误被后续 traceback 淹没。

修复方向：补安装身份、版本来源、阶段化安装结果和受管诊断导出。在线用 agent；离线提供本机 repair/diagnose 命令，报告哪些操作执行了、哪些仍待验证。永久离线的节点不可能仅靠同一个 agent 自救，不能承诺后台无条件修复。

### P1：observe 发布配置夹带本集群信息

证据：`observe/docker-compose.yml` 固定 `GF_SERVER_DOMAIN=luma.itool.tech`、`GF_SERVER_ROOT_URL=https://luma.itool.tech/grafana`；`observe/luma.compose.yml` 多处固定节点名 manager。README 中 listener 文档与实际 Grafana 3100 配置也需统一。

修复方向：标准安装入口从 Control 域名和 manager 身份生成参数，或强制显式参数；模板不包含我们的域名。用不同域名/节点名验收。固定调度到 manager 角色是合理要求，固定名为 manager 的机器不是。

### P2：备份恢复与数据迁移有工具，但没有完整日常运维入口

证据：`docs/control-storage.md` 有本机 maintenance backup/restore/check，包含一致性检查；不是完全没备份能力。`docs/compose-storage.md` 明确 storage migrate 只打印手工迁移计划，不停止写入或复制数据。

判定：本地存储、adopted、数据 owner 等已有标准模型，不应推倒重做。历史跨机数据迁移确有人工步骤，不能宣称常规声明式部署会自动迁移数据。

修复方向：把日常备份任务、结果和异机备份验证纳入管理入口；恢复保持离线、显式确认和新目标目录。数据库迁移必须可选适配器和停写确认，不做通用自动复制活库。

## 已有标准路径，应直接使用

- 应用发布：CLI/Dashboard → Control → Nomad → agent；本地 build 与 Builder/import 有共享项目身份、部署历史与队列，不需要 SSH 发布业务。
- Secret/Registry 凭据：受管 API；不可回退到编辑线上 .env 或给每台机器复制凭据。
- 节点 region、标签、insecure registry 分发：join/register/label/agent 已有链路，仍需完善验收，不是重新发明 join。
- 镜像缓存：已有 Builder 缓存、摘要核验和 manager 更新 operation；人工 SSH docker pull 不是默认流程。
- 存储：本地卷、owner、adopted/initialize 和旧 NFS 兼容已有模型；不能把历史迁移当作每个新开发者必做的初始化。
- 路由、DNS、HTTPS、egress：已有声明或 CLI 能力；云账号授权、域名、安全组等外部前置条件应说明并检测，不应偷偷改云侧授权。

## 最小整改顺序与验收

1. **节点生命周期**：安装身份 + 依赖策略 + 加入验收 + 单一升级事务 + 可诊断失败。先解决 PPT 暴露的整条链，而非单台节点。
2. **集群初始化/升级**：Builder/Registry 可用性、manager 升级门禁归入服务端，消除仅 Dashboard/操作者保证的约束。
3. **配置可移植与运维**：observe 参数化；备份/诊断标准入口；危险数据迁移保留明确人工确认边界。

验收使用新域名、新 manager 名、新 worker 名、全新 Linux 主机；分别覆盖非 root+sudo、root、历史 /opt 安装、错误 pip 源、下载中断、启动失败、心跳失败。加入后从普通客户端完成部署、Secret 更新、查看日志、节点升级、manager 升级，过程中不手改 systemd/pip/daemon 配置。macOS/OrbStack 独立验收，不用 Linux 成功代替。

本轮未实施上述重构、未发布新版本、未修改现网。所列方向是建议设计，不是当前已具备能力。
