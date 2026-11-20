import { useCallback, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, apiGet } from "./apiClient";
import type { DashboardScope } from "./dashboardScope";
import { useDashboardQueryClient, withTimeoutSignal } from "./queryClient";
import type { DashboardPayload, SyncStatus } from "./types";

const REFRESH_MS = 30_000;
const REQUEST_TIMEOUT_MS = 20_000;
const SNAPSHOT_TTL_MS = 60_000;

function isUnauthorizedError(error: unknown): boolean {
  if (error instanceof ApiError && error.status === 401) return true;
  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: unknown }).status === 401
  ) {
    return true;
  }
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /unauthorized|bearer token/i.test(message);
}

// `token` is the credential from useDashboardAuth: a session marker in a signed-in
// browser. `onUnauthorized` runs when Control rejects it, for example after the
// session expired or was revoked elsewhere.
export function useDashboardData(
  scope: DashboardScope = "full",
  query = "",
  token = "",
  onUnauthorized?: () => void,
) {
  const queryClient = useDashboardQueryClient();
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  const [rejected, setRejected] = useState<{ token: string; message: string } | null>(null);
  const isTokenRejected = Boolean(token) && rejected?.token === token;
  const enabled = Boolean(token) && scope !== "none" && !isTokenRejected;

  const queryResult = useQuery<DashboardPayload>(
    {
      queryKey: ["dashboard", scope, query, token] as const,
      enabled,
      staleTime: 0,
      gcTime: SNAPSHOT_TTL_MS,
      refetchInterval: enabled ? REFRESH_MS : false,
      refetchIntervalInBackground: false,
      placeholderData: (previousData, previousQuery) => {
        if (!previousData || !previousQuery) return undefined;
        const [, prevScope, prevQuery, prevToken] = previousQuery.queryKey as readonly [
          string,
          DashboardScope,
          string,
          string,
        ];
        if (prevToken !== token) return undefined;
        // List filters retain their mounted view while refreshing; different objects do not.
        if (scope === "application") {
          return prevScope === scope && prevQuery === query ? keepPreviousData(previousData) : undefined;
        }
        return prevScope === scope ? keepPreviousData(previousData) : undefined;
      },
      queryFn: async ({ signal }) => {
        const timed = withTimeoutSignal(signal, REQUEST_TIMEOUT_MS);
        try {
          return await apiGet<DashboardPayload>(
            `/v1/dashboard?scope=${scope}${query ? `&${query}` : ""}`,
            token,
            timed.signal,
          );
        } catch (error) {
          if (!signal.aborted && isUnauthorizedError(error)) {
            const message = error instanceof Error ? error.message : String(error);
            setRejected({ token, message });
            queryClient.removeQueries({ queryKey: ["dashboard"] });
            onUnauthorizedRef.current?.();
          }
          throw error;
        } finally {
          timed.cleanup();
        }
      },
    },
    queryClient,
  );

  const queryResultRef = useRef(queryResult);
  queryResultRef.current = queryResult;
  const inFlightRef = useRef<{ key: string; promise: Promise<void> } | null>(null);
  const keyString = `${scope}?${query}@${token}`;

  const loadDashboard = useCallback((): Promise<void> => {
    if (!token || scope === "none" || isTokenRejected) return Promise.resolve();
    if (inFlightRef.current?.key === keyString) return inFlightRef.current.promise;
    const promise = queryResultRef.current
      .refetch({ cancelRefetch: false })
      .then(() => undefined)
      .finally(() => {
        if (inFlightRef.current?.promise === promise) inFlightRef.current = null;
      });
    inFlightRef.current = { key: keyString, promise };
    return promise;
  }, [token, scope, isTokenRejected, keyString]);

  const rawPayload = enabled ? (queryResult.data ?? null) : null;
  const isFreshEnough =
    queryResult.isPlaceholderData ||
    queryResult.isError ||
    !queryResult.dataUpdatedAt ||
    Date.now() - queryResult.dataUpdatedAt <= SNAPSHOT_TTL_MS;
  const payload = rawPayload && isFreshEnough ? rawPayload : null;

  const errors = useMemo<string[]>(() => {
    if (isTokenRejected && rejected?.message) return [rejected.message];
    if (queryResult.error) {
      return [queryResult.error instanceof Error ? queryResult.error.message : String(queryResult.error)];
    }
    return payload?.errors || [];
  }, [isTokenRejected, rejected?.message, queryResult.error, payload?.errors]);

  const syncStatus: SyncStatus = !token
    ? "notConnected"
    : isTokenRejected
      ? "tokenRejected"
      : scope === "none"
        ? "updated"
        : queryResult.isFetching
          ? "refreshing"
          : queryResult.isError
            ? "unavailable"
            : queryResult.dataUpdatedAt > 0
              ? "updated"
              : "refreshing";

  const lastUpdated = useMemo(
    () => (payload && queryResult.dataUpdatedAt > 0 ? new Date(queryResult.dataUpdatedAt) : null),
    [payload, queryResult.dataUpdatedAt],
  );

  return {
    token,
    payload,
    cluster: payload?.cluster,
    errors,
    syncStatus,
    lastUpdated,
    loadDashboard,
  };
}
