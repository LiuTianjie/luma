import type { DeployStep } from "./deploy/types";
import type { ServiceHistoryPayload, ServiceRollbackPayload } from "./types";
import { apiPost, authHeaders, consumeNdjson } from "./apiClient";

export type ApplicationRestartResult = {
  mode: "recreate" | "task";
  restarted: Array<{ allocId: string; task: string; mode: "recreate" | "task" }>;
  replacementAllocations?: string[];
};

export async function restartApplication({
  token,
  stack,
  service,
}: {
  token: string;
  stack: string;
  service?: string;
}) {
  return apiPost<ApplicationRestartResult>("/v1/applications/restart", token, {
    stack,
    service,
    // Keep application restart semantics explicit across mixed dashboard and
    // Control versions. A whole-app restart must create a new allocation.
    mode: service ? "task" : "recreate",
  });
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
    headers: authHeaders(token, true),
    body: JSON.stringify({ name }),
  });
  return consumeNdjson(response, (event) => onStep(event as DeployStep), {
    unavailableMessage: "application update stream is unavailable",
  });
}

export async function fetchServiceHistory({
  token,
  name,
}: {
  token: string;
  name: string;
}): Promise<ServiceHistoryPayload> {
  return apiPost<ServiceHistoryPayload>("/v1/services/history", token, { name });
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
  return apiPost<ServiceRollbackPayload>("/v1/services/rollback", token, version === undefined ? { name } : { name, version });
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
  return apiPost("/v1/certificates/retry", token, { domain, routeId });
}
