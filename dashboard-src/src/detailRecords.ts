import type { DashboardNode, DashboardService } from "./types";

export type DetailState =
  | { kind: "node"; title: string; items: Record<string, string | number | boolean | undefined> }
  | { kind: "service"; title: string; items: Record<string, string | number | boolean | undefined> }
  | null;

// Build the flat key/value record shown in the detail drawer for a node.
export function nodeDetail(node: DashboardNode): DetailState {
  return {
    kind: "node",
    title: node.name || "-",
    items: {
      displayName: node.displayName,
      region: node.region,
      role: node.role,
      state: node.state,
      availability: node.availability,
      leader: node.leader,
      agent: [node.agentStatus, node.agentOs, node.terminalStatus ? `terminal: ${node.terminalStatus}` : ""].filter(Boolean).join(" / "),
      cpu: node.metrics?.cpuPercent ?? node.metrics?.loadPercent,
      load1: node.metrics?.load1,
      memory: node.metrics?.memoryUsedPercent,
      memoryTotal: node.metrics?.memoryTotalBytes,
      cpuCapacity: node.capacity?.cpus,
      memoryCapacity: node.capacity?.memoryBytes,
    },
  };
}

// Build the flat key/value record shown in the detail drawer for a service.
export function serviceDetail(service: DashboardService): DetailState {
  return {
    kind: "service",
    title: service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-",
    items: {
      fullName: service.fullName,
      region: service.region,
      exposure: service.exposure,
      image: service.image,
      replicas: `${service.running ?? 0}/${service.desired ?? 0}`,
      pending: service.pending,
      failed: service.failed,
      health: service.health,
      nodes: (service.nodes || []).join(", "),
      limits: [
        service.resources?.limits?.cpus ? `${service.resources.limits.cpus} CPU` : "",
        service.resources?.limits?.memoryBytes ? `${service.resources.limits.memoryBytes} bytes` : "",
      ].filter(Boolean).join(" / "),
      reservations: [
        service.resources?.reservations?.cpus ? `${service.resources.reservations.cpus} CPU` : "",
        service.resources?.reservations?.memoryBytes ? `${service.resources.reservations.memoryBytes} bytes` : "",
      ].filter(Boolean).join(" / "),
      tasks: (service.tasks || []).map((task) => `${task.node || "-"}:${task.state || "-"}`).join(", "),
      storage: (service.storage || []).map((item) => `${item.name || "-"}:${item.kind || "unmanaged"}`).join(", "),
      diagnostics: (service.diagnostics || []).join("; "),
    },
  };
}
