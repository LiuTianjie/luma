import type { DashboardNode, Lang } from "../types";
import type { Exposure, Region } from "./types";

export const REGIONS: Region[] = ["cn", "global", "home"];
export const EXPOSURES: Exposure[] = ["none", "cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"];

const EXPOSURE_REGION: Partial<Record<Exposure, Region>> = {
  "cn-edge": "cn",
  "external-edge": "global",
  "tailscale-relay": "home",
};

export function requiredRegionForExposure(exposure: Exposure): Region | "" {
  return EXPOSURE_REGION[exposure] || "";
}

export function isReadyNode(node: DashboardNode): boolean {
  return node.state === "ready" && ["active", "eligible", ""].includes(node.availability || "");
}

export function hasReadyNodeInRegion(nodes: DashboardNode[], region: Region): boolean {
  return nodes.some((node) => node.region === region && isReadyNode(node));
}

export function nodesForRegion(nodes: DashboardNode[], region: Region | ""): DashboardNode[] {
  return nodes.filter((node) => (!region || node.region === region) && node.name && isReadyNode(node));
}

export function findNode(nodes: DashboardNode[], name: string): DashboardNode | undefined {
  return nodes.find((node) => node.name === name);
}

export function clearNodeIfIncompatible(nodes: DashboardNode[], nodeName: string, region: Region | ""): string {
  if (!nodeName) return "";
  const node = findNode(nodes, nodeName);
  if (!node || !isReadyNode(node)) return "";
  return !region || node.region === region ? nodeName : "";
}

export function regionOptionLabel(nodes: DashboardNode[], region: Region, lang: Lang = "zh"): string {
  if (hasReadyNodeInRegion(nodes, region)) return region;
  return lang === "zh" ? `${region} (无 ready 节点)` : `${region} (no ready nodes)`;
}

export function exposureOptionLabel(nodes: DashboardNode[], exposure: Exposure, lang: Lang = "zh"): string {
  const requiredRegion = requiredRegionForExposure(exposure);
  if (!requiredRegion || hasReadyNodeInRegion(nodes, requiredRegion)) return exposure;
  return lang === "zh" ? `${exposure} (缺少 ${requiredRegion} ready 节点)` : `${exposure} (missing ready ${requiredRegion} node)`;
}
