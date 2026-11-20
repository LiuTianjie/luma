import type { Breadcrumb } from "./ring-buffer";
import { getBrowserLocalStorage, type LumaStorageAdapter } from "./storage";
import type { VitalSample } from "./vitals";

export interface EventPayload {
  ts: number;
  type: "pageview" | "custom" | "identify" | "lifecycle";
  name: string;
  page_url?: string;
  page_path?: string;
  referrer?: string;
  utm_source?: string;
  utm_medium?: string;
  utm_campaign?: string;
  duration_ms?: number;
  trace_id?: string;
  props?: Record<string, unknown>;
}

export interface ErrorPayload {
  ts: number;
  level: string;
  type: string;
  message: string;
  stack?: string;
  breadcrumbs?: Breadcrumb[];
  page_url?: string;
  trace_id?: string;
  tags?: Record<string, string>;
  extra?: Record<string, unknown>;
}

export interface LogPayload {
  ts: number;
  level: string;
  message: string;
  page_url?: string;
  trace_id?: string;
  attrs?: Record<string, unknown>;
}

export interface BatchEnvelope {
  key: string;
  project: string;
  track: "normal" | "urgent";
  env: string;
  release: string;
  sdk?: string;
  os?: string;
  device_type?: string;
  actor_id: string;
  session_id: string;
  page_url?: string;
  referrer?: string;
  utm_source?: string;
  utm_medium?: string;
  utm_campaign?: string;
  tz?: string;
  lang?: string;
  events?: EventPayload[];
  errors?: ErrorPayload[];
  logs?: LogPayload[];
  vitals?: VitalSample[];
}

interface FetchLaterResult {
  readonly activated: boolean;
}

type FetchLaterFn = (
  url: string,
  init?: RequestInit & { activateAfter?: number; priority?: "high" | "low" | "auto" }
) => FetchLaterResult;

export function installFetchLaterPolyfill(): void {
  const g = globalThis as unknown as { fetchLater?: FetchLaterFn };
  if (typeof g.fetchLater === "function") return;

  g.fetchLater = function fetchLater(url, init = {}) {
    let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
    let activated = false;

    const onVisibilityChange = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        sendNow();
      }
    };

    function destroy() {
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
      if (timeoutHandle !== undefined) {
        clearTimeout(timeoutHandle);
        timeoutHandle = undefined;
      }
    }

    function sendNow() {
      if (activated) return;
      if (!(init.signal && init.signal.aborted)) {
        const hasKeepalive = typeof Request !== "undefined" && "keepalive" in Request.prototype;
        if (hasKeepalive || init.method !== "POST" || init.headers) {
          if (typeof fetch === "function") {
            fetch(url, Object.assign({}, init, { keepalive: true })).catch(() => {});
            activated = true;
          }
        } else if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
          activated = navigator.sendBeacon(url, init.body as BodyInit);
        }
      }
      destroy();
    }

    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      queueMicrotask(sendNow);
    } else {
      if (typeof document !== "undefined") {
        document.addEventListener("visibilitychange", onVisibilityChange);
      }
      const activateAfter = (init as { activateAfter?: number }).activateAfter;
      if (typeof activateAfter === "number" && activateAfter >= 0) {
        timeoutHandle = setTimeout(sendNow, activateAfter);
      }
    }

    if (init.signal) {
      init.signal.addEventListener("abort", destroy, { once: true });
    }

    return {
      get activated() {
        return activated;
      },
    };
  };
}

export interface TransportConfig {
  endpointUrl: string;
  publicKey: string;
  project: string;
  env: string;
  release: string;
  sdk?: string;
  os?: string;
  deviceType?: string;
  batchWindowMs: number;
  maxErrorsPerPage: number;
  storage?: LumaStorageAdapter;
  useFetchLater?: boolean;
  maxOutboxItems?: number;
  persistOfflineNormal?: boolean;
  getActorId: () => string;
  getSessionId: () => string;
  getPageContext: () => {
    page_url: string;
    referrer: string;
    utm_source: string;
    utm_medium: string;
    utm_campaign: string;
  };
}

