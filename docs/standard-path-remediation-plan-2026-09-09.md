# Luma 标准路径修复计划

日期：2026-09-09。依据：[标准接入路径审计](standard-path-audit-2026-09-09.md)。
状态：计划，未开始实现。基线 v0.1.316。以下模块名、协议字段和命令是拟议设计，实施时需要契约评审，不表示现有 API。

## 1. 目标、范围与约束

目标：一个新开发者使用自己的域名和机器，按公开文档完成安装、初始化、加入、部署、升级和诊断；正常流程不用编辑 pip/systemd/launchd/Docker 配置，不需要知道维护者使用的隐藏路径。

保留：Nomad 编排、Control API、agent 任务、构建队列、Secret/Registry 凭据、存储 owner 与现有部署协议。不要重写应用发布系统，不做 PPT 特判，不把受管 Shell 当产品接口。

允许用户负责的前置条件：操作系统和 sudo 权限、域名与云凭据、云安全组/私网访问授权。Luma 负责说明、检查、报告不能自动完成的部分；不能替用户静默放宽权限。

边界：不声称任何 OS/架构都支持；先声明支持矩阵，未验证平台明确拒绝或标实验。完全离线节点需要本机恢复入口，不承诺同一个失效 agent 可以远程自救。应用数据库迁移、Control 状态格式降级不做通用自动回滚。

本计划不授权创建付费机器、全量升级、停业务、删除旧环境或迁移数据。实施及发布按后续指令进行。

## 2. 设计必须统一的四个契约（第一阶段先定稿）

### 安装身份

设计统一 InstallationRecord，记录 schema、安装 ID、运行模式、属主 UID/GID、稳定入口、版本目录、解释器、当前/上一版本身份及依赖策略引用。不得保存访问 token。Linux 与 macOS 的存放位置由同一解析器生成，不能多处拼路径；root 服务不能信任普通用户可篡改的任意命令或配置。

识别当前运行解释器/服务来源，PATH/HOME 只能用于旧安装发现，不能覆盖已登记的身份。`python -m`、shim、venv console script 都归一化到同一个安装。开发源码 checkout 与受管节点安装分开：开发模式不允许被 Dashboard 静默改为另一个部署模式。

旧安装单独做 discovery → validate → adopt：识别唯一有效候选时可登记；多个有效候选时明确报告冲突并要求选择。保留旧目录，不自动删除。自定义安装目录继续支持。

### 安装依赖策略

设计 InstallPolicy，覆盖发布渠道、源码/制品源、Python 依赖源、代理、CA/信任策略和超时。配置优先级：显式 Luma 选项 → 已登记 Luma 策略 → 产品公开默认值。主机 pip 配置只作为待导入配置显示，不静默决定受管安装；采用企业源后不得偷偷回退公共源。

首次安装尚不能访问 Control 的配置与加入后集群策略采用同一格式；冲突明确报告或显式采用，不能在 join 后无声改变来源。不得在日志中输出带凭据 URL。

第一版不建设新包代理服务：通过可配置 HTTPS 源、可选企业离线 wheelhouse 和阶段化诊断解决；正常发布默认稳定 release，main/branch 是显式开发选项。版本、来源及摘要写入 operation；摘要验证不冒充发布者签名认证。

### 生命周期 operation

Join：preflight → registered → provisioning → verifying → ready；失败保留 failed/recoverable 阶段与重试信息。

Update：preflight → preparing → validated → switching → verifying → succeeded；切换失败进入 rolling_back → rolled_back 或 rollback_failed。

Operation 至少记录 ID、目标安装 ID、请求/实际版本、阶段、时间、错误类别、脱敏摘要、详细日志引用和恢复动作。失败保留旧版可用与升级成功是两种结果。幂等请求不能派生重复并发任务。

### 单一切换执行器

安装脚本只负责 bootstrap/prepare；CLI、agent、Dashboard 共用生命周期实现，不能分别改服务和重启。候选版本使用最终固定目录创建 venv，避免移动 venv 导致绝对 shebang 失效。稳定入口原子切换，不改动 active/previous 的代码和依赖。

切换由独立于 agent 进程/cgroup 的 supervisor 执行，Linux systemd transient unit、macOS 独立 launchd job 分别实现并实测。超时、断电、Control 重启后从持久记录恢复。

新进程确认必须关联目标安装、目标版本、启动/operation nonce 和切换后的心跳，不能用旧心跳宣布成功。区分短暂 Control 不可达与新 agent 无法启动；有界重试后保留明确的不确定/回滚结果。回滚保留凭据有效性，不把旧 token 文件盲目恢复覆盖新凭据。

## 3. 实施阶段与交付顺序

