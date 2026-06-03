import dagre from "dagre";
import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
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

const NODE_WIDTH = 190;
const NODE_HEIGHT = 68;

function serviceTitle(service: DashboardService) {
  return service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-";
}

function buildNodeTopology(nodes: DashboardNode[], services: DashboardService[]) {
  const graphNodes = new Map<string, Omit<GraphNode, "x" | "y">>();
  const edges: GraphEdge[] = [];

  nodes.forEach((node) => {
    const name = node.name || node.displayName;
    if (!name) return;
    graphNodes.set(`node:${name}`, {
      id: `node:${name}`,
      label: name,
      kind: node.leader ? "leader" : "host",
    });
  });

  services.forEach((service, serviceIndex) => {
    const title = serviceTitle(service);
    const serviceId = `service:${service.fullName || title || serviceIndex}`;
    graphNodes.set(serviceId, {
      id: serviceId,
      label: title,
      kind: service.exposure && service.exposure !== "none" ? "exposedService" : "service",
    });

    (service.nodes || []).forEach((nodeName, nodeIndex) => {
      const hostId = `node:${nodeName}`;
      if (!graphNodes.has(hostId)) {
        graphNodes.set(hostId, {
          id: hostId,
          label: nodeName,
          kind: "host",
        });
      }
      edges.push({
        id: `runs-${serviceIndex}-${nodeIndex}`,
        source: hostId,
        target: serviceId,
      });
    });
  });

  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({ rankdir: "LR", nodesep: 42, ranksep: 120, marginx: 40, marginy: 40 });

  graphNodes.forEach((node) => layout.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => layout.setEdge(edge.source, edge.target));
  dagre.layout(layout);

  const positionedNodes = Array.from(graphNodes.values()).map((node) => {
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

const stylesheet: cytoscape.StylesheetJsonBlock[] = [
  {
    selector: "node",
    style: {
      "background-color": "#ffffff",
      "border-color": "#eaeaea",
      "border-width": 1,
      "font-family": '"Inter", -apple-system, sans-serif',
      "font-size": 13,
      "font-weight": 500,
      "height": `${NODE_HEIGHT}px`,
      "label": "data(label)",
      "padding": "16px",
      "shape": "round-rectangle",
      "text-halign": "center",
      "text-max-width": "160px",
      "text-valign": "center",
      "text-wrap": "ellipsis",
      "width": `${NODE_WIDTH}px`,
      "color": "#171717",
    },
  },
  { selector: "node.leader", style: { "border-width": 2, "border-color": "#171717" } },
  { selector: "node.host", style: { "background-color": "#fafafa", "border-color": "#d4d4d8" } },
  { selector: "node.service", style: { "border-color": "#eaeaea" } },
  { selector: "node.exposedService", style: { "border-width": 2, "border-color": "#171717" } },
  {
    selector: "edge",
    style: {
      "curve-style": "taxi",
      "taxi-direction": "horizontal",
      "taxi-turn": 12,
      "line-color": "#d4d4d8",
      "target-arrow-color": "#d4d4d8",
      "target-arrow-shape": "triangle",
      "width": "1.3px",
    },
  },
];

export function NodeTopology({ lang, nodes, services }: { lang: Lang; nodes: DashboardNode[]; services: DashboardService[] }) {
  const topology = buildNodeTopology(nodes, services);

  return (
    <section className="panel topology-panel" id="section-3">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "nodesEyebrow")}</p>
          <h2>{t(lang, "nodeTopology")}</h2>
        </div>
        <span>{topology.nodes.length} nodes / {topology.edges.length} links</span>
      </div>
      <div className="topology-canvas standalone-topology">
        <CytoscapeComponent
          className="cy-topology"
          elements={topology.elements}
          layout={{ name: "preset", fit: true, padding: 48 }}
          maxZoom={1.6}
          minZoom={0.35}
          stylesheet={stylesheet}
          wheelSensitivity={0.16}
        />
      </div>
    </section>
  );
}
