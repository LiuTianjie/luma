import { useContext } from "react";
import { QueryClient, QueryClientContext } from "@tanstack/react-query";
import { ApiError } from "./apiClient.ts";

export const DASHBOARD_STALE_TIME_MS = 10_000;

export class RequestTimeoutError extends Error {
  constructor(message = "Request timed out") {
    super(message);
    this.name = "RequestTimeoutError";
  }
}

export function shouldRetryDashboardQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false;
  if (error instanceof RequestTimeoutError) return false;
  if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) {
    return false;
  }
  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    typeof (error as { status?: unknown }).status === "number"
  ) {
    const status = (error as { status: number }).status;
    if (status === 401 || status === 403 || status === 404) return false;
  }
  return true;
}

export function createDashboardQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetryDashboardQuery,
        refetchOnWindowFocus: true,
        refetchIntervalInBackground: false,
        staleTime: DASHBOARD_STALE_TIME_MS,
      },
    },
  });
}

export const queryClient = createDashboardQueryClient();

/**
 * Returns the nearest QueryClientProvider's client, falling back to the
 * shared singleton so isolated SSR/unit renders never throw "No QueryClient set".
 */
export function useDashboardQueryClient(): QueryClient {
  return useContext(QueryClientContext) ?? queryClient;
}

/**
 * Invalidate active TanStack Query caches and emit `luma:refresh` for any
 * remaining panels that still listen to the legacy window event.
 */
export async function invalidateDashboardQueries(
  queryKey?: readonly unknown[],
  client: QueryClient = queryClient,
): Promise<void> {
  if (
    !queryKey &&
    typeof window !== "undefined" &&
    typeof window.dispatchEvent === "function" &&
    typeof CustomEvent === "function"
  ) {
    window.dispatchEvent(new CustomEvent("luma:refresh"));
  }
  await client.invalidateQueries(queryKey ? { queryKey } : undefined);
}

/**
 * Derive an AbortSignal bounded by both a parent TanStack Query signal and a
 * per-request deadline. Uses window timers when present so deterministic test
 * runners can drive timeout behavior without real wall-clock delays.
 */
export function withTimeoutSignal(
  parentSignal: AbortSignal | undefined,
  timeoutMs: number,
  timeoutMessage = "Request timed out",
): { signal: AbortSignal; didTimeout: () => boolean; cleanup: () => void } {
  const controller = new AbortController();
  let timedOut = false;
  const onParentAbort = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) {
    controller.abort(parentSignal.reason);
  } else {
    parentSignal?.addEventListener("abort", onParentAbort, { once: true });
  }
  const setTimer =
    typeof window !== "undefined" && typeof window.setTimeout === "function"
      ? window.setTimeout.bind(window)
      : setTimeout;
  const clearTimer =
    typeof window !== "undefined" && typeof window.clearTimeout === "function"
      ? window.clearTimeout.bind(window)
      : clearTimeout;
  const timer = setTimer(() => {
    timedOut = true;
    controller.abort(new RequestTimeoutError(timeoutMessage));
  }, timeoutMs);
  const cleanup = () => {
    clearTimer(timer);
    parentSignal?.removeEventListener("abort", onParentAbort);
  };
  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    cleanup,
  };
}
