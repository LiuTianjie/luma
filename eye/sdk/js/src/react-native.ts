import { installBreadcrumbCollectors } from "./breadcrumbs";
import { RingBuffer, type Breadcrumb } from "./ring-buffer";
import {
  DEFAULT_SESSION_IDLE_TIMEOUT_MS,
  DEFAULT_SESSION_MAX_DURATION_MS,
  makeId,
  SessionManager,
} from "./session";
import {
  createAsyncStorage,
  createMMKVStorage,
  createMemoryStorage,
  type AsyncStorageLike,
  type LumaStorageAdapter,
  type MMKVLike,
} from "./storage";
import { DualTrackTransport } from "./transport";

export type { Breadcrumb } from "./ring-buffer";
export { RingBuffer } from "./ring-buffer";
export {
  DEFAULT_SESSION_IDLE_TIMEOUT_MS,
  DEFAULT_SESSION_MAX_DURATION_MS,
  SessionManager,
} from "./session";
export {
  createMMKVStorage,
  createAsyncStorage,
  createMemoryStorage,
  type LumaStorageAdapter,
  type MMKVLike,
  type AsyncStorageLike,
} from "./storage";

export type RNAppStateStatus = "active" | "background" | "inactive" | "unknown" | "extension" | string;

export interface AppStateSubscription {
  remove(): void;
}

export interface AppStateLike {
  currentState?: RNAppStateStatus;
  addEventListener(
    type: "change",
    handler: (nextState: RNAppStateStatus) => void
  ): AppStateSubscription | (() => void) | void;
  removeEventListener?: (type: "change", handler: (nextState: RNAppStateStatus) => void) => void;
}

export interface PlatformLike {
  OS?: string;
  Version?: string | number;
  isPad?: boolean;
}

export interface ErrorUtilsLike {
  getGlobalHandler?: () => ((error: unknown, isFatal?: boolean) => void) | undefined;
  setGlobalHandler?: (handler: (error: unknown, isFatal?: boolean) => void) => void;
}

export interface LumaEyeRNConfig {
  endpoint: string;
  key: string;
  project: string;
  env?: string;
  release?: string;
  storage?: LumaStorageAdapter;
  breadcrumbCapacity?: number;
  batchWindowMs?: number;
  maxErrorsPerSession?: number;
  maxOutboxItems?: number;
  sessionIdleTimeoutMs?: number;
  sessionMaxDurationMs?: number;
  autoErrors?: boolean;
  autoAppState?: boolean;
  autoNetworkBreadcrumbs?: boolean;
  captureConsoleLogs?: boolean;
  traceOrigins?: Array<string | RegExp>;
  appState?: AppStateLike;
  platform?: PlatformLike;
  errorUtils?: ErrorUtilsLike;
}

export interface LumaEyeRNClient {
  track(name: string, props?: Record<string, unknown>): void;
  screen(screenName: string, props?: Record<string, unknown>, durationMs?: number): void;
  identify(userId: string, traits?: Record<string, unknown>): void;
  addBreadcrumb(category: string, message: string, data?: Record<string, unknown>, level?: string): void;
  captureError(
    error: unknown,
    context?: { level?: string; tags?: Record<string, string>; extra?: Record<string, unknown>; traceId?: string }
  ): void;
  flushFlightRecorder(reason?: string, extra?: Record<string, unknown>): void;
  log(level: "debug" | "info" | "warn" | "error", message: string, attrs?: Record<string, unknown>, traceId?: string): void;
  flush(): void;
  destroy(): void;
  getRingBufferSnapshot(): Breadcrumb[];
  getForegroundTimeMs(): number;
  getActorId(): string;
  getSessionId(): string;
}

declare const require: ((id: string) => unknown) | undefined;

function tryLoadReactNative(): { AppState?: AppStateLike; Platform?: PlatformLike } {
  try {
    if (typeof require === "function") {
      return (require("react-native") as { AppState?: AppStateLike; Platform?: PlatformLike }) || {};
    }
  } catch {
    // react-native package unavailable (e.g. non-RN test environment)
  }
  return {};
}

