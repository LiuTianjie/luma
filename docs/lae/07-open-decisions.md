# 07. 开放决策与交互问题

本文是产品负责人和研发共同关闭的决策清单。`Confirmed` 是已明确输入；`Recommended default` 允许研发继续，但正式实现前仍需确认。

## 1. 已确认

| ID | 决策 | 状态 | 影响 |
| --- | --- | --- | --- |
| D-001 | LAE 建立在 Luma 上，所有基础设施由 Luma 部署 | Confirmed | Luma 是唯一运行执行底座 |
| D-002 | 文件上传只支持 HTML 或已构建静态产物 | Confirmed | 动态源码/Compose 走 Git/模板，不走普通文件上传 |
| D-003 | GitHub、私有 Git 等需要在缺凭据时引导配置 | Confirmed | 需要 tenant-scoped Git integration |
| D-004 | LAE Agent 有公共端点，调用需要 Web 登录态或 deploy token | Confirmed | 公共入口在 LAE API，Agent 容器不直接裸露 |
| D-005 | 平台保存每个应用的 Luma 部署文件，用户仓库无需包含 | Confirmed | revision 保存 manifest/normalized Compose/sidecar |
| D-006 | 注册自动生成用户级 deploy token，CLI 可看部署流程 | Confirmed | token once-display + hash + NDJSON/SSE |
| D-007 | Lite / Pro / Ultra，月付/年付，支付可先 mock | Confirmed | entitlement/ledger/provider adapter 必须先做 |
| D-008 | 默认随机字符串 `.itool.tech`，不支持用户自定义域名 | Confirmed | wildcard DNS/TLS + domain allocator |
| D-009 | Compose 是一等支持，不只开放静态或单 HTTP | Confirmed 2026-07-11 | 数据/API/UI/配额都按 app->services/routes/volumes 建模 |
| D-010 | 暂时不支持 `tcp-relay` | Confirmed 2026-07-11 | LAE/Luma policy 双层拒绝 TCP/UDP/host port |
| D-011 | 一个 Compose 允许多个公网 HTTP service | Confirmed 2026-07-11 | 一个 primary route，其余服务各获稳定随机域名；route 按套餐计数 |
| D-012 | Lite 允许受管 named volume 和应用内自管数据库 | Confirmed 2026-07-11 | Lite 不是纯静态套餐；仍不承诺托管数据库 SLA |
| D-013 | Dockerfile 与 Compose 对所有用户开放 | Confirmed 2026-07-11 | 不采用邀请制/申请 Beta；安全隔离是公开发布门槛而非用户分层 |
| D-014 | Git 源码拉取、Agent runner 分析与镜像构建统一走 Luma builder | Confirmed 2026-07-11 | LAE Agent 是 controller + builder runner；需要 Builder v2 task 协议 |
| D-015 | AI provider 的 Base URL/API key/model 由平台配置，staging 可映射 ARK；Agent 必须携带版本化 LAE Knowledge Pack | Confirmed 2026-07-11 | 用户不提供模型 key；AI 生成 candidate/解释，确定性校验终审并明确不可部署原因 |
| D-016 | `manager` 是唯一控制面，`aly` 是过时历史名称 | Confirmed 2026-07-11 | 升级、placement、SOP 与资产均不得再把 `aly` 当真实节点 |
| D-017 | Staging 租户 runtime 可落在 `manager + tecent`，manager 可显式兼任 runtime；用户只选择 region，不看具体 placement | Confirmed 2026-07-11 | 内部正向 allowlist、runtime role、容量/volume 门禁和管理员审计；生产仍建议专用 runner |

## 2. P0：会改变总体架构

### Q1. 产品是否面向中国大陆公众运营？

**Recommended default：是，按中国大陆 ToC 公开产品设计。**

影响：ICP/公安备案、内容治理、实名边界、日志/数据 region、支付商户、隐私协议和举报处置。如果只是邀请制/内部平台，可以显著缩小上线合规面。

### Q4. 邮件登录方式？

