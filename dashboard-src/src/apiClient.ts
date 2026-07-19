// Shared HTTP helpers for the Control /v1 API.
//
// Every dashboard API call authenticates with the management token as a Bearer
// header, parses JSON with the same error contract, and long-running actions
// stream NDJSON. This module centralizes those three concerns so the per-resource
// API files (lifecycleApi, controlResourcesApi, deployApi, logsApi, ...) stay thin.

export function authHeaders(token: string, json = false): Record<string, string> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

// Parse a response body as JSON, throwing a useful error on a non-2xx status or
// unparseable payload. Mirrors the contract the Control server uses: errors carry
// an `error` string.
export async function readJson<T = Record<string, unknown>>(response: Response): Promise<T> {
  const text = await response.text();
  let payload: Record<string, unknown> = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Invalid response format (HTTP ${response.status}): ${text.slice(0, 100)}`);
  }
  if (!response.ok) throw new Error(String(payload.error || `HTTP ${response.status}`));
  return payload as T;
}

export async function apiGet<T = Record<string, unknown>>(path: string, token: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: authHeaders(token), signal });
  return readJson<T>(response);
}

export async function apiPost<T = Record<string, unknown>>(path: string, token: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: authHeaders(token, true),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  return readJson<T>(response);
}

export type NdjsonOptions = {
  // Status value that marks the terminal frame carrying the result (default "done").
  doneStatus?: string;
  // Status value that marks a failure frame (default "fail").
  failStatus?: string;
  // Error thrown when the response has no readable body.
  unavailableMessage?: string;
};

// Consume an NDJSON stream, invoking `onEvent` for each JSON line. Resolves with the
// `result` field of the terminal `{status: doneStatus}` frame, and throws on a
// `{status: failStatus}` frame. A stream that ends without any terminal frame is
// treated as truncated (e.g. Control restarted mid-deploy) and throws instead of
// silently resolving — callers only use this for step-shaped streams, never for
// line-shaped log tails, which keep their own tolerant readers.
export async function consumeNdjson<T = unknown>(
  response: Response,
  onEvent: (event: Record<string, unknown>) => void,
  opts: NdjsonOptions = {},
): Promise<T> {
  const doneStatus = opts.doneStatus ?? "done";
  const failStatus = opts.failStatus ?? "fail";
  if (!response.ok) {
    const payload = await readJson(response);
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  if (!response.body) throw new Error(opts.unavailableMessage ?? "stream is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: unknown = null;
  let sawTerminal = false;
  const handleLine = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as Record<string, unknown>;
    onEvent(event);
    if (event.status === failStatus) {
      sawTerminal = true;
      throw new Error(String(event.message || "stream failed"));
    }
    if (event.status === doneStatus) {
      sawTerminal = true;
      result = event.result;
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) handleLine(line);
    }
    buffer += decoder.decode();
    handleLine(buffer);
  } finally {
    // Release the underlying connection on every exit path (fail frame, malformed
    // line, network cut); after a clean EOF this cancel is a no-op.
    reader.cancel().catch(() => {});
  }
  if (!sawTerminal) throw new Error("stream ended before completion");
  return result as T;
}
