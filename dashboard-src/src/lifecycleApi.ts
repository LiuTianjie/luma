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
