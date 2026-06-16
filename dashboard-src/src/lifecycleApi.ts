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
