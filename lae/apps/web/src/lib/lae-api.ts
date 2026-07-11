const API_ROOT = (process.env.NEXT_PUBLIC_LAE_API_URL || "/v1").replace(/\/$/, "");
const MAX_RESPONSE_CHARS = 2 * 1024 * 1024;
const ERROR_CODE = /^LAE_[A-Z0-9_]{2,96}$/;

export type LaePrincipal = {
  user: { id: string; email: string; status: "active" };
  tenant: { id: string; type: "personal" };
  entitlement: { plan: "lite" | "pro" | "ultra" };
  credential: { type: "session" | "deploy_token"; scopes: string[] };
};

export type ApplicationSummary = {
  id: string;
  name: string;
  slug: string;
  kind: "pending" | "service" | "compose";
  desiredState: "running" | "suspended" | "deleted";
  observedState:
    | "provisioning"
    | "running"
    | "degraded"
    | "failed"
    | "suspending"
    | "suspended"
    | "unknown";
  currentRevisionId: string | null;
  currentDeploymentId: string | null;
  environmentVersion: number;
  createdAt: string;
  updatedAt: string;
};

export type ApplicationRecord = {
  application: ApplicationSummary;
  services: Array<{
    key: string;
    role: "http" | "internal" | "worker" | "datastore";
    required: boolean;
    desiredState: string;
    observedState: string;
    currentImageDigest: string | null;
  }>;
  routes: Array<{
    serviceKey: string;
    hostname: string;
    primary: boolean;
    containerPort: number;
    status: string;
  }>;
  volumes: Array<{
    key: string;
    requestedBytes: number;
    storagePolicy: "managed";
    backupPolicy: string;
    deletePolicy: string;
    status: string;
  }>;
  environment: {
    version: number;
    variables: Array<{
      serviceScope: string;
      name: string;
      configured: boolean;
      sensitive: boolean;
      required: boolean;
      source: string;
      updatedAt: string;
    }>;
  };
};

export type AnalysisCreateResult = {
  analysis: { id: string; status: string };
  operation: { id: string; status: string };
  links: { analysis: string; events: string };
};

export type Analysis = {
  id: string;
  status: string;
  digests: {
    sourceTree: string | null;
    sourceSnapshot: string | null;
    deploymentPlan: string | null;
    buildPlan: string | null;
    evidence: string | null;
  };
  planStored: boolean;
  links: { operation: string; events: string };
};

export type DeploymentCreateResult = {
  deployment: {
    id: string;
    applicationId: string;
    revisionId: string;
    operationId: string;
    status: string;
    previousDeploymentId: string | null;
    startedAt: string | null;
    finishedAt: string | null;
    createdAt: string;
    links: { operation: string; events: string };
  };
  operation: {
    id: string;
    status: string;
    phase: string;
    cursor: number;
    links: { events: string };
  };
};

export type ApplicationDeployment = DeploymentCreateResult["deployment"] & {
  error?: { code: string; message: string };
};

export type ApplicationAction =
  | "check-update"
  | "suspend"
  | "resume"
  | "restart"
  | "rollback"
  | "delete";

export type ApplicationActionResult = {
  application: Pick<ApplicationSummary, "id" | "desiredState" | "observedState">;
  operation: {
    id: string;
    kind: string;
    status: string;
    phase: string | null;
    cursor: number;
    links: { operation: string; events: string };
  };
  analysis?: { id: string; status: string; links: { analysis: string } };
  rollback?: { deploymentId: string };
};

export type ApplicationLogTail = {
  applicationId: string;
  deploymentId: string;
  serviceKey: string;
  tail: number;
  logs: string[];
  truncated: boolean;
  updatedAt: string;
};

export type ApplicationMetricHistory = {
  applicationId: string;
  deploymentId: string;
  serviceKey: string;
  windowSeconds: number;
  series: Record<string, Array<[number, number]>>;
  updatedAt: string;
};

export type SourceConnection = {
  id: string;
  provider: "github" | "gitea" | "generic";
  displayName: string;
  baseUrl: string;
  allowedHost: string;
  username: string | null;
  credentialVersion: number;
  createdAt: string;
  updatedAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
};

