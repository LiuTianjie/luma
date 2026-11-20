import type { LumaStorageAdapter } from "./storage";

export const DEFAULT_SESSION_IDLE_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes (GA4 / PostHog / APMPlus standard)
export const DEFAULT_SESSION_MAX_DURATION_MS = 24 * 60 * 60 * 1000; // 24 hours hard cap
const PERSIST_THROTTLE_MS = 5_000;
const ACTOR_COOKIE_MAX_AGE_SEC = 365 * 24 * 3600;

interface SessionState {
  id: string;
  s: number; // startedAt (ms)
  a: number; // lastActiveAt (ms)
}

export function makeId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}_${crypto.randomUUID().replace(/-/g, "").slice(0, 16)}`;
  }
  return `${prefix}_${Math.random().toString(36).slice(2, 12)}${Date.now().toString(36)}`;
}

let cachedCookieDomain: string | null | undefined;

function isIpOrLocalhost(host: string): boolean {
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.includes(":")) return true;
  return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);
}

function resolveRootCookieDomain(): string | null {
  if (cachedCookieDomain !== undefined) return cachedCookieDomain;
  if (typeof document === "undefined" || typeof document.cookie !== "string" || typeof location === "undefined") {
    cachedCookieDomain = null;
    return null;
  }
  const host = (location.hostname || "").toLowerCase();
  if (isIpOrLocalhost(host)) {
    cachedCookieDomain = null;
    return null;
  }
  const parts = host.split(".");
  if (parts.length < 2) {
    cachedCookieDomain = null;
    return null;
  }
  const probeKey = "__le_dprobe__";
  for (let i = parts.length - 2; i >= 0; i--) {
    const candidate = `.${parts.slice(i).join(".")}`;
    try {
      document.cookie = `${probeKey}=1; domain=${candidate}; path=/; max-age=10; samesite=lax`;
      if (document.cookie.includes(`${probeKey}=1`)) {
        document.cookie = `${probeKey}=; domain=${candidate}; path=/; max-age=0; samesite=lax`;
        cachedCookieDomain = candidate;
        return candidate;
      }
    } catch {
      // ignore cookie write errors
    }
  }
  cachedCookieDomain = null;
  return null;
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined" || typeof document.cookie !== "string" || !document.cookie) return null;
  const target = `${name}=`;
  const parts = document.cookie.split(";");
  for (const raw of parts) {
    const item = raw.trim();
    if (item.startsWith(target)) {
      try {
        return decodeURIComponent(item.slice(target.length));
      } catch {
        return item.slice(target.length);
      }
    }
  }
  return null;
}

function writeCookie(name: string, value: string, maxAgeSec: number, crossSubdomain: boolean): void {
  if (typeof document === "undefined" || typeof document.cookie !== "string") return;
  try {
    const domain = crossSubdomain ? resolveRootCookieDomain() : null;
    const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; secure" : "";
    const domainAttr = domain ? `; domain=${domain}` : "";
    document.cookie = `${name}=${encodeURIComponent(value)}${domainAttr}; path=/; max-age=${Math.max(1, Math.floor(maxAgeSec))}; samesite=lax${secure}`;
  } catch {
    // ignore cookie errors
  }
}

function sanitizeSlugKey(project: string): string {
  return project.replace(/[^a-zA-Z0-9_-]/g, "_");
}

function parseSessionRaw(raw: string | null | undefined): SessionState | null {
  if (!raw) return null;
  if (raw.startsWith("{")) {
    try {
      const p = JSON.parse(raw) as Partial<SessionState>;
      if (typeof p.id === "string" && p.id && typeof p.s === "number" && typeof p.a === "number") {
        return { id: p.id, s: p.s, a: p.a };
      }
    } catch {
      return null;
    }
  }
  const segs = raw.split(".");
  if (segs.length === 3 && segs[0]) {
    const s = Number(segs[1]);
    const a = Number(segs[2]);
    if (Number.isFinite(s) && Number.isFinite(a) && s > 0 && a > 0) {
      return { id: segs[0], s, a };
    }
  }
  return null;
}

export function resolveBrowserActorId(
  project: string,
  storage: Storage | undefined,
  crossSubdomainCookie = true
): { getActorId: () => string; setActorId: (nextId: string) => void } {
  const storageKey = `__luma_eye_uid_${project}__`;
  const cookieKey = `_le_u_${sanitizeSlugKey(project)}`;

  let current = "";
  try {
    current = storage?.getItem(storageKey) || "";
  } catch {
    // ignore
  }
  const fromCookie = readCookie(cookieKey) || "";
  if (!current && fromCookie) {
    current = fromCookie;
  }
  if (!current) {
    current = makeId("u");
  }

  const persist = (val: string) => {
    try {
      storage?.setItem(storageKey, val);
    } catch {
      // ignore
    }
    writeCookie(cookieKey, val, ACTOR_COOKIE_MAX_AGE_SEC, crossSubdomainCookie);
  };
  persist(current);

  return {
    getActorId: () => current,
    setActorId: (nextId: string) => {
      if (!nextId) return;
      current = nextId;
      persist(current);
    },
  };
}

export interface SessionManagerOptions {
  project: string;
  storage?: LumaStorageAdapter;
  idleTimeoutMs?: number;
  maxDurationMs?: number;
  crossSubdomainCookie?: boolean;
  useCookies?: boolean;
  now?: () => number;
}

export class SessionManager {
  private readonly storageKey: string;
  private readonly cookieKey: string;
  private readonly storage: LumaStorageAdapter | undefined;
  private readonly idleTimeoutMs: number;
  private readonly maxDurationMs: number;
  private readonly crossSubdomainCookie: boolean;
  private readonly useCookies: boolean;
  private readonly nowFn: () => number;

  private state: SessionState;
  private lastPersistedAt = 0;

  constructor(opts: SessionManagerOptions) {
    this.storageKey = `__luma_eye_sess_${opts.project}__`;
    this.cookieKey = `_le_s_${sanitizeSlugKey(opts.project)}`;
    this.storage = opts.storage;
    this.idleTimeoutMs = opts.idleTimeoutMs && opts.idleTimeoutMs > 0 ? opts.idleTimeoutMs : DEFAULT_SESSION_IDLE_TIMEOUT_MS;
    this.maxDurationMs = opts.maxDurationMs && opts.maxDurationMs > 0 ? opts.maxDurationMs : DEFAULT_SESSION_MAX_DURATION_MS;
    this.crossSubdomainCookie = opts.crossSubdomainCookie ?? true;
    this.useCookies = opts.useCookies ?? false;
    this.nowFn = opts.now ?? (() => Date.now());

    const ts = this.nowFn();
    this.state = { id: makeId("s"), s: ts, a: ts };
    this.hydrateInitial(ts);
  }

  private isValid(st: SessionState, ts: number): boolean {
    return ts - st.a <= this.idleTimeoutMs && ts - st.s <= this.maxDurationMs;
  }

  private hydrateInitial(ts: number): void {
    let hydratedSync = false;
    if (this.storage) {
      try {
        const raw = this.storage.getItem(this.storageKey);
        if (raw && typeof (raw as Promise<unknown>).then === "function") {
          (raw as Promise<string | null>)
            .then((resolved) => {
              const nowTs = this.nowFn();
              const parsed = parseSessionRaw(resolved);
              if (parsed && this.isValid(parsed, nowTs)) {
                this.state = { id: parsed.id, s: parsed.s, a: Math.max(parsed.a, this.state.a, nowTs) };
                this.persist(nowTs, true);
              } else {
                this.persist(nowTs, true);
              }
            })
            .catch(() => {});
          return;
        }
        const parsed = parseSessionRaw(raw as string | null);
        if (parsed && this.isValid(parsed, ts)) {
          this.state = { id: parsed.id, s: parsed.s, a: ts };
          hydratedSync = true;
        }
      } catch {
        // ignore storage errors
      }
    }

    if (!hydratedSync && this.useCookies) {
      const parsedCookie = parseSessionRaw(readCookie(this.cookieKey));
      if (parsedCookie && this.isValid(parsedCookie, ts)) {
        this.state = { id: parsedCookie.id, s: parsedCookie.s, a: ts };
      }
    }

    this.persist(ts, true);
  }

  private syncFromSharedStorage(ts: number): void {
    if (this.storage) {
      try {
        const raw = this.storage.getItem(this.storageKey);
        if (typeof raw === "string") {
          const parsed = parseSessionRaw(raw);
          if (parsed && this.isValid(parsed, ts) && parsed.a >= this.state.a) {
            this.state = parsed;
            return;
          }
        }
      } catch {
        // ignore
      }
    }
    if (this.useCookies) {
      const parsedCookie = parseSessionRaw(readCookie(this.cookieKey));
      if (parsedCookie && this.isValid(parsedCookie, ts) && parsedCookie.a >= this.state.a) {
        this.state = parsedCookie;
      }
    }
  }

  private persist(ts: number, force: boolean): void {
    if (!force && ts - this.lastPersistedAt < PERSIST_THROTTLE_MS) {
      return;
    }
    this.lastPersistedAt = ts;
    if (this.storage) {
      try {
        const w = this.storage.setItem(this.storageKey, JSON.stringify(this.state));
        if (w && typeof (w as Promise<unknown>).catch === "function") {
          (w as Promise<void>).catch(() => {});
        }
      } catch {
        // ignore
      }
    }
    if (this.useCookies) {
      writeCookie(
        this.cookieKey,
        `${this.state.id}.${this.state.s}.${this.state.a}`,
        Math.ceil(this.idleTimeoutMs / 1000),
        this.crossSubdomainCookie
      );
    }
  }

  /**
   * Records user activity and returns the active session_id, rotating to a fresh
   * session if idle > idleTimeoutMs (default 30m) or duration > maxDurationMs (default 24h).
   */
  touch(): string {
    const ts = this.nowFn();
    this.syncFromSharedStorage(ts);
    if (!this.isValid(this.state, ts)) {
      this.state = { id: makeId("s"), s: ts, a: ts };
      this.persist(ts, true);
      return this.state.id;
    }
    this.state.a = ts;
    this.persist(ts, false);
    return this.state.id;
  }

  getSessionId(): string {
    return this.touch();
  }
}

export function resetCookieDomainCacheForTests(): void {
  cachedCookieDomain = undefined;
}
