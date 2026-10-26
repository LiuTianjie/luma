import { useCallback, useEffect, useRef, useState } from "react";
import { authHeaders } from "./apiClient";
import type { DashboardScope } from "./dashboardScope";
import type { DashboardPayload, SyncStatus } from "./types";

const REFRESH_MS = 30000;
const SNAPSHOT_TTL_MS = 60000;
const SNAPSHOT_LIMIT = 8;
type Snapshot = { token: string; identity: string; payload: DashboardPayload; updatedAt: number };

// `token` is the credential from useDashboardAuth: a session marker in a signed-in
// browser. `onUnauthorized` runs when Control rejects it, for example after the
// session expired or was revoked elsewhere.
export function useDashboardData(scope: DashboardScope = "full", query = "", token = "", onUnauthorized?: () => void) {
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  // List filters retain their mounted view while refreshing; different objects do not.
  const identity = scope === "application" ? `${scope}:${query}` : scope;
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const cacheRef = useRef(new Map<string, Snapshot>());
  const cacheKey = `${scope}?${query}`;
  const cached = cacheRef.current.get(cacheKey);
  const recent = cached?.token === token && Date.now() - cached.updatedAt <= SNAPSHOT_TTL_MS ? cached : null;
  const visibleSnapshot = snapshot?.token === token && snapshot.identity === identity ? snapshot : recent;
  const payload = visibleSnapshot?.payload || null;
  const requestRef = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [syncStatus, setSyncStatus] = useState<SyncStatus>(token ? "refreshing" : "notConnected");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // Set when the server rejects the token; polling halts until the token changes.
  const pollingHaltedRef = useRef(false);
  // Monotonic generation so a slow earlier response never overwrites a newer one
  // (e.g. an in-flight 401 from an old token clearing a freshly logged-in session).
  const generationRef = useRef(0);

  const loadDashboard = useCallback((): Promise<void> => {
    if (requestRef.current) return requestRef.current.promise;
    if (scope === "none") return Promise.resolve();
    const controller = new AbortController();
    const run = async () => {
      if (!token) {
        setSyncStatus("notConnected");
        return;
      }
      const generation = ++generationRef.current;
      setSyncStatus("refreshing");
      try {
        const response = await fetch(`/v1/dashboard?scope=${scope}${query ? `&${query}` : ""}`, {
          headers: authHeaders(token),
          signal: controller.signal,
        });
        const text = await response.text();
        let nextPayload;
        try {
          nextPayload = JSON.parse(text);
        } catch (e) {
          throw new Error(`Invalid response format (HTTP ${response.status}): ${text.slice(0, 100)}`);
        }
        if (!response.ok) throw new Error(nextPayload.error || `HTTP ${response.status}`);
        if (generation !== generationRef.current) return;
        const updatedAt = Date.now();
        const nextSnapshot = { token, identity, payload: nextPayload as DashboardPayload, updatedAt };
        for (const [key, value] of cacheRef.current) {
          if (value.token !== token || updatedAt - value.updatedAt > SNAPSHOT_TTL_MS) cacheRef.current.delete(key);
        }
        cacheRef.current.delete(cacheKey);
        cacheRef.current.set(cacheKey, nextSnapshot);
        if (cacheRef.current.size > SNAPSHOT_LIMIT) cacheRef.current.delete(cacheRef.current.keys().next().value!);
        setSnapshot(nextSnapshot);
        setErrors(nextPayload.errors || []);
        setLastUpdated(new Date(updatedAt));
        setSyncStatus("updated");
      } catch (error) {
        if (generation !== generationRef.current) return;
        const message = String(error instanceof Error ? error.message : error);
        setErrors([message]);
        if (/unauthorized|bearer token/i.test(message)) {
          pollingHaltedRef.current = true;
          setSyncStatus("tokenRejected");
          cacheRef.current.clear();
          setSnapshot(null);
          onUnauthorizedRef.current?.();
        } else {
          setSyncStatus("unavailable");
        }
      }
    };
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    const promise = run().finally(() => {
      window.clearTimeout(timeout);
      if (requestRef.current?.controller === controller) requestRef.current = null;
    });
    requestRef.current = { controller, promise };
    return promise;
  }, [token, scope, query, identity, cacheKey]);

  // Self-rescheduling poll loop: the next tick is booked once the previous fetch
  // settles, success or failure, so a transient error (Control restart during
  // luma update, network blip, laptop sleep) no longer kills auto-refresh — and
  // a failed first load retries instead of sticking on the empty state forever.
  // Only a rejected token halts the loop, until the token changes.
  useEffect(() => {
    setErrors([]);
    setLastUpdated(recent ? new Date(recent.updatedAt) : null);
    setSyncStatus(!token ? "notConnected" : scope === "none" ? "updated" : "refreshing");
    if (!token || scope === "none") return;
    pollingHaltedRef.current = false;
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      if (document.visibilityState !== "hidden") await loadDashboard();
      if (!stopped && !pollingHaltedRef.current) {
        timer = window.setTimeout(tick, REFRESH_MS);
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible" && !requestRef.current && !pollingHaltedRef.current) {
        window.clearTimeout(timer);
        void tick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    void tick();
    return () => {
      stopped = true;
      ++generationRef.current;
      requestRef.current?.controller.abort();
      requestRef.current = null;
      document.removeEventListener("visibilitychange", onVisible);
      window.clearTimeout(timer);
    };
  }, [loadDashboard, token, scope]);

  return {
    token,
    payload,
    cluster: snapshot?.token === token ? snapshot.payload.cluster : undefined,
    errors,
    syncStatus,
    lastUpdated,
    loadDashboard,
  };
}
