import { Breadcrumb, RingBuffer } from "./ring-buffer";
import { createTraceContext, shouldInjectTraceHeader } from "./trace";

const SENSITIVE_QS_RE = /([?&](?:token|access_token|password|passwd|secret|api_key|apikey|key|auth)=)[^&#]*/gi;

export function scrubUrl(raw: string): string {
  return raw.replace(SENSITIVE_QS_RE, "$1[REDACTED]");
}

function describeElement(target: EventTarget | null): string {
  if (!target || typeof (target as HTMLElement).tagName !== "string") return "";
  const el = target as HTMLElement;
  const tag = el.tagName.toLowerCase();
  // Never inspect sensitive form inputs
  if (tag === "input" || tag === "textarea" || tag === "select") {
    const type = el.getAttribute("type") || "";
    const name = el.getAttribute("name") || el.id || "";
    return `${tag}${type ? `[type=${type}]` : ""}${name ? `#${name}` : ""}`;
  }
  let desc = tag;
  if (el.id) desc += `#${el.id}`;
  else if (typeof el.className === "string" && el.className.trim()) {
    const cls = el.className.trim().split(/\s+/).slice(0, 2).join(".");
    if (cls) desc += `.${cls}`;
  }
  const text = (el.innerText || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ");
  if (text) {
    desc += ` "${text.slice(0, 40)}"`;
  }
  return desc;
}

export interface CollectorHooks {
  ring: RingBuffer<Breadcrumb>;
  ingestUrlPrefix: string;
  traceOrigins?: Array<string | RegExp>;
  captureConsoleLogs?: boolean;
  captureConsole?: boolean;
  captureFetch?: boolean;
  onLog?: (level: string, message: string, attrs?: Record<string, unknown>, traceId?: string) => void;
  onRouteChange?: (from: string, to: string) => void;
  onHttpError?: (err: Error, traceId: string, meta: Record<string, unknown>) => void;
  onActivity?: () => void;
}

export function installBreadcrumbCollectors(hooks: CollectorHooks): () => void {
  const cleanups: Array<() => void> = [];
  const push = (category: string, level: string, message: string, data?: Record<string, unknown>) => {
    hooks.ring.push({
      ts: Date.now(),
      category,
      level,
      message: message.slice(0, 300),
      data,
    });
  };

  // 1. Sanitized DOM clicks (with normalized viewport coordinates for trajectory fallback)
  if (typeof document !== "undefined") {
    const onClick = (ev: Event) => {
      hooks.onActivity?.();
      const desc = describeElement(ev.target);
      if (!desc) return;
      let clickData: Record<string, unknown> | undefined;
      if (typeof window !== "undefined" && "clientX" in ev && typeof (ev as MouseEvent).clientX === "number") {
        const me = ev as MouseEvent;
        const vw = Math.round(window.innerWidth || 0);
        const vh = Math.round(window.innerHeight || 0);
        if (vw > 0 && vh > 0) {
          clickData = {
            x_pct: Math.round((me.clientX / vw) * 1000) / 10,
            y_pct: Math.round((me.clientY / vh) * 1000) / 10,
            vw,
            vh,
          };
        }
      }
      push("ui.click", "info", desc, clickData);
    };
    document.addEventListener("click", onClick, { capture: true, passive: true });
    cleanups.push(() => document.removeEventListener("click", onClick, { capture: true }));

    const onVis = () => {
      push("lifecycle", "info", `visibility:${document.visibilityState}`);
    };
    document.addEventListener("visibilitychange", onVis);
    cleanups.push(() => document.removeEventListener("visibilitychange", onVis));
  }

  // 2. Network online/offline
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    const onOnline = () => push("lifecycle", "info", "network:online");
    const onOffline = () => push("lifecycle", "warn", "network:offline");
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    cleanups.push(() => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    });
  }

  // 3. Console interceptor (debug/info stay strictly in RingBuffer; warn/error optionally ship on Track 1)
  const g = globalThis as Record<string, unknown>;
  if (hooks.captureConsole !== false && typeof console !== "undefined" && !g.__lumaEyeConsoleWrapped) {
    const orig = {
      debug: console.debug,
      info: console.info,
      warn: console.warn,
      error: console.error,
    };
    g.__lumaEyeConsoleWrapped = true;
    const wrap = (lvl: "debug" | "info" | "warn" | "error") => {
      const fn = orig[lvl];
      if (typeof fn !== "function") return;
      console[lvl] = (...args: unknown[]) => {
        try {
          const msg = args
            .map((a) => (typeof a === "string" ? a : a instanceof Error ? `${a.name}: ${a.message}` : safeStringify(a)))
            .join(" ")
            .slice(0, 500);
          push("console", lvl, msg);
          if (hooks.captureConsoleLogs && (lvl === "warn" || lvl === "error")) {
            hooks.onLog?.(lvl, msg);
          }
        } catch {
          // never break app console
        }
        return fn.apply(console, args);
      };
    };
    wrap("debug");
    wrap("info");
    wrap("warn");
    wrap("error");
    cleanups.push(() => {
      console.debug = orig.debug;
      console.info = orig.info;
      console.warn = orig.warn;
      console.error = orig.error;
      delete g.__lumaEyeConsoleWrapped;
    });
  }

  // 4. SPA History route changes
  if (typeof history !== "undefined" && typeof location !== "undefined") {
    let currentHref = location.pathname + location.search;
    const notifyRoute = () => {
      const next = location.pathname + location.search;
      if (next !== currentHref) {
        const prev = currentHref;
        currentHref = next;
        hooks.onActivity?.();
        push("navigation", "info", `${prev} -> ${next}`, { from: prev, to: next });
        hooks.onRouteChange?.(prev, next);
      }
    };
    const origPush = history.pushState;
    const origReplace = history.replaceState;
    history.pushState = function (...args: Parameters<History["pushState"]>) {
      const ret = origPush.apply(this, args);
      notifyRoute();
      return ret;
    };
    history.replaceState = function (...args: Parameters<History["replaceState"]>) {
      const ret = origReplace.apply(this, args);
      notifyRoute();
      return ret;
    };
    if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
      window.addEventListener("popstate", notifyRoute);
    }
    cleanups.push(() => {
      history.pushState = origPush;
      history.replaceState = origReplace;
      if (typeof window !== "undefined" && typeof window.removeEventListener === "function") {
        window.removeEventListener("popstate", notifyRoute);
      }
    });
  }

  // 5. Fetch interceptor + W3C traceparent injection
  if (hooks.captureFetch !== false && typeof globalThis.fetch === "function" && !g.__lumaEyeFetchWrapped) {
    const origFetch = globalThis.fetch.bind(globalThis);
    g.__lumaEyeFetchWrapped = true;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const rawUrl = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (
        (hooks.ingestUrlPrefix && rawUrl.startsWith(hooks.ingestUrlPrefix)) ||
        rawUrl.includes("/_e/r?") ||
        rawUrl.endsWith("/_e/eye-replay.js")
      ) {
        return origFetch(input, init);
      }

      const method = (init?.method || (typeof input === "object" && "method" in input ? input.method : "GET") || "GET").toUpperCase();
      const started = typeof performance !== "undefined" ? performance.now() : Date.now();
      let traceId = "";
      let nextInit = init;

      if (shouldInjectTraceHeader(rawUrl, hooks.traceOrigins)) {
        const tc = createTraceContext();
        traceId = tc.traceId;
        const headers = new Headers(init?.headers || (typeof input === "object" && "headers" in input ? input.headers : undefined));
        if (!headers.has("traceparent")) {
          headers.set("traceparent", tc.traceparent);
        }
        nextInit = { ...init, headers };
      }

      const cleanUrl = scrubUrl(rawUrl);
      try {
        const resp = await origFetch(input, nextInit);
        const dur = Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - started);
        push("http", resp.status >= 400 ? "error" : "info", `${method} ${cleanUrl} -> ${resp.status} (${dur}ms)`, {
          method,
          url: cleanUrl,
          status: resp.status,
          duration_ms: dur,
          trace_id: traceId || undefined,
        });
        if (resp.status >= 500) {
          hooks.onLog?.("error", `HTTP ${resp.status} ${method} ${cleanUrl}`, { status: resp.status, duration_ms: dur }, traceId);
        }
        return resp;
      } catch (err) {
        const dur = Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - started);
        const errObj = err instanceof Error ? err : new Error(String(err));
        push("http", "error", `${method} ${cleanUrl} -> NETWORK_ERROR (${dur}ms)`, {
          method,
          url: cleanUrl,
          duration_ms: dur,
          trace_id: traceId || undefined,
          error: errObj.message,
        });
        hooks.onHttpError?.(errObj, traceId, { method, url: cleanUrl, duration_ms: dur });
        throw err;
      }
    };
    cleanups.push(() => {
      globalThis.fetch = origFetch;
      delete g.__lumaEyeFetchWrapped;
    });
  }

  return () => {
    for (const fn of cleanups) {
      try {
        fn();
      } catch {
        // ignore
      }
    }
  };
}

function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
