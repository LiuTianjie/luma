---
name: luma-eye
description: Integrate Web/React/Next.js and React Native/Expo apps with Luma Eye (`@nickname4th/luma-eye`), inspect production UV/PV/Web Vitals, calculate conversion funnels and retention, debug symbolicated crashes + 50-item Ring Buffer breadcrumbs, search client logs, upload SourceMaps, and manage projects. Trigger whenever the user asks in natural language about 接入埋点, 查线上报错, 看用户访问量/UV/PV, 看转化漏斗/留存, 查客户端日志, 上传 SourceMap, or debugging user-side bugs.
---

# Luma Eye Agent Skill

Control `luma-eye` directly via the `luma-eye` CLI with `--json` when fulfilling natural-language requests.

## 1. Authentication & Server Connection

If `luma-eye` CLI is not installed yet on a machine with Node.js:
```bash
npm install -g @nickname4th/luma-eye
luma-eye skills install
```

`luma-eye` automatically resolves credentials in priority order:
1. Flags: `--url https://eye.example.com --token <admin-token>`
2. Environment variables: `LUMA_EYE_URL` and `LUMA_EYE_ADMIN_TOKEN`
3. Saved config file: `~/.config/luma-eye/config.json` (set once with `luma-eye login`)

Verify connectivity first when unsure:
```bash
luma-eye status --json
luma-eye projects list --json
```
If unauthenticated, tell the user to run once:
```bash
luma-eye login --url https://<your-eye-domain> --token <ADMIN_TOKEN>
```

## 2. Natural-Language Intent -> CLI Playbook

### A. "帮这个项目接入埋点和错误监控" / Onboard an App
1. Provision or look up the project key:
   ```bash
   luma-eye projects ensure <app-slug> --origins https://<app-domain> --json
   ```
   Extract `project.public_key` (`pk_...`), `ingest_url`, and `sentry_dsn`.
2. Install the multi-platform npm SDK in the app repo:
   ```bash
   npm install @nickname4th/luma-eye
   ```
3. Wire SDK initialization cleanly once at app boot:
   - **Web / React / Vite / Next.js** (`@nickname4th/luma-eye`):
     ```ts
     import { initLumaEye, track, captureError } from '@nickname4th/luma-eye';

     initLumaEye({
       endpoint: '<PUBLIC_URL>',
       key: '<PUBLIC_KEY>',
       project: '<app-slug>',
       env: import.meta.env.MODE,
       release: import.meta.env.VITE_GIT_SHA || 'dev',
       traceOrigins: ['/api/'], // W3C traceparent for Luma Tempo correlation
       // replay: true is enabled by default in v1.3+ (lazy-loads 1:1 DOM Session Replay in idle time).
       // If strict CSP `script-src 'self'` blocks external scripts, add `import '@nickname4th/luma-eye/replay';`.
       // Use `data-eye-mask` or `data-eye-block` on HTML elements displaying plaintext secrets.
     });
     ```
   - **Upgrade Existing App for 1:1 DOM Session Replay**:
     - Zero business code changes required: simply run `npm install @nickname4th/luma-eye@latest` and rebuild/deploy (static `<script src=".../_e/eye.js">` apps upgrade automatically with zero rebuild).
   - **React Native / Expo (Hermes)** (`@nickname4th/luma-eye/react-native`):
     ```ts
     import { initLumaEyeRN, createMMKVStorage, attachNavigationContainer } from '@nickname4th/luma-eye/react-native';
     import { MMKV } from 'react-native-mmkv';

     initLumaEyeRN({
       endpoint: '<PUBLIC_URL>',
       key: '<PUBLIC_KEY>',
       project: '<app-slug>',
       release: '1.0.0+1',
       storage: createMMKVStorage(new MMKV({ id: 'luma-eye' })),
       traceOrigins: ['https://api.example.com'],
     });
     ```
   - **Zero-Build Static HTML**:
     ```html
     <script src="<PUBLIC_URL>/_e/eye.js" data-key="<PUBLIC_KEY>" data-project="<app-slug>" data-env="production" defer></script>
     ```
   - **Existing Sentry SDK**: Keep `@sentry/react` / `@sentry/nextjs` / `@sentry/react-native` and set `dsn: '<SENTRY_DSN>'`.

### B. "看看最近访问量 / 在线人数 / 国家分布 / 性能" / Portfolio, Traffic & Web Vitals
```bash
# 多项目全局总览：
luma-eye portfolio --since 24h --json

# 单项目流量、国家/地区 (top_countries)、渠道/UTM、终端与性能概览：
luma-eye stats <app-slug> --since 24h --json

# 分页面 PV/UV、真实前台停留时长与 Web Vitals P75：
luma-eye pages <app-slug> --since 24h --json

# 查看实时埋点事件流 (pageview / custom) 或用户会话列表：
luma-eye events <app-slug> --since 24h --json
luma-eye sessions <app-slug> --since 7d --json

# 回放单个用户或单次会话的完整操作时间线（含崩溃前 50 步点击面包屑）：
luma-eye timeline <app-slug> --session <session_id> --json
```
Summarizes `active_users_5m`, `unique_visitors`, `pageviews`, `custom_events`, `sessions` (industry-standard 30m idle sliding window + cross-tab shared), `top_countries` (offline DB-IP resolved), `error_count`, `crash_free_rate`, `vitals_p75` (`LCP`, `INP`, `CLS`, `FCP`, `TTFB`), `top_pages`, and `top_events`.

### C. "查一下某个功能转化漏斗 / 七日留存" / Funnel & Retention
```bash
luma-eye funnel <app-slug> --steps "pageview,upload_code,scan_complete" --window 3600 --since 7d --json
luma-eye retention <app-slug> --days 7 --json
```

### D. "查线上报错并帮我修复" / Debug Production Crashes
1. List open issues:
   ```bash
   luma-eye issues <app-slug> --since 7d --status open --json
   ```
2. Inspect full detail for the top `fingerprint` (symbolicated `.ts`/`.tsx` source context lines + 50-item Ring Buffer user clicks/route/console/fetch breadcrumbs):
   ```bash
   luma-eye issues <app-slug> --detail <fingerprint> --json
   ```
3. Check correlated logs if `trace_id` is present:
   ```bash
   luma-eye logs <app-slug> --trace <trace_id> --json
   ```
4. Locate and fix the root cause in source code; once verified/deployed, mark resolved:
   ```bash
   luma-eye issues <app-slug> --resolve <fingerprint> --json
   ```

### E. "查客户端报错日志" / Search Client Logs
```bash
luma-eye logs <app-slug> --level error --search "timeout" --since 1h --json
```

### F. Build/Deploy Hooks: SourceMaps & Luma Grafana Dashboard
```bash
# Upload Web .map files:
luma-eye sourcemaps upload --project <app-slug> --release "$GIT_SHA" --prefix assets/ ./dist/assets

# Upload React Native Hermes .map file (iOS main.jsbundle.map or Android index.android.bundle.map):
luma-eye sourcemaps upload --project <app-slug> --release "1.0.0+1" ./dist/main.jsbundle.map

# Generate Luma Observe dashboard tagged luma-app:<app-slug>:
luma-eye grafana render --project <app-slug> \
  --output /srv/luma/data/luma-observe/extensions/dashboards/<app-slug>-eye.json
```

### G. Delete a Project
```bash
luma-eye projects delete <app-slug> --json
```
