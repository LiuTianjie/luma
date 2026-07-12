import { apiGet, apiPost } from "./apiClient";

export type FleetUpdateNode = {
  nodeName?: string;
  region?: string;
  os?: string;
  agentVersionBefore?: string;
  agentVersionAfter?: string;
  installRef?: string;
  status?: "pending" | "succeeded" | "failed" | "skipped" | string;
  message?: string;
};

export type FleetUpdateOperation = {
  id?: string;
  installRef?: string;
  status?: "queued" | "running" | "succeeded" | "attention" | "failed" | "interrupted" | string;
  createdAt?: number;
  updatedAt?: number;
  finishedAt?: number;
  message?: string;
  nodes?: FleetUpdateNode[];
  result?: { total?: number; succeeded?: number; failed?: number; skipped?: number };
};

export type ManagerUpdate = {
  updateId?: string;
  managerNode?: string;
  installRef?: string;
  controlImage?: string;
  status?: "none" | "running" | "succeeded" | "failed" | "interrupted" | string;
  createdAt?: number;
  exitCode?: number | null;
  message?: string;
  log?: string[];
};

export type RouteSentinelResult = {
  domain?: string;
  status?: number;
  ok?: boolean;
  latencyMs?: number;
  error?: string;
};

export type RouteSentinel = {
  checkedAt?: number;
  total?: number;
  succeeded?: number;
  failed?: number;
  results?: RouteSentinelResult[];
};

export async function listFleetUpdates(token: string, signal?: AbortSignal) {
  return apiGet<{ operations?: FleetUpdateOperation[] }>("/v1/dashboard/updates/fleet", token, signal);
}

export async function getFleetUpdate(token: string, id: string, signal?: AbortSignal) {
  return apiGet<FleetUpdateOperation>(`/v1/dashboard/updates/fleet/${encodeURIComponent(id)}`, token, signal);
}

export async function startFleetUpdate(token: string, installRef: string, nodeNames: string[]) {
  return apiPost<FleetUpdateOperation>("/v1/dashboard/updates/fleet", token, {
    installRef,
    nodeNames,
    waitReadySeconds: 45,
  });
}

export async function getManagerUpdate(token: string, updateId = "", signal?: AbortSignal) {
  const query = updateId ? `?updateId=${encodeURIComponent(updateId)}` : "";
  return apiGet<ManagerUpdate>(`/v1/dashboard/updates/manager${query}`, token, signal);
}

export async function startManagerUpdate(token: string, installRef: string, controlImage: string) {
  return apiPost<ManagerUpdate>("/v1/dashboard/updates/manager", token, { installRef, controlImage });
}

export async function runRouteSentinel(token: string) {
  return apiPost<RouteSentinel>("/v1/dashboard/route-sentinel", token, {});
}
