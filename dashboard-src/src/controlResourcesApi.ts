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

export type GitProviderCredential = {
  id?: string;
  type?: "github" | "gitea" | string;
  account?: string;
  baseUrl?: string;
  cloneBaseUrl?: string;
  username?: string;
  configured?: boolean;
  updatedAt?: number;
};

export type GitRepository = {
  fullName: string;
  cloneUrl?: string;
  defaultBranch?: string;
  private?: boolean;
};

export type GitRef = {
  name: string;
  type: "branch" | "tag" | string;
};

export type RegistriesPayload = {
  registries?: RegistryCredential[];
};

export type GitProvidersPayload = {
  providers?: GitProviderCredential[];
};

export type GitRepositoriesPayload = {
  repositories?: GitRepository[];
};

export type GitRefsPayload = {
  refs?: GitRef[];
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

export async function fetchGitProviders({ token, signal }: { token: string; signal?: AbortSignal }): Promise<GitProvidersPayload> {
  const response = await fetch("/v1/git-providers", {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<GitProvidersPayload>;
}

export async function fetchGitProviderRepositories({ token, providerId, signal }: { token: string; providerId: string; signal?: AbortSignal }): Promise<GitRepositoriesPayload> {
  const response = await fetch(`/v1/git-providers/${encodeURIComponent(providerId)}/repositories`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<GitRepositoriesPayload>;
}

export async function fetchGitProviderRefs({
  token,
  providerId,
  repository,
  signal,
}: {
  token: string;
  providerId: string;
  repository: string;
  signal?: AbortSignal;
}): Promise<GitRefsPayload> {
  const [owner, repo] = repository.split("/", 2);
  const response = await fetch(`/v1/git-providers/${encodeURIComponent(providerId)}/repositories/${encodeURIComponent(owner || "")}/${encodeURIComponent(repo || "")}/refs`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<GitRefsPayload>;
}

export async function fetchStorageClasses({ token, signal }: { token: string; signal?: AbortSignal }): Promise<StorageClassesPayload> {
  const response = await fetch("/v1/storage", {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  return readJson(response) as Promise<StorageClassesPayload>;
}

async function postJson(path: string, token: string, body: Record<string, unknown>) {
  const response = await fetch(path, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readJson(response);
}

export async function setSecret({ token, name, value, scope }: { token: string; name: string; value: string; scope?: string }) {
  const body: Record<string, unknown> = { name, value };
  if (scope) body.scope = scope;
  return postJson("/v1/secrets", token, body);
}

export async function setRegistry({ token, host, username, password }: { token: string; host: string; username: string; password: string }) {
  return postJson("/v1/registries", token, { host, username, password });
}

export async function removeRegistry({ token, host }: { token: string; host: string }) {
  return postJson("/v1/registries/remove", token, { host });
}

export async function setGitProvider({
  token,
  providerType,
  account,
  baseUrl,
  cloneBaseUrl,
  username,
  gitToken,
}: {
  token: string;
  providerType: string;
  account: string;
  baseUrl?: string;
  cloneBaseUrl?: string;
  username?: string;
  gitToken: string;
}) {
  const body: Record<string, unknown> = { type: providerType, account, token: gitToken };
  if (baseUrl) body.baseUrl = baseUrl;
  if (cloneBaseUrl) body.cloneBaseUrl = cloneBaseUrl;
  if (username) body.username = username;
  return postJson("/v1/git-providers", token, body);
}

export async function removeGitProvider({ token, id }: { token: string; id: string }) {
  return postJson("/v1/git-providers/remove", token, { id });
}
