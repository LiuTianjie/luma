import { DEPLOY_TEMPLATES } from "./deploy/templates";
import type { DashboardAddonsSummary } from "./addonCapabilities";
import type { DashboardIssue, DashboardNode, DashboardPayload, DashboardRegion, DashboardService, TrafficPath } from "./types";
import { groupApplications, isServiceHealthy, type Application } from "./components/applicationModel";

export type PageId =
  | "overview"
  | "applications"
  | "deploy"
  | "builder"
  | "deployments"
  | "observability"
  | "storage"
  | "registry"
  | "nodes"
  | "setup"
  | "addons"
  | "credentials"
  | "maintenance"
  | "security"
  | "update";
export type NavPage = Exclude<PageId, "update">;

export type IssueCounts = {
  critical: number;
  warning: number;
  info: number;
};

export type DashboardViewModel = {
  clusterId: string;
  addons?: DashboardAddonsSummary;
  nodes: DashboardNode[];
  regions: DashboardRegion[];
  services: DashboardService[];
  applications: Application[];
  trafficPaths: TrafficPath[];
  issues: DashboardIssue[];
  storageVolumes: NonNullable<NonNullable<DashboardPayload["storage"]>["volumes"]>;
  storageWarnings: string[];
  activeNodes: number;
  healthyServices: number;
  issueCounts: IssueCounts;
  metricNodes: number;
  builderNodes: number;
  hasManagedRegistry?: boolean;
  templateCount: number;
  deployServiceTemplates: number;
  deployComposeTemplates: number;
  healthScore: number;
};

function readyNode(node: DashboardNode) {
  return (node.state || "").toLowerCase() === "ready" && (node.availability || "").toLowerCase() !== "drain";
}

function issueCounts(issues: DashboardIssue[]): IssueCounts {
  return issues.reduce<IssueCounts>(
    (counts, issue) => {
      if (issue.severity === "critical") return { ...counts, critical: counts.critical + 1 };
      if (issue.severity === "warning") return { ...counts, warning: counts.warning + 1 };
      return { ...counts, info: counts.info + 1 };
    },
    { critical: 0, warning: 0, info: 0 },
  );
}

function healthScore(activeNodes: number, totalNodes: number, healthyServices: number, totalServices: number, issues: IssueCounts) {
  if (!totalNodes && !totalServices) return 0;
  const nodeRatio = totalNodes ? activeNodes / totalNodes : 1;
  const serviceRatio = totalServices ? healthyServices / totalServices : 1;
  const issuePenalty = Math.min(0.45, issues.critical * 0.18 + issues.warning * 0.07);
  return Math.max(0, Math.round((nodeRatio * 0.38 + serviceRatio * 0.62 - issuePenalty) * 100));
}

const NODE_DISK_WARNING_PERCENT = 85;
const NODE_DISK_CRITICAL_PERCENT = 92;

function augmentNodeDiskIssues(nodes: DashboardNode[], baseIssues: DashboardIssue[]): DashboardIssue[] {
  const existingDiskTargets = new Set(
    baseIssues
      .filter((item) => item.kind === "node-disk" && item.target)
      .map((item) => item.target as string),
  );
  const extra: DashboardIssue[] = [];
  for (const node of nodes) {
    const name = (node.name || "").trim();
    const disk = node.metrics?.diskUsedPercent;
    if (!name || typeof disk !== "number" || !Number.isFinite(disk) || existingDiskTargets.has(name)) continue;
    if (disk >= NODE_DISK_CRITICAL_PERCENT) {
      extra.push({
        severity: "critical",
        kind: "node-disk",
        target: name,
        message: `Node ${name} disk usage is ${Math.round(disk)}% (>= ${NODE_DISK_CRITICAL_PERCENT}%)`,
      });
    } else if (disk >= NODE_DISK_WARNING_PERCENT) {
      extra.push({
        severity: "warning",
        kind: "node-disk",
        target: name,
        message: `Node ${name} disk usage is ${Math.round(disk)}% (>= ${NODE_DISK_WARNING_PERCENT}%)`,
      });
    }
  }
  return extra.length ? [...baseIssues, ...extra] : baseIssues;
}

export function createDashboardViewModel(
  payload: DashboardPayload | null,
  addonsOverride?: DashboardAddonsSummary,
): DashboardViewModel {
  const nodes = payload?.nodes || [];
  const services = payload?.services || [];
  const issues = augmentNodeDiskIssues(nodes, payload?.issues || []);
  const trafficPaths = payload?.trafficPaths || [];
  const storageVolumes = payload?.storage?.volumes || [];
  const storageWarnings = payload?.storage?.warnings || [];
  const activeNodes = nodes.filter(readyNode).length;
  const healthyServices = services.filter(isServiceHealthy).length;
  const counts = issueCounts(issues);

  return {
    clusterId: payload?.cluster?.id || "-",
    addons: payload?.addons || addonsOverride,
    nodes,
    regions: payload?.regions || [],
    services,
    applications: groupApplications(services),
    trafficPaths,
    issues,
    storageVolumes,
    storageWarnings,
    activeNodes,
    healthyServices,
    issueCounts: counts,
    metricNodes: nodes.filter((node) => node.metrics?.cpuPercent || node.metrics?.memoryUsedPercent).length,
    builderNodes: (payload?.build?.nodes || []).length,
    hasManagedRegistry: payload ? Boolean(payload.build?.registryHost?.trim()) : undefined,
    templateCount: DEPLOY_TEMPLATES.length,
    deployServiceTemplates: DEPLOY_TEMPLATES.filter((template) => template.mode === "service").length,
    deployComposeTemplates: DEPLOY_TEMPLATES.filter((template) => template.mode === "compose").length,
    healthScore: healthScore(activeNodes, nodes.length, healthyServices, services.length, counts),
  };
}