interface OutboxItem {
  id: string;
  track?: "normal" | "urgent";
  body: string;
}

const MAX_FETCH_LATER_BYTES = 48 * 1024; // stay well inside ~64 KiB origin quota

function parseOutbox(raw: string | null): OutboxItem[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export class DualTrackTransport {
  private readonly cfg: TransportConfig;
  private readonly storage: LumaStorageAdapter | undefined;
  private readonly useFetchLater: boolean;
  private readonly maxOutboxItems: number;
  private readonly persistOfflineNormal: boolean;
  private readonly outboxKey: string;

  private events: EventPayload[] = [];
  private logs: LogPayload[] = [];
  private vitals: VitalSample[] = [];
  private fetchLaterController: AbortController | null = null;
  private fetchLaterResult: FetchLaterResult | null = null;
  private normalTimer: ReturnType<typeof setTimeout> | undefined;
  private errorSentCount = 0;
  private droppedErrorCount = 0;

  private outboxCache: OutboxItem[] = [];
  private readonly inFlightOutboxIds = new Set<string>();

  constructor(cfg: TransportConfig) {
    this.cfg = cfg;
    this.storage = cfg.storage ?? getBrowserLocalStorage();
    this.useFetchLater = cfg.useFetchLater ?? true;
    this.maxOutboxItems = cfg.maxOutboxItems ?? 2;
    this.persistOfflineNormal = cfg.persistOfflineNormal ?? false;
    this.outboxKey = `__luma_eye_outbox_${cfg.project}__`;

    if (this.useFetchLater) {
      installFetchLaterPolyfill();
    }
    this.initOutbox();
  }

  private initOutbox(): void {
    if (!this.storage) return;
    try {
      const res = this.storage.getItem(this.outboxKey);
      if (res && typeof (res as Promise<unknown>).then === "function") {
        (res as Promise<string | null>)
          .then((raw) => {
            const loaded = parseOutbox(raw);
            if (loaded.length > 0) {
              const seen = new Set(this.outboxCache.map((i) => i.id));
              const merged = [...loaded.filter((i) => !seen.has(i.id)), ...this.outboxCache];
              this.outboxCache = merged.slice(-this.maxOutboxItems);
              this.drainOutbox();
            }
          })
          .catch(() => {});
      } else {
        this.outboxCache = parseOutbox(res as string | null);
      }
    } catch {
      // ignore storage errors
    }
  }

  private ingestTarget(track: "normal" | "urgent"): string {
    const sep = this.cfg.endpointUrl.includes("?") ? "&" : "?";
    return `${this.cfg.endpointUrl}${sep}k=${encodeURIComponent(this.cfg.publicKey)}&p=${encodeURIComponent(this.cfg.project)}&t=${track}`;
  }

  private buildEnvelope(track: "normal" | "urgent", extra: Partial<BatchEnvelope>): BatchEnvelope {
    const pageCtx = this.cfg.getPageContext();
    let tz: string | undefined;
    let lang: string | undefined;
    try {
      if (typeof Intl !== "undefined" && typeof Intl.DateTimeFormat === "function") {
        tz = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
      }
      if (typeof navigator !== "undefined" && typeof navigator.language === "string") {
        lang = navigator.language || undefined;
      }
    } catch {
      // ignore runtime env inspection errors
    }
    return {
      key: this.cfg.publicKey,
      project: this.cfg.project,
      track,
      env: this.cfg.env,
      release: this.cfg.release,
      sdk: this.cfg.sdk || undefined,
      os: this.cfg.os || undefined,
      device_type: this.cfg.deviceType || undefined,
      actor_id: this.cfg.getActorId(),
      session_id: this.cfg.getSessionId(),
      page_url: pageCtx.page_url || undefined,
      referrer: pageCtx.referrer || undefined,
      utm_source: pageCtx.utm_source || undefined,
      utm_medium: pageCtx.utm_medium || undefined,
      utm_campaign: pageCtx.utm_campaign || undefined,
      tz,
      lang,
      ...extra,
    };
  }

  enqueueEvent(ev: EventPayload): void {
    this.resetIfActivated();
    this.events.push(ev);
    this.scheduleTrack1();
  }

  enqueueLog(lg: LogPayload): void {
    this.resetIfActivated();
    this.logs.push(lg);
    this.scheduleTrack1();
  }

  enqueueVital(vt: VitalSample): void {
    this.resetIfActivated();
    this.vitals.push(vt);
    this.scheduleTrack1();
  }

  private resetIfActivated(): void {
    if (this.fetchLaterResult?.activated) {
      this.events = [];
      this.logs = [];
      this.vitals = [];
      this.fetchLaterController = null;
      this.fetchLaterResult = null;
    }
  }

  private scheduleTrack1(): void {
    if (this.events.length === 0 && this.logs.length === 0 && this.vitals.length === 0) {
      return;
    }
    const payload = JSON.stringify(
      this.buildEnvelope("normal", {
        events: this.events.length ? this.events : undefined,
        logs: this.logs.length ? this.logs : undefined,
        vitals: this.vitals.length ? this.vitals : undefined,
      })
    );

    if (payload.length >= MAX_FETCH_LATER_BYTES || this.events.length + this.logs.length + this.vitals.length >= 100) {
      this.flushNormal();
      return;
    }

    if (!this.useFetchLater) {
      if (this.normalTimer !== undefined) {
        clearTimeout(this.normalTimer);
      }
      this.normalTimer = setTimeout(() => {
        this.normalTimer = undefined;
        this.flushNormal();
      }, this.cfg.batchWindowMs);
      return;
    }

    if (this.fetchLaterController) {
      this.fetchLaterController.abort();
    }
    this.fetchLaterController = new AbortController();

    const g = globalThis as unknown as { fetchLater?: FetchLaterFn };
    if (typeof g.fetchLater === "function") {
      try {
        // Send as string body without custom headers -> text/plain simple CORS request, zero preflight.
        this.fetchLaterResult = g.fetchLater(this.ingestTarget("normal"), {
          method: "POST",
          body: payload,
          signal: this.fetchLaterController.signal,
          activateAfter: this.cfg.batchWindowMs,
          priority: "low",
        });
      } catch {
        this.flushNormal();
      }
    }
  }

  flushNormal(): void {
    if (this.normalTimer !== undefined) {
      clearTimeout(this.normalTimer);
      this.normalTimer = undefined;
    }
    if (this.fetchLaterResult?.activated) {
      this.events = [];
      this.logs = [];
      this.vitals = [];
      this.fetchLaterController = null;
      this.fetchLaterResult = null;
      return;
    }
    if (this.fetchLaterController) {
      this.fetchLaterController.abort();
      this.fetchLaterController = null;
    }
    this.fetchLaterResult = null;

    if (this.events.length === 0 && this.logs.length === 0 && this.vitals.length === 0) {
      return;
    }
    const body = JSON.stringify(
      this.buildEnvelope("normal", {
        events: this.events.length ? this.events : undefined,
        logs: this.logs.length ? this.logs : undefined,
        vitals: this.vitals.length ? this.vitals : undefined,
      })
    );
    this.events = [];
    this.logs = [];
    this.vitals = [];
    this.sendImmediate(this.ingestTarget("normal"), body, true, {
      persistOnFail: this.persistOfflineNormal,
    });
  }

  /**
   * Track 2 urgent dispatch: attaches full Ring Buffer snapshot, saves to Crash Outbox,
   * and immediately dispatches via sendBeacon / fetch.
   */
  dispatchUrgent(errPayload: ErrorPayload): boolean {
    if (this.errorSentCount >= this.cfg.maxErrorsPerPage) {
      this.droppedErrorCount++;
      return false;
    }
    this.errorSentCount++;

    if (this.droppedErrorCount > 0) {
      errPayload.extra = {
        ...(errPayload.extra || {}),
        luma_eye_client_dropped_errors: this.droppedErrorCount,
      };
      this.droppedErrorCount = 0;
    }

    const body = JSON.stringify(
      this.buildEnvelope("urgent", {
        errors: [errPayload],
      })
    );

    const outboxId = this.saveToOutbox(body, "urgent");
    return this.sendImmediate(this.ingestTarget("urgent"), body, false, {
      outboxId: outboxId ?? undefined,
      persistOnFail: true,
    });
  }

  drainOutbox(): void {
    const items = [...this.outboxCache];
    for (const item of items) {
      if (this.inFlightOutboxIds.has(item.id)) continue;
      this.inFlightOutboxIds.add(item.id);
      const track = item.track ?? "urgent";
      const lowPriority = track === "normal";
      const dispatched = this.sendImmediate(this.ingestTarget(track), item.body, lowPriority, {
        outboxId: item.id,
      });
      if (!dispatched) {
        this.inFlightOutboxIds.delete(item.id);
      }
    }
  }

  private sendImmediate(
    url: string,
    body: string,
    lowPriority: boolean,
    opts?: { outboxId?: string; persistOnFail?: boolean }
  ): boolean {
    const markFailed = () => {
      if (opts?.outboxId) {
        this.inFlightOutboxIds.delete(opts.outboxId);
      } else if (opts?.persistOnFail) {
        this.saveToOutbox(body, lowPriority ? "normal" : "urgent");
      }
    };

    try {
      if (!lowPriority && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
        if (navigator.sendBeacon(url, body)) {
          if (opts?.outboxId) this.removeFromOutbox(opts.outboxId);
          return true;
        }
      }
      if (typeof fetch === "function") {
        const init: RequestInit & { priority?: "low" | "high" | "auto" } = {
          method: "POST",
          body,
          keepalive: true,
        };
        if (lowPriority) {
          init.priority = "low";
        }
        const p = fetch(url, init);
        if (p && typeof p.then === "function") {
          p.then(
            (resp) => {
              if (!resp || resp.status < 500) {
                if (opts?.outboxId) this.removeFromOutbox(opts.outboxId);
              } else {
                markFailed();
              }
            },
            () => {
              markFailed();
            }
          );
        }
        return true;
      }
      if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
        if (navigator.sendBeacon(url, body)) {
          if (opts?.outboxId) this.removeFromOutbox(opts.outboxId);
          return true;
        }
      }
    } catch {
      // fall through to markFailed
    }
    markFailed();
    return false;
  }

  private syncOutboxStorage(): void {
    if (!this.storage) return;
    try {
      const r =
        this.outboxCache.length === 0
          ? this.storage.removeItem(this.outboxKey)
          : this.storage.setItem(this.outboxKey, JSON.stringify(this.outboxCache));
      if (r && typeof (r as Promise<unknown>).catch === "function") {
        (r as Promise<void>).catch(() => {});
      }
    } catch {
      // ignore quota / storage errors
    }
  }

  private saveToOutbox(body: string, track: "normal" | "urgent"): string | null {
    if (body.length > 32 * 1024) return null;
    const id = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    this.outboxCache = [...this.outboxCache, { id, track, body }].slice(-this.maxOutboxItems);
    this.syncOutboxStorage();
    return id;
  }

  private removeFromOutbox(id: string): void {
    this.inFlightOutboxIds.delete(id);
    const next = this.outboxCache.filter((it) => it.id !== id);
    if (next.length !== this.outboxCache.length) {
      this.outboxCache = next;
      this.syncOutboxStorage();
    }
  }
}
