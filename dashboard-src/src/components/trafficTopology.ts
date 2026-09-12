import dagre from "dagre";
import type cytoscape from "cytoscape";
import type { TrafficPath, TrafficDestination } from "../types";

type TopologyNode = {
  id: string;
  label: string;
  meta: string;
  kind: string;
  routes: number;
  x: number;
  y: number;
};

type TopologyEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: string;
  routeKeys: string[];
};

const NODE_WIDTH = 190;
const NODE_HEIGHT = 44;
const DESTINATION_WIDTH = 220;

export function normalizePathSegments(path: TrafficPath) {
  const segments = (path.segments || []).filter(Boolean);
  const domain = (path.domain || "").trim();
  const fullPath = domain && segments[0] !== domain ? [domain, ...segments] : segments;
  // Only replace the trailing destination suffix. An address appearing earlier
  // may also be an ingress hop; substring matches must never erase a service.
  const endpoints = new Set((path.destinations || []).flatMap(destination =>
    [destination.address, destination.node, destination.nodeAddress].filter(Boolean)));
  let end = fullPath.length;
  while (end > 0 && endpoints.has(fullPath[end - 1])) end--;
  return fullPath.slice(0, end);
}

function classifySegment(segment: string) {
  const value = segment.toLowerCase();
  if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(segment) && !value.includes("cloudflare")) return { kind: "domain", meta: "Public domain" };
  if (value.includes("cloudflare")) return { kind: "edge", meta: "Cloudflare" };
  if (value.includes("traefik")) return { kind: "proxy", meta: "Ingress proxy" };
  if (value.includes("tailscale") || value.includes("cloudflared")) return { kind: "tunnel", meta: "Private bridge" };
  if (value.includes("client/internal")) return { kind: "internal", meta: "Internal client" };
  if (value.includes("missing") || value.includes("unresolved") || value.includes("no running")) return { kind: "issue", meta: "Needs attention" };
  if (/^https?:\/\//.test(value) || /^\d{1,3}(\.\d{1,3}){3}/.test(value)) return { kind: "target", meta: "Network target" };
  return { kind: "service", meta: "Nomad job" };
}

function destinationLabel(destination: TrafficDestination) {
  const region = destination.region || "unknown";
  const node = destination.node || "unresolved";
  const address = destination.address || destination.nodeAddress || "";
  return ["Destination", `${region} / ${node}`, address].filter(Boolean).join("\n");
}

function destinationMeta(destination: TrafficDestination) {
  const state = destination.state || "unknown";
  const service = destination.service || "";
  return [state, service].filter(Boolean).join(" · ");
}

