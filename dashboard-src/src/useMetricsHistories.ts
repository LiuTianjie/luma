import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchMetricsHistoryBatch, historyKey, type HistoryState, type HistoryTarget } from "./metricsApi";
import { RequestTimeoutError, useDashboardQueryClient, withTimeoutSignal } from "./queryClient";

const HISTORY_REFRESH_MS = 30_000;
const HISTORY_TIMEOUT_MS = 20_000;

/** One bounded request, with refresh deduplication and cancellation on navigation. */
export function useMetricsHistories(
  token: string,
  targets: HistoryTarget[],
  historyWindow: number,
  until?: number,
): Record<string, HistoryState> {
  const queryClient = useDashboardQueryClient();
  const signature = JSON.stringify(targets.map((item) => [item.kind, item.name]).sort());
  const targetsRef = useRef(targets);
  targetsRef.current = targets;
  const enabled = Boolean(token) && targets.length > 0;
  const queryKey = useMemo(
    () => ["metrics-history", token, signature, historyWindow, until ?? null] as const,
    [token, signature, historyWindow, until],
  );
  const queryKeyRef = useRef(queryKey);
  queryKeyRef.current = queryKey;

  const query = useQuery<Record<string, HistoryState>>(
    {
      queryKey,
      enabled,
      refetchInterval: enabled ? HISTORY_REFRESH_MS : false,
      refetchIntervalInBackground: false,
      refetchOnWindowFocus: (activeQuery) => (activeQuery.state.status === "error" ? "always" : true),
      queryFn: async ({ signal }) => {
        const currentTargets = targetsRef.current;
        const timed = withTimeoutSignal(signal, HISTORY_TIMEOUT_MS, "Metrics history request timed out");
        try {
          const batch = await fetchMetricsHistoryBatch({
            token,
            targets: currentTargets,
            window: historyWindow,
            until,
            signal: timed.signal,
          });
          if (timed.didTimeout()) {
            throw new RequestTimeoutError("Metrics history request timed out");
          }
          const previous = queryClient.getQueryData<Record<string, HistoryState>>(queryKey) ?? {};
          const results = new Map(batch.results.map((item) => [historyKey(item.kind, item.name), item]));
          return Object.fromEntries(
            currentTargets.map((target) => {
              const key = historyKey(target.kind, target.name);
              const result = results.get(key);
              if (result?.error) {
                return [key, { payload: previous[key]?.payload, error: result.error }];
              }
              if (result?.payload) {
                return [key, { payload: result.payload }];
              }
              return [key, { payload: previous[key]?.payload, error: "History response omitted this target" }];
            }),
          );
        } catch (error) {
          if (timed.didTimeout()) {
            throw new RequestTimeoutError(
              error instanceof Error && error.message ? error.message : "Metrics history request timed out",
            );
          }
          throw error;
        } finally {
          timed.cleanup();
        }
      },
    },
    queryClient,
  );

  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof window.addEventListener !== "function") return;
    const onRefresh = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void queryClient.refetchQueries({ queryKey: queryKeyRef.current, type: "active" }, { cancelRefetch: false });
    };
    window.addEventListener("luma:refresh", onRefresh);
    return () => window.removeEventListener("luma:refresh", onRefresh);
  }, [enabled, queryClient]);

  return useMemo(() => {
    if (!enabled) return {};
    const base = query.data ?? {};
    if (!query.error) return base;
    const message = query.error instanceof Error ? query.error.message : String(query.error);
    return Object.fromEntries(
      targets.map((target) => {
        const key = historyKey(target.kind, target.name);
        return [key, { payload: base[key]?.payload, error: message }];
      }),
    );
  }, [enabled, query.data, query.error, targets]);
}
