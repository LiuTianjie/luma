import { apiGet } from "./apiClient";

export type DeploymentConfig = {
  kind?: string;
  name?: string;
  slug?: string;
  sourceName?: string;
  updatedAt?: number;
  manifest?: string;
  composeContent?: string;
  gitSource?: {
    repoUrl?: string;
    providerId?: string;
    repository?: string;
    ref?: string;
    buildNode?: string;
    buildRunId?: string;
  } | null;
};

export async function fetchDeploymentConfig({ token, name }: { token: string; name: string }): Promise<DeploymentConfig> {
  return apiGet<DeploymentConfig>(`/v1/deployments/${encodeURIComponent(name)}/config`, token);
}
