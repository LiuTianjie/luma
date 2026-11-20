---
name: luma-eye
description: Integrate Web/React/Next.js and React Native/Expo apps with Luma Eye (`@nickname4th/luma-eye`), inspect production UV/PV/Web Vitals, calculate conversion funnels and retention, debug symbolicated crashes + 50-item Ring Buffer breadcrumbs, search client logs, upload SourceMaps, and manage projects. Trigger whenever the user asks in natural language about 接入埋点, 查线上报错, 看用户访问量/UV/PV, 看转化漏斗/留存, 查客户端日志, 上传 SourceMap, or debugging user-side bugs.
---

# Luma Eye Agent Skill

Control `luma-eye` via the zero-config `luma eye` CLI (reusing `luma login`) or the standalone `luma-eye` CLI with `--json` when fulfilling natural-language requests.

## 1. Provisioning, Authentication & CLI Options

### Provision Luma Eye (if not deployed yet)
Deploy Luma Eye on a Luma cluster with unified Control SSO:
```bash
luma addon install eye --preset node-edge --domain eye.example.com --region global --node edge-1
# Or interactive / all-in-one (also available in Dashboard → Platform → Addons):
luma addon install eye
```

### Zero-Config Native `luma eye` CLI (bundled with `luma-infra`)
When `luma` is already logged in, prefer `luma eye` for common queries — it auto-discovers the deployed Luma Eye URL via Control (`/v1/addons/eye`) and reuses your active `luma login` token with no separate `luma-eye login`:
- `luma eye status`
- `luma eye projects list|ensure|delete`
- `luma eye portfolio --since 24h`
- `luma eye stats <app-slug> --since 24h`
- `luma eye issues <app-slug> --status open` (`--detail <fp>`, `--resolve <fp>`)
- `luma eye logs <app-slug> --level error --search "..." --trace <id>`

Append `--format json` on `luma eye` commands for machine-readable output.

### Standalone `luma-eye` CLI (npm vs Go binary)
For full product analytics (`pages`, `events`, `sessions`, `timeline`, `funnel`, `retention`), SourceMap uploads, and Grafana dashboard rendering, install the npm CLI on a machine with Node.js:
```bash
npm install -g @nickname4th/luma-eye
luma-eye skills install
```

Command coverage note:
- **`@nickname4th/luma-eye` (npm CLI)** supports all `luma-eye` subcommands, including `portfolio`, `pages`, `events`, `sessions`, and `timeline`.
- **Standalone Go `luma-eye` binary** covers `login`, `logout`, `status`, `projects`, `stats`, `funnel`, `retention`, `issues`, `logs`, `sourcemaps`, `grafana`, and `skills`. It does not include `portfolio`, `pages`, `events`, `sessions`, or `timeline` (use the npm CLI `@nickname4th/luma-eye`, or `luma eye portfolio` for cross-project portfolio).

`luma-eye` automatically resolves credentials in priority order:
1. Flags: `--url https://eye.example.com --token <token>`
2. Environment variables: `LUMA_EYE_URL` and `LUMA_EYE_ADMIN_TOKEN`
3. Saved config file: `~/.config/luma-eye/config.json` (or `$LUMA_EYE_CONFIG`, set once with `luma-eye login`)
4. Automatic Luma token fallback (when no Eye-specific token is set): `LUMA_DEPLOY_TOKEN`, then the active `~/.config/luma` context token (`$LUMA_CONFIG_HOME` / `~/.config/luma/contexts/<current>.json` from `luma login`)

Verify connectivity first when unsure:
```bash
luma eye status --format json
# Or with standalone luma-eye:
luma-eye status --json
luma-eye projects list --json
```
If unauthenticated, tell the user to run `luma login` (for `luma eye` and automatic token fallback) or run once:
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
> **CLI note**: `portfolio`, `pages`, `events`, `sessions`, and `timeline` require the npm CLI (`@nickname4th/luma-eye`), with `luma eye portfolio` also available natively via `luma-infra`. The standalone Go `luma-eye` binary covers `login`, `projects`, `stats`, `funnel`, `retention`, `issues`, `logs`, `sourcemaps`, `grafana`, and `skills` (not those 5 commands).

```bash
# 多项目全局总览（requires npm @nickname4th/luma-eye, or use `luma eye portfolio --since 24h --format json`）：
luma-eye portfolio --since 24h --json

# 单项目流量、国家/地区 (top_countries)、渠道/UTM、终端与性能概览（also `luma eye stats <app-slug> --since 24h --format json`）：
luma-eye stats <app-slug> --since 24h --json

# 分页面 PV/UV、真实前台停留时长与 Web Vitals P75（requires npm @nickname4th/luma-eye）：
luma-eye pages <app-slug> --since 24h --json

# 查看实时埋点事件流 (pageview / custom) 或用户会话列表（requires npm @nickname4th/luma-eye）：
luma-eye events <app-slug> --since 24h --json
luma-eye sessions <app-slug> --since 7d --json

# 回放单个用户或单次会话的完整操作时间线，含崩溃前 50 步点击面包屑（requires npm @nickname4th/luma-eye）：
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
