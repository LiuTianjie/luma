import type { DashboardStorageClass } from "./types";
import { apiGet, apiPost } from "./apiClient";

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
  return apiGet<SecretsPayload>("/v1/secrets", token, signal);
}

export async function fetchRegistries({ token, signal }: { token: string; signal?: AbortSignal }): Promise<RegistriesPayload> {
  return apiGet<RegistriesPayload>("/v1/registries", token, signal);
}

export async function fetchGitProviders({ token, signal }: { token: string; signal?: AbortSignal }): Promise<GitProvidersPayload> {
  return apiGet<GitProvidersPayload>("/v1/git-providers", token, signal);
}

export async function fetchGitProviderRepositories({ token, providerId, signal }: { token: string; providerId: string; signal?: AbortSignal }): Promise<GitRepositoriesPayload> {
  return apiGet<GitRepositoriesPayload>(`/v1/git-providers/${encodeURIComponent(providerId)}/repositories`, token, signal);
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
  return apiGet<GitRefsPayload>(
    `/v1/git-providers/${encodeURIComponent(providerId)}/repositories/${encodeURIComponent(owner || "")}/${encodeURIComponent(repo || "")}/refs`,
    token,
    signal,
  );
}

export async function fetchStorageClasses({ token, signal }: { token: string; signal?: AbortSignal }): Promise<StorageClassesPayload> {
  return apiGet<StorageClassesPayload>("/v1/storage", token, signal);
}

export async function setSecret({ token, name, value, scope }: { token: string; name: string; value: string; scope?: string }) {
  const body: Record<string, unknown> = { name, value };
  if (scope) body.scope = scope;
  return apiPost("/v1/secrets", token, body);
}

export async function setRegistry({ token, host, username, password }: { token: string; host: string; username: string; password: string }) {
  return apiPost("/v1/registries", token, { host, username, password });
}

export async function removeRegistry({ token, host }: { token: string; host: string }) {
  return apiPost("/v1/registries/remove", token, { host });
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
  return apiPost("/v1/git-providers", token, body);
}

export async function removeGitProvider({ token, id }: { token: string; id: string }) {
  return apiPost("/v1/git-providers/remove", token, { id });
}
