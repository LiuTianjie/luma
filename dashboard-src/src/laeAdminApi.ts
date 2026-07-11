import { apiGet } from "./apiClient";

export type AdminPage = { limit: number; offset: number; total: number };

export type LaeAdminUser = {
  id: string;
  email: string;
  status: string;
  emailVerifiedAt?: string | null;
  lastLoginAt?: string | null;
  createdAt?: string | null;
};

export type LaeAdminTenant = {
  id: string;
  name: string;
  slug: string;
  status: string;
  ownerEmail: string;
  plan?: string | null;
  createdAt?: string | null;
};

export type LaeAdminApplication = {
  id: string;
  tenantId: string;
  name: string;
  slug: string;
  lumaName: string;
  kind: string;
  desiredState: string;
  observedState: string;
  serviceCount: number;
  requestedVolumeBytes: number;
  currentDeploymentId?: string | null;
  updatedAt?: string | null;
};

export type LaeAdminOperation = {
  id: string;
  tenantId: string;
  kind: string;
  targetId: string;
  status: string;
  phase?: string | null;
  errorCode?: string | null;
  createdAt?: string | null;
};

export type LaeAdminUsage = {
  tenantId: string;
  applicationCount: number;
  requestedVolumeBytes: number;
  storedUploadBytes: number;
};

export type LaeAdminPlacement = {
  runtimeDeploymentRef: string;
  tenantRef: string;
  applicationRef: string;
  deploymentRef: string;
  jobSlug: string;
  status: string;
  region: string;
  stateful: boolean;
  continuity: string;
  candidateNodeIds: string[];
  preferredNodeId: string;
  activeAllocations: Array<{
    allocationId: string;
    nodeId: string;
    nodeName: string;
    status: string;
  }>;
  observationStatus: string;
  decisionDigest: string;
  updatedAt: number;
};

export async function fetchLaeAdmin<T>(resource: string, token: string, signal?: AbortSignal): Promise<T> {
  return apiGet<T>(`/v1/dashboard/lae/${encodeURIComponent(resource)}?limit=100&offset=0`, token, signal);
}
