import type { DeployPreviewResult, DeployStep } from "./types";

type DeployRequest = {
  token: string;
  manifest: string;
  composeContent?: string;
  sourceName: string;
  skipDns: boolean;
  skipOrchestrator: boolean;
};

async function readJson(response: Response) {
  const text = await response.text();
  let payload: Record<string, unknown> = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Invalid response format (HTTP ${response.status}): ${text.slice(0, 100)}`);
  }
  if (!response.ok) throw new Error(String(payload.error || `HTTP ${response.status}`));
  return payload;
}

function bodyFor(request: DeployRequest) {
  return JSON.stringify({
    manifest: request.manifest,
    composeContent: request.composeContent,
    sourceName: request.sourceName,
    skipDns: request.skipDns,
    skipOrchestrator: request.skipOrchestrator,
    source: "dashboard",
    client: { name: "luma-dashboard", source: "dashboard" },
  });
}

export async function previewService(request: DeployRequest): Promise<DeployPreviewResult> {
  const response = await fetch("/v1/deployments/preview", {
    method: "POST",
    headers: { Authorization: `Bearer ${request.token}`, "Content-Type": "application/json" },
    body: bodyFor(request),
  });
  return readJson(response) as Promise<DeployPreviewResult>;
}

export async function previewCompose(request: DeployRequest): Promise<DeployPreviewResult> {
  const response = await fetch("/v1/compose-deployments/preview", {
    method: "POST",
    headers: { Authorization: `Bearer ${request.token}`, "Content-Type": "application/json" },
    body: bodyFor(request),
  });
  return readJson(response) as Promise<DeployPreviewResult>;
}

export async function deployStream(request: DeployRequest, mode: "service" | "compose", onStep: (step: DeployStep) => void): Promise<unknown> {
  const response = await fetch(mode === "service" ? "/v1/deployments/stream" : "/v1/compose-deployments/stream", {
    method: "POST",
    headers: { Authorization: `Bearer ${request.token}`, "Content-Type": "application/json" },
    body: bodyFor(request),
  });
  return consumeStream(response, onStep);
}

export type BuildImportRequest = {
  token: string;
  repoUrl: string;
  buildNode: string;
  ref?: string;
  region?: string;
  exposure?: string;
  domain?: string;
  port?: string;
  platform?: string;
  registryHost?: string;
};

export async function buildImportStream(request: BuildImportRequest, onStep: (step: DeployStep) => void): Promise<unknown> {
  const body: Record<string, unknown> = { repoUrl: request.repoUrl, buildNode: request.buildNode, source: "dashboard", client: { name: "luma-dashboard", source: "dashboard" } };
  for (const [src, dst] of [["ref", "ref"], ["region", "region"], ["exposure", "exposure"], ["domain", "domain"], ["platform", "platform"], ["registryHost", "registryHost"]] as const) {
    const value = request[src];
    if (value) body[dst] = value;
  }
  if (request.port && request.port.trim()) body.port = Number(request.port);
  const response = await fetch("/v1/builds/stream", {
    method: "POST",
    headers: { Authorization: `Bearer ${request.token}`, "Content-Type": "application/json" },
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
    headers: { Authorization: `Bearer ${request.token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return consumeStream(response, onStep);
}

async function consumeStream(response: Response, onStep: (step: DeployStep) => void): Promise<unknown> {
  if (!response.ok) {
    const payload = await readJson(response);
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  if (!response.body) throw new Error("deployment stream is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: unknown = null;
  const handleLine = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as DeployStep;
    onStep(event);
    if (event.status === "fail") throw new Error(event.message || "deployment failed");
    if (event.status === "done") result = event.result;
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) handleLine(line);
  }
  buffer += decoder.decode();
  handleLine(buffer);
  return result;
}
