import { useEffect, useMemo, useState } from "react";
import dagre from "dagre";
import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import { retryCertificate } from "../lifecycleApi";
import type { Lang, TrafficDestination, TrafficPath } from "../types";
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
const DESTINATION_WIDTH = 220;

function normalizePathSegments(path: TrafficPath) {
  const segments = (path.segments || []).filter(Boolean);
  const kind = path.kind || "";
  const domain = (path.domain || "").trim();
  const publicPrefix = domain ? [domain] : [];
  if (path.destinations?.length) return filterDestinationSegments([...publicPrefix, ...segments], path.destinations);
  if (kind === "cn-edge" || kind === "external-edge") return [...publicPrefix, ...segments.slice(0, 4)];
  if (kind === "cloudflare-tunnel") return [...publicPrefix, ...segments.slice(0, 3)];
  if (kind === "tailscale-relay" || kind === "tcp-relay") return [...publicPrefix, ...segments];
  return segments.slice(0, 2);
}

function filterDestinationSegments(segments: string[], destinations: TrafficDestination[]) {
  const destinationValues = new Set<string>();
  destinations.forEach((destination) => {
    [destination.address, destination.node, destination.nodeAddress].forEach((value) => {
      if (value) destinationValues.add(value);
    });
  });
  const filtered = segments.filter((segment) => {
    if (destinationValues.has(segment)) return false;
    return !Array.from(destinationValues).some((value) => value && segment.includes(value));
  });
  return filtered.length ? filtered : segments;
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
    const routeLabel = path.domain || path.id || `route-${pathIndex + 1}`;
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
        });
      }
    });
  });

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
  const textColor = isDark ? "#fdfcfc" : "#201d1d";
  const nodeBg = isDark ? "#302c2c" : "#ffffff";
  const nodeBorder = isDark ? "rgba(253, 252, 252, 0.16)" : "rgba(15, 0, 0, 0.12)";
  const edgeColor = isDark ? "rgba(154, 152, 152, 0.4)" : "rgba(110, 110, 115, 0.5)";

  const domainBorder = "#30d158";
  const proxyBorder = "#007aff";
  const issueBorder = "#ff3b30";
  const targetBorder = isDark ? "#646262" : "#d3d0d0";
  const destinationBorder = "#ff9f0a";

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
    { selector: "node.edge", style: { "border-color": nodeBorder } },
    { selector: "node.domain", style: { "border-width": 2.5, "border-color": domainBorder, "background-color": isDark ? "#1e2a20" : "#ecfdf0" } },
    { selector: "node.proxy", style: { "border-width": 2.5, "border-color": proxyBorder, "background-color": isDark ? "#1c2733" : "#eaf3ff" } },
    { selector: "node.tunnel", style: { "border-color": nodeBorder } },
    { selector: "node.target", style: { "border-color": targetBorder, "color": isDark ? "#9a9898" : "#6e6e73" } },
    { selector: "node.destination", style: { "border-width": 2.5, "border-color": destinationBorder, "background-color": isDark ? "#2e2717" : "#fff7e8", "width": `${DESTINATION_WIDTH}px` } },
    { selector: "node.issue", style: { "border-width": 2.5, "border-color": issueBorder, "background-color": isDark ? "#33201f" : "#ffefee" } },
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
    { selector: "edge.cn-edge, edge.external-edge", style: { "line-color": "#007aff", "target-arrow-color": "#007aff" } },
    { selector: "edge.tailscale-relay, edge.tcp-relay, edge.cloudflare-tunnel", style: { "line-color": "#30d158", "target-arrow-color": "#30d158" } },
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

export function TrafficPaths({
  lang,
  paths,
  theme,
  token,
  onRefresh,
}: {
  lang: Lang;
  paths: TrafficPath[];
  theme: "light" | "dark";
  token: string;
  onRefresh: () => Promise<void> | void;
}) {
  const [cyRef, setCyRef] = useState<cytoscape.Core | null>(null);
  const [certBusy, setCertBusy] = useState("");
  const [certMessage, setCertMessage] = useState<{ routeId: string; kind: "ok" | "error"; text: string } | null>(null);

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

  const handleCertificateRetry = async (path: TrafficPath) => {
    const domain = path.domain || "";
    const routeId = path.certificateRetry?.routeId || path.id || "";
    if (!domain || !routeId) return;
    setCertBusy(routeId);
    setCertMessage(null);
    try {
      await retryCertificate({ token, domain, routeId });
      setCertMessage({
        routeId,
        kind: "ok",
        text: lang === "zh" ? "已触发路由重载，Traefik 会重新尝试签发。" : "Route reload triggered. Traefik will retry ACME.",
      });
      await onRefresh();
    } catch (error) {
      setCertMessage({
        routeId,
        kind: "error",
        text: String(error instanceof Error ? error.message : error),
      });
    } finally {
      setCertBusy("");
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
              cy={(cy) => setCyRef(cy)}
            />
            
            <div className="cy-controls" aria-label="Topology controls">
              <button aria-label="Zoom in" className="cy-control-btn" onClick={handleZoomIn} type="button" title="Zoom In">+</button>
              <button aria-label="Zoom out" className="cy-control-btn" onClick={handleZoomOut} type="button" title="Zoom Out">-</button>
              <button aria-label="Reset view" className="cy-control-btn" onClick={handleReset} type="button" title="Reset View">0</button>
            </div>
          </div>
          <aside className="route-index" aria-label={t(lang, "trafficPaths")}>
            {paths.map((path, index) => {
              const destinations = path.destinations || [];
              const routeId = path.certificateRetry?.routeId || path.id || "";
              const certificateRetryAvailable = Boolean(path.certificateRetry?.available && path.domain && routeId);
              const destinationSummary = destinations
                .map((destination) => [destination.region, destination.node].filter(Boolean).join(" / "))
                .filter(Boolean)
                .join(", ");
              return (
                <article key={`${path.id || "path"}-${index}`}>
                  <PrimaryCell meta={destinationSummary || path.domain || t(lang, "noPublicDomain")} title={path.id || "-"} />
                  <div className="route-index-actions">
                    <Badge value={path.kind || "unknown"} />
                    {certificateRetryAvailable ? (
                      <button
                        type="button"
                        className="ghost"
                        disabled={Boolean(certBusy)}
                        onClick={() => void handleCertificateRetry(path)}
                      >
                        {certBusy === routeId
                          ? (lang === "zh" ? "重试中..." : "Retrying...")
                          : (lang === "zh" ? "重试证书" : "Retry cert")}
                      </button>
                    ) : null}
                  </div>
                  {certMessage?.routeId === routeId ? (
                    <small className={`route-cert-message ${certMessage.kind}`}>{certMessage.text}</small>
                  ) : null}
                </article>
              );
            })}
          </aside>
        </div>
      ) : (
        <div className="topology-empty">{t(lang, "missing")}</div>
      )}
    </section>
  );
}
