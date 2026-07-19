import { useCallback, useEffect, useRef, useState } from "react";
import type { DashboardPayload, SyncStatus } from "./types";

const TOKEN_KEY = "luma.dashboard.deployToken";
const REFRESH_MS = 30000;
const isDev = typeof window !== "undefined" && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");

export function useDashboardData() {
  const [token, setTokenState] = useState(() => localStorage.getItem(TOKEN_KEY) || (isDev ? "dev-token" : ""));
  const [payload, setPayload] = useState<DashboardPayload | null>(null);
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
    setPayload(null);
    setErrors([]);
    setSyncStatus("notConnected");
    setLastUpdated(null);
  }, [setToken]);

  const loadDashboard = useCallback(async () => {
    if (!token) {
      setSyncStatus("notConnected");
      return;
    }
    const generation = ++generationRef.current;
    setSyncStatus("refreshing");
    try {
      const response = await fetch("/v1/dashboard", {
        headers: { Authorization: `Bearer ${token}` },
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
      setPayload(nextPayload as DashboardPayload);
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
        setPayload(null);
      } else {
        setSyncStatus("unavailable");
      }
    }
  }, [token]);

  // Self-rescheduling poll loop: the next tick is booked once the previous fetch
  // settles, success or failure, so a transient error (Control restart during
  // luma update, network blip, laptop sleep) no longer kills auto-refresh — and
  // a failed first load retries instead of sticking on the empty state forever.
  // Only a rejected token halts the loop, until the token changes.
  useEffect(() => {
    if (!token) return;
    pollingHaltedRef.current = false;
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      await loadDashboard();
      if (!stopped && !pollingHaltedRef.current) {
        timer = window.setTimeout(tick, REFRESH_MS);
      }
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [loadDashboard, token]);

  return {
    token,
    payload,
    errors,
    syncStatus,
    lastUpdated,
    setToken,
    signOut,
    loadDashboard,
  };
}
