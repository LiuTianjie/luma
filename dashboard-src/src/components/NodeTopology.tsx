import { useEffect, useMemo, useState } from "react";
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
    const isLeader = node.leader;
    const labelText = isLeader ? `Leader\n${name}` : `Worker\n${name}`;
    graphNodes.set(`node:${name}`, {
      id: `node:${name}`,
      label: labelText,
      kind: isLeader ? "leader" : "host",
    });
  });

  services.forEach((service, serviceIndex) => {
    const title = serviceTitle(service);
    const serviceId = `service:${service.fullName || title || serviceIndex}`;
    const isExposed = service.exposure && service.exposure !== "none";
    const labelText = isExposed ? `Public\n${title}` : `Nomad\n${title}`;
    
    graphNodes.set(serviceId, {
      id: serviceId,
      label: labelText,
      kind: isExposed ? "exposedService" : "service",
    });

    (service.nodes || []).forEach((nodeName, nodeIndex) => {
      const hostId = `node:${nodeName}`;
      if (!graphNodes.has(hostId)) {
        graphNodes.set(hostId, {
          id: hostId,
          label: `Worker\n${nodeName}`,
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

function getStylesheet(theme: "light" | "dark"): cytoscape.StylesheetJsonBlock[] {
  const isDark = theme === "dark";
  const textColor = isDark ? "#f8fafc" : "#0f172a";
  const nodeBg = isDark ? "#0f172a" : "#ffffff";
  const nodeBorder = isDark ? "rgba(125, 211, 252, 0.28)" : "rgba(14, 116, 144, 0.18)";
  const edgeColor = isDark ? "rgba(148, 163, 184, 0.32)" : "rgba(100, 116, 139, 0.5)";
  
  const leaderBorder = "#38bdf8";
  const hostBg = isDark ? "#111827" : "#f8fafc";
  const hostBorder = isDark ? "#334155" : "#cbd5e1";
  
  const serviceBorder = isDark ? "rgba(20, 184, 166, 0.38)" : "rgba(15, 118, 110, 0.2)";
  const exposedBorder = "#22c55e";
  
  return [
    {
      selector: "node",
      style: {
        "background-color": nodeBg,
        "border-color": nodeBorder,
        "border-width": 1.5,
        "font-family": '"IBM Plex Sans", "Aptos", "Segoe UI", sans-serif',
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
    {
      selector: "node.leader",
      style: {
        "border-width": 2.5,
        "border-color": leaderBorder,
        "background-color": isDark ? "#082f49" : "#ecfeff",
      },
    },
    {
      selector: "node.host",
      style: {
        "background-color": hostBg,
        "border-color": hostBorder,
        "border-width": 1.5,
      },
    },
    {
      selector: "node.service",
      style: {
        "border-color": serviceBorder,
        "border-width": 1.5,
      },
    },
    {
      selector: "node.exposedService",
      style: {
        "border-width": 2.5,
        "border-color": exposedBorder,
        "background-color": isDark ? "#052e2b" : "#ecfdf5",
      },
    },
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
    {
      selector: ".dimmed",
      style: {
        "opacity": 0.15,
      },
    },
    {
      selector: "node.highlighted",
      style: {
        "border-color": "#38bdf8",
        "border-width": 3,
        "background-color": isDark ? "#0c4a6e" : "#e0f2fe",
      },
    },
    {
      selector: "edge.highlighted",
      style: {
        "line-color": "#38bdf8",
        "target-arrow-color": "#38bdf8",
        "width": "3px",
      },
    },
  ];
}

export function NodeTopology({
  lang,
  nodes,
  services,
  theme,
}: {
  lang: Lang;
  nodes: DashboardNode[];
  services: DashboardService[];
  theme: "light" | "dark";
}) {
  const [cyRef, setCyRef] = useState<cytoscape.Core | null>(null);

  const topology = useMemo(() => buildNodeTopology(nodes, services), [nodes, services]);
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
        
        // Highlight neighbors
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
    <section className="panel topology-panel" id="section-4">
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
          cy={(cy) => setCyRef(cy)}
        />
        
        <div className="cy-controls" aria-label="Topology controls">
          <button aria-label="Zoom in" className="cy-control-btn" onClick={handleZoomIn} type="button" title="Zoom In">+</button>
          <button aria-label="Zoom out" className="cy-control-btn" onClick={handleZoomOut} type="button" title="Zoom Out">-</button>
          <button aria-label="Reset view" className="cy-control-btn" onClick={handleReset} type="button" title="Reset View">0</button>
        </div>
      </div>
    </section>
  );
}
