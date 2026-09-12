import { useEffect, useState } from "react";
import { fetchMetricsHistoryBatch, historyKey, type HistoryState, type HistoryTarget } from "./metricsApi";

const HISTORY_REFRESH_MS = 30000;
const HISTORY_TIMEOUT_MS = 20000;

/** One bounded request, with refresh deduplication and cancellation on navigation. */
export function useMetricsHistories(token: string, targets: HistoryTarget[], historyWindow: number) {
  const signature = JSON.stringify(targets.map((item) => [item.kind, item.name]).sort());
  const requestKey = JSON.stringify([token, signature, historyWindow]);
  const [state, setState] = useState<{ requestKey: string; histories: Record<string, HistoryState> }>({ requestKey: "", histories: {} });

  useEffect(() => {
    if (!token || !targets.length) return;
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const load = async () => {
      if (cancelled || controller || document.visibilityState === "hidden") return;
      window.clearTimeout(timer);
      const active = new AbortController();
      controller = active;
      const timeout = window.setTimeout(() => active.abort(), HISTORY_TIMEOUT_MS);
      let next: Record<string, HistoryState>;
      try {
        const batch = await fetchMetricsHistoryBatch({ token, targets, window: historyWindow, signal: active.signal });
        if (active.signal.aborted) throw new Error("Metrics history request timed out");
        const results = new Map(batch.results.map((item) => [historyKey(item.kind, item.name), item]));
        next = Object.fromEntries(targets.map((target) => {
          const key = historyKey(target.kind, target.name);
          const result = results.get(key);
          return [key, result?.error ? { error: result.error } : result?.payload ? { payload: result.payload } : { error: "History response omitted this target" }];
        }));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        next = Object.fromEntries(targets.map((target) => [historyKey(target.kind, target.name), { error: message }]));
      } finally {
        window.clearTimeout(timeout);
      }
      // Hidden pages and old selections may finish after their abort; neither
      // may overwrite the current selection or start another polling timer.
      if (cancelled || controller !== active) return;
      controller = undefined;
      setState((previous) => {
        const histories = previous.requestKey === requestKey ? previous.histories : {};
        return { requestKey, histories: Object.fromEntries(Object.entries(next).map(([key, value]) => [
          key, value.error ? { payload: histories[key]?.payload, error: value.error } : value,
        ])) };
      });
      timer = window.setTimeout(() => void load(), HISTORY_REFRESH_MS);
    };
    const reload = () => { void load(); };
    const visibilityChanged = () => {
      if (document.visibilityState === "hidden") {
        window.clearTimeout(timer);
        controller?.abort();
        controller = undefined;
      } else {
        void load();
      }
    };
    window.addEventListener("luma:refresh", reload);
    document.addEventListener("visibilitychange", visibilityChanged);
    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearTimeout(timer);
      window.removeEventListener("luma:refresh", reload);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
    // The signature covers all targets without restarting on dashboard refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, signature, historyWindow]);
  return state.requestKey === requestKey ? state.histories : {};
}
