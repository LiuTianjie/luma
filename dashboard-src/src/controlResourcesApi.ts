import type { DashboardStorageClass } from "./types";

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

export type SecretsPayload = {
  secrets?: string[];
};

export type RegistryCredential = {
  host?: string;
  serverAddress?: string;
  username?: string;
  configured?: boolean;
};

export type RegistriesPayload = {
  registries?: RegistryCredential[];
};

export type StorageClassesPayload = {
  storageClasses?: DashboardStorageClass[];
};

export async function fetchSecrets({ token, signal }: { token: string; signal?: AbortSignal }): Promise<SecretsPayload> {
  const response = await fetch("/v1/secrets", {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<SecretsPayload>;
}

export async function fetchRegistries({ token, signal }: { token: string; signal?: AbortSignal }): Promise<RegistriesPayload> {
  const response = await fetch("/v1/registries", {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<RegistriesPayload>;
}

export async function fetchStorageClasses({ token, signal }: { token: string; signal?: AbortSignal }): Promise<StorageClassesPayload> {
  const response = await fetch("/v1/storage", {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<StorageClassesPayload>;
}
