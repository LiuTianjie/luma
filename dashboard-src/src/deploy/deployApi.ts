import type { DeployPreviewResult, DeployStep } from "./types";
import { authHeaders, consumeNdjson, readJson } from "../apiClient";

type DeployRequest = {
  token: string;
  manifest: string;
  composeContent?: string;
  sourceName: string;
  skipDns: boolean;
  skipOrchestrator: boolean;
};

function bodyFor(request: DeployRequest) {
  return JSON.stringify({
    manifest: request.manifest,
    composeContent: request.composeContent,
    sourceName: request.sourceName,
    skipDns: request.skipDns,
    skipOrchestrator: request.skipOrchestrator,
    // Tag the deploy origin so the Deployments timeline can distinguish dashboard
    // deploys from CLI ones. The CLI omits this and defaults to "cli" server-side.
    origin: "dashboard",
  });
}

export async function previewService(request: DeployRequest): Promise<DeployPreviewResult> {
  const response = await fetch("/v1/deployments/preview", {
    method: "POST",
    headers: authHeaders(request.token, true),
    body: bodyFor(request),
  });
  return readJson(response) as Promise<DeployPreviewResult>;
}

export async function previewCompose(request: DeployRequest): Promise<DeployPreviewResult> {
  const response = await fetch("/v1/compose-deployments/preview", {
    method: "POST",
    headers: authHeaders(request.token, true),
    body: bodyFor(request),
  });
  return readJson(response) as Promise<DeployPreviewResult>;
}

export async function deployStream(request: DeployRequest, mode: "service" | "compose", onStep: (step: DeployStep) => void): Promise<unknown> {
  const response = await fetch(mode === "service" ? "/v1/deployments/stream" : "/v1/compose-deployments/stream", {
    method: "POST",
    headers: authHeaders(request.token, true),
    body: bodyFor(request),
  });
  return consumeStream(response, onStep);
}

export type BuildImportRequest = {
  token: string;
  repoUrl?: string;
  providerId?: string;
  repository?: string;
  buildNode?: string;
  ref?: string;
  region?: string;
  exposure?: string;
  domain?: string;
  port?: string;
  manifest?: string;
  platform?: string;
  registryHost?: string;
  pushHost?: string;
  context?: string;
  dockerfile?: string;
  envSecrets?: Record<string, string>;
};

export type BuildRun = {
  id?: string;
  status?: string;
  source?: string;
  buildNode?: string;
  providerId?: string;
  repository?: string;
  ref?: string;
  message?: string;
  createdAt?: number;
  updatedAt?: number;
  request?: Partial<BuildImportRequest> & Record<string, unknown>;
  events?: DeployStep[];
};

export type BuilderStatus = {
  registry?: { registryHost?: string; namespace?: string };
  runners?: { name?: string; online?: boolean }[];
};

type BuilderBuild = {
  id: string;
  status: string;
  message?: string;
  events?: { seq: number; stage: string; message: string }[];
};

export async function fetchBuilderStatus(token: string, signal?: AbortSignal): Promise<BuilderStatus> {
  return readJson<BuilderStatus>(await fetch("/v1/builder/status", { headers: authHeaders(token), signal }));
}