export type StaticUpload = {
  id: string;
  applicationId: string;
  filename: string;
  kind: "html" | "zip";
  mediaType: string;
  expectedBytes: number;
  actualBytes: number | null;
  sha256: string;
  status: "quarantined" | "verifying" | "scanning" | "ready" | "failed" | "deleting" | "deleted" | "expired";
  cleanupStatus: string;
  sourceRevisionId: string | null;
  failureCode?: string;
  expiresAt: string;
  createdAt: string;
  updatedAt: string;
};

export type StaticUploadCreateResult = {
  upload: StaticUpload;
  operation: { id: string; status: string };
  uploadUrlIssued: boolean;
  transfer?: {
    method: "PUT";
    url: string;
    headers: Record<string, string>;
    expiresAt: string;
  };
};

export type DeploymentConfiguration = {
  sourceRevisionId: string;
  kind: "service" | "compose";
  serviceKeys: string[];
  environmentSchemaDigest: string;
  environment: Array<{
    name: string;
    serviceKeys: string[];
    required: boolean;
    sensitive: boolean;
  }>;
};

export type ApplicationTemplate = {
  id: string;
  version: string;
  name: string;
  description: string;
  stack: string;
  kind: "service" | "compose";
  icon: string;
  tone: "pearl" | "moss" | "amber" | "mist";
  estimatedResources: { memoryMiB: number };
  verification: {
    status: "agent-pass";
    policyVersion: string;
    sourceCommit: string;
  };
};

export type TemplateLaunchResult = AnalysisCreateResult & {
  template: ApplicationTemplate;
  application: ApplicationSummary;
};

export type Operation = {
  id: string;
  kind: string;
  status: string;
  phase: string | null;
  cancelRequested: boolean;
  cursor: number;
  terminal: boolean;
  links: { events: string };
  error?: { code: string; message: string };
  updateCheck?: UpdateCheckResult;
};

export type UpdateCheckResult = {
  baselineAvailable: boolean;
  sourceChanged: boolean;
  deploymentPlanChanged: boolean;
  changed: boolean;
  digests: {
    baseline: { sourceTree: string; deploymentPlan: string } | null;
    candidate: { sourceTree: string; deploymentPlan: string };
  };
};

export type OperationEvent = {
  eventId: string;
  operationId: string;
  cursor: number;
  type: string;
  phase: string | null;
  status: string;
  level: "debug" | "info" | "warning" | "error";
  message: string;
  data: Record<string, unknown>;
  createdAt: string;
};

export type OperationEventPage = {
  operationId: string;
  events: OperationEvent[];
  cursor: number;
  status: string;
  terminal: boolean;
  hasMore: boolean;
};

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  idempotencyKey?: string;
  mutation?: boolean;
  signal?: AbortSignal;
};

export class LaeApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;
  readonly retryable: boolean;

  constructor(input: {
    code: string;
    status: number;
    requestId?: string | null;
    retryable?: boolean;
  }) {
    super(publicMessage(input.status));
    this.name = "LaeApiError";
    this.code = ERROR_CODE.test(input.code) ? input.code : "LAE_API_REQUEST_FAILED";
    this.status = input.status;
    this.requestId = input.requestId || null;
    this.retryable = Boolean(input.retryable);
  }
}

export function newIdempotencyKey(prefix: string) {
  const safePrefix = prefix.toLowerCase().replace(/[^a-z0-9._:-]/g, "-").slice(0, 24);
  return `${safePrefix || "web"}:${crypto.randomUUID()}`;
}

export function staticUploadMediaType(filename: string) {
  const lowered = filename.toLowerCase();
  if (lowered.endsWith(".html")) return "text/html";
  if (lowered.endsWith(".zip")) return "application/zip";
  throw new LaeApiError({ code: "LAE_UPLOAD_TYPE_UNSUPPORTED", status: 400 });
}

