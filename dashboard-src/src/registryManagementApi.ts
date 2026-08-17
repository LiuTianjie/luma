import { apiGet, apiPost } from "./apiClient";

export type RegistryProtectionReason = {
  kind?: string;
  source?: string;
  reference?: string;
};

export type RegistryManifest = {
  repository: string;
  digest: string;
  tags: string[];
  mediaType?: string;
  manifestBytes?: number;
  logicalBytes?: number;
  createdAt?: number;
  lastModified?: number;
  platforms?: string[];
  childManifestDigests?: string[];
  role?: "root" | "dependency" | string;
  protectionStatus?: "protected" | "retained" | "candidate" | "unknown" | string;
  protectionReasons?: RegistryProtectionReason[];
  deletable?: boolean;
  retention?: {
    repositoryPosition?: number;
    keptByCount?: boolean;
    keptByAge?: boolean;
  };
};

export type RegistryPolicy = {
  mode: "off" | "recommend" | "enforce";
  keepLast: number;
  maxAgeDays: number;
  systemKeepLast: number;
  queueGraceHours: number;
  gcGraceDays: number;
  warningPercent: number;
  criticalPercent: number;
  emergencyPercent: number;
};

export type RegistryDeletion = {
  id: string;
  status: string;
  manifests?: RegistryManifest[];
  logicalBytes?: number;
  createdAt?: number;
  updatedAt?: number;
  notBefore?: number;
  deletedAt?: number;
  gcAfter?: number;
  message?: string;
};

export type RegistryInventory = {
  registry?: { host?: string; node?: string; volumeName?: string; jobId?: string };
  summary?: {
    repositoryCount?: number;
    tagCount?: number;
    manifestCount?: number;
    protectedCount?: number;
    retainedCount?: number;
    candidateCount?: number;
    unknownCount?: number;
    scanErrors?: number;
    scannedAt?: number;
    durationMs?: number;
  };
  usage?: {
    volumeBytes?: number;
    filesystemTotalBytes?: number;
    filesystemUsedBytes?: number;
    filesystemAvailableBytes?: number;
    filesystemUsePercent?: number;
    monthlyBlobs?: Array<{ month: string; bytes: number; files: number }>;
    error?: string;
  };
  entries?: RegistryManifest[];
  errors?: Array<{ repository?: string; tag?: string; digest?: string; message?: string }>;
  protectionComplete?: boolean;
  scanPending?: boolean;
  referenceError?: string;
  policy?: RegistryPolicy;
  deletions?: RegistryDeletion[];
  page?: { offset?: number; limit?: number; total?: number; hasMore?: boolean };
};

export async function fetchRegistryInventory(
  token: string,
  refresh = false,
  signal?: AbortSignal,
  options: { offset?: number; limit?: number; query?: string; status?: string } = {},
) {
  const params = new URLSearchParams();
  if (refresh) params.set("refresh", "true");
  if (options.offset) params.set("offset", String(options.offset));
  if (options.limit) params.set("limit", String(options.limit));
  if (options.query?.trim()) params.set("q", options.query.trim());
  if (options.status && options.status !== "all") params.set("status", options.status);
  const suffix = params.size ? `?${params.toString()}` : "";
  return apiGet<RegistryInventory>(`/v1/registry/inventory${suffix}`, token, signal);
}

export async function saveRegistryPolicy(token: string, policy: RegistryPolicy) {
  return apiPost<{ policy: RegistryPolicy; saved: boolean }>("/v1/registry/policy", token, policy);
}

export async function previewRegistryDeletion(token: string, manifests: Array<{ repository: string; digest: string }>) {
  return apiPost<{ allowed: boolean; selected?: RegistryManifest[]; dependentManifests?: RegistryManifest[]; blocked?: Array<RegistryManifest & { reason?: string }>; risks?: Array<RegistryManifest & { reason?: string }>; logicalBytes?: number; warning?: string }>(
    "/v1/registry/deletions/preview",
    token,
    { manifests, manualOverride: true },
  );
}

export async function createRegistryDeletion(token: string, manifests: Array<{ repository: string; digest: string }>) {
  return apiPost<{ deletion: RegistryDeletion }>("/v1/registry/deletions", token, { manifests, confirm: "delete", manualOverride: true });
}

export async function registryDeletionAction(token: string, id: string, action: "cancel" | "execute" | "restore", force = false) {
  return apiPost<{ deletion: RegistryDeletion }>(`/v1/registry/deletions/${encodeURIComponent(id)}/${action}`, token, { force });
}

export async function registryGc(token: string, execute: boolean, force = false) {
  return apiPost<{ preview?: Record<string, unknown>; result?: Record<string, unknown> }>(
    execute ? "/v1/registry/gc" : "/v1/registry/gc/preview",
    token,
    { force },
  );
}
