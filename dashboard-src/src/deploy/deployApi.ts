import type { DeployPreviewResult, DeployStep } from "./types";

type DeployRequest = {
  token: string;
  manifest: string;
  composeContent?: string;
  sourceName: string;
  skipDns: boolean;
  skipWebhook: boolean;
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
    skipWebhook: request.skipWebhook,
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
  if (!response.ok) {
    const payload = await readJson(response);
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  if (!response.body) throw new Error("deployment stream is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: unknown = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line) as DeployStep;
      onStep(event);
      if (event.status === "fail") throw new Error(event.message || "deployment failed");
      if (event.status === "done") result = event.result;
    }
  }
  return result;
}