export async function buildImportStream(request: BuildImportRequest, onStep: (step: DeployStep) => void, signal?: AbortSignal): Promise<BuilderBuild> {
  const body: Record<string, unknown> = {};
  for (const key of ["repoUrl", "providerId", "repository", "ref", "region", "exposure", "domain", "manifest", "platform", "context", "dockerfile"] as const) {
    if (request[key]) body[key] = request[key];
  }
  if (request.port?.trim()) body.port = Number(request.port);
  if (request.envSecrets) body.envSecrets = request.envSecrets;
  const queued = await readJson<{ build: BuilderBuild }>(await fetch("/v1/builder/builds", {
    method: "POST", headers: authHeaders(request.token, true), body: JSON.stringify(body), signal,
  }));
  const id = queued.build?.id;
  if (!id) throw new Error("Builder did not return a build ID");
  onStep({ name: "Builder", status: "start", message: `Queued: ${id}` });
  let afterSeq = 0;
  while (true) {
    const { build } = await readJson<{ build: BuilderBuild }>(await fetch(`/v1/builder/builds/${encodeURIComponent(id)}?afterSeq=${afterSeq}`, {
      headers: authHeaders(request.token), signal,
    }));
    if (!build || build.id !== id) throw new Error("Builder returned an invalid build record");
    for (const event of build.events || []) {
      if (event.seq <= afterSeq) continue;
      afterSeq = event.seq;
      onStep({ name: event.stage || "Builder", status: "ok", message: event.message });
    }
    if ((build.events?.length || 0) < 500 && ["succeeded", "failed", "canceled"].includes(build.status)) {
      if (build.status !== "succeeded") throw new Error(build.message || `Build ${build.status}: ${id}`);
      onStep({ name: "Builder", status: "ok", message: `Completed: ${id}` });
      return build;
    }
    if (signal?.aborted) throw new DOMException("Build log follow aborted", "AbortError");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

export async function fetchBuildRuns(token: string): Promise<{ runs?: BuildRun[] }> {
  const response = await fetch("/v1/builds", { headers: authHeaders(token) });
  return readJson(response) as Promise<{ runs?: BuildRun[] }>;
}

export type DeploymentEvent = {
  id?: string;
  kind?: "service" | "compose" | string;
  name?: string;
  slug?: string;
  sourceName?: string;
  origin?: "cli" | "dashboard" | string;
  status?: string;
  stepCount?: number;
  steps?: DeployStep[];
  createdAt?: number;
  error?: string;
  gitSource?: { repoUrl?: string; providerId?: string; repository?: string; ref?: string } | null;
};

export async function fetchDeploymentHistory(token: string): Promise<{ events?: DeploymentEvent[] }> {
  const response = await fetch("/v1/deployments/history", { headers: authHeaders(token) });
  return readJson(response) as Promise<{ events?: DeploymentEvent[] }>;
}

export async function fetchDeploymentEvent(token: string, id: string): Promise<{ event?: DeploymentEvent }> {
  const response = await fetch(`/v1/deployments/history/${encodeURIComponent(id)}`, { headers: authHeaders(token) });
  return readJson(response) as Promise<{ event?: DeploymentEvent }>;
}

export async function fetchBuildRun(token: string, id: string): Promise<{ run?: BuildRun }> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/v1/builds/${encodeURIComponent(id)}`, {
      headers: authHeaders(token),
      signal: controller.signal,
    });
    return await readJson(response) as { run?: BuildRun };
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function retryBuildRun(token: string, id: string, overrides?: { envSecrets?: Record<string, string> }): Promise<unknown> {
  const body: Record<string, unknown> = {};
  if (overrides?.envSecrets) body.envSecrets = overrides.envSecrets;
  const response = await fetch(`/v1/builds/${encodeURIComponent(id)}/retry`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(body),
  });
  return readJson(response);
}

export async function cancelBuildRun(token: string, id: string): Promise<{ run?: BuildRun; replayed?: boolean }> {
  const response = await fetch(`/v1/builds/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify({}),
  });
  return readJson(response) as Promise<{ run?: BuildRun; replayed?: boolean }>;
}

export async function retryBuildRunStream(token: string, id: string, onStep: (step: DeployStep) => void, overrides?: { envSecrets?: Record<string, string> }): Promise<unknown> {
  const body: Record<string, unknown> = {};
  if (overrides?.envSecrets) body.envSecrets = overrides.envSecrets;
  const response = await fetch(`/v1/builds/${encodeURIComponent(id)}/retry/stream`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(body),
  });
  return consumeStream(response, onStep);
}

export async function registryServeStream(
  request: { token: string; node: string; port?: string; image?: string; name?: string; storageClass?: string },
  onStep: (step: DeployStep) => void,
): Promise<unknown> {
  const body: Record<string, unknown> = { node: request.node };
  if (request.port && request.port.trim()) body.port = Number(request.port);
  for (const [src, dst] of [["image", "image"], ["name", "name"], ["storageClass", "storageClass"]] as const) {
    const value = request[src];
    if (value && value.trim()) body[dst] = value.trim();
  }
  const response = await fetch("/v1/registry/serve/stream", {
    method: "POST",
    headers: authHeaders(request.token, true),
    body: JSON.stringify(body),
  });
  return consumeStream(response, onStep);
}

// Thin wrapper over the shared NDJSON consumer for deploy/build step streams.
function consumeStream(response: Response, onStep: (step: DeployStep) => void): Promise<unknown> {
  return consumeNdjson(response, (event) => onStep(event as DeployStep), {
    unavailableMessage: "deployment stream is unavailable",
  });
}
