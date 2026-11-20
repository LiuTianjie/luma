import { installBreadcrumbCollectors, scrubUrl } from "./breadcrumbs";
import { ForegroundTracker } from "./foreground-time";
import { Breadcrumb, RingBuffer } from "./ring-buffer";
import {
  DEFAULT_SESSION_IDLE_TIMEOUT_MS,
  DEFAULT_SESSION_MAX_DURATION_MS,
  resolveBrowserActorId,
  SessionManager,
} from "./session";
import { DualTrackTransport } from "./transport";
import { observeWebVitals } from "./vitals";

export type { Breadcrumb } from "./ring-buffer";
export { RingBuffer } from "./ring-buffer";
export { getTotalForegroundTime } from "./foreground-time";
export {
  DEFAULT_SESSION_IDLE_TIMEOUT_MS,
  DEFAULT_SESSION_MAX_DURATION_MS,
  SessionManager,
} from "./session";

export interface LumaEyeConfig {
  endpoint: string;
  key: string;
  project: string;
  env?: string;
  release?: string;
  breadcrumbCapacity?: number;
  batchWindowMs?: number;
  maxErrorsPerPage?: number;
  sessionIdleTimeoutMs?: number;
  sessionMaxDurationMs?: number;
  crossSubdomainCookie?: boolean;
  autoPageviews?: boolean;
  autoVitals?: boolean;
  autoErrors?: boolean;
  captureConsoleLogs?: boolean;
  traceOrigins?: Array<string | RegExp>;
  /** Automatically lazy-load 1:1 DOM Session Replay in idle time (default: true in browser) */
  replay?: boolean;
  /** Mask every form input instead of balanced sensitive-input masking (default: false) */
  replayMaskAllInputs?: boolean;
}

export interface LumaEyeClient {
  track(name: string, props?: Record<string, unknown>): void;
  pageview(path?: string, durationMs?: number): void;
  identify(userId: string, traits?: Record<string, unknown>): void;
  addBreadcrumb(category: string, message: string, data?: Record<string, unknown>, level?: string): void;
  captureError(error: unknown, context?: { level?: string; tags?: Record<string, string>; extra?: Record<string, unknown>; traceId?: string }): void;
  flushFlightRecorder(reason?: string, extra?: Record<string, unknown>): void;
  log(level: "debug" | "info" | "warn" | "error", message: string, attrs?: Record<string, unknown>, traceId?: string): void;
  flush(): void;
  destroy(): void;
  getRingBufferSnapshot(): Breadcrumb[];
  getActorId(): string;
  getSessionId(): string;
}

function getSafeBrowserStorage(): Storage | undefined {
  try {
    if (typeof localStorage !== "undefined" && localStorage) {
      return localStorage;
    }
  } catch {
    // localStorage blocked (e.g. strict sandboxed iframe)
  }
  try {
    if (typeof sessionStorage !== "undefined" && sessionStorage) {
      return sessionStorage;
    }
  } catch {
    // ignore
  }
  return undefined;
}

function getUtmParams(): { utm_source: string; utm_medium: string; utm_campaign: string } {
  if (typeof location === "undefined") {
    return { utm_source: "", utm_medium: "", utm_campaign: "" };
  }
  try {
    const sp = new URLSearchParams(location.search);
    return {
      utm_source: sp.get("utm_source") || "",
      utm_medium: sp.get("utm_medium") || "",
      utm_campaign: sp.get("utm_campaign") || "",
    };
  } catch {
    return { utm_source: "", utm_medium: "", utm_campaign: "" };
  }
}

