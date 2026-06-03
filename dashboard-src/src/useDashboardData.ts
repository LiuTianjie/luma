import { useCallback, useEffect, useState } from "react";
import type { DashboardPayload, SyncStatus } from "./types";

const TOKEN_KEY = "luma.dashboard.deployToken";
const REFRESH_MS = 30000;

export function useDashboardData() {
  const [token, setTokenState] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [payload, setPayload] = useState<DashboardPayload | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [syncStatus, setSyncStatus] = useState<SyncStatus>(token ? "refreshing" : "notConnected");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

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
      setPayload(nextPayload as DashboardPayload);
      setErrors(nextPayload.errors || []);
      setLastUpdated(new Date());
      setSyncStatus("updated");
    } catch (error) {
      const message = String(error instanceof Error ? error.message : error);
      setErrors([message]);
      if (/unauthorized|bearer token/i.test(message)) {
        setSyncStatus("tokenRejected");
        setPayload(null);
      } else {
        setSyncStatus("unavailable");
      }
    }
  }, [token]);

  useEffect(() => {
    if (!token) return;
    void loadDashboard();
  }, [loadDashboard, token]);

  useEffect(() => {
    if (!token || !payload) return;
    const timer = window.setTimeout(() => {
      void loadDashboard();
    }, REFRESH_MS);
    return () => window.clearTimeout(timer);
  }, [loadDashboard, payload, token]);

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
