import type { DashboardNode, DashboardRegion, Lang } from "../types";
import type { Exposure, Region } from "./types";

export const BUILTIN_REGIONS: Region[] = ["cn", "global", "home"];
export const REGIONS = BUILTIN_REGIONS;

export function regionChoices(registered: Array<{ name?: string } | string> = [], nodes: DashboardNode[] = []): Region[] {
  const names = new Set<string>(BUILTIN_REGIONS);
  for (const item of registered) {
    const name = typeof item === "string" ? item : item.name;
    if (name) names.add(name);
  }
  for (const node of nodes) {
    if (node.region) names.add(node.region);
  }
  return [...names].sort((left, right) => {
    const leftBuiltin = BUILTIN_REGIONS.indexOf(left);
    const rightBuiltin = BUILTIN_REGIONS.indexOf(right);
    if (leftBuiltin >= 0 && rightBuiltin >= 0) return leftBuiltin - rightBuiltin;
    if (leftBuiltin >= 0) return -1;
    if (rightBuiltin >= 0) return 1;
    return left.localeCompare(right);
  });
}
export const EXPOSURES: Exposure[] = ["none", "cn-edge", "external-edge", "node-edge", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"];

const EXPOSURE_REGION: Partial<Record<Exposure, Region>> = {
  "cn-edge": "cn",
  "external-edge": "global",
};

export function requiredRegionForExposure(exposure: Exposure): Region | "" {
  return EXPOSURE_REGION[exposure] || "";
}

const DEFAULT_EXPOSURES: Record<string, Exposure[]> = {
  cn: ["none", "cn-edge", "cloudflare-tunnel", "tcp-relay", "node-edge"],
  global: ["none", "external-edge", "cloudflare-tunnel", "tcp-relay", "node-edge"],
  home: ["none", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"],
};

export function exposureAllowedInRegion(exposure: Exposure, region: Region, catalog: DashboardRegion[] = []): boolean {
  const allowed = catalog.find((item) => item.name === region)?.exposures ?? DEFAULT_EXPOSURES[region] ?? ["none"];
  return allowed.includes(exposure);
}

export function isReadyNode(node: DashboardNode): boolean {
  return node.state === "ready" && ["active", "eligible", ""].includes(node.availability || "");
}

export function hasReadyNodeInRegion(nodes: DashboardNode[], region: Region): boolean {
  return nodes.some((node) => node.region === region && isReadyNode(node));
}

export function hasPublicIngress(node: DashboardNode): boolean {
  return Boolean(node.publicIngress?.enabled && node.publicIngress.publicIp);
}

/** Nodes a node-edge service can pin to: ready, in the region, serving public ingress. */
export function publicIngressNodes(nodes: DashboardNode[], region: Region | ""): DashboardNode[] {
  return nodesForRegion(nodes, region).filter(hasPublicIngress);
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

/** Node choices for a service: node-edge can only pin to a node serving public ingress. */
export function nodeChoicesForExposure(nodes: DashboardNode[], region: Region | "", exposure: Exposure): DashboardNode[] {
  return exposure === "node-edge" ? publicIngressNodes(nodes, region) : nodesForRegion(nodes, region);
}

/** Keep a still-valid pin; node-edge otherwise defaults to the first node with public ingress. */
export function nodeForExposure(nodes: DashboardNode[], current: string, region: Region | "", exposure: Exposure): string {
  const choices = nodeChoicesForExposure(nodes, region, exposure);
  if (current && choices.some((node) => node.name === current)) return current;
  if (exposure === "node-edge") return choices[0]?.name || "";
  return clearNodeIfIncompatible(nodes, current, region);
}

export function nodeEdgeError(nodes: DashboardNode[], exposure: Exposure, nodeName: string, label: string, lang: Lang): string {
  if (exposure !== "node-edge") return "";
  const zh = lang === "zh";
  if (!nodeName) return zh ? `${label} 使用 node-edge 时必须选择已开启公网入口的节点` : `${label} needs a node with public ingress for node-edge`;
  const node = findNode(nodes, nodeName);
  if (nodes.length && (!node || !hasPublicIngress(node))) {
    return zh
      ? `${label} 节点 ${nodeName} 尚未开启公网入口：在「基础设施 → 节点 → ${nodeName}」中开启`
      : `${label} node ${nodeName} has no public ingress yet; enable it under Infrastructure → Nodes → ${nodeName}`;
  }
  return "";
}

export function exposureOptionLabel(nodes: DashboardNode[], exposure: Exposure, lang: Lang = "zh", region: Region | "" = ""): string {
  if (exposure === "node-edge" && nodes.length && !publicIngressNodes(nodes, region).length) {
    return lang === "zh" ? "node-edge (无已开启公网入口的节点)" : "node-edge (no node with public ingress)";
  }
  const requiredRegion = requiredRegionForExposure(exposure);
  if (!requiredRegion || hasReadyNodeInRegion(nodes, requiredRegion)) return exposure;
  return lang === "zh" ? `${exposure} (缺少 ${requiredRegion} ready 节点)` : `${exposure} (missing ready ${requiredRegion} node)`;
}
