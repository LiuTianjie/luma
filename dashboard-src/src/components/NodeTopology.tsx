import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { TopologyFullscreenButton } from "./TopologyFullscreenButton";

import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { layoutNodeTopology, nodeTopologySnapshot, NODE_HEIGHT, NODE_WIDTH } from "./nodeTopologyModel";

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
  const exposedBorder = nodeBorder;
  
  return [
    {
      selector: "node",
      style: {
        "background-color": nodeBg,
        "border-color": nodeBorder,
        "border-width": 1.5,
        "font-family": 'ui-monospace, Menlo, Monaco, Consolas, monospace',
        "font-size": 12,
        "font-weight": 500,
        "height": `${NODE_HEIGHT}px`,
        "label": "data(label)",
        "padding": "10px",
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
        "background-color": nodeBg,
        "font-weight": 700,
      },
    },
    {
      selector: "node.region",
      style: {
        "border-width": 1.5,
        "border-color": isDark ? "rgba(253,252,252,0.28)" : "rgba(15,0,0,0.2)",
        "background-color": nodeBg,
        "shape": "round-rectangle",
      },
    },
    {
      selector: "node.leader",
      style: {
        "border-width": 1.5,
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
        "border-width": 1.5,
        "border-color": exposedBorder,
        "background-color": nodeBg,
      },
    },
    {
      selector: "edge",
      style: {
        "curve-style": "bezier",
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

  const snapshot = nodeTopologySnapshot(nodes, services, lang);
  const topology = useMemo(() => layoutNodeTopology(snapshot), [snapshot]);
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
          // Allow fit() to include every node even in large clusters.
          minZoom={0}
          stylesheet={stylesheet}
          cy={(cy) => setCyRef(cy)}
        />
        
        <div className="cy-controls" aria-label="Topology controls">
          <Button variant="outline" size="icon-sm" aria-label="Zoom in" className="cy-control-btn" onClick={handleZoomIn} type="button" title="Zoom In">+</Button>
          <Button variant="outline" size="icon-sm" aria-label="Zoom out" className="cy-control-btn" onClick={handleZoomOut} type="button" title="Zoom Out">-</Button>
          <Button variant="outline" size="icon-sm" aria-label="Reset view" className="cy-control-btn" onClick={handleReset} type="button" title="Reset View">0</Button>
          <TopologyFullscreenButton cy={cyRef} lang={lang} />
        </div>
      </div>
    </section>
  );
}
