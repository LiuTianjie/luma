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

function buildNodeTopology(nodes: DashboardNode[], services: DashboardService[], lang: Lang) {
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
  const textColor = isDark ? "#fdfcfc" : "#201d1d";
  const nodeBg = isDark ? "#302c2c" : "#ffffff";
  const nodeBorder = isDark ? "rgba(253, 252, 252, 0.16)" : "rgba(15, 0, 0, 0.12)";
  const edgeColor = isDark ? "rgba(154, 152, 152, 0.4)" : "rgba(110, 110, 115, 0.5)";
  
  const leaderBorder = "#007aff";
  const hostBg = isDark ? "#201d1d" : "#f8f7f7";
  const hostBorder = isDark ? "#646262" : "#d3d0d0";
  
  const serviceBorder = isDark ? "rgba(48, 209, 88, 0.35)" : "rgba(48, 209, 88, 0.3)";
  const exposedBorder = "#30d158";
  
  return [
    {
      selector: "node",
      style: {
        "background-color": nodeBg,
        "border-color": nodeBorder,
        "border-width": 1.5,
        "font-family": '"Berkeley Mono", "IBM Plex Mono", ui-monospace, Menlo, monospace',
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
      selector: "node.cluster",
      style: {
        "border-width": 2,
        "border-color": leaderBorder,
        "background-color": isDark ? "#26221f" : "#f5f1ea",
        "font-weight": 700,
      },
    },
    {
      selector: "node.region",
      style: {
        "border-width": 1.5,
        "border-color": isDark ? "rgba(253,252,252,0.28)" : "rgba(15,0,0,0.2)",
        "background-color": isDark ? "#242020" : "#f2efef",
        "shape": "round-rectangle",
      },
    },
    {
      selector: "node.leader",
      style: {
        "border-width": 2.5,
        "border-color": leaderBorder,
        "background-color": isDark ? "#1c2733" : "#eaf3ff",
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
        "background-color": isDark ? "#1e2a20" : "#ecfdf0",
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
        "border-color": "#007aff",
        "border-width": 3,
        "background-color": isDark ? "#22303f" : "#e0eefe",
      },
    },
    {
      selector: "edge.highlighted",
      style: {
        "line-color": "#007aff",
        "target-arrow-color": "#007aff",
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

  const topology = useMemo(() => buildNodeTopology(nodes, services, lang), [nodes, services, lang]);
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
