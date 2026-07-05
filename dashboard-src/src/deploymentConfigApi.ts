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
  const response = await fetch(`/v1/deployments/${encodeURIComponent(name)}/config`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const text = await response.text();
  let payload: Record<string, unknown> = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Invalid response format (HTTP ${response.status}): ${text.slice(0, 100)}`);
  }
  if (!response.ok) throw new Error(String(payload.error || `HTTP ${response.status}`));
  return payload as DeploymentConfig;
}
