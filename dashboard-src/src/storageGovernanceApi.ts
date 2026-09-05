import { apiGet, apiPost } from "./apiClient";

export type StorageComponent = { id: string; label: string; location?: string; bytes: number | null; growthBytes: number | null; status: string; reason?: string; reclaimableBytes?: number | null };
export type StoragePolicy = { summaryDays: number; detailDays: number; graceHours: number };
export type StorageInventory = {
  components: StorageComponent[]; totalKnownBytes: number; measuredAt: number | string;
  databaseReusableBytes?: number; note?: string; historyPlans?: {planId: string; status: string; eligibleAfter: number; expiresAt: number}[];
  growthSince?: number | string | null; policy?: StoragePolicy;
  builderTasks?: StorageTask[];
  builders?: { name: string; status?: string }[];
  backup?: { latestAt?: number | string | null; status?: string; note?: string };
};
export type StorageOperation = "inventory" | "preview" | "quarantine" | "restore" | "purge";
export type StorageResult = {
  operation: StorageOperation; root?: string; totalBytes?: number; protectedBytes?: number; reclaimableBytes?: number;
  blockedReasons?: string[]; planId?: string; expiresAt?: string | number; eligibleAfter?: string | number;
  files?: { path: string; bytes: number; status: string }[]; fileCount?: number; truncated?: boolean; filesTruncated?: boolean; measuredAt?: string | number; bytes?: number; status?: string;
  message?: string;
};
export type StorageTask = { id: string; nodeName?: string; updatedAt?: number | string; status: string; message?: string; error?: string; result?: StorageResult };
export type HistoryCleanupPreview = { planId: string; expiresAt?: string | number; eligibleAfter?: string | number; alertHistory?: { incidentIds: number[]; deliveryIds: number[]; eventsCount: number; estimatedBytes: number; hasMore: boolean; cutoff: number }; candidateCount: number; protectedCount: number; estimatedReclaimableBytes: number; candidates: {kind: string; id: string; app?: string; action: string; estimatedBytes: number}[]; truncated?: boolean; blockedReasons?: string[] };
const ROOT = "/v1/governance";
export const getStorageInventory = (token: string, signal?: AbortSignal) => apiGet<StorageInventory>(`${ROOT}/inventory`, token, signal);
export const getStoragePolicy = (token: string, signal?: AbortSignal) => apiGet<StoragePolicy>(`${ROOT}/policy`, token, signal);
export const saveStoragePolicy = (token: string, policy: StoragePolicy, signal?: AbortSignal) => apiPost<StoragePolicy>(`${ROOT}/policy`, token, { summaryDays: policy.summaryDays, detailDays: policy.detailDays, graceHours: policy.graceHours }, signal);
export const previewHistoryCleanup = (token: string, signal?: AbortSignal) => apiPost<HistoryCleanupPreview>(`${ROOT}/history/preview`, token, {}, signal);
export const executeHistoryCleanup = (token: string, planId: string, signal?: AbortSignal) => apiPost<{ removedCount: number; skippedCount: number; estimatedReclaimedBytes: number; alertHistory?: { incidentsDeleted: number; deliveriesDeleted: number } }>(`${ROOT}/history/apply`, token, { planId, confirmed: true }, signal);
export const startStorageTask = (token: string, body: { node: string; operation: StorageOperation; planId?: string; confirmed?: boolean }, signal?: AbortSignal) => apiPost<{ task: StorageTask }>(`${ROOT}/builder`, token, body, signal);
export const getStorageTask = (token: string, id: string, signal?: AbortSignal) => apiGet<{ task: StorageTask }>(`${ROOT}/builder/${encodeURIComponent(id)}`, token, signal);

export const getHistoryCleanupPlan = (token: string, planId: string, signal?: AbortSignal) => apiGet<HistoryCleanupPreview>(`${ROOT}/history/plans/${encodeURIComponent(planId)}`, token, signal);
