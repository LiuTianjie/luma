# Luma Eye (`luma-eye`)

面向 **Luma 基础设施**与 **Vibe Coding 多项目** 的自托管统一埋点、客户端异常监控（兼容 Sentry SDK + SourceMap 源码还原）、Ring Buffer 黑匣子回放、产品漏斗留存分析与日志查询服务。

---

## 为什么做 Luma Eye（而不是直接硬上原版 Sentry / PostHog）？

| 维度 | 官方 Sentry 自托管 | `luma-eye` |
| :--- | :--- | :--- |
| **容器数与内存** | 25～70 个容器（Kafka + ClickHouse + Snuba + PG + Redis），最低 16–32 GB RAM | **1 个静态 Go 二进制 + 1 个单机调优版 ClickHouse**（边缘网关仅需 **64 MB RAM**） |
| **产品分析能力** | 无（偏异常与 APM） | 原生 `windowFunnel` 转化漏斗、队列留存、PV/UV、UTM、Web Vitals P75 |
| **客户端 SDK 体积与带宽** | 体积较大 | **双轨上报 + Ring Buffer 黑匣子 SDK（`eye.min.js` ~6.1 KB gzipped）**，正常调试面包屑 0 流量 |
| **跨节点流量成本** | 不适合跨节点低成本转发 | 支持 **Edge / Core 分离部署**，`cn-edge` 仅占 64MB 内存，攒批 **Zstd 压缩（缩减 ~90% 出站流量）** 转给家庭/闲置节点 |
| **Luma 融合度** | 完全割裂 | 原生生成 `luma-app:<app>` 标签的 Grafana 看板、推送指标给 `luma-observe`、触发飞书异常卡片 |

---

## 核心架构：双轨上报 + 边缘存算分离

```text
浏览器 / React Native App (@nickname4th/luma-eye 或官方 @sentry/* SDK)
   ├── Track 1 (常规轨道): PV / track() / Web Vitals / warn+error 日志
   │     └─ fetchLater() + AbortController 防抖合并 + priority: 'low' + text/plain (0 次 OPTIONS 预检)
   └── Track 2 (崩溃快轨): 未捕获异常 / React ErrorBoundary / captureError()
         └─ 冻结本地 50 条 Ring Buffer 黑匣子 (点击/路由/console.debug/fetch+trace_id)
            -> 写入 localStorage Crash Outbox -> navigator.sendBeacon 立即抢救发出
                         │
                         ▼ (纯入站流量，响应仅 204 No Content)
        ┌───────────────────────────────────────────────┐
        │ luma-eye --mode=edge (64MB RAM, cn-edge 节点) │
        │ Origin 校验 / 令牌桶限流 / PII 脱敏 / 爬虫过滤 │
        │ 内存攒批 3s -> Zstd 高倍压缩 (带宽降 90%)      │
        └───────────────────────┬───────────────────────┘
                                │ (Tailscale 内网直传 或 同机 All-in-One)
                                ▼
        ┌───────────────────────────────────────────────┐
        │ luma-eye --mode=core + 单容器 ClickHouse       │
        │ • SourceMap v3 压缩存储 + LRU 缓存堆栈反解     │
        │ • Error Storm 报错风暴折叠 (occurrence_weight) │
        │ • 新异常 / 回归异常秒发飞书富文本卡片告警        │
        │ • 内嵌控制台 /console/ + CLI + Grafana 看板     │
        └───────────────────────────────────────────────┘
```

---

## 快速本地验证

```bash
# 构建 JS SDK、Web 控制台和 Go 二进制，并运行完整单元与竞态测试
make test

# 运行真实 Docker ClickHouse + Split Edge/Core 端到端冒烟测试
make e2e
```

---

## 在 Luma 上部署

### 推荐方式：通过 Luma Addon 一键插拔安装（统一 Control SSO，零手改 YAML）

无需手改 compose 文件、无需手工生成和逐个填写内部密钥，也无需再配置第二套 Passkey / 登录密码。默认直接拉取预构建的 GHCR 镜像；如需从本地源码自定义构建，追加 `--source ./eye`：

```bash
# 交互式向导安装（默认使用预构建 GHCR 镜像；自动生成内部密钥、注入 LUMA_CONTROL_URL 并接入 Control 统一单点登录）
luma addon install eye

# 或非交互指定参数（例如 node-edge 独立公网节点直连模式）
luma addon install eye \
  --preset node-edge \
  --domain eye.example.com \
  --region global \
  --node public-node \
  --yes

# 如需基于本地源码自定义构建镜像：
# luma addon install eye --source ./eye

# 后续修改保留天数/存储上限/告警 Webhook（也可在 Control 控制台「插件中心」可视化配置并热更新）
luma addon configure eye --set retentionDays=14 --apply

# CLI 直接复用 `luma login` 凭据与自动发现地址，无需单独 `luma-eye login`
luma eye status
luma eye projects list
luma eye portfolio --since 24h
```

### 方式一：单机一体部署（All-in-One 手动模板）

```bash
# 1. 设置管理令牌与内部中继密钥
luma secret set LUMA_EYE_ADMIN_TOKEN --scope luma-eye
luma secret set LUMA_EYE_RELAY_SECRET --scope luma-eye
luma secret set LUMA_EYE_AUTO_KEY --scope luma-eye         # 可选：自动建档密钥
luma secret set LUMA_EYE_FEISHU_WEBHOOK --scope luma-eye   # 可选：飞书机器人 Webhook

# 2. 部署到 Luma（修改 luma.compose.yml 中的 domain 为你的域名）
luma compose deploy luma.compose.yml
```

