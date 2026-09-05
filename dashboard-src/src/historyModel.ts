import { localizeState } from "./i18n";
import type { DeployStep } from "./deploy/types";

export type HistoryKind = "build" | "deployment";
export type HistoryItem = {
  id: string;
  kind: HistoryKind;
  application?: string;
  source?: string;
  status?: string;
  createdAt?: number;
  updatedAt?: number;
  title?: string;
  repository?: string;
  ref?: string;
  message?: string;
  buildNode?: string;
  stepCount?: number;
  retryOf?: string;
  retryRootId?: string;
  detailsExpiredAt?: number;
  detailsRetentionDays?: number;
};
export type HistoryPage = { limit: number; nextCursor: string | null; hasMore: boolean };
export type HistoryList = { items: HistoryItem[]; page: HistoryPage };
export type HistoryDetail = { item: HistoryItem; record: Record<string, unknown>; events: DeployStep[]; page: HistoryPage };
export type HistorySelection = { kind: HistoryKind; id: string };
export const HISTORY_FILTERS = ["app", "status", "source", "kind", "since", "until"] as const;
export const HISTORY_PAGE_SIZE = 50;

export function historyItemKey(item: HistorySelection): string {
  return JSON.stringify([item.kind, item.id]);
}

export function historyFilters(search: string): URLSearchParams {
  const current = new URLSearchParams(search);
  const filters = new URLSearchParams();
  for (const name of HISTORY_FILTERS) {
    const value = current.get(name)?.trim();
    if (value) filters.set(name, value);
  }
  return filters;
}

export function historySelection(search: string): HistorySelection | null {
  const params = new URLSearchParams(search);
  const kind = params.get("entryKind");
  const id = params.get("entryId");
  return id && (kind === "build" || kind === "deployment") ? { kind, id } : null;
}

export function historySelectionSearch(search: string, selection: HistorySelection | null): string {
  const params = new URLSearchParams(search);
  if (selection) {
    params.set("entryKind", selection.kind);
    params.set("entryId", selection.id);
  } else {
    params.delete("entryKind");
    params.delete("entryId");
  }
  return params.toString();
}

export function mergeHistoryItems(current: HistoryItem[], incoming: HistoryItem[]): HistoryItem[] {
  const merged = new Map(current.map((item) => [historyItemKey(item), item]));
  for (const item of incoming) merged.set(historyItemKey(item), item);
  // Server cursor uses creation order. A running build update must not move it across pages.
  return [...merged.values()].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0)
    || historyItemKey(b).localeCompare(historyItemKey(a)));
}

export function historyListPath(filters: string, cursor?: string | null): string {
  const params = historyFilters(filters);
  params.set("limit", String(HISTORY_PAGE_SIZE));
  if (cursor) params.set("cursor", cursor);
  return `/v1/history?${params}`;
}

export function historyDetailPath(selection: HistorySelection, cursor?: string | null): string {
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) });
  if (cursor) params.set("cursor", cursor);
  return `/v1/history/${selection.kind}/${encodeURIComponent(selection.id)}?${params}`;
}

export function localDateInput(timestamp: string): string {
  if (!timestamp) return "";
  const date = new Date(/^\d+(\.\d+)?$/.test(timestamp) ? Number(timestamp) * 1000 : timestamp);
  if (!Number.isFinite(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function dateInputTimestamp(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString() : "";
}

// A retry creates a separate attempt. Never point its actions back at the failed parent.
export function retryBuildSelection(result: unknown, parentId: string): HistorySelection | null {
  if (!result || typeof result !== "object") return null;
  const id = (result as Record<string, unknown>).buildRunId;
  return typeof id === "string" && id.trim() && id !== parentId ? { kind: "build", id } : null;
}

export function historyRetentionNotice(item: HistoryItem | undefined, lang: "zh" | "en"): string {
  if (!item || !Number.isFinite(item.detailsExpiredAt) || (item.detailsExpiredAt || 0) <= 0) return "";
  const days = item.detailsRetentionDays;
  const knownDays = typeof days === "number" && Number.isFinite(days) && days > 0;
  return lang === "zh"
    ? `详细日志已按${knownDays ? ` ${days} 天` : ""}保留策略清理，摘要仍保留。`
    : `Detailed logs were removed under the${knownDays ? ` ${days}-day` : ""} retention policy. The summary is retained.`;
}

export function historyStatus(status: string | undefined, lang: "zh" | "en"): string {
  const value = (status || "").trim().toLowerCase();
  const labels: Record<string, [string, string]> = {
    succeeded: ["成功", "Succeeded"], completed: ["成功", "Completed"], complete: ["成功", "Completed"], success: ["成功", "Success"],
    timeout: ["超时", "Timed out"], timed_out: ["超时", "Timed out"],
    active: ["已部署", "Deployed"], failed_partial: ["部分失败", "Partially failed"],
    canceling: ["正在取消", "Canceling"], canceled: ["已取消", "Canceled"], cancelled: ["已取消", "Canceled"],
    interrupted: ["已中断", "Interrupted"], error: ["错误", "Error"], queued: ["排队中", "Queued"],
  };
  return labels[value]?.[lang === "zh" ? 0 : 1] || localizeState(lang, value || "-");
}

export function historyStatusValue(status?: string): string | undefined {
  const value = status?.trim().toLowerCase();
  if (["success", "complete", "completed"].includes(value || "")) return "succeeded";
  if (["failed_partial", "timeout", "timed_out", "interrupted"].includes(value || "")) return "failed";
  if (value === "canceling" || value === "queued") return "pending";
  return value;
}