export function createLumaEye(config: LumaEyeConfig): LumaEyeClient {
  const endpointBase = config.endpoint.replace(/\/+$/, "");
  const ingestUrl = endpointBase.endsWith("/_e/b") || endpointBase.endsWith("/v1/b") ? endpointBase : `${endpointBase}/_e/b`;

  const ring = new RingBuffer<Breadcrumb>(config.breadcrumbCapacity ?? 50);
  const fg = new ForegroundTracker();
  const browserStorage = getSafeBrowserStorage();
  const crossSubdomain = config.crossSubdomainCookie ?? true;

  const actor = resolveBrowserActorId(config.project, browserStorage, crossSubdomain);
  const session = new SessionManager({
    project: config.project,
    storage: browserStorage,
    idleTimeoutMs: config.sessionIdleTimeoutMs,
    maxDurationMs: config.sessionMaxDurationMs,
    crossSubdomainCookie: crossSubdomain,
    useCookies: true,
  });

  const transport = new DualTrackTransport({
    endpointUrl: ingestUrl,
    publicKey: config.key,
    project: config.project,
    env: config.env || "production",
    release: config.release || "",
    batchWindowMs: config.batchWindowMs ?? 3000,
    maxErrorsPerPage: config.maxErrorsPerPage ?? 20,
    getActorId: () => actor.getActorId(),
    getSessionId: () => session.getSessionId(),
    getPageContext: () => {
      const utm = getUtmParams();
      return {
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
        referrer: typeof document !== "undefined" ? document.referrer || "" : "",
        ...utm,
      };
    },
  });

  // Drain any crash payloads saved in Crash Outbox before a previous hard reload.
  transport.drainOutbox();

  const cleanups: Array<() => void> = [];
  let replayCtrl: { flushNow(urgent?: boolean): void; stop(): void } | null = null;

  if (typeof window !== "undefined") {
    cleanups.push(
      installBreadcrumbCollectors({
        ring,
        ingestUrlPrefix: ingestUrl,
        traceOrigins: config.traceOrigins,
        captureConsoleLogs: config.captureConsoleLogs,
        onActivity: () => {
          session.touch();
        },
        onLog: (level, message, attrs, traceId) => {
          transport.enqueueLog({
            ts: Date.now(),
            level,
            message,
            page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
            trace_id: traceId,
            attrs,
          });
        },
        onRouteChange: (_from, to) => {
          if (config.autoPageviews !== false) {
            const dur = fg.sampleDeltaMs();
            transport.enqueueEvent({
              ts: Date.now(),
              type: "pageview",
              name: "pageview",
              page_path: to.split("?")[0] || "/",
              page_url: typeof location !== "undefined" ? scrubUrl(location.href) : to,
              duration_ms: dur,
            });
          }
        },
        onHttpError: (err, traceId, meta) => {
          if (config.autoErrors !== false) {
            client.captureError(err, { traceId, extra: meta, tags: { source: "fetch" } });
          }
        },
      })
    );
  }

  if (config.autoVitals !== false && typeof window !== "undefined") {
    cleanups.push(
      observeWebVitals((vital) => {
        transport.enqueueVital(vital);
      })
    );
  }

  if (config.autoErrors !== false && typeof window !== "undefined" && typeof window.addEventListener === "function") {
    const onWindowError = (ev: ErrorEvent) => {
      if (ev.error) {
        client.captureError(ev.error, { tags: { source: "window.onerror" } });
      } else if (ev.message) {
        client.captureError(new Error(ev.message), {
          tags: { source: "window.onerror" },
          extra: { filename: ev.filename, lineno: ev.lineno, colno: ev.colno },
        });
      }
    };
    const onRejection = (ev: PromiseRejectionEvent) => {
      const reason = ev.reason instanceof Error ? ev.reason : new Error(typeof ev.reason === "string" ? ev.reason : "UnhandledPromiseRejection");
      client.captureError(reason, { tags: { source: "unhandledrejection" } });
    };
    window.addEventListener("error", onWindowError);
    window.addEventListener("unhandledrejection", onRejection);
    cleanups.push(() => {
      window.removeEventListener("error", onWindowError);
      window.removeEventListener("unhandledrejection", onRejection);
    });
  }

  const client: LumaEyeClient = {
    track(name, props) {
      transport.enqueueEvent({
        ts: Date.now(),
        type: "custom",
        name,
        page_path: typeof location !== "undefined" ? location.pathname : "/",
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
        props,
      });
    },

    pageview(path, durationMs) {
      const p = path || (typeof location !== "undefined" ? location.pathname : "/");
      transport.enqueueEvent({
        ts: Date.now(),
        type: "pageview",
        name: "pageview",
        page_path: p,
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : p,
        duration_ms: durationMs ?? fg.sampleDeltaMs(),
      });
    },

    identify(userId, traits) {
      if (!userId) return;
      actor.setActorId(userId);
      session.touch();
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
      replayCtrl?.flushNow(true);
      transport.dispatchUrgent({
        ts: Date.now(),
        level: context?.level || "error",
        type: errObj.name || "Error",
        message: errObj.message || String(error),
        stack: errObj.stack || "",
        breadcrumbs: ring.toArray(),
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
        trace_id: context?.traceId,
        tags: context?.tags,
        extra: context?.extra,
      });
    },

    flushFlightRecorder(reason = "manual_dump", extra) {
      replayCtrl?.flushNow(true);
      transport.dispatchUrgent({
        ts: Date.now(),
        level: "info",
        type: "FlightRecorderDump",
        message: reason,
        breadcrumbs: ring.toArray(),
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
        tags: { flight_recorder: "true" },
        extra,
      });
    },

    log(level, message, attrs, traceId) {
      ring.push({ ts: Date.now(), category: "log", level, message, data: attrs });
      if (level === "debug" || level === "info") {
        // Track 1 saves bandwidth: debug/info stay in Ring Buffer unless explicitly shipped via Track 2,
        // except explicit client.log("info"|"warn"|"error") where warn/error ship on Track 1.
        if (level === "debug") return;
      }
      transport.enqueueLog({
        ts: Date.now(),
        level,
        message,
        page_url: typeof location !== "undefined" ? scrubUrl(location.href) : "",
        trace_id: traceId,
        attrs,
      });
    },

    flush() {
      replayCtrl?.flushNow(false);
      transport.flushNormal();
    },

    destroy() {
      replayCtrl?.stop();
      replayCtrl = null;
      transport.flushNormal();
      for (const fn of cleanups) {
        fn();
      }
    },

    getRingBufferSnapshot() {
      return ring.toArray();
    },

    getActorId() {
      return actor.getActorId();
    },

    getSessionId() {
      return session.getSessionId();
    },
  };

  if (config.autoPageviews !== false && typeof window !== "undefined") {
    client.pageview();
  }

  // Lazy-load 1:1 DOM Session Replay in background idle time (0 bytes added to critical path)
  if (
    config.replay !== false &&
    typeof window !== "undefined" &&
    typeof document !== "undefined" &&
    typeof document.createElement === "function" &&
    typeof MutationObserver !== "undefined"
  ) {
    let destroyed = false;
    cleanups.push(() => {
      destroyed = true;
    });

    const replayOrigin = endpointBase.replace(/\/_e\/b$/, "").replace(/\/v1\/b$/, "");
    const replayScriptUrl = `${replayOrigin}/_e/eye-replay.js`;

    const mountRecorder = () => {
      if (destroyed || replayCtrl) return;
      const g = globalThis as unknown as {
        __LumaEyeReplay?: {
          startSessionReplay?: (opts: {
            endpointUrl: string;
            publicKey: string;
            project: string;
            env?: string;
            release?: string;
            maskAllInputs?: boolean;
            getSessionId: () => string;
            getActorId: () => string;
          }) => { flushNow(urgent?: boolean): void; stop(): void };
        };
      };
      if (g.__LumaEyeReplay && typeof g.__LumaEyeReplay.startSessionReplay === "function") {
        replayCtrl = g.__LumaEyeReplay.startSessionReplay({
          endpointUrl: ingestUrl,
          publicKey: config.key,
          project: config.project,
          env: config.env || "production",
          release: config.release || "",
          maskAllInputs: config.replayMaskAllInputs,
          getSessionId: () => session.getSessionId(),
          getActorId: () => actor.getActorId(),
        });
        return;
      }

      if (!document.head) return;
      const existing = document.querySelector(`script[data-luma-eye-replay="1"]`) as HTMLScriptElement | null;
      if (existing) {
        existing.addEventListener("load", mountRecorder, { once: true });
        return;
      }
      const script = document.createElement("script");
      script.async = true;
      script.crossOrigin = "anonymous";
      script.setAttribute("data-luma-eye-replay", "1");
      script.src = replayScriptUrl;
      script.onload = () => mountRecorder();
      script.onerror = () => {
        // Silently degrade to Schematic Trajectory Fallback if blocked by strict CSP
      };
      document.head.appendChild(script);
    };

    const scheduleIdleMount = () => {
      if (destroyed) return;
      const sched = (globalThis as unknown as {
        scheduler?: { postTask?: (cb: () => void, opts?: { priority?: string }) => Promise<void> };
        requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number;
      });
      if (sched.scheduler && typeof sched.scheduler.postTask === "function") {
        void sched.scheduler.postTask(mountRecorder, { priority: "background" }).catch(() => {});
      } else if (typeof sched.requestIdleCallback === "function") {
        sched.requestIdleCallback(mountRecorder, { timeout: 2000 });
      } else {
        setTimeout(mountRecorder, 300);
      }
    };

    if (document.readyState === "complete") {
      scheduleIdleMount();
    } else if (typeof window.addEventListener === "function") {
      const onLoad = () => scheduleIdleMount();
      window.addEventListener("load", onLoad, { once: true });
      cleanups.push(() => window.removeEventListener("load", onLoad));
    }
  }

  return client;
}