**Recommended default：邮箱验证码/magic link；后续再加密码。**

备选：邮箱+密码+验证。影响 session、找回、密码策略、客服和邮件送达依赖。

### Q5. 是否第一版开放组织/成员？

**Recommended default：底层 tenant/member/RBAC 完整，UI 只开放个人账户。**

影响：计费归属、token owner、应用转移、审计和邀请流程。

### Q6. 用户源码是否允许发送给外部大模型？

**Recommended default：不发送源码正文或 secret；允许模型处理脱敏、限量的结构化 evidence 和版本化 Knowledge Pack。Production 启用前取得用户同意并记录披露/audit event。**

影响：隐私协议、私有代码安全、global egress、Agent 解释质量和成本。

## 3. P1：影响产品规则与实施优先级

### Q8. 随机域名是否永久稳定？

**Recommended default：绑定 app，在 suspend/update/rollback 时不变；软删除过期后才释放。**

Compose additional route 同样稳定。预览部署域名作为后续能力。

### Q9. 套餐的首批数值和价格？

文档 [05](./05-security-billing-operations.md) 给出了容量测试用草案，不应直接发布。需要确认：

- app/service/route/volume/build/log 限额。
- 月价/年价/年付折扣。
- Lite 是永久免费还是试用期。
- 超额是硬停、禁止新部署还是按量计费。
- Ultra 是自助购买还是销售联系。

### Q10. Volume 删除与欠费语义？

**Recommended default：欠费先 suspend，保留 30 天；用户主动 delete 进入 7–30 天软删除；volume 默认 retain，最终删除前再次通知。**

Lite 已确认允许 volume/应用内数据库。为继续设计，当前暂按“Lite 每日快照保留 3 天、Pro 每日保留 30 天、Ultra 可配置”处理；仍需确认：Lite 是否真包含自动备份、用户自助 restore 次数、最终 retain 时间和数据库 major update 提示。影响存储成本、隐私删除承诺和客服恢复。

### Q11. Update 自动化？

**Recommended default：首版只支持手动 Check update；Pro 后续支持 webhook/定时检测，自动部署需单独开关和 policy。**

Compose 出现 env/route/volume/destructive diff 时永远要求人工确认。

### Q12. 邮件、微信支付、支付宝的真实供应商/商户是否已就绪？

**Recommended default：接口先完成，dev/staging 用 Mailpit + mock payment，production provider 用 feature flag。**

第三方未就绪不阻塞核心部署，但阻塞公开收费和真实注册邮件。

### Q13. LAE 是否新建独立仓库？

**Recommended default：新建 `lae` 产品 monorepo；`infra-stacks` 只改 Luma Core 和保存本设计。**

影响团队并行、release/version、contracts 和部署文件位置。

### Q14. 是否允许新增专用机器？

**Recommended default：至少新增/重分配 dedicated cn builder、runner、stateful；manager 不跑用户 workload。**

当前 home builder 和现有混合节点不满足公开多租户隔离。若暂时不能新增/重分配容量，则 Dockerfile/Compose 能力不能公开上线；产品不退化成长期邀请制能力。

### Q15. 超级管理员的边界？

**Recommended default：Luma Dashboard 可看 tenant/app/usage 与关联 runtime，可发起 LAE action；不能查看 secret 明文、用户 session 或 Git credential。**

需要确认是否需要客服 impersonation；推荐第一版不做，采用只读 support bundle。

## 4. 下一轮建议只回答的问题

为了减少一次性决策负担，下一轮优先回答：

1. 是否面向中国大陆公众运营？
2. 邮件验证码/magic link 还是邮箱+密码？
3. Lite 基础备份是否确认；若确认，保留期和自助恢复次数是多少？
4. 是否允许新增 dedicated builder/runner/stateful 节点？
5. 首版是否只开放个人账户 UI，以及源码是否永不发送外部大模型？

其余问题可以在 Phase 0 ADR 中继续确认，不阻塞当前协议草案。
