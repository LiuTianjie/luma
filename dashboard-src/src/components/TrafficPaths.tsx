import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Spinner } from "@/components/ui/spinner";
import { useTopologyTheme, type TopologyTheme } from "./topologyTheme";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Search, Network, X, Maximize, Plus, Minus, CircleAlert, Check } from "lucide-react";
import { buildTopology, normalizePathSegments, routeElementIds } from "./trafficTopology";
const NODE_WIDTH = 190;
const NODE_HEIGHT = 44;
const DESTINATION_WIDTH = 220;
import CytoscapeComponent from "react-cytoscapejs";
import type cytoscape from "cytoscape";
import { t } from "../i18n";
import { retryCertificate } from "../lifecycleApi";
import type { Lang, TrafficPath } from "../types";
import { TopologyFullscreenButton } from "./TopologyFullscreenButton";

function getStylesheet(tokens: TopologyTheme): cytoscape.StylesheetJsonBlock[] {
  const textColor = tokens.foreground;
  const nodeBg = tokens.card;
  const nodeBorder = tokens.border;
  const edgeColor = tokens.mutedForeground;
  const domainBorder = nodeBorder;
  const proxyBorder = nodeBorder;
  const issueBorder = tokens.destructive;
  const targetBorder = nodeBorder;
  const destinationBorder = nodeBorder;

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
    { selector: "node.edge", style: { "border-color": nodeBorder } },
    { selector: "node.domain", style: { "border-width": 1, "border-color": domainBorder, "background-color": nodeBg } },
    { selector: "node.proxy", style: { "border-width": 1, "border-color": proxyBorder, "background-color": nodeBg } },
    { selector: "node.tunnel", style: { "border-color": nodeBorder } },
    { selector: "node.target", style: { "border-color": targetBorder, "color": tokens.mutedForeground } },
    { selector: "node.destination", style: { "border-width": 1, "border-color": destinationBorder, "background-color": nodeBg, "width": `${DESTINATION_WIDTH}px` } },
    { selector: "node.issue", style: { "border-width": 1, "border-color": issueBorder, "background-color": tokens.muted } },
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
  const tokens = useTopologyTheme(theme);
  const stylesheet = useMemo(() => getStylesheet(tokens), [tokens]);

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

  const ingressItems = [{ value: "", label: zh ? "所有入口类型" : "All ingress types" }, ...kinds.map(value => ({ value, label: value }))];
  const controls = [
    { icon: Plus, label: zh ? "放大" : "Zoom in", action: handleZoomIn },
    { icon: Minus, label: zh ? "缩小" : "Zoom out", action: handleZoomOut },
    { icon: Maximize, label: zh ? "适应画布" : "Fit view", action: handleReset },
  ];

  return (
    <section className="route-workspace" aria-label={t(lang, "trafficPaths")}>
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant="outline"><Network data-icon="inline-start" />{filteredPaths.length} {zh ? "条路由" : "routes"}</Badge>
        <span className="text-sm text-muted-foreground">{nodes.length} {zh ? "个节点" : "nodes"} · {edges.length} {zh ? "条连接" : "connections"}</span>
      </div>
      <p className="text-sm text-muted-foreground">{zh ? "按路由配置与运行实例展示链路，未进行逐跳连通性探测。悬停可高亮完整路径；点击下方域名可单独查看。" : "Paths reflect route configuration and running instances, not hop-by-hop connectivity probes. Hover to trace complete paths; select a domain below to isolate one."}</p>
      <FieldGroup className="route-filters">
        <Field>
          <FieldLabel htmlFor="route-search">{zh ? "搜索路由" : "Search routes"}</FieldLabel>
          <InputGroup>
            <InputGroupAddon><Search /></InputGroupAddon>
            <InputGroupInput id="route-search" value={query} onChange={event => { setQuery(event.target.value); setSelectedPath(null); }} placeholder={zh ? "搜索域名、应用、节点或地址…" : "Search domains, applications, nodes or addresses…"} />
          </InputGroup>
        </Field>
        <Field>
          <FieldLabel htmlFor="route-ingress-type">{zh ? "入口类型" : "Ingress type"}</FieldLabel>
          <Select items={ingressItems} value={kind} onValueChange={value => { setKind(value || ""); setSelectedPath(null); }}>
            <SelectTrigger id="route-ingress-type" className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectGroup>{ingressItems.map(item => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectGroup>
            </SelectContent>
          </Select>
        </Field>
      </FieldGroup>
      {certMessage ? <Alert variant={certMessage.kind === "error" ? "destructive" : "default"}>
        {certMessage.kind === "error" ? <CircleAlert /> : <Check />}
        <AlertTitle>{certMessage.kind === "error" ? (zh ? "证书重试失败" : "Certificate retry failed") : (zh ? "已提交证书重试" : "Certificate retry submitted")}</AlertTitle>
        <AlertDescription><p>{certMessage.routeId}</p><p>{certMessage.text}</p></AlertDescription>
      </Alert> : null}
      {filteredPaths.length ? <>
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "入口与服务关系" : "Ingress and services"}</CardTitle>
            <CardDescription className="break-words">{selected ? (selected.domain || selected.id) : (zh ? "选择下方路由可查看单条完整路径。" : "Select a route below to inspect its complete path.")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="topology-canvas route-map" aria-label={zh ? "路由关系图" : "Route diagram"}>
              {/* A fixed zoom floor clamps fit() and clips large route graphs. */}
              <CytoscapeComponent className="cy-topology" elements={elements} layout={{ name: "preset", fit: true, padding: 36 }} maxZoom={1.6} minZoom={0} stylesheet={stylesheet} cy={setCyRef} />
              <div className="cy-controls" role="group" aria-label={zh ? "关系图操作" : "Diagram controls"}>
                {selected ? <Button variant="outline" size="sm" onClick={() => setSelectedPath(null)}><X data-icon="inline-start" />{zh ? "显示全部" : "Show all"}</Button> : null}
                {controls.map(({ icon: Icon, label, action }) => <Tooltip key={label}>
                  <TooltipTrigger render={<Button variant="outline" size="icon-sm" aria-label={label} title={label} disabled={!cyRef} onClick={action} />}><Icon data-icon="inline-start" /></TooltipTrigger>
                  <TooltipContent>{label}</TooltipContent>
                </Tooltip>)}
                <TopologyFullscreenButton cy={cyRef} lang={lang} />
              </div>
            </div>
          </CardContent>
        </Card>
        {selected ? <Card aria-label={zh ? "完整路由路径" : "Complete route path"}>
          <CardHeader>
            <CardTitle className="break-all">{selected.domain || selected.id}</CardTitle>
            <CardDescription>{zh ? "完整路由路径" : "Complete route path"}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <ol className="flex flex-wrap items-center gap-2">
              {normalizePathSegments(selected).map((segment, index, segments) => <li className="flex min-w-0 items-center gap-2" key={`${index}-${segment}`}>
                <span className="break-all font-mono text-sm">{segment}</span>
                {index < segments.length - 1 ? <span className="text-muted-foreground" aria-hidden="true">→</span> : null}
              </li>)}
            </ol>
            <dl className="flex flex-col gap-3">
              {(selected.destinations || []).map((destination, index) => <div className="flex flex-col gap-1" key={index}>
                <dt className="text-sm text-muted-foreground">{zh ? "目标实例" : "Destination"}</dt>
                <dd className="break-all text-sm">{[destination.service, destination.region, destination.node, destination.address || destination.nodeAddress, destination.state].filter(Boolean).join(" · ") || (zh ? "未知" : "Unknown")}</dd>
              </div>)}
            </dl>
          </CardContent>
        </Card> : null}
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "路由列表" : "Routes"}</CardTitle>
            <CardDescription>{zh ? `显示 ${filteredPaths.length} / ${paths.length} 条路由` : `Showing ${filteredPaths.length} of ${paths.length} routes`}</CardDescription>
          </CardHeader>
          <CardContent>
            <Table className="min-w-160" containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "路由列表，可横向滚动" : "Routes, horizontally scrollable" }}>
              <TableHeader><TableRow><TableHead>{zh ? "域名 / 路由" : "Domain / route"}</TableHead><TableHead>{zh ? "入口类型" : "Ingress type"}</TableHead><TableHead>{zh ? "目标节点" : "Destination"}</TableHead><TableHead>{zh ? "操作" : "Actions"}</TableHead></TableRow></TableHeader>
              <TableBody>{filteredPaths.map((path, index) => {
                const routeId = path.certificateRetry?.routeId || path.id || "";
                const canRetry = Boolean(path.certificateRetry?.available && path.domain && routeId);
                const destination = (path.destinations || []).map(item => [item.region, item.node].filter(Boolean).join(" / ")).filter(Boolean).join(", ");
                return <TableRow key={`${path.id}-${path.domain}-${index}`} data-state={selected === path ? "selected" : undefined}>
                  <TableCell>
                    <Button type="button" variant="link" size="sm" className="max-w-80" aria-pressed={selected === path} onClick={() => setSelectedPath(selected === path ? null : path)}>
                      <span className="truncate font-mono" title={path.domain || path.id}>{path.domain || path.id || "—"}</span>
                    </Button>
                    {path.domain && path.id ? <span className="block max-w-80 break-all text-xs text-muted-foreground">{path.id}</span> : null}
                  </TableCell>
                  <TableCell><Badge variant="outline" className="max-w-40"><span className="truncate" title={path.kind || "unknown"}>{path.kind || "unknown"}</span></Badge></TableCell>
                  <TableCell className="max-w-80 break-all whitespace-normal">{destination || "—"}</TableCell>
                  <TableCell>{canRetry ? <Button variant="outline" size="sm" disabled={Boolean(certBusy)} onClick={() => void handleCertificateRetry(path)}>{certBusy === routeId ? <Spinner data-icon="inline-start" aria-hidden="true" /> : null}{certBusy === routeId ? (zh ? "重试中…" : "Retrying…") : (zh ? "重试证书" : "Retry certificate")}</Button> : <span className="text-muted-foreground">—</span>}</TableCell>
                </TableRow>;
              })}</TableBody>
            </Table>
          </CardContent>
        </Card>
      </> : <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon"><Network /></EmptyMedia>
          <EmptyTitle>{paths.length ? (zh ? "没有匹配的路由" : "No matching routes") : (zh ? "暂无路由" : "No routes yet")}</EmptyTitle>
          <EmptyDescription>{paths.length ? (zh ? "试试其他关键词或入口类型。" : "Try another search or ingress type.") : (zh ? "应用配置入口后会显示在这里。" : "Routes appear when an application has an ingress configured.")}</EmptyDescription>
        </EmptyHeader>
        {paths.length ? <EmptyContent><Button variant="outline" onClick={() => { setQuery(""); setKind(""); }}>{zh ? "清除筛选" : "Clear filters"}</Button></EmptyContent> : null}
      </Empty>}
    </section>
  );
}
