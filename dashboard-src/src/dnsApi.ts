import { apiGet, apiPost } from "./apiClient";
import type { NodePublicIngress } from "./types";

export type DnsProviderId = "cloudflare" | "alidns" | "dnspod";

export type DnsProvider = {
  id: DnsProviderId;
  label: string;
  fields: string[];
};

export type DnsZone = {
  name: string;
  provider: DnsProviderId;
  providerLabel?: string;
  primary?: boolean;
  credentialSecrets?: Record<string, string>;
  credentialsConfigured?: boolean;
  status?: "ready" | "error" | "missing-credentials" | string;
  detail?: string;
  checkedAt?: number;
  createdAt?: number;
  services?: string[];
};

export type DnsZonesPayload = {
  zones?: DnsZone[];
  providers?: DnsProvider[];
};

export function fetchDnsZones(token: string, signal?: AbortSignal): Promise<DnsZonesPayload> {
  return apiGet<DnsZonesPayload>("/v1/dns/zones", token, signal);
}

export function addDnsZone({ token, name, provider, credentials, sameAs, update }: {
  token: string;
  name: string;
  provider: DnsProviderId;
  credentials?: Record<string, string>;
  sameAs?: string;
  update?: boolean;
}): Promise<{ zone?: DnsZone }> {
  const body: Record<string, unknown> = { name, provider, update: Boolean(update) };
  if (sameAs) body.sameAs = sameAs;
  else if (credentials) body.credentials = credentials;
  return apiPost("/v1/dns/zones", token, body);
}

export function checkDnsZone({ token, name }: { token: string; name: string }): Promise<{ status?: string; detail?: string; checkedAt?: number }> {
  return apiPost("/v1/dns/zones/check", token, { name });
}

export function removeDnsZone({ token, name }: { token: string; name: string }) {
  return apiPost("/v1/dns/zones/remove", token, { name });
}

export function setNodePublicIngress({ token, node, enabled, publicIp }: {
  token: string;
  node: string;
  enabled: boolean;
  publicIp?: string;
}): Promise<{ node?: string; publicIngress?: NodePublicIngress; dns?: string[] }> {
  const body: Record<string, unknown> = { node, enabled };
  if (publicIp) body.publicIp = publicIp;
  return apiPost("/v1/nodes/public-ingress", token, body);
}