let defaultClient: LumaEyeClient | null = null;

export function initLumaEye(config: LumaEyeConfig): LumaEyeClient {
  if (defaultClient) {
    defaultClient.destroy();
  }
  defaultClient = createLumaEye(config);
  return defaultClient;
}

export function getLumaEye(): LumaEyeClient | null {
  return defaultClient;
}

export function track(name: string, props?: Record<string, unknown>): void {
  defaultClient?.track(name, props);
}

export function identify(userId: string, traits?: Record<string, unknown>): void {
  defaultClient?.identify(userId, traits);
}

export function captureError(
  error: unknown,
  context?: { level?: string; tags?: Record<string, string>; extra?: Record<string, unknown>; traceId?: string }
): void {
  defaultClient?.captureError(error, context);
}

export function addBreadcrumb(
  category: string,
  message: string,
  data?: Record<string, unknown>,
  level = "info"
): void {
  defaultClient?.addBreadcrumb(category, message, data, level);
}

export function flushFlightRecorder(reason?: string, extra?: Record<string, unknown>): void {
  defaultClient?.flushFlightRecorder(reason, extra);
}

export function log(level: "debug" | "info" | "warn" | "error", message: string, attrs?: Record<string, unknown>, traceId?: string): void {
  defaultClient?.log(level, message, attrs, traceId);
}

export function flush(): void {
  defaultClient?.flush();
}

export function getActorId(): string {
  return defaultClient?.getActorId() ?? "";
}

export function getSessionId(): string {
  return defaultClient?.getSessionId() ?? "";
}

// Zero-build <script src="https://eye.example.com/_e/eye.js" data-key="..." data-project="..."> auto-init
if (typeof document !== "undefined" && document.currentScript) {
  const s = document.currentScript as HTMLScriptElement;
  const key = s.getAttribute("data-key");
  const project = s.getAttribute("data-project");
  if (key && project) {
    const endpoint = s.getAttribute("data-endpoint") || new URL(s.src, location.href).origin;
    const replayAttr = s.getAttribute("data-replay");
    initLumaEye({
      endpoint,
      key,
      project,
      env: s.getAttribute("data-env") || "production",
      release: s.getAttribute("data-release") || "",
      replay: replayAttr === "false" || replayAttr === "0" ? false : true,
      replayMaskAllInputs: s.getAttribute("data-mask-all-inputs") === "true",
    });
  }
}
