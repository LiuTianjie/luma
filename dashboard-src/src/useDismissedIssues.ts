import { useCallback, useEffect, useState } from "react";
import type { DashboardIssue } from "./types";

const STORAGE_KEY = "luma.dashboard.dismissedIssues";

// A stable identity for an issue so a dismissal survives refreshes and only re-appears
// if the underlying condition changes (severity/kind/target/message all factor in).
export function issueKey(issue: DashboardIssue): string {
  return [issue.severity || "info", issue.kind || "", issue.target || "", issue.message || ""].join("|");
}

function readStored(): Set<string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
  } catch {
    return new Set();
  }
}

// Track which issues the user has dismissed. Dismissals persist to localStorage; a
// "clear" resets them so a user can bring hidden items back.
export function useDismissedIssues() {
  const [dismissed, setDismissed] = useState<Set<string>>(() => readStored());

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...dismissed]));
    } catch {
      /* storage unavailable; keep in-memory only */
    }
  }, [dismissed]);

  const dismiss = useCallback((key: string) => {
    setDismissed((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, []);

  const clear = useCallback(() => setDismissed(new Set()), []);

  const isDismissed = useCallback((key: string) => dismissed.has(key), [dismissed]);

  return { dismiss, clear, isDismissed, dismissedCount: dismissed.size };
}