function normalizeOS(raw?: string): string {
  switch ((raw || "").toLowerCase()) {
    case "ios":
      return "iOS";
    case "android":
      return "Android";
    case "macos":
      return "macOS";
    case "windows":
      return "Windows";
    case "web":
      return "Web";
    default:
      return raw || "";
  }
}

function normalizeScreen(screenName: string): { path: string; url: string; display: string } {
  const display = (screenName || "").trim() || "Root";
  const path = display.startsWith("/") ? display : `/${display}`;
  return { path, url: `app://${path}`, display };
}

/**
 * Tracks active foreground time in React Native, pausing whenever AppState leaves "active".
 */
export class RNForegroundTracker {
  private accumulatedMs = 0;
  private activeSinceMs: number | null;
  private lastSampleMs = 0;

  constructor(initiallyActive = true) {
    this.activeSinceMs = initiallyActive ? Date.now() : null;
  }

  onAppStateChange(nextState: RNAppStateStatus): void {
    const now = Date.now();
    if (nextState === "active") {
      if (this.activeSinceMs === null) {
        this.activeSinceMs = now;
      }
    } else if (this.activeSinceMs !== null) {
      this.accumulatedMs += Math.max(0, now - this.activeSinceMs);
      this.activeSinceMs = null;
    }
  }

  getTotalMs(): number {
    if (this.activeSinceMs === null) return this.accumulatedMs;
    return this.accumulatedMs + Math.max(0, Date.now() - this.activeSinceMs);
  }

  sampleDeltaMs(): number {
    const total = this.getTotalMs();
    const delta = Math.max(0, total - this.lastSampleMs);
    this.lastSampleMs = total;
    return delta;
  }
}