### 阶段 A：安装与节点生命周期（首个必须完整交付的范围）

A1. 安装身份与历史接管
- 范围：`luma/installer.py`、`luma/agent.py` 的路径解析与服务生成、`luma/cli.py` 的 installer 调用；拟新增 `luma/installation.py`。
- 输出：统一解析、持久化、身份校验、只读迁移预览；旧 `.local` 和 `/opt` 安装的兼容适配。
- 测试：root、普通用户+sudo、HOME 改变、PATH 冲突、python -m、双有效安装、错误所有权、开发 checkout。
- 门禁：同一安装通过任何入口均选中同一个运行环境；无法确定时不修改原服务。

A2. 依赖策略与加入前预检
- 依赖：A1 的策略引用和安装身份。
- 范围：安装脚本、installer、preflight/doctor、配置加载与脱敏日志。
- 输出：解释器/权限/磁盘/依赖源的预检，隔离主机 pip 配置；失败指出下载目标、阶段和处理方式。HEAD/HTTP 200 不能代替实际依赖准备验证。
- 测试：PPT 式坏源、代理失效、TLS 失败、离线源、私有索引、缺 wheel/build backend、不支持的 Python/平台。不得为通过测试绕过 TLS 或追加公共 extra-index。
- 门禁：新环境依赖实际准备成功；失败不触碰 active，用户不需手写 pip.conf。

A3. 候选安装与安全切换
- 依赖：A1、A2。
- 范围：安装脚本、agent update、CLI update、服务 supervisor；拟新增 `luma/node_lifecycle.py`。
- 输出：独立候选目录、锁、可恢复 operation、单一切换/验证/回滚执行器。
- 测试：并发升级、断网、下载中断、磁盘满、安装失败、启动失败、心跳超时、切换时 agent 退出、Control 重启、主机重启后的恢复。
- 门禁：候选安装失败时旧版可用；切换失败时自动恢复旧版或明确 rollback_failed；绝不把仅 pip 成功视为升级成功。active、previous、运行中 operation 引用的版本均禁止清理。

A4. 加入验收及恢复入口
- 依赖：A1–A3 的共同模型。
- 范围：`cmd_node join`、注册/标签/agent API、Dashboard join/status、doctor。
- 输出：加入过程可重试；agent token 缺失不得输出加入完成；Control 验证新身份、心跳及声明角色的最小能力后置 ready。普通节点不要求具备 builder 能力。
- 预检分层：注册前检查不需集群身份的条件；临时注册后检查 Control 返回的私网/Registry 参数；最后验证 agent 回连。Nomad 健康不能替代 agent 健康。
- 拟提供本机 diagnose/repair（默认只读，修复需显式执行）；在线相同诊断通过 agent API。
- 门禁：首次 join、重试 join、失败恢复不产生重复节点或删除已工作的 Nomad 节点；开发者用公开命令恢复，不需编辑配置。

A5. Dashboard/CLI 错误与端到端验收
- 依赖：A4。
- 范围：现有 maintenance/join/节点详情 UI、API types；不另造第二套控制台。
- 输出：显示当前/目标版本、阶段、旧版是否可用、下一步动作；traceback 折叠到详细日志。断线重连显示同一 operation，不能重复提交。
- 门禁：通过 Dashboard API 跑完历史 `/opt` 安装 → 新版升级，并在真实浏览器检查成功/失败/恢复界面。静态与 SSR 测试不算浏览器验收。

### 阶段 B：集群初始化与 manager 升级

B1. Builder/Registry 能力准备
- 依赖：A2、A4。
- 范围：`handle_build_config_set`、join Registry 分发、agent build/pull 能力、现有构建配置 UI。
- 输出：统一 Registry TLS/凭据/信任字段，旧 LAE 命名环境变量只在兼容层读取；新增从 BuildKit namespace 到 Registry push、目标节点 pull 的验证。
- 验证使用独立临时 repository/tag，记录并清理自己创建的资源；不得清理业务镜像。配置修改后对应验证状态失效。
- 门禁：全新集群从公开入口配置成功后可构建并跨节点部署；端点、CA、认证失败在初始化阶段清楚显示。未配置 Builder 的镜像部署场景不能被强制要求购买/创建 Builder。

B2. manager 更新统一服务端门禁
- 依赖：A3、A5；复用现有 Control image prepare 和 update API。
- 输出：服务端维护租约与构建/部署接受逻辑协调，阻止检查后又进入新冲突操作；可选择拒绝或排队，但必须在接口中明确。影像预缓存、回滚信息、健康与路由基线属于同一 operation。
- 活跃工作不强杀；历史僵死记录必须以实际任务/租约证据判定，不能按固定时长随便清除。维护租约可在 Control 重启后恢复且有受控解除途径。
- Manager 本机 supervisor 执行升级/恢复，不能依赖即将被替换的 Control 进程作为唯一执行者。
- 门禁：CLI/Dashboard 不能绕过；升级后版本、摘要、agent 心跳、Control health、路由新增失败均校验。状态格式不兼容禁止自动降级；应用 jobs 不重部署。

