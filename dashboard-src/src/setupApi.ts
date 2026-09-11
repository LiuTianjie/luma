import { apiPost } from "./apiClient";

export type SetupCheck = {
  status?: "ready" | "missing" | "error" | "skipped" | string;
  required?: boolean;
  detail?: string;
};

export type SetupCheckPayload = {
  checkedAt?: number;
  checks?: Record<string, SetupCheck>;
};

export function runSetupChecks(token: string): Promise<SetupCheckPayload> {
  return apiPost<SetupCheckPayload>("/v1/dashboard/setup/check", token, {});
}

export function configureSetup(token: string, values: {
  cloudflareZone?: string;
  cloudflareZoneId?: string;
  edgeTarget?: string;
}): Promise<unknown> {
  return apiPost("/v1/dashboard/setup/configure", token, values);
}

export function setBuildConfig(token: string, values: { registryHost?: string; pushHost?: string }): Promise<unknown> {
  return apiPost("/v1/builds/config", token, values);
}
