import { Activity, Bot, MapPin, Server, SquareTerminal, TerminalSquare } from "@/components/icons";
import { localizeState } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { StatTile, UsageMeter } from "./primitives";
import { formatBytes, formatPercent } from "../format";

function clampPercent(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function nodeHealth(node: DashboardNode) {
  const state = (node.state || "").toLowerCase();
  const availability = (node.availability || "").toLowerCase();
  const agent = (node.agentStatus || "").toLowerCase();
  if (state === "down" || agent === "missing" || agent === "stale" || agent === "offline" || availability === "drain") return "danger";
  if (!state || state === "missing") return agent === "ready" ? "warn" : "danger";
  if (agent === "ready" && availability !== "drain" && state === "ready") return "good";
  return "warn";
}

function pressureOf(node: DashboardNode) {
  const metrics = node.metrics || {};
  return Math.max(
    clampPercent(metrics.cpuPercent ?? metrics.loadPercent),
    clampPercent(metrics.memoryUsedPercent),
  );
}

function pressureAvailable(node: DashboardNode) {
  const metrics = node.metrics || {};
  return [metrics.cpuPercent ?? metrics.loadPercent, metrics.memoryUsedPercent].some(value => typeof value === "number" && Number.isFinite(value));
}

function pressureLabel(value: number, lang: Lang, available: boolean) {
  if (!available) return lang === "zh" ? "无数据" : "No data";
  if (value >= 85) return lang === "zh" ? "高压" : "hot";
  if (value >= 65) return lang === "zh" ? "偏高" : "busy";
  return lang === "zh" ? "平稳" : "steady";
}

function terminalReady(node: DashboardNode) {
  return Boolean(node.terminalConnected);
}

function terminalUnavailableLabel(node: DashboardNode, lang: Lang) {
  const status = node.terminalStatus || "unsupported";
  if (status === "waiting") return lang === "zh" ? "Terminal supervisor 未连接" : "Terminal supervisor not connected";
  if (status === "unsupported") return lang === "zh" ? "节点 agent 不支持 Terminal" : "Node agent does not support Terminal";
  return lang === "zh" ? "Terminal 不可用" : "Terminal unavailable";
}

function nodeStateLabel(node: DashboardNode, lang: Lang) {
  const state = (node.state || "").toLowerCase();
  const agent = (node.agentStatus || "").toLowerCase();
  if ((state === "missing" || !state) && agent === "ready") return lang === "zh" ? "调度状态未知" : "Scheduler unknown";
  return localizeState(lang, node.state);
}

function workloadCounts(services: DashboardService[]) {
  const counts = new Map<string, { services: number; tasks: number }>();
  for (const service of services) {
    const serviceNodes = new Set<string>();
    for (const nodeName of service.nodes || []) {
      if (nodeName) serviceNodes.add(nodeName);
    }
    for (const task of service.tasks || []) {
      if (!task.node) continue;
      serviceNodes.add(task.node);
      const current = counts.get(task.node) || { services: 0, tasks: 0 };
      counts.set(task.node, { ...current, tasks: current.tasks + 1 });
    }
    for (const nodeName of serviceNodes) {
      const current = counts.get(nodeName) || { services: 0, tasks: 0 };
      counts.set(nodeName, { ...current, services: current.services + 1 });
    }
  }
  return counts;
}

function regionsFor(nodes: DashboardNode[]) {
  const groups = new Map<string, DashboardNode[]>();
  for (const node of nodes) {
    const region = node.region || "unknown";
    groups.set(region, [...(groups.get(region) || []), node]);
  }
  return Array.from(groups.entries())
    .map(([region, items]) => ({
      region,
      nodes: items.slice().sort((a, b) => (a.name || "").localeCompare(b.name || "")),
    }))
    .sort((a, b) => a.region.localeCompare(b.region));
}

const HEALTH_DOT = { good: "bg-success", warn: "bg-warning", danger: "bg-destructive" } as const;

export function NodeFleetMap({
  lang,
  nodes,
  services,
  onSelect,
  onTerminal,
}: {
  lang: Lang;
  nodes: DashboardNode[];
  services?: DashboardService[];
  onSelect: (node: DashboardNode) => void;
  onTerminal?: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const regions = regionsFor(nodes);
  const workloads = workloadCounts(services || []);
  const readyNodes = nodes.filter((node) => nodeHealth(node) === "good").length;
  const terminalNodes = nodes.filter(terminalReady).length;
  const readyAgents = nodes.filter((node) => (node.agentStatus || "").toLowerCase() === "ready").length;
  const pressuredNodes = nodes.filter((node) => pressureOf(node) >= 85).length;
  const maxPressure = nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);
  const hasAnyPressure = nodes.some(pressureAvailable);

  if (!nodes.length) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon"><Server /></EmptyMedia>
          <EmptyTitle>{zh ? "暂无节点" : "No nodes"}</EmptyTitle>
          <EmptyDescription>{zh ? "加入节点后，可在这里查看资源和调度状态。" : "Once a node joins, its resources and scheduling state appear here."}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <section className="@container flex flex-col gap-6" aria-label={zh ? "节点态势" : "Node fleet"}>
      <div className="grid grid-cols-2 gap-3 @2xl:grid-cols-4">
        <StatTile icon={Server} label={zh ? "在线节点" : "Ready nodes"} value={readyNodes} detail={`/ ${nodes.length}`} tone={readyNodes < nodes.length ? "warning" : "normal"} />
        <StatTile icon={Bot} label={zh ? "Agent 就绪" : "Agents ready"} value={readyAgents} detail={`/ ${nodes.length}`} tone={readyAgents < nodes.length ? "warning" : "normal"} />
        <StatTile icon={TerminalSquare} label={zh ? "终端可用" : "Terminal ready"} value={terminalNodes} detail={`/ ${nodes.length}`} />
        <StatTile icon={Activity} label={zh ? "高负载节点" : "High load"} value={pressuredNodes} detail={hasAnyPressure ? (zh ? `峰值 ${formatPercent(maxPressure)}` : `peak ${formatPercent(maxPressure)}`) : (zh ? "无指标" : "no metrics")} tone={pressuredNodes ? "critical" : "normal"} />
      </div>

      <Card className="gap-0 py-0">
        <CardContent>
          <Table className="min-w-3xl" containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "节点列表，可横向滚动" : "Nodes, horizontally scrollable" }}>
            <TableHeader>
              <TableRow>
                <TableHead>{zh ? "节点" : "Node"}</TableHead>
                <TableHead>{zh ? "状态" : "Status"}</TableHead>
                <TableHead className="w-[17%]">CPU</TableHead>
                <TableHead className="w-[17%]">{zh ? "内存" : "Memory"}</TableHead>
                <TableHead>{zh ? "负载" : "Workload"}</TableHead>
                <TableHead className="w-12"><span className="sr-only">{zh ? "终端" : "Terminal"}</span></TableHead>
              </TableRow>
            </TableHeader>
            {regions.map((group) => {
              const groupReady = group.nodes.filter((node) => nodeHealth(node) === "good").length;
              const groupPressure = group.nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);
              const hasPressure = group.nodes.some(pressureAvailable);
              const pressureTone = !hasPressure ? "outline" : groupPressure >= 85 ? "destructive" : groupPressure >= 65 ? "warning" : "success";
              return (
                <TableBody key={group.region} aria-label={group.region}>
                  <TableRow className="bg-muted/35 hover:bg-muted/35">
                    <TableCell colSpan={6} className="py-2">
                      <span className="flex min-w-0 items-center gap-2 text-xs">
                        <MapPin className="size-3.5 text-muted-foreground" aria-hidden="true" />
                        <span className="truncate font-medium" title={group.region}>{group.region}</span>
                        <span className="text-muted-foreground tabular-nums">{zh ? `${groupReady}/${group.nodes.length} 在线` : `${groupReady}/${group.nodes.length} online`}</span>
                        <Badge variant={pressureTone} appearance={pressureTone === "success" || pressureTone === "outline" ? "dot" : "filled"} className="ml-auto">{pressureLabel(groupPressure, lang, hasPressure)}</Badge>
                      </span>
                    </TableCell>
                  </TableRow>
                  {group.nodes.map((node, index) => {
                    const metrics = node.metrics || {};
                    const capacity = node.capacity || {};
                    const nodeName = node.name || "-";
                    const health = nodeHealth(node);
                    const hasTerminal = terminalReady(node);
                    const workload = workloads.get(nodeName) || { services: 0, tasks: 0 };
                    return (
                      <TableRow key={`${node.name || "node"}-${index}`}>
                        <TableCell className="max-w-64">
                          <div className="flex min-w-0 flex-col gap-0.5">
                            <button type="button" className="max-w-full truncate text-left font-medium text-link outline-none hover:underline focus-visible:underline" title={zh ? `查看节点 ${nodeName}` : `View node ${nodeName}`} onClick={() => onSelect(node)}>{nodeName}</button>
                            <span className="truncate text-xs text-muted-foreground">{[node.role, node.agentOs, node.availability].filter(Boolean).join(" · ") || "—"}</span>
                          </div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-2">
                            <span className={cn("size-1.5 shrink-0 rounded-full", HEALTH_DOT[health])} aria-hidden="true" />
                            <span className={cn(health === "danger" && "text-destructive-foreground", health === "warn" && "text-warning-foreground")}>{nodeStateLabel(node, lang)}</span>
                          </span>
                        </TableCell>
                        <TableCell><UsageMeter value={metrics.cpuPercent ?? metrics.loadPercent} label={`${nodeName} CPU`} /></TableCell>
                        <TableCell><UsageMeter value={metrics.memoryUsedPercent} label={`${nodeName} ${zh ? "内存" : "memory"}`} /></TableCell>
                        <TableCell>
                          <div className="flex flex-col gap-0.5 tabular-nums">
                            <span>{zh ? `${workload.services} 服务 · ${workload.tasks} 任务` : `${workload.services} services · ${workload.tasks} tasks`}</span>
                            <span className="text-xs text-muted-foreground">{zh ? "内存" : "Memory"} {formatBytes(metrics.memoryTotalBytes ?? capacity.memoryBytes)}</span>
                          </div>
                        </TableCell>
                        <TableCell className="text-right">
                          <Tooltip>
                            <TooltipTrigger render={<span tabIndex={hasTerminal && onTerminal ? undefined : 0} className="inline-flex" />}>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-sm"
                                disabled={!hasTerminal || !onTerminal}
                                aria-label={hasTerminal ? `Terminal ${nodeName}` : terminalUnavailableLabel(node, lang)}
                                onClick={() => onTerminal?.(node)}
                              >
                                <SquareTerminal />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs break-all">{hasTerminal ? `Terminal ${nodeName}` : terminalUnavailableLabel(node, lang)}</TooltipContent>
                          </Tooltip>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              );
            })}
          </Table>
        </CardContent>
      </Card>
    </section>
  );
}
