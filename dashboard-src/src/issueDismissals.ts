import type { DashboardIssue } from "./types";

export const DISMISS_DURATION_MS = 60 * 60 * 1000;
export type IssueDismissals = Record<string, number>;

export function issueKey(issue: DashboardIssue): string {
  return JSON.stringify([issue.severity || "info", issue.kind || "", issue.target || "", issue.message || ""]);
}

export function dismissalStorageKey(clusterId: string, origin: string): string {
  return `luma.dashboard.dismissedIssues.v2:${encodeURIComponent(origin)}:${encodeURIComponent(clusterId || "unknown")}`;
}

// A recovered issue loses its dismissal so the same fault can be surfaced again.
export function activeDismissals(value: unknown, activeKeys: Set<string>, now: number): IssueDismissals {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([key, expiry]) =>
    activeKeys.has(key) && typeof expiry === "number" && Number.isFinite(expiry)
    && expiry > now && expiry <= now + DISMISS_DURATION_MS,
  ));
}