### 阶段 C：通用配置与日常维护

C1. observe 参数化
- 范围：`observe/docker-compose.yml`、`observe/luma.compose.yml`、README 与现有 Control 集成。
- 输出：从明确配置得到 Control 域名、manager 身份、端口；移除项目实例值，统一文档和实际监听。保留独立、可选应用的架构。
- 门禁：使用非 itool 域名、非 manager 节点名部署；子路径、鉴权边界、loopback 监听及端口冲突处理验证；无需 SSH 手工修改路由。

C2. 备份与运维收敛
- 范围：复用 `luma.control.maintenance` 和现有 storage governance，不重写一致性备份。
- 输出：统一命令/任务状态与诊断包；先支持显式备份和验证，调度与异机备份另行明确，不以本地 receipt 冒充异地备份成功。敏感备份不能通过一般日志/公开附件下载。
- 恢复仍为本机离线操作，保留新目录、完整性验证、权限与显式确认。应用数据恢复与 Control 备份分开。
- 门禁：隔离环境恢复演练能恢复节点/应用声明及凭据权限；不依赖原 manager 私有路径知识。
- 延后：通用数据库跨节点在线迁移、自动移动活库、HA 控制面，不纳入此次必交范围。

## 4. 拆分与依赖

实现顺序：A1 → A2 → A3 → A4 → A5 → B1 → B2 → C2；C1 无需等待 B 阶段，但独立验收。

每个编号作为可审阅的变更集，尽量新增聚焦测试文件，不继续往 `test_productization.py` 堆全部场景。现有字符串断言保留兼容价值，但关键行为必须通过隔离环境执行验证。没有用户授权不派生并行 agent 或自行发布。

## 5. 从零及失败测试矩阵

| 场景 | 要求 |
| --- | --- |
| 全新受支持 Linux：root / 普通用户+sudo | 安装→manager bootstrap→worker join→发布→升级，不手改系统文件 |
| 历史 `.local` / `/opt` / python -m / 冲突 PATH | 接管唯一安装或明确拒绝，不隐式分叉 |
| macOS+OrbStack | 独立加入、launchd 切换、回连与回滚实测，不由 Linux 测试代替 |
| 公网默认源 / 显式企业源 / 错误主机 pip 配置 | 策略可预测、来源透明；私有策略不泄漏到公网 |
| 下载/解压/依赖失败、磁盘满 | 旧代码、旧依赖、旧入口不变 |
| agent 启动失败/旧心跳仍在 | 不误报成功，有界回滚 |
| Control 网络抖动/进程或主机重启 | operation 可恢复，身份不重复，维护租约不永久卡死 |
| 两个升级请求、升级与部署同时到达 | 锁及幂等规则生效，不靠 UI 防重 |
| 新域名/新节点名/空集群状态 | 不依赖 itool 域名、现有 token、节点名或主机补丁 |

验证层次：单元测试 → 隔离进程/真实 venv 与假包源的集成测试 → 支持 OS 的真实服务管理测试 → 临时集群与浏览器端到端 → 灰度。
CI 使用仓库 `scripts/check-luma.sh` 全门禁；真实机器验收额外记录，不用测试数量代替。

## 6. 发布与迁移

不预先占用版本号；按完成且验收的发布单元 bump。

1. 先部署向后兼容的 Control 协议：新旧 agent 均能报告，未具备新生命周期能力的节点明确标记 legacy；不能要求旧 agent 执行它不支持的新协议。
2. 提供旧 agent 到新生命周期的引导适配：候选准备+独立监督执行；在 `.local` 和 `/opt` 两类旧节点上真实验证。若旧端能力不足，使用公开本机接管命令，不用临时 SSH 编辑。
3. 灰度先选无关键业务的 Linux 节点，再测试 macOS；PPT 等历史节点作为兼容验收样本，不用它替代全新环境验收。
4. 节点生命周期稳定后，再通过新门禁升级 manager；开始前保留备份、旧镜像/版本、健康基线，并确认活跃工作。
5. 最后逐批切换其余在线节点；离线节点标记待迁移，不伪装升级完成。
6. 旧环境仅在确认没有 active/previous/operation 引用且经过保留期后按显式清理计划处理。迁移期间保存可用原环境。