export function createLumaEyeRN(config: LumaEyeRNConfig): LumaEyeRNClient {
  const rn = !config.appState || !config.platform ? tryLoadReactNative() : {};
  const appState = config.appState ?? rn.AppState;
  const platform = config.platform ?? rn.Platform;
  const errorUtils = config.errorUtils ?? (globalThis as { ErrorUtils?: ErrorUtilsLike }).ErrorUtils;

  const endpointBase = config.endpoint.replace(/\/+$/, "");
  const ingestUrl =
    endpointBase.endsWith("/_e/b") || endpointBase.endsWith("/v1/b") ? endpointBase : `${endpointBase}/_e/b`;

  const os = normalizeOS(platform?.OS);
  const deviceType = platform?.isPad ? "tablet" : os === "iOS" || os === "Android" ? "mobile" : "";

  const storage = config.storage ?? createMemoryStorage();
  const ring = new RingBuffer<Breadcrumb>(config.breadcrumbCapacity ?? 50);
  const initiallyActive = !appState?.currentState || appState.currentState === "active";
  const fg = new RNForegroundTracker(initiallyActive);

  const actorKey = `__luma_eye_uid_${config.project}__`;
  let actorId = makeId("u");
  let identifiedExplicitly = false;
  const session = new SessionManager({
    project: config.project,
    storage,
    idleTimeoutMs: config.sessionIdleTimeoutMs,
    maxDurationMs: config.sessionMaxDurationMs,
    useCookies: false,
  });

  try {
    const storedActor = storage.getItem(actorKey);
    if (storedActor && typeof (storedActor as Promise<unknown>).then === "function") {
      (storedActor as Promise<string | null>)
        .then((val) => {
          if (!identifiedExplicitly) {
            if (val) {
              actorId = val;
            } else {
              const w = storage.setItem(actorKey, actorId);
              if (w && typeof (w as Promise<unknown>).catch === "function") {
                (w as Promise<void>).catch(() => {});
              }
            }
          }
        })
        .catch(() => {});
    } else if (typeof storedActor === "string" && storedActor) {
      actorId = storedActor;
    } else {
      const w = storage.setItem(actorKey, actorId);
      if (w && typeof (w as Promise<unknown>).catch === "function") {
        (w as Promise<void>).catch(() => {});
      }
    }
  } catch {
    // ignore storage errors
  }

  let currentScreen = "";

  const transport = new DualTrackTransport({
    endpointUrl: ingestUrl,
    publicKey: config.key,
    project: config.project,
    env: config.env || "production",
    release: config.release || "",
    sdk: "luma-eye-rn/1.1.0",
    os: os || undefined,
    deviceType: deviceType || undefined,
    batchWindowMs: config.batchWindowMs ?? 3000,
    maxErrorsPerPage: config.maxErrorsPerSession ?? 30,
    storage,
    useFetchLater: false,
    maxOutboxItems: config.maxOutboxItems ?? 10,
    persistOfflineNormal: true,
    getActorId: () => actorId,
    getSessionId: () => session.getSessionId(),
    getPageContext: () => {
      const s = currentScreen ? normalizeScreen(currentScreen) : null;
      return {
        page_url: s ? s.url : "",
        referrer: "",
        utm_source: "",
        utm_medium: "",
        utm_campaign: "",
      };
    },
  });

  transport.drainOutbox();

  const cleanups: Array<() => void> = [];

  cleanups.push(
    installBreadcrumbCollectors({
      ring,
      ingestUrlPrefix: ingestUrl,
      traceOrigins: config.traceOrigins,
      captureConsoleLogs: config.captureConsoleLogs,
      captureFetch: config.autoNetworkBreadcrumbs !== false,
      captureConsole: true,
      onLog: (level, message, attrs, traceId) => {
        const s = currentScreen ? normalizeScreen(currentScreen) : null;
        transport.enqueueLog({
          ts: Date.now(),
          level,
          message,
          page_url: s ? s.url : undefined,
          trace_id: traceId,
          attrs,
        });
      },
      onHttpError: (err, traceId, meta) => {
        if (config.autoErrors !== false) {
          client.captureError(err, { traceId, extra: meta, tags: { source: "fetch" } });
        }
      },
    })
  );

  // AppState listener: pause/resume foreground clock, flush Track 1 on background, drain Outbox on active.
  if (config.autoAppState !== false && appState && typeof appState.addEventListener === "function") {
    let prevState: RNAppStateStatus = appState.currentState || "active";
    const onAppState = (nextState: RNAppStateStatus) => {
      fg.onAppStateChange(nextState);
      if (nextState !== prevState) {
        ring.push({
          ts: Date.now(),
          category: "lifecycle",
          level: "info",
          message: `app_state:${prevState}->${nextState}`,
        });
        if (nextState === "background" || nextState === "inactive") {
          transport.flushNormal();
        } else if (nextState === "active") {
          session.touch();
          transport.drainOutbox();
        }
        prevState = nextState;
      }
    };
    const sub = appState.addEventListener("change", onAppState);
    cleanups.push(() => {
      if (typeof sub === "function") {
        sub();
      } else if (sub && typeof sub.remove === "function") {
        sub.remove();
      } else if (typeof appState.removeEventListener === "function") {
        appState.removeEventListener("change", onAppState);
      }
    });
  }

  // React Native ErrorUtils global JS/Hermes exception hook
  if (config.autoErrors !== false && errorUtils && typeof errorUtils.setGlobalHandler === "function") {
    const prevHandler = typeof errorUtils.getGlobalHandler === "function" ? errorUtils.getGlobalHandler() : undefined;
    const wrappedHandler = (err: unknown, isFatal?: boolean) => {
      try {
        client.captureError(err, {
          level: isFatal ? "fatal" : "error",
          tags: {
            source: "ErrorUtils",
            fatal: String(Boolean(isFatal)),
          },
        });
      } catch {
        // never break host error handler
      }
      if (typeof prevHandler === "function") {
        prevHandler(err, isFatal);
      }
    };
    errorUtils.setGlobalHandler(wrappedHandler);
    cleanups.push(() => {
      if (typeof prevHandler === "function") {
        errorUtils.setGlobalHandler?.(prevHandler);
      }
    });
  }

  // Unhandled promise rejection hook if global addEventListener exists (e.g. Hermes polyfills / web)
  const gEvent = globalThis as unknown as {
    addEventListener?: (type: string, cb: (ev: { reason?: unknown }) => void) => void;
    removeEventListener?: (type: string, cb: (ev: { reason?: unknown }) => void) => void;
  };
  if (config.autoErrors !== false && typeof gEvent.addEventListener === "function") {
    const onRejection = (ev: { reason?: unknown }) => {
      const reason =
        ev?.reason instanceof Error
          ? ev.reason
          : new Error(typeof ev?.reason === "string" ? ev.reason : "UnhandledPromiseRejection");
      client.captureError(reason, { tags: { source: "unhandledrejection" } });
    };
    gEvent.addEventListener("unhandledrejection", onRejection);
    cleanups.push(() => {
      gEvent.removeEventListener?.("unhandledrejection", onRejection);
    });
  }

  const client: LumaEyeRNClient = {
    track(name, props) {
      const s = currentScreen ? normalizeScreen(currentScreen) : { path: "/", url: "" };
      transport.enqueueEvent({
        ts: Date.now(),
        type: "custom",
        name,
        page_path: s.path,
        page_url: s.url || undefined,
        props,
      });
    },

    screen(screenName, props, durationMs) {
      const s = normalizeScreen(screenName);
      if (currentScreen && currentScreen !== s.display) {
        ring.push({
          ts: Date.now(),
          category: "navigation",
          level: "info",
          message: `${currentScreen} -> ${s.display}`,
          data: { from: currentScreen, to: s.display },
        });
      } else if (!currentScreen) {
        ring.push({
          ts: Date.now(),
          category: "navigation",
          level: "info",
          message: `screen:${s.display}`,
          data: { to: s.display },
        });
      }
      currentScreen = s.display;
      transport.enqueueEvent({
        ts: Date.now(),
        type: "pageview",
        name: "pageview",
        page_path: s.path,
        page_url: s.url,
        duration_ms: durationMs ?? fg.sampleDeltaMs(),
        props,
      });
    },

    identify(userId, traits) {
      if (!userId) return;
      identifiedExplicitly = true;
      actorId = userId;
      try {
        const w = storage.setItem(actorKey, userId);
        if (w && typeof (w as Promise<unknown>).catch === "function") {
          (w as Promise<void>).catch(() => {});
        }
      } catch {
        // ignore
      }
      transport.enqueueEvent({
        ts: Date.now(),
        type: "identify",
        name: "identify",
        props: traits,
      });
    },

    addBreadcrumb(category, message, data, level = "info") {
      ring.push({
        ts: Date.now(),
        category,
        level,
        message,
        data,
      });
    },

    captureError(error, context) {
      const errObj = error instanceof Error ? error : new Error(typeof error === "string" ? error : String(error));
      ring.push({
        ts: Date.now(),
        category: "exception",
        level: context?.level || "error",
        message: `${errObj.name}: ${errObj.message}`,
      });
      const s = currentScreen ? normalizeScreen(currentScreen) : null;
      transport.dispatchUrgent({
        ts: Date.now(),
        level: context?.level || "error",
        type: errObj.name || "Error",
        message: errObj.message || String(error),
        stack: errObj.stack || "",
        breadcrumbs: ring.toArray(),
        page_url: s ? s.url : "",
        trace_id: context?.traceId,
        tags: context?.tags,
        extra: context?.extra,
      });
    },

    flushFlightRecorder(reason = "manual_dump", extra) {
      const s = currentScreen ? normalizeScreen(currentScreen) : null;
      transport.dispatchUrgent({
        ts: Date.now(),
        level: "info",
        type: "FlightRecorderDump",
        message: reason,
        breadcrumbs: ring.toArray(),
        page_url: s ? s.url : "",
        tags: { flight_recorder: "true" },
        extra,
      });
    },

    log(level, message, attrs, traceId) {
      ring.push({ ts: Date.now(), category: "log", level, message, data: attrs });
      if (level === "debug") return;
      const s = currentScreen ? normalizeScreen(currentScreen) : null;
      transport.enqueueLog({
        ts: Date.now(),
        level,
        message,
        page_url: s ? s.url : undefined,
        trace_id: traceId,
        attrs,
      });
    },

    flush() {
      transport.flushNormal();
    },

    destroy() {
      transport.flushNormal();
      for (const fn of cleanups) {
        try {
          fn();
        } catch {
          // ignore
        }
      }
    },

    getRingBufferSnapshot() {
      return ring.toArray();
    },

    getForegroundTimeMs() {
      return fg.getTotalMs();
    },

    getActorId() {
      return actorId;
    },

    getSessionId() {
      return session.getSessionId();
    },
  };

  return client;
}

