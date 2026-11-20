# `@nickname4th/luma-eye`

Zero-dependency **Dual-Track telemetry & Ring Buffer flight-recorder SDK** for **Browser / React / Next.js**, **React Native / Expo (Hermes)**, and custom runtimes.

- **Track 1 (Normal)**: `pageview` / `screen` / `track()` / Web Vitals / `warn`+`error` logs — debounced batching (`fetchLater` + `priority: 'low'` in Browser, `AppState` background flush on React Native).
- **Track 2 (Urgent Crash / Flight Recorder)**: snapshots up to 50 local Ring Buffer breadcrumbs (UI clicks/navigation, `console.debug`, HTTP latency + W3C `trace_id`) into persistent Crash Outbox (`localStorage` / `MMKV` / `AsyncStorage`) and dispatches immediately.
- **1:1 DOM Session Replay (v1.3+)**: automatically lazy-loads `/_e/eye-replay.js` in browser idle time (`0 B` added to initial bundle), masks passwords/tokens/phones/cards automatically (`data-eye-mask` / `data-eye-block` for custom elements), and uploads gzipped DOM mutation segments to `/_e/r`.

## Install

```bash
npm install @nickname4th/luma-eye
```

### CLI & AI Agent Skill

Install globally to manage projects, query production analytics/crashes, upload SourceMaps, or let AI agents (`Claude Code`, `Antigravity`, `Codex`) operate Luma Eye directly:

```bash
npm install -g @nickname4th/luma-eye
luma-eye skills install
luma-eye login --url https://eye.example.com --token <ADMIN_TOKEN>
luma-eye status
```

## 1. Browser / Vite / Next.js / React

```ts
import { initLumaEye, track, captureError, flushFlightRecorder } from '@nickname4th/luma-eye';
import { createReactErrorBoundary } from '@nickname4th/luma-eye/react';
import React from 'react';

initLumaEye({
  endpoint: 'https://eye.example.com',
  key: 'pk_your_project_key',
  project: 'my-web-app',
  env: 'production',
  release: process.env.GIT_SHA,
  traceOrigins: [/^https:\/\/api\./, '/api/'],
  replay: true, // default true; lazy-loads 1:1 DOM Session Replay in idle time
});

// Optional: if your app sets strict CSP `script-src 'self'` blocking external scripts,
// bundle the recorder locally by importing once:
// import '@nickname4th/luma-eye/replay';

export const AppErrorBoundary = createReactErrorBoundary(React);

track('checkout_started', { plan: 'pro' });
```

## 2. React Native / Expo (Hermes)

Supports `ErrorUtils` fatal crash capture, `AppState` background auto-flush + true active foreground duration, sync `MMKV` / async `AsyncStorage` persistent `actor_id` & offline outbox, W3C `traceparent` on `fetch`, and React Navigation / Expo Router screen tracking:

```ts
import {
  initLumaEyeRN,
  createMMKVStorage, // or pass AsyncStorage directly
  attachNavigationContainer,
  track,
  captureError,
} from '@nickname4th/luma-eye/react-native';
import { MMKV } from 'react-native-mmkv';

const mmkv = new MMKV({ id: 'luma-eye' });

initLumaEyeRN({
  endpoint: 'https://eye.example.com',
  key: 'pk_your_project_key',
  project: 'my-rn-app',
  env: __DEV__ ? 'development' : 'production',
  release: '1.0.0+1',
  storage: createMMKVStorage(mmkv),
  traceOrigins: ['https://api.example.com'],
});

// React Navigation integration
// const nav = attachNavigationContainer(navigationRef);
// <NavigationContainer ref={navigationRef} onReady={nav.onReady} onStateChange={nav.onStateChange} />
```

Upload Hermes / Metro `.map` SourceMaps after release builds (iOS `main.jsbundle.map` or Android `index.android.bundle.map`):

```bash
luma-eye sourcemaps upload --project my-rn-app --release "1.0.0+1" ./dist/main.jsbundle.map
```

## 3. Core Primitives (`@nickname4th/luma-eye/core`)

Import `RingBuffer`, `DualTrackTransport`, `createTraceContext`, `createMMKVStorage`, and `createAsyncStorage` directly in custom JS runtimes (Node, Deno, Cloudflare Workers, Electron, Tauri).
