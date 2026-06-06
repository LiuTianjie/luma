import type { MetricsHistoryPayload } from "./types";

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
  const response = await fetch(`/v1/dashboard/metrics/history?${params.toString()}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  const text = await response.text();
  let payload: MetricsHistoryPayload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Invalid response format (HTTP ${response.status}): ${text.slice(0, 100)}`);
  }
  if (!response.ok) throw new Error(String((payload as { error?: string }).error || `HTTP ${response.status}`));
  return payload;
}
