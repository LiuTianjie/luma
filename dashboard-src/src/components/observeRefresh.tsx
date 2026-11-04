import { createContext, useContext, useEffect, useRef, useSyncExternalStore } from "react";
import { RefreshCw } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { formatDateTime } from "../format";
import type { Lang } from "../types";

/**
 * One refresh control per observability page. The page header owns the button and
 * the "updated at" time; panels report successful reads through `markUpdated` and
 * reload on the shared `luma:refresh` event. Panels rendered outside the observe
 * workspace (application tabs) see `hosted: false` and keep their own controls.
 */
type RefreshState = { updatedAt: number; refreshing: boolean };

export type ObserveRefreshStore = {
  hosted: boolean;
  markUpdated: (at?: number | Date) => void;
  setRefreshing: (value: boolean) => void;
  reset: () => void;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => RefreshState;
};

const IDLE: RefreshState = { updatedAt: 0, refreshing: false };

export function createObserveRefreshStore(): ObserveRefreshStore {
  let state = IDLE;
  const listeners = new Set<() => void>();
  const set = (next: RefreshState) => {
    if (next.updatedAt === state.updatedAt && next.refreshing === state.refreshing) return;
    state = next;
    listeners.forEach((listener) => listener());
  };
  return {
    hosted: true,
    markUpdated: (at) => {
      const ms = at instanceof Date ? at.getTime() : typeof at === "number" ? (at < 1e12 ? at * 1000 : at) : Date.now();
      set({ updatedAt: ms, refreshing: false });
    },
    setRefreshing: (refreshing) => set({ ...state, refreshing }),
    reset: () => set(IDLE),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => state,
  };
}

const DETACHED: ObserveRefreshStore = {
  hosted: false,
  markUpdated: () => {},
  setRefreshing: () => {},
  reset: () => {},
  subscribe: () => () => {},
  getSnapshot: () => IDLE,
};

const ObserveRefreshContext = createContext<ObserveRefreshStore>(DETACHED);
export const ObserveRefreshProvider = ObserveRefreshContext.Provider;

export function useObserveRefresh() {
  return useContext(ObserveRefreshContext);
}

/** Run `callback` whenever the page asks every panel to reload. */
export function useRefreshEvent(callback: () => void) {
  const ref = useRef(callback);
  useEffect(() => {
    ref.current = callback;
  });
  useEffect(() => {
    const handler = () => ref.current();
    window.addEventListener("luma:refresh", handler);
    return () => window.removeEventListener("luma:refresh", handler);
  }, []);
}

/** Time of day for today's reads, a full date-time otherwise. */
export function formatClock(value: number | Date, lang: Lang) {
  const date = value instanceof Date ? value : new Date(value < 1e12 ? value * 1000 : value);
  const today = new Date();
  if (date.toDateString() !== today.toDateString()) return formatDateTime(date, lang);
  return date.toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function ObserveRefreshControl({ lang, intervalSeconds }: { lang: Lang; intervalSeconds?: number }) {
  const zh = lang === "zh";
  const store = useObserveRefresh();
  const { updatedAt, refreshing } = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const fallback = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(fallback.current), []);
  const refresh = () => {
    store.setRefreshing(true);
    window.clearTimeout(fallback.current);
    // A panel that cannot report (an embedded Grafana view) still ends the spin.
    fallback.current = window.setTimeout(() => store.setRefreshing(false), 8000);
    window.dispatchEvent(new CustomEvent("luma:refresh"));
  };
  const cadence = intervalSeconds
    ? zh ? `每 ${intervalSeconds} 秒自动刷新` : `Refreshes every ${intervalSeconds} seconds`
    : zh ? "重新读取当前页面数据" : "Reload this page's data";
  return (
    <div className="flex items-center gap-3">
      {updatedAt ? (
        <span className="hidden text-xs text-muted-foreground tabular-nums sm:inline" role="status">
          {zh ? "数据更新于 " : "Updated "}
          {formatClock(updatedAt, lang)}
        </span>
      ) : null}
      <Tooltip>
        <TooltipTrigger render={<Button type="button" variant="outline" size="sm" onClick={refresh} aria-busy={refreshing} />}>
          <RefreshCw data-icon="inline-start" className={cn(refreshing && "animate-spin")} aria-hidden="true" />
          {zh ? "刷新" : "Refresh"}
        </TooltipTrigger>
        <TooltipContent>{cadence}</TooltipContent>
      </Tooltip>
    </div>
  );
}
