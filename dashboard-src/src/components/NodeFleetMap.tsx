import { Activity, Cpu, MapPin, Server, SquareTerminal, TerminalSquare } from "lucide-react";
import { localizeState } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

function clampPercent(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function formatPercent(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)}%`;
}

function formatBytes(value?: number) {
  if (!value) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
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

function Meter({ label, value }: { label: string; value?: number }) {
  return (
    <Progress value={clampPercent(value)} aria-valuetext={formatPercent(value)}>
      <ProgressLabel>{label}</ProgressLabel>
      <ProgressValue>{() => formatPercent(value)}</ProgressValue>
    </Progress>
  );
}

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
  const pressuredNodes = nodes.filter((node) => pressureOf(node) >= 85).length;
  const maxPressure = nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);

  return (
    <section className="flex flex-col gap-6" aria-label={zh ? "节点态势" : "Node fleet"}>
      <div className="flex flex-wrap items-center gap-2">
        {[
          { icon: Server, value: readyNodes, label: zh ? "在线" : "Ready" },
          { icon: TerminalSquare, value: terminalNodes, label: zh ? "终端可用" : "Terminal" },
          { icon: Activity, value: pressuredNodes, label: zh ? "高负载" : "hot" },
          { icon: Cpu, value: formatPercent(nodes.some(pressureAvailable) ? maxPressure : undefined), label: zh ? "峰值" : "peak" },
        ].map((item) => (
          <Badge key={item.label} variant="outline">
            <item.icon data-icon="inline-start" />
            <span className="tabular-nums">{item.value}</span>
            {item.label}
          </Badge>
        ))}
      </div>

      {regions.map((group) => {
        const groupReady = group.nodes.filter((node) => nodeHealth(node) === "good").length;
        const groupPressure = group.nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);
        const hasPressure = group.nodes.some(pressureAvailable);
        const pressureTone = !hasPressure ? "outline" : groupPressure >= 85 ? "destructive" : groupPressure >= 65 ? "warning" : "success";
        return (
          <section className="flex flex-col gap-4" key={group.region} aria-label={group.region}>
            <div className="flex flex-wrap items-center gap-2">
              <Tooltip>
                <TooltipTrigger tabIndex={0} render={<Badge variant="secondary" className="max-w-full" />}>
                  <MapPin data-icon="inline-start" /><span className="truncate">{group.region}</span>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs break-all">{group.region}</TooltipContent>
              </Tooltip>
              <span className="text-sm text-muted-foreground">{groupReady}/{group.nodes.length} {zh ? "在线" : "online"}</span>
              <Badge variant={pressureTone} className="ml-auto">{pressureLabel(groupPressure, lang, hasPressure)}</Badge>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {group.nodes.map((node, index) => {
                const metrics = node.metrics || {};
                const capacity = node.capacity || {};
                const nodeName = node.name || "-";
                const health = nodeHealth(node);
                const hasTerminal = terminalReady(node);
                const workload = workloads.get(nodeName) || { services: 0, tasks: 0 };
                const cpu = metrics.cpuPercent ?? metrics.loadPercent;
                const memory = metrics.memoryUsedPercent;
                return (
                  <Card
                    size="sm"
                    key={`${node.name || "node"}-${index}`}
                  >
                    <CardHeader>
                      <CardTitle className="min-w-0">
                        <Tooltip>
                          <TooltipTrigger render={<Button variant="link" size="sm" className="max-w-full" aria-label={zh ? `查看节点 ${nodeName}` : `View node ${nodeName}`} onClick={() => onSelect(node)} />}>
                            <Server data-icon="inline-start" />
                            <span className="truncate">{nodeName}</span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs break-all">{zh ? `查看节点 ${nodeName}` : `View node ${nodeName}`}</TooltipContent>
                        </Tooltip>
                      </CardTitle>
                      <CardAction className="row-span-1 max-w-32">
                        <Badge className="max-w-full" variant={health === "good" ? "success" : health === "danger" ? "destructive" : "warning"}>
                          <span className="truncate" title={nodeStateLabel(node, lang)}>{nodeStateLabel(node, lang)}</span>
                        </Badge>
                      </CardAction>
                      <CardDescription className="col-span-full min-w-0 break-words">{[node.role, node.agentOs, node.availability].filter(Boolean).join(" · ") || "—"}</CardDescription>
                    </CardHeader>
                    <CardContent className="flex flex-col gap-4">
                      <Meter label="CPU" value={cpu} />
                      <Meter label={zh ? "内存" : "Memory"} value={memory} />
                      <p className="text-sm text-muted-foreground">{zh ? "内存容量" : "Memory capacity"} <span className="tabular-nums">{formatBytes(metrics.memoryTotalBytes || capacity.memoryBytes)}</span></p>
                    </CardContent>
                    <CardFooter className="flex-wrap justify-between gap-2">
                      <span className="text-sm text-muted-foreground">{zh ? `${workload.services} 个服务 · ${workload.tasks} 个任务` : `${workload.services} services · ${workload.tasks} tasks`}</span>
                      <Tooltip>
                        <TooltipTrigger render={<span tabIndex={hasTerminal && onTerminal ? undefined : 0} />}>
                          <Button
                            type="button"
                            variant="outline"
                            size="icon-sm"
                            disabled={!hasTerminal || !onTerminal}
                            aria-label={hasTerminal ? `Terminal ${nodeName}` : terminalUnavailableLabel(node, lang)}
                            onClick={() => onTerminal?.(node)}
                          >
                            <SquareTerminal data-icon="inline-start" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-xs break-all">{hasTerminal ? `Terminal ${nodeName}` : terminalUnavailableLabel(node, lang)}</TooltipContent>
                      </Tooltip>
                    </CardFooter>
                  </Card>
                );
              })}
            </div>
          </section>
        );
      })}

      {!nodes.length ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><Server /></EmptyMedia>
            <EmptyTitle>{zh ? "暂无节点" : "No nodes"}</EmptyTitle>
            <EmptyDescription>{zh ? "加入节点后，可在这里查看资源和调度状态。" : "Once a node joins, its resources and scheduling state appear here."}</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : null}
    </section>
  );
}