每次发布报告分开写：源码检查、制品发布、安装迁移、agent/Control 激活、真实功能验证。发布成功不代表节点已升级。

## 7. 完成定义

- 新用户按文档完成生命周期，不需要维护者提供节点专用路径/镜像源补丁。
- CLI 和 Dashboard 使用相同的 operation、锁、校验及结果语义。
- 错误提前暴露，失败不破坏现有可用环境；必要时用标准恢复命令修复。
- 不再有部署模板写死本团队域名/节点名；外部前置权限清楚列出。
- 旧节点有明确迁移路径；不以删除历史环境、重装服务器来规避兼容测试。
- 每项真实验收有记录；未实测平台/灾难恢复边界明确保留，不能宣称全平台无条件可用。


## 8. 实施记录（2026-09-09，未发布）

此工作区变更是阶段 A 的第一批，**不是阶段 A 或整体计划验收完成**。

| 项目 | 本批源码落地 | 尚未完成 |
| --- | --- | --- |
| A1 | 基于 sys.prefix 的身份解析、候选 runtime 内原子身份记录、历史 .local/其他 venv 布局保持、冲突配置拒绝、记录格式/所有权检查 | 多安装冲突检测、显式接管、特权目录链信任、服务引用全面对账 |
| A2 | 统一 pip 子进程策略、隔离主机 pip/Python 设置、单一 HTTPS 索引/离线 wheelhouse/CA、策略随安装保存、构建/包失败即停止、doctor --local | 注册前磁盘/权限/平台/真实网络预检、认证企业源、稳定版本默认解析 |
| A3 | 固定候选目录、真实 prepare 文件锁、验证后原子 shim 发布、agent 更新延后 installer 内部 service refresh | 独立 supervisor、切换全周期锁、持久 operation、启动失败自动回滚、重启恢复；不得据此宣称升级原子性 |
| A4 | 缺少 agentToken 时失败、新凭据心跳验收、两套 HTTP 栈只读 readiness endpoint、超时保留节点 | 持久 join operation、最小角色能力预检、公开 repair、完整恢复流程 |
| A5/B1/B2/C1/C2 | 本批未实施 | 按原依赖顺序继续，不删除原计划 |

新增开发者说明：`docs/installation-lifecycle.md`。未 bump 版本、未提交/推送、未发布制品、未变更生产节点或业务配置。

验收范围：身份/策略单测、真实 pip 配置隔离、真实空 venv 失败保护、本地归档候选准备、并发 shell installer 排他/失败释放、CLI 加入语义、readiness 的 ASGI 和 legacy HTTP 路由。完整仓库门禁结果以本批最终执行记录为准。

未执行：真实 Linux systemd/macOS launchd 升级及回滚、临时集群从零 onboarding、Dashboard 浏览器验收、生产灰度。现有开发 `.venv` 的历史包 metadata 为 0.1.149 且未安装 python_socks；新增只读诊断能报告这个缺失，即使 pip check 通过。没有为让诊断变绿而删除门禁或改动现网。

本批最终验证记录：`PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh` 退出 0；1296 个 Python 测试、19+48 个 JavaScript 测试通过，版本一致性、CLI 文档同步、Dashboard 类型检查/构建、diff whitespace 门禁通过。最后补充了管道 bootstrap（`curl ... | sh`）兼容、非破坏性锁软链接拒绝和失效身份记录软链接拒绝。安装锁由当前 shell 持有共享文件描述符，无需重新执行不存在的管道脚本文件。以上为本机代码/隔离进程验证，不代表真实节点切换或生产部署验收。


### 发布前 review（2026-09-09，v0.1.317）

修复 review 发现的候选目录回归：升级后 python -m 可能重入旧版本，现改为安装身份中的稳定 shim；macOS /tmp 与 /private/tmp 目录别名导致身份自检失败，现统一目录规范路径且不解析 venv Python 可执行文件。显式 CA 防止被 Requests 环境覆盖，detached manager 更新转发 Luma 依赖策略，带版本的安装示例保证脚本/源码同 ref。

真实隔离安装冒烟测试使用本地源码归档和全新 venv，故意注入错误 PIP_INDEX_URL/PIP_NO_INDEX；依赖安装、CLI version、doctor --local 均通过。此测试不是 systemd/launchd 服务切换或自动回滚证明。此次用户已授权 review 后提交、发布、升级控制面；生产操作按发布顺序另行核验，未完成计划项继续保留。

v0.1.317 发布门禁：1299 个 Python 测试、67 个 JavaScript 测试和完整 scripts/check-luma.sh 均通过。Review 新增旧解释器重入、目录别名一致性、显式 CA 优先级回归覆盖；真实全新 venv 冒烟通过。后续未实施阶段不计入本次完成度。
