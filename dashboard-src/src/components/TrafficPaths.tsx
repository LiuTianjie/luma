import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";

import { Input } from "@/components/ui/input";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Search, Network, X, Maximize, Plus, Minus } from "lucide-react";
import { buildTopology, normalizePathSegments, routeElementIds } from "./trafficTopology";
const NODE_WIDTH = 190;
const NODE_HEIGHT = 44;
const DESTINATION_WIDTH = 220;
import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import { retryCertificate } from "../lifecycleApi";
import type { Lang, TrafficPath } from "../types";
import { Badge } from "./primitives";
import { TopologyFullscreenButton } from "./TopologyFullscreenButton";

function getStylesheet(theme: "light" | "dark"): cytoscape.StylesheetJsonBlock[] {
  const isDark = theme === "dark";
  const textColor = isDark ? "#fdfcfc" : "#201d1d";
  const nodeBg = isDark ? "#302c2c" : "#ffffff";
  const nodeBorder = isDark ? "rgba(253, 252, 252, 0.16)" : "rgba(15, 0, 0, 0.12)";
  const edgeColor = isDark ? "rgba(154, 152, 152, 0.4)" : "rgba(110, 110, 115, 0.5)";

  const domainBorder = nodeBorder;
  const proxyBorder = nodeBorder;
  const issueBorder = "#ff3b30";
  const targetBorder = isDark ? "#646262" : "#d3d0d0";
  const destinationBorder = nodeBorder;

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
    { selector: "node.edge", style: { "border-color": nodeBorder } },
    { selector: "node.domain", style: { "border-width": 1, "border-color": domainBorder, "background-color": nodeBg } },
    { selector: "node.proxy", style: { "border-width": 1, "border-color": proxyBorder, "background-color": nodeBg } },
    { selector: "node.tunnel", style: { "border-color": nodeBorder } },
    { selector: "node.target", style: { "border-color": targetBorder, "color": isDark ? "#9a9898" : "#6e6e73" } },
    { selector: "node.destination", style: { "border-width": 1, "border-color": destinationBorder, "background-color": nodeBg, "width": `${DESTINATION_WIDTH}px` } },
    { selector: "node.issue", style: { "border-width": 1, "border-color": issueBorder, "background-color": isDark ? "#33201f" : "#ffefee" } },
    {
      selector: "edge",
      style: {
        "curve-style": "bezier",
        "line-color": edgeColor,
        "target-arrow-color": edgeColor,
        "target-arrow-shape": "triangle",
        "width": "1px",
      },
    },
    {
      selector: ".dimmed",
      style: {
        "opacity": 0.35,
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

  const zh = lang === "zh";
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [selectedPath, setSelectedPath] = useState<TrafficPath | null>(null);
  const kinds = useMemo(() => [...new Set(paths.map(path => path.kind || "unknown"))].sort(), [paths]);
  const filteredPaths = useMemo(() => paths.filter(path => {
    const text = [path.id, path.domain, ...(path.segments || []), ...(path.destinations || []).flatMap(destination => [destination.node, destination.region, destination.service, destination.address])].join(" ").toLowerCase();
    return (!kind || (path.kind || "unknown") === kind) && text.includes(query.trim().toLowerCase());
  }), [paths, kind, query]);
  const selected = selectedPath ? filteredPaths.find(path => path.id === selectedPath.id && path.domain === selectedPath.domain && path.kind === selectedPath.kind) : undefined;
  const { elements, nodes, edges } = useMemo(() => buildTopology(selected ? [selected] : filteredPaths), [selected, filteredPaths]);
  const stylesheet = useMemo(() => getStylesheet(theme), [theme]);

  useEffect(() => {
    if (cyRef) {
      cyRef.style(stylesheet);
    }
  }, [cyRef, stylesheet]);

  useEffect(() => {
    if (!cyRef) return;

    const resetHighlight = () => cyRef.elements().removeClass("dimmed highlighted");
    const handleMouseOver = (event: cytoscape.EventObject) => {
      resetHighlight();
      const ids = new Set(routeElementIds(elements, event.target.id()));
      if (!ids.size) return;
      cyRef.elements().addClass("dimmed");
      cyRef.elements().filter(element => ids.has(element.id())).removeClass("dimmed").addClass("highlighted");
    };
    cyRef.on("mouseover", "node, edge", handleMouseOver);
    cyRef.on("mouseout", "node, edge", resetHighlight);
    const container = cyRef.container();
    container?.addEventListener("pointerleave", resetHighlight);
    resetHighlight();
    return () => {
      cyRef.off("mouseover", "node, edge", handleMouseOver);
      cyRef.off("mouseout", "node, edge", resetHighlight);
      container?.removeEventListener("pointerleave", resetHighlight);
    };
  }, [cyRef, elements]);

  useEffect(() => {
    if (!cyRef || cyRef.destroyed()) return;
    const frame = requestAnimationFrame(() => { cyRef.resize(); cyRef.fit(undefined, 36); });
    const container = cyRef.container();
    const observer = new ResizeObserver(() => { if (!cyRef.destroyed()) { cyRef.resize(); cyRef.fit(undefined, 36); } });
    if (container) observer.observe(container);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [cyRef, elements]);

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
    <section className="route-workspace" aria-label={t(lang, "trafficPaths")}>
      <div className="route-summary">
        <span><Network aria-hidden="true" />{filteredPaths.length} {zh ? "条路由" : "routes"}</span>
        <span>{nodes.length} {zh ? "个节点" : "nodes"} · {edges.length} {zh ? "条连接" : "connections"}</span>
      </div>
      <p className="route-evidence-note">{zh ? "按路由配置与运行实例展示链路，未进行逐跳连通性探测。悬停可高亮完整路径；点击下方域名可单独查看。" : "Paths reflect route configuration and running instances, not hop-by-hop connectivity probes. Hover to trace complete paths; select a domain below to isolate one."}</p>
      <div className="route-filters">
        <label className="route-search"><span className="sr-only">{zh ? "搜索路由" : "Search routes"}</span><Search aria-hidden="true" /><Input value={query} onChange={event => { setQuery(event.target.value); setSelectedPath(null); }} placeholder={zh ? "搜索域名、应用、节点或地址…" : "Search domains, applications, nodes or addresses…"} /></label>
        <label><span className="sr-only">{zh ? "入口类型" : "Ingress type"}</span><select value={kind} onChange={event => { setKind(event.target.value); setSelectedPath(null); }}><option value="">{zh ? "所有入口类型" : "All ingress types"}</option>{kinds.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      </div>
      {filteredPaths.length ? <>
        <div className="topology-canvas route-map">
          {/* A fixed zoom floor clamps fit() and clips large route graphs. */}
          <CytoscapeComponent className="cy-topology" elements={elements} layout={{ name: "preset", fit: true, padding: 36 }} maxZoom={1.6} minZoom={0} stylesheet={stylesheet} cy={setCyRef} />
          <div className="cy-controls" aria-label={zh ? "关系图操作" : "Diagram controls"}>
            {selected ? <Button variant="outline" size="sm" onClick={() => setSelectedPath(null)}><X data-icon="inline-start" />{zh ? "显示全部" : "Show all"}</Button> : null}
            <Button variant="outline" size="icon-sm" aria-label={zh ? "放大" : "Zoom in"} onClick={handleZoomIn}><Plus /></Button>
            <Button variant="outline" size="icon-sm" aria-label={zh ? "缩小" : "Zoom out"} onClick={handleZoomOut}><Minus /></Button>
            <Button variant="outline" size="icon-sm" aria-label={zh ? "适应画布" : "Fit view"} onClick={handleReset}><Maximize /></Button>
            <TopologyFullscreenButton cy={cyRef} lang={lang} />
          </div>
          <p className="route-map-caption">{selected ? (selected.domain || selected.id) : (zh ? "入口与服务关系 · 点击下方路由查看单条路径" : "Ingress and services · Select a route below to inspect its path")}</p>
        </div>
        {selected ? <div className="route-path-detail" aria-label={zh ? "完整路由路径" : "Complete route path"}>
          <strong>{selected.domain || selected.id}</strong>
          <ol>{normalizePathSegments(selected).map((segment, index) => <li key={`${index}-${segment}`}>{segment}</li>)}</ol>
          {(selected.destinations || []).map((destination, index) => <p key={index}>{zh ? "目标实例" : "Destination"}: {[destination.service, destination.region, destination.node, destination.address || destination.nodeAddress, destination.state].filter(Boolean).join(" · ") || (zh ? "未知" : "Unknown")}</p>)}
        </div> : null}
        <div className="route-table">
          <Table>
            <TableHeader><TableRow><TableHead>{zh ? "域名 / 路由" : "Domain / route"}</TableHead><TableHead>{zh ? "入口类型" : "Ingress type"}</TableHead><TableHead>{zh ? "目标节点" : "Destination"}</TableHead><TableHead>{zh ? "操作" : "Actions"}</TableHead></TableRow></TableHeader>
            <TableBody>{filteredPaths.map((path, index) => {
              const routeId = path.certificateRetry?.routeId || path.id || "";
              const canRetry = Boolean(path.certificateRetry?.available && path.domain && routeId);
              const destination = (path.destinations || []).map(item => [item.region, item.node].filter(Boolean).join(" / ")).filter(Boolean).join(", ");
              return <TableRow key={`${path.id}-${path.domain}-${index}`} data-state={selected === path ? "selected" : undefined}>
                <TableCell><button type="button" className="route-select" aria-pressed={selected === path} onClick={() => setSelectedPath(selected === path ? null : path)}>{path.domain || path.id || "—"}</button>{path.domain && path.id ? <small className="block text-muted-foreground">{path.id}</small> : null}</TableCell>
                <TableCell><Badge value={path.kind || "unknown"} /></TableCell>
                <TableCell>{destination || "—"}</TableCell>
                <TableCell>{canRetry ? <Button variant="outline" size="sm" disabled={Boolean(certBusy)} onClick={() => void handleCertificateRetry(path)}>{certBusy === routeId ? (zh ? "重试中…" : "Retrying…") : (zh ? "重试证书" : "Retry certificate")}</Button> : <span className="text-muted-foreground">—</span>}{certMessage?.routeId === routeId ? <p role="status" className={`route-cert-message ${certMessage.kind}`}>{certMessage.text}</p> : null}</TableCell>
              </TableRow>;
            })}</TableBody>
          </Table>
          <div className="route-table-footer">{zh ? `显示 ${filteredPaths.length} / ${paths.length} 条路由` : `Showing ${filteredPaths.length} of ${paths.length} routes`}</div>
        </div>
      </> : <div className="empty-inline"><p>{paths.length ? (zh ? "没有匹配的路由。试试其他关键词或入口类型。" : "No matching routes. Try another search or ingress type.") : (zh ? "暂无路由。应用配置入口后会显示在这里。" : "No routes yet. Routes appear when an application has an ingress configured.")}</p>{paths.length ? <Button variant="outline" onClick={() => { setQuery(""); setKind(""); }}>{zh ? "清除筛选" : "Clear filters"}</Button> : null}</div>}
    </section>
  );
}