export function buildTopology(paths: TrafficPath[]): {
  elements: cytoscape.ElementDefinition[];
  nodes: TopologyNode[];
  edges: TopologyEdge[];
} {
  const nodeData = new Map<string, Omit<TopologyNode, "x" | "y" | "routes" | "routesLabel">>();
  const routeCounts = new Map<string, Set<string>>();
  const segmentIds = new Map<string, string>();
  const edges: TopologyEdge[] = [];

  const addRouteCount = (id: string, routeLabel: string) => {
    const routeSet = routeCounts.get(id) || new Set<string>();
    routeSet.add(routeLabel);
    routeCounts.set(id, routeSet);
  };

  const getSegmentId = (segment: string) => {
    const key = segment.trim() || "unknown";
    const existing = segmentIds.get(key);
    if (existing) return existing;
    const nextId = `n-${segmentIds.size + 1}`;
    segmentIds.set(key, nextId);
    return nextId;
  };

  const getDestinationId = (pathIndex: number, destinationIndex: number, destination: TrafficDestination) => {
    const routeKey = destination.service || destination.address || destination.node || "destination";
    const nodeKey = destination.node || destination.nodeAddress || destination.address || destinationIndex;
    return `d-${pathIndex}-${destinationIndex}-${routeKey}-${nodeKey}`;
  };

  const addSegmentNode = (segment: string, routeLabel: string) => {
    const id = getSegmentId(segment);
    const classification = classifySegment(segment);
    addRouteCount(id, routeLabel);

    let nodeLabel = segment;
    if (classification.kind === "domain") nodeLabel = `Domain\n${segment}`;
    else if (classification.kind === "edge") nodeLabel = `Edge\n${segment}`;
    else if (classification.kind === "proxy") nodeLabel = `Proxy\n${segment}`;
    else if (classification.kind === "tunnel") nodeLabel = `Tunnel\n${segment}`;
    else if (classification.kind === "internal") nodeLabel = `Client\n${segment}`;
    else if (classification.kind === "issue") nodeLabel = `Issue\n${segment}`;
    else if (classification.kind === "target") nodeLabel = `Target\n${segment}`;
    else nodeLabel = `Nomad\n${segment}`;

    if (!nodeData.has(id)) {
      nodeData.set(id, {
        id,
        label: nodeLabel,
        meta: classification.meta,
        kind: classification.kind,
      });
    }
    return id;
  };

  paths.forEach((path, pathIndex) => {
    const routeLabel = String(pathIndex);
    const segments = normalizePathSegments(path);
    let lastSegmentId = "";

    segments.forEach((segment, segmentIndex) => {
      const id = addSegmentNode(segment, routeLabel);
      lastSegmentId = id;
      const nextSegment = segments[segmentIndex + 1];
      if (!nextSegment) return;
      edges.push({
        id: `e-${pathIndex}-${segmentIndex}`,
        source: id,
        target: getSegmentId(nextSegment),
        label: "",
        kind: path.kind || "unknown",
        routeKeys: [routeLabel],
      });
    });

    (path.destinations || []).forEach((destination, destinationIndex) => {
      const id = getDestinationId(pathIndex, destinationIndex, destination);
      addRouteCount(id, routeLabel);
      if (!nodeData.has(id)) {
        nodeData.set(id, {
          id,
          label: destinationLabel(destination),
          meta: destinationMeta(destination),
          kind: destination.state === "unresolved" ? "issue" : "destination",
        });
      }
      if (lastSegmentId) {
        edges.push({
          id: `e-${pathIndex}-destination-${destinationIndex}`,
          source: lastSegmentId,
          target: id,
          label: "",
          kind: path.kind || "unknown",
          routeKeys: [routeLabel],
        });
      }
    });
  });

  const uniqueEdges = new Map<string, TopologyEdge>();
  for (const edge of edges) {
    const key = JSON.stringify([edge.source, edge.target]);
    const existing = uniqueEdges.get(key);
    if (existing) existing.routeKeys.push(...edge.routeKeys);
    else uniqueEdges.set(key, { ...edge, routeKeys: [...edge.routeKeys] });
  }
  edges.splice(0, edges.length, ...uniqueEdges.values());

  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 48, ranksep: 120, marginx: 40, marginy: 40 });

  nodeData.forEach((node) => graph.setNode(node.id, { width: node.kind === "destination" ? DESTINATION_WIDTH : NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);

  const nodes = Array.from(nodeData.values()).map((node) => {
    const positioned = graph.node(node.id);
    const count = routeCounts.get(node.id)?.size || 1;
    return {
      ...node,
      routes: count,
      x: positioned.x,
      y: positioned.y,
    };
  });

  const elements: cytoscape.ElementDefinition[] = [
    ...nodes.map((node) => ({
      data: {
        id: node.id,
        label: node.label,
        meta: node.meta,
        kind: node.kind,
        routeKeys: [...(routeCounts.get(node.id) || [])],
        routes: `${node.routes} route${node.routes === 1 ? "" : "s"}`,
      },
      classes: node.kind,
      position: { x: node.x, y: node.y },
    })),
    ...edges.map((edge) => ({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.label,
        kind: edge.kind,
        routeKeys: edge.routeKeys,
      },
      classes: edge.kind,
    })),
  ];

  return { elements, nodes, edges };
}

/** Keep path identity when a graph has shared ingress/proxy nodes. Walking
 * ancestors + descendants alone can combine two unrelated routes at a merge. */
export function routeElementIds(elements: cytoscape.ElementDefinition[], targetId: string): string[] {
  const target = elements.find(element => element.data.id === targetId);
  const routes = new Set<string>(target?.data.routeKeys || []);
  return elements.filter(element => (element.data.routeKeys || []).some((key: string) => routes.has(key)))
    .map(element => element.data.id as string);
}
