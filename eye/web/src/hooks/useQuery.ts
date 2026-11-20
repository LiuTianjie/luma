import { useCallback, useEffect, useRef, useState } from "react";

export interface QueryState<T> {
  data: T | null;
  loading: boolean;
  refreshing: boolean;
  error: string;
  reload: () => void;
}

/**
 * Deterministic, AbortController-backed query hook that prevents infinite render/fetch loops:
 * - `fetcher` is read through a ref (`fetcherRef.current`), NEVER placed in `useEffect` dependencies.
 * - `useEffect` fires strictly once per distinct primitive `key` (or explicit `reload()`).
 * - Never mutates global context state on start/finish/error, eliminating cascading re-renders.
 * - Aborts in-flight requests cleanly when `key` changes or component unmounts.
 */
export function useQuery<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
): QueryState<T> {
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const [manualTick, setManualTick] = useState(0);
  const [state, setState] = useState<{
    key: string | null;
    data: T | null;
    loading: boolean;
    refreshing: boolean;
    error: string;
  }>({
    key,
    data: null,
    loading: key !== null,
    refreshing: false,
    error: "",
  });

  useEffect(() => {
    if (key === null) {
      setState({ key: null, data: null, loading: false, refreshing: false, error: "" });
      return;
    }

    const controller = new AbortController();
    let active = true;

    setState((prev) => {
      const hasData = prev.data !== null;
      return {
        key,
        data: prev.data,
        loading: !hasData,
        refreshing: hasData,
        error: "",
      };
    });

    fetcherRef
      .current(controller.signal)
      .then((result) => {
        if (!active || controller.signal.aborted) return;
        setState({
          key,
          data: result,
          loading: false,
          refreshing: false,
          error: "",
        });
      })
      .catch((err: unknown) => {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        const msg = err instanceof Error ? err.message : String(err);
        setState((prev) => ({
          key,
          data: prev.data,
          loading: false,
          refreshing: false,
          error: msg,
        }));
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [key, manualTick]);

  const reload = useCallback(() => {
    setManualTick((t) => t + 1);
  }, []);

  return {
    data: state.data,
    loading: state.loading,
    refreshing: state.refreshing,
    error: state.error,
    reload,
  };
}
