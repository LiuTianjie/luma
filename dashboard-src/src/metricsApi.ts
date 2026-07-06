import type { MetricsHistoryPayload } from "./types";
import { apiGet } from "./apiClient";

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
