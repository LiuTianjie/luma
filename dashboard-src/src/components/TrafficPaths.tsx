import dagre from "dagre";
import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import type { Lang, TrafficPath } from "../types";
import { Badge, PrimaryCell } from "./ui";

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
};

const NODE_WIDTH = 190;
const NODE_HEIGHT = 68;

function classifySegment(segment: string) {
  const value = segment.toLowerCase();
  if (value.includes("cloudflare")) return { kind: "edge", meta: "Cloudflare" };
  if (value.includes("traefik")) return { kind: "proxy", meta: "Ingress proxy" };
  if (value.includes("tailscale") || value.includes("cloudflared")) return { kind: "tunnel", meta: "Private bridge" };
  if (value.includes("client/internal")) return { kind: "internal", meta: "Internal client" };
  if (value.includes("missing") || value.includes("unresolved") || value.includes("no running")) return { kind: "issue", meta: "Needs attention" };
  if (/^https?:\/\//.test(value) || /^\d{1,3}(\.\d{1,3}){3}/.test(value)) return { kind: "target", meta: "Network target" };
  return { kind: "service", meta: "Swarm service" };
}

function buildTopology(paths: TrafficPath[]): {
  elements: cytoscape.ElementDefinition[];
  nodes: TopologyNode[];
  edges: TopologyEdge[];
} {
  const nodeData = new Map<string, Omit<TopologyNode, "x" | "y" | "routes">>();
  const routeCounts = new Map<string, Set<string>>();
  const segmentIds = new Map<string, string>();
  const edges: TopologyEdge[] = [];

  const getSegmentId = (segment: string) => {
    const key = segment.trim() || "unknown";
    const existing = segmentIds.get(key);
    if (existing) return existing;
    const nextId = `n-${segmentIds.size + 1}`;
    segmentIds.set(key, nextId);
    return nextId;
  };

  paths.forEach((path, pathIndex) => {
    const routeLabel = path.domain || path.id || `route-${pathIndex + 1}`;
    const segments = path.segments || [];

    segments.forEach((segment, segmentIndex) => {
      const id = getSegmentId(segment);
      const classification = classifySegment(segment);
      const routeSet = routeCounts.get(id) || new Set<string>();
      routeSet.add(routeLabel);
      routeCounts.set(id, routeSet);

      if (!nodeData.has(id)) {
        nodeData.set(id, {
          id,
          label: segment,
          meta: classification.meta,
          kind: classification.kind,
        });
      }

      const nextSegment = segments[segmentIndex + 1];
      if (!nextSegment) return;
      edges.push({
        id: `e-${pathIndex}-${segmentIndex}`,
        source: id,
        target: getSegmentId(nextSegment),
        label: segmentIndex === segments.length - 2 ? path.id || path.kind || "" : "",
        kind: path.kind || "unknown",
      });
    });
  });

  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 48, ranksep: 120, marginx: 40, marginy: 40 });

  nodeData.forEach((node) => graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);

  const nodes = Array.from(nodeData.values()).map((node) => {
    const positioned = graph.node(node.id);
    return {
      ...node,
      routes: routeCounts.get(node.id)?.size || 1,
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
      },
      classes: edge.kind,
    })),
  ];

  return { elements, nodes, edges };
}

const stylesheet: cytoscape.StylesheetJsonBlock[] = [
  {
    selector: "node",
    style: {
      "background-color": "#ffffff",
      "border-color": "#eaeaea",
      "border-width": 1,
      "border-style": "solid",
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
  { selector: "node.edge", style: { "border-color": "#eaeaea" } },
  { selector: "node.proxy", style: { "border-width": 2, "border-color": "#171717" } },
  { selector: "node.tunnel", style: { "border-color": "#eaeaea" } },
  { selector: "node.target", style: { "border-color": "#eaeaea", "color": "#666666" } },
  { selector: "node.issue", style: { "border-width": 2, "border-color": "#ef4444" } },
  {
    selector: "edge",
    style: {
      "curve-style": "taxi",
      "taxi-direction": "horizontal",
      "taxi-turn": 12,
      "taxi-turn-min-distance": 5,
      "font-family": '"Inter", -apple-system, sans-serif',
      "font-size": 10,
      "font-weight": 500,
      "label": "data(label)",
      "line-color": "#d4d4d8",
      "target-arrow-color": "#d4d4d8",
      "target-arrow-shape": "triangle",
      "text-background-color": "#fafafa",
      "text-background-opacity": 1,
      "text-background-padding": "4px",
      "text-background-shape": "roundrectangle",
      "width": "1.5px",
    },
  },
  { selector: "edge.cn-edge, edge.external-edge", style: { "line-color": "#171717", "target-arrow-color": "#171717" } },
  { selector: "edge.tailscale-relay, edge.cloudflare-tunnel", style: { "line-color": "#171717", "target-arrow-color": "#171717" } },
];

export function TrafficPaths({ lang, paths }: { lang: Lang; paths: TrafficPath[] }) {
  const { elements, nodes, edges } = buildTopology(paths);

  return (
    <section className="panel path-panel" id="section-3">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "pathsEyebrow")}</p>
          <h2>{t(lang, "trafficPaths")}</h2>
        </div>
        <span>{nodes.length} nodes / {edges.length} links</span>
      </div>
      {paths.length ? (
        <div className="topology-layout">
          <div className="topology-canvas">
            <CytoscapeComponent
              className="cy-topology"
              elements={elements}
              layout={{ name: "preset", fit: true, padding: 48 }}
              maxZoom={1.6}
              minZoom={0.35}
              stylesheet={stylesheet}
              wheelSensitivity={0.16}
            />
          </div>
          <aside className="route-index" aria-label={t(lang, "trafficPaths")}>
            {paths.map((path, index) => (
              <article key={`${path.id || "path"}-${index}`}>
                <PrimaryCell meta={path.domain || t(lang, "noPublicDomain")} title={path.id || "-"} />
                <Badge value={path.kind || "unknown"} />
              </article>
            ))}
          </aside>
        </div>
      ) : (
        <div className="topology-empty">{t(lang, "missing")}</div>
      )}
    </section>
  );
}
