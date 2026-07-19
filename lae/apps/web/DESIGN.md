# LAE Console design language

## Direction: Stillwater Instrument

LAE 不采用常见的 SaaS 卡片墙、霓虹紫渐变或拟聊天机器人界面。控制台的核心隐喻是“静水承载运行中的系统”：深墨绿背景提供湖面纵深，模板像可被发现的浮标，右侧部署仪表则保持工程工具应有的确定性。

情绪目标是安静、可信、精确。艺术感来自材质、留白、字体与运动节奏，不来自装饰性 3D 或夸张粒子。用户应该先记住湖面中的应用形状，随后自然把注意力交给部署状态。

## Visual constants

- Display：Newsreader Variable，低字重，用于产品主张和阶段标题。
- UI/body：Manrope Variable，用于状态、操作和数据。
- Background：`#07110f`，只有低饱和 moss/amber 光晕，不使用纯黑。
- Text：主文字 `#edf3e8`；次要信息按 `#9daaa1 -> #6f7d75` 两级衰减。
- Accent：moss `#9dbd87` 表示健康和可继续；amber `#d0a96e` 只用于引导注意，不表示错误。
- Surfaces：半透明深色材质 + 极低对比边界；避免每个内容块都拥有独立阴影。
- Radius：主场景 24px，操作行 13px，胶囊只用于短状态。

这些值在 `src/app/globals.css` 的 CSS variables 中维护。组件不得另造相近但不一致的主题色。

## Spatial system

桌面端由三层组成：固定品牌顶栏、窄工具轨、内容水域。内容水域先用不对称的大标题建立尺度，再进入约 65/35 的模板湖面与部署仪表。应用列表不是卡片网格，而是一条贴近水岸的横向运行带。

模板位置使用有意的不规则坐标，避免“把圆形图标排成网格”。Compose 多公网 HTTP、带 volume 的 PocketBase 等模板在标签中直接表达能力边界。

小于 760px 时，工具轨沉到底部，主场景纵向排列；模板湖仍保留空间关系，不降级成普通列表。任何 breakpoint 都必须满足 `scrollWidth === clientWidth`。

## Motion identity

Motion personality：Premium。

- Signature easing：`cubic-bezier(0.4, 0, 0.2, 1)`；进入场景可使用更柔和的 `cubic-bezier(0.22, 1, 0.36, 1)`。
- Duration palette：quick 140ms、standard 360ms、slow 620ms。
- Entrance：元素从下方 10–18px 减速落定，禁止重要状态只做 opacity fade。
- Ambient：模板仅做 5px 以内的错相漂浮；背景光斑以 18–24 秒循环，不与操作争夺注意。
- Secondary：模板选中时先扩大 ripple，再提升图标与光泽。
- Deployment narrative：诊断、可部署、构建与上线是同一个仪表中的状态替换，不让用户在多个页面之间失去上下文。

必须尊重 `prefers-reduced-motion`。在 reduced 模式下，流程仍可被操作和理解，连续漂浮与纯装饰旋转被关闭；真实 operation 等待时间与结果语义不因动画偏好改变。

## Interaction rules

1. 模板永远走正常诊断，不能出现绕过 policy 的“一键成功”路径。
2. 上传入口只接受 HTML/ZIP 的静态产物；Dockerfile/Compose 从 Git/模板进入。
3. 私有 Git 文案明确说明短期凭据租约，不出现“保存 token”暗示。
4. 诊断阶段展示固定结构化步骤，不渲染 builder 原始 stdout/stderr。
5. `ready` 才出现主部署动作；`deploying` 时锁定会破坏 operation 的切换动作。
6. 成功态显示稳定随机域名和应用入口；真实接线后必须从 API operation result 获取，不能由前端生成。
7. hover 不是唯一提示；选择状态使用 `aria-pressed`，流程区域使用 `aria-live`。

## Identity portal

`/login` 延续同一材质，但改成 editorial split：左侧只承担产品承诺和信任信号，右侧是唯一操作焦点。邮箱注册与登录共享同一个无密码流程；验证码和 magic link 都使用通用错误文案，避免账户枚举。

默认 deploy token 只在首次注册完成态出现一次，不写入 local/session storage，不进入 URL 或日志；复制必须由用户显式点击。Magic link 把一次性凭据放在 URL fragment，客户端在任何网络请求前用 `history.replaceState` 清除 fragment，再提交验证；所有页面同时发送 `Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 与最小 Permissions Policy。

## Current implementation boundary

当前页面已用真实 API 驱动 session `/v1/me`、tenant-scoped application list/detail、应用 draft、公开 Git analysis、cursor-resumable operation events 与 deployment admission。API 不可用或未登录时只显示诚实空态，不使用 fixture 冒充正在运行的应用；deployment resolver/worker 未就绪时展示公开 503，不用前端计时器伪造成功。

私有 Git 表单已经使用 `connectionId` 契约，但 connection picker 仍需接列表 API；HTML/ZIP 页面暂不读取或上传所选文件，等待 HTTPS object-store 与 task-bound redemption broker 完成后再接 transfer；模板湖仍是产品入口预览，模板 registry/daily smoke 未完成前不会发起假部署。

错误、需要环境变量、配额阻塞和 destructive Compose diff 应作为现有仪表的显式状态加入，而不是另开一套页面风格。
