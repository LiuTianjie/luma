import { useCallback, useEffect, useRef, useState } from "react";
import type { DashboardScope } from "./dashboardScope";
import type { DashboardPayload, SyncStatus } from "./types";

const TOKEN_KEY = "luma.dashboard.deployToken";
const REFRESH_MS = 30000;
const isDev = typeof window !== "undefined" && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");

export function useDashboardData(scope: DashboardScope = "full", query = "") {
  const [token, setTokenState] = useState(() => localStorage.getItem(TOKEN_KEY) || (isDev ? "dev-token" : ""));
  // List filters retain their mounted view while refreshing; different objects do not.
  const identity = scope === "application" ? `${scope}:${query}` : scope;
  const [snapshot, setSnapshot] = useState<{ token: string; identity: string; payload: DashboardPayload } | null>(null);
  const payload = snapshot?.token === token && snapshot.identity === identity ? snapshot.payload : null;
  const requestRef = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [syncStatus, setSyncStatus] = useState<SyncStatus>(token ? "refreshing" : "notConnected");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // Set when the server rejects the token; polling halts until the token changes.
  const pollingHaltedRef = useRef(false);
  // Monotonic generation so a slow earlier response never overwrites a newer one
  // (e.g. an in-flight 401 from an old token clearing a freshly logged-in session).
  const generationRef = useRef(0);

  const setToken = useCallback((nextToken: string) => {
    const trimmed = nextToken.trim();
    setTokenState(trimmed);
    if (trimmed) localStorage.setItem(TOKEN_KEY, trimmed);
    else localStorage.removeItem(TOKEN_KEY);
  }, []);

  const signOut = useCallback(() => {
    setToken("");
    setSnapshot(null);
    setErrors([]);
    setSyncStatus("notConnected");
    setLastUpdated(null);
  }, [setToken]);

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
          headers: { Authorization: `Bearer ${token}` },
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
        setSnapshot({ token, identity, payload: nextPayload as DashboardPayload });
        setErrors(nextPayload.errors || []);
        setLastUpdated(new Date());
        setSyncStatus("updated");
      } catch (error) {
        if (generation !== generationRef.current) return;
        const message = String(error instanceof Error ? error.message : error);
        setErrors([message]);
        if (/unauthorized|bearer token/i.test(message)) {
          pollingHaltedRef.current = true;
          setSyncStatus("tokenRejected");
          setSnapshot(null);
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
  }, [token, scope, query, identity]);

  // Self-rescheduling poll loop: the next tick is booked once the previous fetch
  // settles, success or failure, so a transient error (Control restart during
  // luma update, network blip, laptop sleep) no longer kills auto-refresh — and
  // a failed first load retries instead of sticking on the empty state forever.
  // Only a rejected token halts the loop, until the token changes.
  useEffect(() => {
    setErrors([]);
    setLastUpdated(null);
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
    setToken,
    signOut,
    loadDashboard,
  };
}
