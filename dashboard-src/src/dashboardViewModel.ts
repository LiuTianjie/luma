import { DEPLOY_TEMPLATES } from "./deploy/templates";
import type { DashboardIssue, DashboardNode, DashboardPayload, DashboardService, TrafficPath } from "./types";
import { groupApplications, isServiceHealthy, type Application } from "./components/applicationModel";

export type PageId = "overview" | "applications" | "deploy" | "topology" | "observability" | "storage" | "update";
export type NavPage = Exclude<PageId, "update">;

export type IssueCounts = {
  critical: number;
  warning: number;
  info: number;
};

export type DashboardViewModel = {
  clusterId: string;
  nodes: DashboardNode[];
  services: DashboardService[];
  applications: Application[];
  trafficPaths: TrafficPath[];
  issues: DashboardIssue[];
  storageVolumes: NonNullable<NonNullable<DashboardPayload["storage"]>["volumes"]>;
  storageClasses: NonNullable<NonNullable<DashboardPayload["storage"]>["storageClasses"]>;
  storageWarnings: string[];
  activeNodes: number;
  healthyServices: number;
  issueCounts: IssueCounts;
  metricNodes: number;
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

export function createDashboardViewModel(payload: DashboardPayload | null): DashboardViewModel {
  const nodes = payload?.nodes || [];
  const services = payload?.services || [];
  const issues = payload?.issues || [];
  const trafficPaths = payload?.trafficPaths || [];
  const storageVolumes = payload?.storage?.volumes || [];
  const storageClasses = payload?.storage?.storageClasses || [];
  const storageWarnings = payload?.storage?.warnings || [];
  const activeNodes = nodes.filter(readyNode).length;
  const healthyServices = services.filter(isServiceHealthy).length;
  const counts = issueCounts(issues);

  return {
    clusterId: payload?.cluster?.id || "-",
    nodes,
    services,
    applications: groupApplications(services),
    trafficPaths,
    issues,
    storageVolumes,
    storageClasses,
    storageWarnings,
    activeNodes,
    healthyServices,
    issueCounts: counts,
    metricNodes: nodes.filter((node) => node.metrics?.cpuPercent || node.metrics?.memoryUsedPercent).length,
    templateCount: DEPLOY_TEMPLATES.length,
    deployServiceTemplates: DEPLOY_TEMPLATES.filter((template) => template.mode === "service").length,
    deployComposeTemplates: DEPLOY_TEMPLATES.filter((template) => template.mode === "compose").length,
    healthScore: healthScore(activeNodes, nodes.length, healthyServices, services.length, counts),
  };
}