### 方式二：零出站浪费的分体部署（`cn-edge` 64MB 边缘 + `home`/`render` 大内存存储）

1. 在家里或闲置大内存节点部署 Core + ClickHouse：
   ```bash
   luma compose deploy deploy/split/luma.core.compose.yml
   ```
2. 在国内边缘节点部署 64MB 无状态采集网关（[deploy/split/luma.edge.yaml](deploy/split/luma.edge.yaml)）：
   ```bash
   luma secret set LUMA_EYE_CORE_URL --scope luma-eye-edge
   luma secret set LUMA_EYE_RELAY_SECRET --scope luma-eye-edge
   luma deploy deploy/split/luma.edge.yaml
   ```

### 方式三：独立公网节点直连部署（`node-edge`，流量不经过 Manager）

使用 [deploy/node-edge/luma.compose.yml](deploy/node-edge/luma.compose.yml)，让公网埋点流量直接通过 `DNS -> 独立节点公网 IP -> 节点 Traefik -> luma-eye`，完全绕开 Manager 带宽与转发：

```bash
# 1. 为独立公网节点开启 node-edge 入口（首次执行一次，需安全组放行 TCP 80/443）
luma node ingress enable <your-public-node> --public-ip <NODE_PUBLIC_IP>

# 2. 若域名托管在阿里云 DNS 或 DNSPod，先添加一次 DNS Zone（Cloudflare 主域名无需此步）
# luma dns zone add example.cn --provider alidns

# 3. 复制为受 gitignore 忽略的本地配置并填入实际节点与域名（与 builder/ 保持一致）
cp deploy/node-edge/luma.compose.yml deploy/node-edge/luma.local.compose.yml
cp deploy/node-edge/docker-compose.yml deploy/node-edge/docker-compose.local.yml
# 修改 luma.local.compose.yml（并将 compose 指向 docker-compose.local.yml）与 docker-compose.local.yml 后部署：
luma compose deploy deploy/node-edge/luma.local.compose.yml
# 或使用本地构建并部署脚本（默认读取 deploy/node-edge/luma.local.compose.yml）：
# bash eye/deploy.sh
```

---

## 让 Vibe Coding 项目接入（30 秒）

### 1. 创建项目并拿 Key
```bash
export LUMA_EYE_URL="https://eye.example.com"
export LUMA_EYE_ADMIN_TOKEN="<your-admin-token>"

luma-eye projects ensure demo-app --origins https://app.example.com
```

### 2. 前端 / React Native 接入（推荐：npm 包 `@nickname4th/luma-eye`）

Web / React / Next.js：
```bash
npm install @nickname4th/luma-eye
```
```ts
import { initLumaEye, track, captureError, flushFlightRecorder } from '@nickname4th/luma-eye';

initLumaEye({
  endpoint: 'https://eye.example.com',
  key: 'pk_demo_...',
  project: 'demo-app',
  env: import.meta.env.MODE,
  release: import.meta.env.VITE_GIT_SHA,
  traceOrigins: ['/api/'], // 自动注入 W3C traceparent 关联后端 Tempo
});

track('upload_code', { files: 12 });
```

React Native / Expo（支持 Hermes `ErrorUtils`、`AppState` 切后台上报、`MMKV`/`AsyncStorage` 离线队列与导航埋点）：
```ts
import {
  initLumaEyeRN,
  createMMKVStorage,
  attachNavigationContainer,
} from '@nickname4th/luma-eye/react-native';
import { MMKV } from 'react-native-mmkv';

initLumaEyeRN({
  endpoint: 'https://eye.example.com',
  key: 'pk_rnapp_...',
  project: 'rnapp',
  release: '1.0.0+1',
  storage: createMMKVStorage(new MMKV({ id: 'luma-eye' })),
  traceOrigins: ['https://api.example.com'],
});
```

或者在纯 HTML 原型中直接加一行 `<script>`：
```html
<script src="https://eye.example.com/_e/eye.js" data-key="pk_demo_..." data-project="demo-app" defer></script>
```
也可以直接把现有 `@sentry/react` / `@sentry/nextjs` / `@sentry/react-native` 的 `dsn` 指向输出的 `sentry_dsn`。

### 3. 构建后上传 SourceMap（支持 Web & RN Hermes `.map`，不暴露给公网）
```bash
luma-eye sourcemaps upload --project demo-app --release "$GIT_SHA" --prefix assets/ ./dist/assets
# React Native Hermes Bundle:
luma-eye sourcemaps upload --project rnapp --release "1.0.0+1" ./dist/main.jsbundle.map
```

### 4. 挂载进 Luma 控制台「应用详情 → 看板」
```bash
luma-eye grafana render --project demo-app \
  --output /srv/luma/data/luma-observe/extensions/dashboards/demo-app-eye.json
```

### 5. AI Agent 直接查询线上问题（CLI / Skill）
详见 [skills/luma-eye/SKILL.md](skills/luma-eye/SKILL.md)：
```bash
luma-eye stats demo-app --since 24h --json
luma-eye funnel demo-app --steps "pageview,upload_code,scan_complete" --json
luma-eye issues demo-app --since 7d --json
luma-eye issues demo-app --detail <fingerprint> --json
luma-eye logs demo-app --level error --since 1h --json
```