let defaultRNClient: LumaEyeRNClient | null = null;

export function initLumaEyeRN(config: LumaEyeRNConfig): LumaEyeRNClient {
  if (defaultRNClient) {
    defaultRNClient.destroy();
  }
  defaultRNClient = createLumaEyeRN(config);
  return defaultRNClient;
}

export const initLumaEye = initLumaEyeRN;
export const createLumaEye = createLumaEyeRN;

export function getLumaEyeRN(): LumaEyeRNClient | null {
  return defaultRNClient;
}

export function track(name: string, props?: Record<string, unknown>): void {
  defaultRNClient?.track(name, props);
}

export function screen(screenName: string, props?: Record<string, unknown>, durationMs?: number): void {
  defaultRNClient?.screen(screenName, props, durationMs);
}

export function identify(userId: string, traits?: Record<string, unknown>): void {
  defaultRNClient?.identify(userId, traits);
}

export function captureError(
  error: unknown,
  context?: { level?: string; tags?: Record<string, string>; extra?: Record<string, unknown>; traceId?: string }
): void {
  defaultRNClient?.captureError(error, context);
}

export function addBreadcrumb(
  category: string,
  message: string,
  data?: Record<string, unknown>,
  level = "info"
): void {
  defaultRNClient?.addBreadcrumb(category, message, data, level);
}

