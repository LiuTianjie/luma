import { apiGet } from "./apiClient";
import type { MetricPoint } from "./types";

export type ObserveHttpApp = {
  id: string;
  requests: MetricPoint[];
  errors: MetricPoint[];
  requestRate: number;
  errorRate: number;
  errorRatio: number;
};

export type ObserveJob = {
  id: string;
  job: string;
  taskGroup: string;
  failed: number;
  running: number;
  failedPoints: MetricPoint[];
  runningPoints: MetricPoint[];
};

export type ObserveAppsPayload = {
  available: boolean;
  message: string;
  window: number;
  http: ObserveHttpApp[];
  jobs: ObserveJob[];
  updatedAt: number;
};

export function fetchObserveApps(token: string, window = 3600, signal?: AbortSignal) {
  return apiGet<ObserveAppsPayload>(`/v1/dashboard/observe/apps?window=${window}`, token, signal);
}
