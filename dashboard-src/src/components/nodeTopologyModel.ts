import dagre from "dagre";
import type cytoscape from "cytoscape";
import type { DashboardNode, DashboardService, Lang } from "../types";

type GraphNode = {
  id: string;
  label: string;
  kind: string;
  x: number;
  y: number;
};

type GraphEdge = {
  id: string;
  source: string;
  target: string;
};

export const NODE_WIDTH = 190;
export const NODE_HEIGHT = 44;

function serviceTitle(service: DashboardService) {
  return service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-";
}

// Only the graph's labels, styles, nodes and edges enter this snapshot. Live
// metrics and task state may refresh every poll without requiring Dagre to rerun.
export function nodeTopologySnapshot(nodes: DashboardNode[], services: DashboardService[], lang: Lang): string {
  const zh = lang === "zh";
  const graphNodes = new Map<string, Omit<GraphNode, "x" | "y">>();
  const edges: GraphEdge[] = [];
  const edgeKeys = new Set<string>();

  const addEdge = (source: string, target: string) => {
    const key = `${source}->${target}`;
    if (edgeKeys.has(key)) return;
    edgeKeys.add(key);
    edges.push({ id: `e-${edgeKeys.size}`, source, target });
  };

  // Root cluster anchor so every branch shares one connected tree.
  const rootId = "cluster:root";
  graphNodes.set(rootId, { id: rootId, label: zh ? "集群\nCluster" : "Cluster", kind: "cluster" });

  const regionId = (region: string) => `region:${region || "unknown"}`;
  const ensureRegion = (region: string) => {
    const id = regionId(region);
    if (!graphNodes.has(id)) {
      graphNodes.set(id, { id, label: `Region\n${region || (zh ? "未知" : "unknown")}`, kind: "region" });
      addEdge(rootId, id);
    }
    return id;
  };

  // Hosts hang under their region; region hangs under the cluster root.
  const hostRegion = new Map<string, string>();
  nodes.forEach((node) => {
    const name = node.name || node.displayName;
    if (!name) return;
    const region = node.region || "";
    hostRegion.set(name, region);
    const isLeader = node.leader;
    graphNodes.set(`node:${name}`, {
      id: `node:${name}`,
      label: isLeader ? `Leader\n${name}` : `Worker\n${name}`,
      kind: isLeader ? "leader" : "host",
    });
    addEdge(ensureRegion(region), `node:${name}`);
  });

  services.forEach((service, serviceIndex) => {
    const title = serviceTitle(service);
    const serviceId = `service:${service.fullName || title || serviceIndex}`;
    const isExposed = service.exposure && service.exposure !== "none";
    graphNodes.set(serviceId, {
      id: serviceId,
      label: isExposed ? `Public\n${title}` : `Nomad\n${title}`,
      kind: isExposed ? "exposedService" : "service",
    });

    const placedNodes = (service.nodes || []).filter(Boolean);
    if (placedNodes.length) {
      // A placed service connects to each host it runs on.
      placedNodes.forEach((nodeName) => {
        const hostId = `node:${nodeName}`;
        if (!graphNodes.has(hostId)) {
          graphNodes.set(hostId, { id: hostId, label: `Worker\n${nodeName}`, kind: "host" });
          addEdge(ensureRegion(hostRegion.get(nodeName) || service.region || ""), hostId);
        }
        addEdge(hostId, serviceId);
      });
    } else {
      // Unplaced/pending services still connect — anchored to their region so they
      // never float as orphans.
      addEdge(ensureRegion(service.region || ""), serviceId);
    }
  });

  return JSON.stringify({ nodes: Array.from(graphNodes.values()), edges });
}

export function layoutNodeTopology(snapshot: string) {
  const graph = JSON.parse(snapshot) as { nodes: Omit<GraphNode, "x" | "y">[]; edges: GraphEdge[] };
  const { nodes, edges } = graph;
  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({ rankdir: "LR", nodesep: 42, ranksep: 120, marginx: 40, marginy: 40 });

  nodes.forEach((node) => layout.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => layout.setEdge(edge.source, edge.target));
  dagre.layout(layout);

  const positionedNodes = nodes.map((node) => {
    const positioned = layout.node(node.id);
    return {
      ...node,
      x: positioned.x,
      y: positioned.y,
    };
  });

  const elements: cytoscape.ElementDefinition[] = [
    ...positionedNodes.map((node) => ({
      data: {
        id: node.id,
        label: node.label,
        kind: node.kind,
      },
      classes: node.kind,
      position: { x: node.x, y: node.y },
    })),
    ...edges.map((edge) => ({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
      },
      classes: "runsOn",
    })),
  ];

  return { elements, nodes: positionedNodes, edges };
}
