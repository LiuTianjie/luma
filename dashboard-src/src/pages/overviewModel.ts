import type { Application } from "../components/applicationModel";
import type { DashboardIssue, DashboardNode } from "../types";

export type OverviewIssueGroup = {
  key: string;
  target: string;
  app?: Application;
  node?: DashboardNode;
  severity: string;
  issues: DashboardIssue[];
};
const severityRank = (severity?: string) => severity === "critical" ? 0 : severity === "warning" ? 1 : 2;

/** Group only by explicit object identity. Similar error text is not a causal relationship. */
export function groupOverviewIssues(issues: DashboardIssue[], applications: Application[], nodes: DashboardNode[]): OverviewIssueGroup[] {
  const groups = new Map<string, OverviewIssueGroup>();
  for (const issue of issues) {
    const node = issue.kind === "agent" || issue.kind?.startsWith("node-")
      ? nodes.find((item) => item.name === issue.target) : undefined;
    const matches = issue.kind === "deployment" || issue.kind?.startsWith("service-")
      ? applications.filter((item) => item.stack === issue.target || item.services.some((service) => (service.fullName || service.name) === issue.target)) : [];
    // Ambiguous short service names must not link to an arbitrary application.
    const app = matches.length === 1 ? matches[0] : undefined;
    const key = node ? `node:${node.name}` : app ? `app:${app.stack}` : `issue:${issue.kind || ""}:${issue.target || ""}`;
    const group = groups.get(key) || { key, target: node?.name || app?.stack || issue.target || issue.kind || "—", app, node, severity: issue.severity || "info", issues: [] };
    group.issues.push(issue);
    if (severityRank(issue.severity) < severityRank(group.severity)) group.severity = issue.severity || "info";
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || a.target.localeCompare(b.target));
}

export function nodePressure(node: DashboardNode) {
  return Math.max(node.metrics?.cpuPercent ?? node.metrics?.loadPercent ?? 0, node.metrics?.memoryUsedPercent ?? 0, node.metrics?.diskUsedPercent ?? 0);
}