export async function sha256File(file: File) {
  // WebCrypto is intentionally bounded here because it is not incremental.
  // Larger artifacts remain available through the streaming LAE CLI path.
  if (file.size <= 0 || file.size > 64 * 1024 * 1024) {
    throw new LaeApiError({ code: "LAE_WEB_UPLOAD_SIZE_UNSUPPORTED", status: 400 });
  }
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return `sha256:${Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("")}`;
}

export async function getPrincipal(signal?: AbortSignal) {
  return requestJson<LaePrincipal>("/me", { signal });
}

export async function listApplications(signal?: AbortSignal) {
  return requestJson<{ applications: ApplicationSummary[] }>("/applications", {
    signal,
  });
}

export async function listTemplates(signal?: AbortSignal) {
  return requestJson<{ templates: ApplicationTemplate[] }>("/templates", { signal });
}

export async function launchTemplate(
  templateId: string,
  input: { name: string; slug: string; region?: "cn" | "global" },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<TemplateLaunchResult>(
    `/templates/${encodeURIComponent(templateId)}/launch`,
    {
      method: "POST",
      body: {
        name: input.name,
        slug: input.slug,
        region: input.region || "cn",
      },
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function createApplication(
  input: { name: string; slug: string },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<{ application: ApplicationSummary }>("/applications", {
    method: "POST",
    body: input,
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function getApplication(applicationId: string, signal?: AbortSignal) {
  return requestJson<ApplicationRecord>(
    `/applications/${encodeURIComponent(applicationId)}`,
    { signal },
  );
}

export async function createGitAnalysis(
  input: {
    applicationId: string;
    repository: string;
    ref?: string;
    subdirectory?: string;
    connectionId?: string;
    region?: "cn" | "global";
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<AnalysisCreateResult>("/analyses", {
    method: "POST",
    body: {
      applicationId: input.applicationId,
      source: {
        type: "git",
        repository: input.repository,
        ref: input.ref || "main",
        subdirectory: input.subdirectory || "",
        ...(input.connectionId ? { connectionId: input.connectionId } : {}),
      },
      intent: { region: input.region || "cn", publicProtocols: ["http"] },
    },
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function createUploadAnalysis(
  input: {
    applicationId: string;
    uploadId: string;
    region?: "cn" | "global";
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<AnalysisCreateResult>("/analyses", {
    method: "POST",
    body: {
      applicationId: input.applicationId,
      source: { type: "upload", uploadId: input.uploadId },
      intent: { region: input.region || "cn", publicProtocols: ["http"] },
    },
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function createStaticUpload(
  input: {
    applicationId: string;
    filename: string;
    mediaType: string;
    sizeBytes: number;
    sha256: string;
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<StaticUploadCreateResult>("/uploads", {
    method: "POST",
    body: input,
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function transferStaticUpload(
  file: File,
  transfer: NonNullable<StaticUploadCreateResult["transfer"]>,
  signal?: AbortSignal,
) {
  if (transfer.method !== "PUT") {
    throw new LaeApiError({ code: "LAE_UPLOAD_PROTOCOL_ERROR", status: 502 });
  }
  let target: URL;
  try {
    target = new URL(transfer.url);
  } catch {
    throw new LaeApiError({ code: "LAE_UPLOAD_PROTOCOL_ERROR", status: 502 });
  }
  if (
    target.username ||
    target.password ||
    !isAllowedUploadOrigin(target.origin)
  ) {
    throw new LaeApiError({ code: "LAE_UPLOAD_ORIGIN_REJECTED", status: 502 });
  }
  const headers = new Headers();
  for (const [name, value] of Object.entries(transfer.headers)) {
    if (/^(authorization|cookie|proxy-authorization)$/i.test(name)) {
      throw new LaeApiError({ code: "LAE_UPLOAD_PROTOCOL_ERROR", status: 502 });
    }
    headers.set(name, value);
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5 * 60_000);
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(target, {
      method: "PUT",
      body: file,
      headers,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new LaeApiError({
        code: "LAE_UPLOAD_TRANSFER_FAILED",
        status: response.status >= 500 ? 503 : 422,
        retryable: response.status >= 500,
      });
    }
  } catch (error) {
    if (error instanceof LaeApiError) throw error;
    throw new LaeApiError({
      code: "LAE_UPLOAD_TRANSFER_FAILED",
      status: 503,
      retryable: true,
    });
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export async function completeStaticUpload(
  uploadId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<{ upload: StaticUpload; operation: { id: string; status: string } }>(
    `/uploads/${encodeURIComponent(uploadId)}/complete`,
    {
      method: "POST",
      body: {},
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function getStaticUpload(uploadId: string, signal?: AbortSignal) {
  return requestJson<{ upload: StaticUpload; operation: { id: string; status: string } }>(
    `/uploads/${encodeURIComponent(uploadId)}`,
    { signal },
  );
}

export async function listSourceConnections(signal?: AbortSignal) {
  return requestJson<{ connections: SourceConnection[] }>("/source-connections", {
    signal,
  });
}

export async function createSourceConnection(
  input: {
    provider: SourceConnection["provider"];
    displayName: string;
    baseUrl: string;
    username?: string;
    secret: string;
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<{ connection: SourceConnection }>("/source-connections", {
    method: "POST",
    body: input,
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function rotateSourceConnection(
  connectionId: string,
  input: { secret: string; username?: string },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<{ connection: SourceConnection }>(
    `/source-connections/${encodeURIComponent(connectionId)}/rotate`,
    {
      method: "POST",
      body: input,
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function revokeSourceConnection(
  connectionId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<void>(
    `/source-connections/${encodeURIComponent(connectionId)}`,
    {
      method: "DELETE",
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function getAnalysis(analysisId: string, signal?: AbortSignal) {
  return requestJson<Analysis>(`/analyses/${encodeURIComponent(analysisId)}`, {
    signal,
  });
}

export async function getDeploymentConfiguration(
  applicationId: string,
  analysisId: string,
  signal?: AbortSignal,
) {
  return requestJson<{ configuration: DeploymentConfiguration }>(
    `/applications/${encodeURIComponent(applicationId)}/analyses/${encodeURIComponent(analysisId)}/configuration`,
    { signal },
  );
}

export async function patchApplicationEnvironment(
  applicationId: string,
  input: {
    expectedVersion: number;
    set: Record<
      string,
      { value: string; sensitive: boolean; required: boolean }
    >;
    unset?: string[];
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<{
    environment: ApplicationRecord["environment"];
    operation?: { id: string; status: string };
  }>(`/applications/${encodeURIComponent(applicationId)}/environment`, {
    method: "PATCH",
    body: { ...input, unset: input.unset || [] },
    idempotencyKey,
    mutation: true,
    signal,
  });
}

export async function createDeployment(
  input: {
    applicationId: string;
    analysisId: string;
    environmentVersion: number;
  },
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<DeploymentCreateResult>(
    `/applications/${encodeURIComponent(input.applicationId)}/deployments`,
    {
      method: "POST",
      body: {
        analysisId: input.analysisId,
        environmentVersion: input.environmentVersion,
      },
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function listApplicationDeployments(
  applicationId: string,
  limit = 20,
  signal?: AbortSignal,
) {
  const boundedLimit = Math.max(1, Math.min(100, Math.trunc(limit)));
  return requestJson<{ deployments: ApplicationDeployment[] }>(
    `/applications/${encodeURIComponent(applicationId)}/deployments?limit=${boundedLimit}`,
    { signal },
  );
}

export async function requestApplicationAction(
  applicationId: string,
  action: ApplicationAction,
  input: { deploymentId?: string } = {},
  idempotencyKey: string,
  signal?: AbortSignal,
) {
  return requestJson<ApplicationActionResult>(
    `/applications/${encodeURIComponent(applicationId)}/actions/${encodeURIComponent(action)}`,
    {
      method: "POST",
      body: action === "rollback" && input.deploymentId
        ? { deploymentId: input.deploymentId }
        : {},
      idempotencyKey,
      mutation: true,
      signal,
    },
  );
}

export async function getApplicationLogs(
  applicationId: string,
  input: { service?: string; tail?: number } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (input.service) query.set("service", input.service);
  query.set("tail", String(input.tail || 120));
  return requestJson<ApplicationLogTail>(
    `/applications/${encodeURIComponent(applicationId)}/logs?${query.toString()}`,
    { signal },
  );
}

export async function getApplicationMetrics(
  applicationId: string,
  input: { service?: string; window?: number } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (input.service) query.set("service", input.service);
  query.set("window", String(input.window || 3600));
  return requestJson<ApplicationMetricHistory>(
    `/applications/${encodeURIComponent(applicationId)}/metrics?${query.toString()}`,
    { signal },
  );
}

export async function getOperation(operationId: string, signal?: AbortSignal) {
  return requestJson<Operation>(`/operations/${encodeURIComponent(operationId)}`, {
    signal,
  });
}

export async function getOperationEvents(
  operationId: string,
  cursor: number,
  signal?: AbortSignal,
) {
  return requestJson<OperationEventPage>(
    `/operations/${encodeURIComponent(operationId)}/events?after=${cursor}&limit=100`,
    { signal },
  );
}

export async function cancelOperation(operationId: string, signal?: AbortSignal) {
  return requestJson<Operation>(
    `/operations/${encodeURIComponent(operationId)}/cancel`,
    { method: "POST", body: {}, mutation: true, signal },
  );
}

export async function requestJson<T>(path: string, options: RequestOptions = {}) {
  if (!path.startsWith("/") || path.startsWith("//") || /[\\\r\n]/.test(path)) {
    throw new LaeApiError({ code: "LAE_WEB_PATH_INVALID", status: 400 });
  }
  const method = options.method || "GET";
  const headers = new Headers({ Accept: "application/json" });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  if (options.mutation) {
    const csrf = readCookie("__Host-lae_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 20_000);
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      method,
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const raw = await response.text();
    if (response.status === 204 && response.ok && raw.length === 0) {
      return undefined as T;
    }
    if (raw.length > MAX_RESPONSE_CHARS || !contentType.includes("application/json")) {
      throw new LaeApiError({
        code: "LAE_API_PROTOCOL_ERROR",
        status: response.ok ? 502 : response.status,
        retryable: response.status >= 500,
      });
    }
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!isObject(parsed)) {
      throw new LaeApiError({ code: "LAE_API_PROTOCOL_ERROR", status: 502 });
    }
    if (!response.ok) throw apiError(response.status, parsed);
    return parsed as T;
  } catch (error) {
    if (error instanceof LaeApiError) throw error;
    throw new LaeApiError({
      code: "LAE_API_UNAVAILABLE",
      status: 503,
      retryable: true,
    });
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

function apiError(status: number, parsed: Record<string, unknown>) {
  const envelope = isObject(parsed.error) ? parsed.error : {};
  return new LaeApiError({
    code: typeof envelope.code === "string" ? envelope.code : "LAE_API_REQUEST_FAILED",
    status,
    requestId:
      typeof envelope.requestId === "string" && envelope.requestId.length <= 128
        ? envelope.requestId
        : null,
    retryable: envelope.retryable === true,
  });
}

function readCookie(name: string) {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) {
      try {
        return decodeURIComponent(value.slice(prefix.length));
      } catch {
        return null;
      }
    }
  }
  return null;
}

function isAllowedUploadOrigin(origin: string) {
  if (origin === window.location.origin) return true;
  const configured = (process.env.NEXT_PUBLIC_LAE_UPLOAD_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  return configured.includes(origin);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function publicMessage(status: number) {
  if (status === 401) return "请先登录 LAE。";
  if (status === 403) return "当前凭据没有执行此操作的权限。";
  if (status === 409) return "状态已发生变化，请刷新后重试。";
  if (status === 422) return "当前应用还不满足部署条件。";
  if (status === 429) return "请求过于频繁或已达到当前套餐限制。";
  if (status >= 500) return "LAE 暂时不可用，请稍后重试。";
  return "请求未能完成，请检查输入。";
}
