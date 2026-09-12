import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Maximize, Minus, Network, Plus } from "lucide-react";
import { useTopologyTheme, type TopologyTheme } from "./topologyTheme";
import { TopologyFullscreenButton } from "./TopologyFullscreenButton";

import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { layoutNodeTopology, nodeTopologySnapshot, NODE_HEIGHT, NODE_WIDTH } from "./nodeTopologyModel";

function getStylesheet(tokens: TopologyTheme): cytoscape.StylesheetJsonBlock[] {
  const textColor = tokens.foreground;
  const nodeBg = tokens.card;
  const nodeBorder = tokens.border;
  const edgeColor = tokens.mutedForeground;
  const leaderBorder = tokens.foreground;
  const hostBg = tokens.muted;
  const hostBorder = tokens.border;
  const serviceBorder = nodeBorder;
  const exposedBorder = nodeBorder;

  return [
    {
      selector: "node",
      style: {
        "background-color": nodeBg,
        "border-color": nodeBorder,
        "border-width": 1.5,
        "font-family": tokens.fontMono,
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
        "border-color": tokens.border,
        "background-color": nodeBg,
        "shape": "round-rectangle",
      },
    },
    {
      selector: "node.leader",
      style: {
        "border-width": 1.5,
        "border-color": leaderBorder,
        "background-color": tokens.muted,
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
        "border-color": tokens.primary,
        "border-width": 3,
        "background-color": tokens.accent,
      },
    },
    {
      selector: "edge.highlighted",
      style: {
        "line-color": tokens.primary,
        "target-arrow-color": tokens.primary,
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
  const tokens = useTopologyTheme(theme);
  const stylesheet = useMemo(() => getStylesheet(tokens), [tokens]);

  useEffect(() => {
    if (cyRef) {
      cyRef.style(stylesheet);
    }
  }, [cyRef, stylesheet]);

  useEffect(() => {
    if (!cyRef) return;

    const handleMouseOver = (event: cytoscape.EventObject) => {
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

  const zh = lang === "zh";
  const controls = [
    { icon: Plus, label: zh ? "放大" : "Zoom in", action: handleZoomIn },
    { icon: Minus, label: zh ? "缩小" : "Zoom out", action: handleZoomOut },
    { icon: Maximize, label: zh ? "适应画布" : "Fit view", action: handleReset },
  ];

  return (
    <Card id="section-4">
      <CardHeader>
        <CardTitle>{t(lang, "nodeTopology")}</CardTitle>
        <CardDescription className="col-span-full">{zh ? "查看集群、区域、节点和服务之间的关系。悬停可高亮相邻连接。" : "Explore cluster, region, node, and service relationships. Hover to highlight neighboring connections."}</CardDescription>
        <CardAction className="row-span-1"><Badge variant="outline">{zh ? `${topology.nodes.length} 个节点 · ${topology.edges.length} 条连接` : `${topology.nodes.length} nodes · ${topology.edges.length} links`}</Badge></CardAction>
      </CardHeader>
      <CardContent>
        {nodes.length ? <div className="topology-canvas standalone-topology" aria-label={t(lang, "nodeTopology")}>
          <CytoscapeComponent
            className="cy-topology"
            elements={topology.elements}
            layout={{ name: "preset", fit: true, padding: 48 }}
            maxZoom={1.6}
            // Allow fit() to include every node even in large clusters.
            minZoom={0}
            stylesheet={stylesheet}
            cy={setCyRef}
          />
          <div className="cy-controls" role="group" aria-label={zh ? "拓扑图操作" : "Topology controls"}>
            {controls.map(({ icon: Icon, label, action }) => <Tooltip key={label}>
              <TooltipTrigger render={<Button variant="outline" size="icon-sm" aria-label={label} title={label} disabled={!cyRef} onClick={action} />}><Icon data-icon="inline-start" /></TooltipTrigger>
              <TooltipContent>{label}</TooltipContent>
            </Tooltip>)}
            <TopologyFullscreenButton cy={cyRef} lang={lang} />
          </div>
        </div> : <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><Network /></EmptyMedia>
            <EmptyTitle>{zh ? "暂无节点拓扑" : "No node topology yet"}</EmptyTitle>
            <EmptyDescription>{zh ? "节点加入集群后，可在这里查看连接关系。" : "Connections appear here after a node joins the cluster."}</EmptyDescription>
          </EmptyHeader>
        </Empty>}
      </CardContent>
    </Card>
  );
}