export function flushFlightRecorder(reason?: string, extra?: Record<string, unknown>): void {
  defaultRNClient?.flushFlightRecorder(reason, extra);
}

export function log(
  level: "debug" | "info" | "warn" | "error",
  message: string,
  attrs?: Record<string, unknown>,
  traceId?: string
): void {
  defaultRNClient?.log(level, message, attrs, traceId);
}

export function flush(): void {
  defaultRNClient?.flush();
}

export interface NavigationRouteLike {
  name?: string;
  key?: string;
  params?: Record<string, unknown>;
}

export interface NavigationContainerLike {
  getCurrentRoute?: () => NavigationRouteLike | undefined;
  addListener?: (event: "state", listener: () => void) => (() => void) | { remove?: () => void } | void;
  current?: {
    getCurrentRoute?: () => NavigationRouteLike | undefined;
    addListener?: (event: "state", listener: () => void) => (() => void) | { remove?: () => void } | void;
  } | null;
}

export interface NavigationAttachment {
  onReady: () => void;
  onStateChange: () => void;
  detach: () => void;
}

/**
 * Attaches React Navigation container transitions to Luma Eye screen views + breadcrumbs.
 * Works either via ref state listeners or `<NavigationContainer ref={ref} onReady={...} onStateChange={...} />`.
 */
export function attachNavigationContainer(
  navRef: NavigationContainerLike,
  options?: { client?: LumaEyeRNClient | null; captureParams?: boolean }
): NavigationAttachment {
  let lastRouteKey = "";

  const recordCurrent = () => {
    const container = navRef?.current ?? navRef;
    const route = container?.getCurrentRoute?.();
    if (!route?.name) return;
    const routeKey = `${route.name}:${route.key || ""}`;
    if (routeKey === lastRouteKey) return;
    lastRouteKey = routeKey;
    const target = options?.client ?? defaultRNClient;
    if (!target) return;
    target.screen(route.name, options?.captureParams && route.params ? route.params : undefined);
  };

  let unsub: (() => void) | undefined;
  const container = navRef?.current ?? navRef;
  if (container && typeof container.addListener === "function") {
    const ret = container.addListener("state", recordCurrent);
    if (typeof ret === "function") {
      unsub = ret;
    } else if (ret && typeof ret.remove === "function") {
      unsub = () => ret.remove?.();
    }
  }

  return {
    onReady: recordCurrent,
    onStateChange: recordCurrent,
    detach: () => {
      unsub?.();
      unsub = undefined;
    },
  };
}

/**
 * Helper for Expo Router: call inside `useEffect(() => trackPathname(pathname), [pathname])`.
 */
export function createPathnameTracker(client?: LumaEyeRNClient | null) {
  let lastPath = "";
  return (pathname: string, props?: Record<string, unknown>) => {
    const clean = (pathname || "").trim() || "/";
    if (clean === lastPath) return;
    lastPath = clean;
    const target = client ?? defaultRNClient;
    target?.screen(clean, props);
  };
}
