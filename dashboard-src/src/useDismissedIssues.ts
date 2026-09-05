import { useCallback, useEffect, useMemo, useState } from "react";
import type { DashboardIssue } from "./types";
import { activeDismissals, dismissalStorageKey, DISMISS_DURATION_MS, issueKey, type IssueDismissals } from "./issueDismissals";
export { issueKey } from "./issueDismissals";

function readStored(key: string): unknown {
  try { return JSON.parse(localStorage.getItem(key) || "{}"); }
  catch { return {}; }
}

export function useDismissedIssues(clusterId: string, issues: DashboardIssue[]) {
  const storageKey = dismissalStorageKey(clusterId, window.location.origin);
  const keysJson = JSON.stringify(issues.map(issueKey));
  const activeKeys = useMemo(() => new Set<string>(JSON.parse(keysJson)), [keysJson]);
  const [now, setNow] = useState(Date.now);
  const [state, setState] = useState(() => ({ storageKey, items: activeDismissals(readStored(storageKey), activeKeys, Date.now()) }));
  const items = useMemo(() => activeDismissals(state.storageKey === storageKey ? state.items : readStored(storageKey), activeKeys, now), [state, storageKey, activeKeys, now]);

  useEffect(() => {
    // Persist recovery/expiry too, otherwise a recurring identical fault stays hidden.
    setState((current) => current.storageKey === storageKey && JSON.stringify(current.items) === JSON.stringify(items)
      ? current : { storageKey, items });
    try { localStorage.setItem(storageKey, JSON.stringify(items)); }
    catch { /* Browser-local state still works when storage is unavailable. */ }
  }, [storageKey, items]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  const dismiss = useCallback((key: string) => {
    const time = Date.now();
    setNow(time);
    setState({ storageKey, items: { ...activeDismissals(items, activeKeys, time), [key]: time + DISMISS_DURATION_MS } });
  }, [storageKey, items, activeKeys]);
  const clear = useCallback(() => setState({ storageKey, items: {} as IssueDismissals }), [storageKey]);
  const isDismissed = useCallback((key: string) => Boolean(items[key]), [items]);
  return { dismiss, clear, isDismissed, dismissedCount: Object.keys(items).length };
}
