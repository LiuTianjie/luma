# Luma GTM 首次安装可用性计划

## 目标

让一个没有 Luma 经验的新用户，从安装 CLI 到第一次成功部署一个最小 Web 服务，有一条可验证、可恢复、不会依赖隐含人工操作的主流程。所有可选能力（Cloudflare、Tailscale、egress、Registry、LAE）必须明确标记为“必需 / 按需”，并在 Dashboard 中给出配置、验证和故障处理入口。

## 用户主流程与验收标准

```text
安装 CLI
  -> luma preflight
  -> bootstrap manager
  -> 打开 Dashboard
  -> 首次安装预检
  -> 配置必需依赖
  -> 加入第一个节点
  -> 部署 hello-world
  -> 查看公开 URL / 日志 / 回滚
```

每一步都必须有四种结果：成功、缺少输入、验证失败、可重试的暂时失败。错误信息需要包含原因、影响、修复动作和重新检查入口；不能只返回底层命令或容器错误。

## 分阶段实施

### Phase 0：基线与契约（当前阶段）

- 固定首装支持矩阵：单机公网 Manager 为最小路径；多节点、Tailscale、Cloudflare Tunnel、egress、LAE 为按需能力。
- 为模板和组件建立 manifest 元数据：依赖的 Secret、网络、端口、节点角色、Region、健康检查、初始化动作和是否可跳过。
- 为每个依赖定义 `configured / verified / degraded / unavailable` 状态，不用“Secret 名字存在”代替真实验证。
- 将安装、bootstrap、Dashboard、CLI 使用同一份配置契约，避免环境变量、YAML 和 Dashboard 各自维护默认值。

### Phase 1：首装预检与配置向导

- Dashboard 增加“首次安装”页面，按阻塞顺序显示：Control、Nomad、Manager 节点、Registry、DNS、TLS、存储、可选网络能力。
- Cloudflare 向导保存 Token、Zone ID、边缘目标并执行权限与 DNS 写入验证。
- Tailscale 向导保存 Auth Key，并验证 tailnet 加入、地址分配和节点可达性。
- Registry、egress、Traefik ACME 邮箱、LAE principal 作为独立卡片配置；写入后支持重新验证，不回显 Secret。
- 预检结果写入可追踪的检查记录，显示最近检查时间和失败原因。

### Phase 2：节点与组件生命周期

- 节点加入命令由服务端生成，包含当前控制域、可选 Region 和短期 join Token；支持轮换、撤销和重新生成。
- 节点加入完成后自动验证 Agent 心跳、Nomad 注册、角色、Region、资源和终端连接。
- 组件启动前执行依赖检查；依赖缺失时阻止部署并指向对应向导。
- 所有模板移除运行时相关硬编码，改为从 manifest、全局配置或已验证的能力结果渲染。

### Phase 3：最小部署闭环

- 提供内置 `hello-world` 模板，零业务 Secret，支持单节点部署。
- Dashboard 引导用户选择模板、Region、暴露方式，展示最终 manifest 和依赖状态。
- 部署页统一展示构建、调度、健康检查、入口路由和首个 HTTP 验证结果。
- 失败时支持重试、取消和查看对应日志；成功后提供回滚入口。

### Phase 4：发布与运维质量

- 建立干净主机首装验收：Linux manager、单节点、双节点、无 Cloudflare、无 Tailscale、网络受限五种场景。
- 每次 Manager 发布前执行 manifest 校验、Dashboard 类型检查、API 合约测试和最小部署 smoke test。
- 发布后验证 `/v1/health`、Dashboard、节点加入、hello-world URL 和回滚；CLI/镜像版本必须与验证记录关联。
- 对关键操作增加审计记录：配置变更、Token 轮换、节点加入/撤销、部署、回滚。

## 首装故障矩阵

| 阶段 | 可能问题 | 用户看到的结果 | 自动修复或下一步 |
| --- | --- | --- | --- |
| CLI 安装 | Python/venv、权限、下载失败 | 安装前置条件失败 | 给出依赖命令、重试和指定版本方式 |
| Bootstrap | 80/443 被占用、Docker/Nomad 未就绪 | Manager 未 ready | 检查端口、服务状态和可复制修复命令 |
| DNS | Token 权限不足、Zone 错误、edge target 缺失 | DNS 未验证 | 指向 Cloudflare 向导并显示所需权限 |
| TLS | ACME 邮箱或公网入口无效 | HTTPS 未就绪 | 显示 DNS/端口验证和重新签发入口 |
| 节点加入 | Token 过期、版本不兼容、Tailscale 不通 | Agent/Nomad 未注册 | 重新生成 Token、显示兼容版本、检查 tailnet |
| 镜像 | Registry 登录失败、egress 不可用 | 构建或拉取失败 | 区分认证、网络和镜像不存在三类错误 |
| 调度 | Region 无节点、资源不足、角色不匹配 | allocation pending | 显示满足条件的节点和缺失约束 |
| 路由 | Traefik 未发现服务、Cloudflare 记录未生效 | URL 502/404 | 展示 route、DNS、allocation 三段状态 |
| 运行 | 健康检查失败、环境变量缺失 | 服务 unhealthy | 链接到依赖 Secret、日志和重启/回滚 |

## 不接受的状态

- Dashboard 显示“已配置”，但只检查 Secret 名称，没有实际验证。
- 复制的节点命令缺少 Token、Region 或控制域。
- 模板默认值与 bootstrap 或 Dashboard 的默认值不一致。
- 把 Control、Nomad、Traefik、DNS、节点 Agent 的健康状态合并成一个绿色状态。
- 把“任务提交成功”或“镜像构建成功”当作服务已经可访问。
- 失败只能重新安装，不能重试、修复后重新验证或回滚。

## 当前实现与下一步

当前已落地：节点加入命令使用服务端签发的真实 Token 和当前控制域/Region，旧状态缺少 Token 时 Dashboard 会自动补发并持久化；Dashboard 有 Cloudflare、Tailscale、ACME、egress、Registry 的配置检查入口，Cloudflare Zone、Zone ID 和边缘目标会同步回控制配置；服务模板声明依赖和初始化动作；部署预览与真实部署执行前置条件检查；部署失败会记录为 `failed_partial`；部署结果包含结构化的 public-route / Nomad health 状态；提供零 Secret、零 DNS 的 `templates/hello-world.yml` 和 Dashboard 首装模板；`tests/test_first_install_scenarios.py` 和 `scripts/validate-first-install.sh` 提供可重复的首装验收矩阵。

仍需在发布前完成真实环境验收：一台干净 Linux Manager、至少一个加入节点、受限网络环境，以及 Cloudflare/Tailscale 开启和关闭两组路径。验收应使用 Dashboard 的 setup check、节点命令、hello-world 模板、公开 URL 探测和回滚记录，结果关联到发布版本，不能只用单元测试代替。
