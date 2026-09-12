import type { MetricsHistoryPayload } from "./types";
import { apiGet, apiPost } from "./apiClient";

export type HistoryTarget = { kind: "node" | "service"; name: string };
export type HistoryState = { payload?: MetricsHistoryPayload; error?: string };
export type MetricsHistoryBatch = {
  results: (HistoryTarget & HistoryState)[];
  updatedAt: number;
  maxTargets: number;
};

export function historyKey(kind: "node" | "service", name: string) {
  return `${kind}:${name}`;
}

export async function fetchMetricsHistoryBatch({ token, targets, window = 3600, signal }: {
  token: string;
  targets: HistoryTarget[];
  window?: number;
  signal?: AbortSignal;
}): Promise<MetricsHistoryBatch> {
  return apiPost<MetricsHistoryBatch>("/v1/dashboard/metrics/history/batch", token, { targets, window }, signal);
}

export async function fetchMetricsHistory({
  token,
  kind,
  name,
  window = 3600,
  signal,
}: {
  token: string;
  kind: "node" | "service";
  name: string;
  window?: number;
  signal?: AbortSignal;
}): Promise<MetricsHistoryPayload> {
  const params = new URLSearchParams({ kind, name, window: String(window) });
  return apiGet<MetricsHistoryPayload>(`/v1/dashboard/metrics/history?${params.toString()}`, token, signal);
}
