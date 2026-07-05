import type { DeployStep } from "./deploy/types";
import type { ServiceHistoryPayload, ServiceRollbackPayload } from "./types";

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

export async function restartApplication({
  token,
  stack,
  service,
}: {
  token: string;
  stack: string;
  service?: string;
}) {
  const response = await fetch("/v1/applications/restart", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ stack, service }),
  });
  return readJson(response);
}

export async function updateApplicationStream(
  {
    token,
    name,
  }: {
    token: string;
    name: string;
  },
  onStep: (step: DeployStep) => void,
) {
  const response = await fetch("/v1/applications/update/stream", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) {
    const payload = await readJson(response);
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  if (!response.body) throw new Error("application update stream is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: unknown = null;
  const handleLine = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as DeployStep;
    onStep(event);
    if (event.status === "fail") throw new Error(event.message || "application update failed");
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

export async function fetchServiceHistory({
  token,
  name,
}: {
  token: string;
  name: string;
}): Promise<ServiceHistoryPayload> {
  const response = await fetch("/v1/services/history", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return readJson(response) as Promise<ServiceHistoryPayload>;
}

export async function rollbackService({
  token,
  name,
  version,
}: {
  token: string;
  name: string;
  version?: number;
}): Promise<ServiceRollbackPayload> {
  const response = await fetch("/v1/services/rollback", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(version === undefined ? { name } : { name, version }),
  });
  return readJson(response) as Promise<ServiceRollbackPayload>;
}

export async function retryCertificate({
  token,
  domain,
  routeId,
}: {
  token: string;
  domain: string;
  routeId?: string;
}) {
  const response = await fetch("/v1/certificates/retry", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ domain, routeId }),
  });
  return readJson(response);
}
