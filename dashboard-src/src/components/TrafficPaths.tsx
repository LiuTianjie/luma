import { useEffect, useMemo, useState } from "react";
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

function normalizePathSegments(path: TrafficPath) {
  const segments = (path.segments || []).filter(Boolean);
  const kind = path.kind || "";
  const domain = (path.domain || "").trim();
  const publicPrefix = domain ? [domain] : [];
  if (kind === "cn-edge" || kind === "external-edge") return [...publicPrefix, ...segments.slice(0, 4)];
  if (kind === "cloudflare-tunnel") return [...publicPrefix, ...segments.slice(0, 3)];
  if (kind === "tailscale-relay" || kind === "tcp-relay") return [...publicPrefix, ...segments];
  return segments.slice(0, 2);
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
  return { kind: "service", meta: "Swarm service" };
}

function buildTopology(paths: TrafficPath[]): {
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

  const addSegmentNode = (segment: string, routeLabel: string) => {
    const id = getSegmentId(segment);
    const classification = classifySegment(segment);
    addRouteCount(id, routeLabel);

    let emojiLabel = segment;
    if (classification.kind === "domain") emojiLabel = `🌐 Domain\n${segment}`;
    else if (classification.kind === "edge") emojiLabel = `☁️ Edge\n${segment}`;
    else if (classification.kind === "proxy") emojiLabel = `🚦 Proxy\n${segment}`;
    else if (classification.kind === "tunnel") emojiLabel = `🔒 Tunnel\n${segment}`;
    else if (classification.kind === "internal") emojiLabel = `💻 Client\n${segment}`;
    else if (classification.kind === "issue") emojiLabel = `⚠️ Issue\n${segment}`;
    else if (classification.kind === "target") emojiLabel = `🎯 Target\n${segment}`;
    else emojiLabel = `📦 Swarm\n${segment}`;

    if (!nodeData.has(id)) {
      nodeData.set(id, {
        id,
        label: emojiLabel,
        meta: classification.meta,
        kind: classification.kind,
      });
    }
    return id;
  };

  paths.forEach((path, pathIndex) => {
    const routeLabel = path.domain || path.id || `route-${pathIndex + 1}`;
    const segments = normalizePathSegments(path);

    segments.forEach((segment, segmentIndex) => {
      const id = addSegmentNode(segment, routeLabel);
      const nextSegment = segments[segmentIndex + 1];
      if (!nextSegment) return;
      edges.push({
        id: `e-${pathIndex}-${segmentIndex}`,
        source: id,
        target: getSegmentId(nextSegment),
        label: "",
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

function getStylesheet(theme: "light" | "dark"): cytoscape.StylesheetJsonBlock[] {
  const isDark = theme === "dark";
  const textColor = isDark ? "#f8fafc" : "#0f172a";
  const nodeBg = isDark ? "#0f172a" : "#ffffff";
  const nodeBorder = isDark ? "rgba(99, 102, 241, 0.35)" : "rgba(99, 102, 241, 0.15)";
  const edgeColor = isDark ? "rgba(148, 163, 184, 0.3)" : "rgba(148, 163, 184, 0.6)";

  const domainBorder = "#10b981"; // Emerald
  const proxyBorder = "#8b5cf6"; // Violet
  const issueBorder = "#f43f5e"; // Rose
  const targetBorder = isDark ? "#475569" : "#cbd5e1";

  return [
    {
      selector: "node",
      style: {
        "background-color": nodeBg,
        "border-color": nodeBorder,
        "border-width": 1.5,
        "font-family": '"Outfit", "Inter", sans-serif',
        "font-size": 12,
        "font-weight": 500,
        "height": `${NODE_HEIGHT}px`,
        "label": "data(label)",
        "padding": "16px",
        "shape": "round-rectangle",
        "text-halign": "center",
        "text-max-width": "160px",
        "text-valign": "center",
        "text-wrap": "wrap",
        "width": `${NODE_WIDTH}px`,
        "color": textColor,
      },
    },
    { selector: "node.edge", style: { "border-color": nodeBorder } },
    { selector: "node.domain", style: { "border-width": 2.5, "border-color": domainBorder, "background-color": isDark ? "#064e3b" : "#ecfdf5" } },
    { selector: "node.proxy", style: { "border-width": 2.5, "border-color": proxyBorder, "background-color": isDark ? "#1e1b4b" : "#eef2ff" } },
    { selector: "node.tunnel", style: { "border-color": nodeBorder } },
    { selector: "node.target", style: { "border-color": targetBorder, "color": isDark ? "#94a3b8" : "#475569" } },
    { selector: "node.issue", style: { "border-width": 2.5, "border-color": issueBorder, "background-color": isDark ? "#4c0519" : "#fff1f2" } },
    {
      selector: "edge",
      style: {
        "curve-style": "taxi",
        "taxi-direction": "horizontal",
        "taxi-turn": 20,
        "line-color": edgeColor,
        "target-arrow-color": edgeColor,
        "target-arrow-shape": "triangle",
        "width": "1.8px",
      },
    },
    { selector: "edge.cn-edge, edge.external-edge", style: { "line-color": isDark ? "#8b5cf6" : "#6366f1", "target-arrow-color": isDark ? "#8b5cf6" : "#6366f1" } },
    { selector: "edge.tailscale-relay, edge.tcp-relay, edge.cloudflare-tunnel", style: { "line-color": "#10b981", "target-arrow-color": "#10b981" } },
    {
      selector: ".dimmed",
      style: {
        "opacity": 0.15,
      },
    },
    {
      selector: "node.highlighted",
      style: {
        "border-color": "#8b5cf6",
        "border-width": 3,
        "background-color": isDark ? "#2e1065" : "#f5f3ff",
      },
    },
    {
      selector: "edge.highlighted",
      style: {
        "line-color": "#6366f1",
        "target-arrow-color": "#6366f1",
        "width": "3px",
      },
    },
  ];
}

export function TrafficPaths({
  lang,
  paths,
  theme,
}: {
  lang: Lang;
  paths: TrafficPath[];
  theme: "light" | "dark";
}) {
  const [cyRef, setCyRef] = useState<cytoscape.Core | null>(null);

  const { elements, nodes, edges } = useMemo(() => buildTopology(paths), [paths]);
  const stylesheet = useMemo(() => getStylesheet(theme), [theme]);

  useEffect(() => {
    if (cyRef) {
      cyRef.style(stylesheet);
    }
  }, [cyRef, stylesheet]);

  useEffect(() => {
    if (!cyRef) return;

    const handleMouseOver = (event: any) => {
      const target = event.target;
      if (target.isNode()) {
        cyRef.elements().addClass("dimmed");
        target.removeClass("dimmed").addClass("highlighted");
        
        const connectedEdges = target.connectedEdges();
        connectedEdges.removeClass("dimmed").addClass("highlighted");
        
        const connectedNodes = target.neighborhood().nodes();
        connectedNodes.removeClass("dimmed").addClass("highlighted");
      }
    };

    const handleMouseOut = () => {
      cyRef.elements().removeClass("dimmed").removeClass("highlighted");
    };

    cyRef.on("mouseover", "node", handleMouseOver);
    cyRef.on("mouseout", "node", handleMouseOut);

    return () => {
      cyRef.off("mouseover", "node", handleMouseOver);
      cyRef.off("mouseout", "node", handleMouseOut);
    };
  }, [cyRef]);

  const handleZoomIn = () => {
    if (cyRef) {
      cyRef.zoom(cyRef.zoom() * 1.25);
    }
  };

  const handleZoomOut = () => {
    if (cyRef) {
      cyRef.zoom(cyRef.zoom() / 1.25);
    }
  };

  const handleReset = () => {
    if (cyRef) {
      cyRef.reset();
      cyRef.fit(undefined, 48);
    }
  };

  return (
    <section className="panel path-panel" id="section-5">
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
              cy={(cy) => setCyRef(cy)}
            />
            
            <div className="cy-controls" aria-label="Topology controls">
              <button className="cy-control-btn" onClick={handleZoomIn} type="button" title="Zoom In">+</button>
              <button className="cy-control-btn" onClick={handleZoomOut} type="button" title="Zoom Out">-</button>
              <button className="cy-control-btn" onClick={handleReset} type="button" title="Reset View">⟲</button>
            </div>
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
