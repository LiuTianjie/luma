import { Activity, Cpu, HardDrive, MapPin, MemoryStick, Server, SquareTerminal, TerminalSquare } from "lucide-react";
import { localizeState } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

function clampPercent(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function formatPercent(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
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

function pressureLabel(value: number, lang: Lang) {
  if (value >= 85) return lang === "zh" ? "高压" : "hot";
  if (value >= 65) return lang === "zh" ? "偏高" : "busy";
  if (value > 0) return lang === "zh" ? "平稳" : "steady";
  return lang === "zh" ? "无数据" : "no data";
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

function Meter({ label, value, icon: Icon }: { label: string; value: number; icon: typeof Cpu }) {
  return (
    <div className="flex items-center gap-2">
      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
          <span>{label}</span>
          <span className="tabular-nums">{formatPercent(value)}</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary" style={{ width: `${clampPercent(value)}%` }} />
        </div>
      </div>
    </div>
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
  const pressuredNodes = nodes.filter((node) => pressureOf(node) >= 80).length;
  const maxPressure = nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);

  return (
    <section className="flex flex-col gap-6" aria-label={zh ? "节点态势" : "Node fleet"}>
      <div className="flex flex-wrap items-center gap-2">
        {[
          { icon: Server, value: readyNodes, label: "ready" },
          { icon: TerminalSquare, value: terminalNodes, label: "terminal" },
          { icon: Activity, value: pressuredNodes, label: zh ? "高负载" : "hot" },
          { icon: Cpu, value: formatPercent(maxPressure), label: zh ? "峰值" : "peak" },
        ].map((item) => (
          <span key={item.label} className="inline-flex items-center gap-1.5 rounded-lg border bg-card px-2.5 py-1.5 text-xs">
            <item.icon className="size-3.5 text-muted-foreground" />
            <strong className="tabular-nums">{item.value}</strong>
            <span className="text-muted-foreground">{item.label}</span>
          </span>
        ))}
      </div>

      {regions.map((group) => {
        const groupReady = group.nodes.filter((node) => nodeHealth(node) === "good").length;
        const groupPressure = group.nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);
        const pressureTone = groupPressure >= 80 ? "destructive" : groupPressure >= 60 ? "warning" : "success";
        return (
          <section className="flex flex-col gap-3" key={group.region}>
            <div className="flex items-center gap-2">
              <MapPin className="size-4 text-muted-foreground" />
              <strong className="text-sm">{group.region}</strong>
              <span className="text-xs text-muted-foreground">{groupReady}/{group.nodes.length} {zh ? "在线" : "online"}</span>
              <Badge variant={pressureTone} className="ml-auto">{pressureLabel(groupPressure, lang)}</Badge>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {group.nodes.map((node, index) => {
                const metrics = node.metrics || {};
                const capacity = node.capacity || {};
                const nodeName = node.name || "-";
                const health = nodeHealth(node);
                const hasTerminal = terminalReady(node);
                const workload = workloads.get(nodeName) || { services: 0, tasks: 0 };
                const cpu = clampPercent(metrics.cpuPercent ?? metrics.loadPercent);
                const memory = clampPercent(metrics.memoryUsedPercent);
                return (
                  <article
                    className="flex cursor-pointer flex-col gap-3 rounded-xl border bg-card p-3 text-left shadow-xs transition-colors hover:bg-muted/40"
                    key={`${node.name || "node"}-${index}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => onSelect(node)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelect(node);
                      }
                    }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <Server className="size-4 shrink-0 text-muted-foreground" />
                        <strong className="truncate text-sm">{nodeName}</strong>
                      </div>
                      <Badge variant={health === "good" ? "success" : health === "danger" ? "destructive" : "warning"}>
                        {nodeStateLabel(node, lang)}
                      </Badge>
                    </div>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                      {node.role ? <span className="inline-flex items-center gap-1"><Server className="size-3" />{node.role}</span> : null}
                      {node.agentOs ? <span className="inline-flex items-center gap-1"><HardDrive className="size-3" />{node.agentOs}</span> : null}
                      {node.availability ? <span>{node.availability}</span> : null}
                    </div>
                    <Meter label="CPU" value={cpu} icon={Cpu} />
                    <Meter label="MEM" value={memory} icon={MemoryStick} />
                    <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
                      <span>{workload.services} svc · {workload.tasks} task</span>
                      <span className="ml-auto tabular-nums">{formatBytes(metrics.memoryTotalBytes || capacity.memoryBytes)}</span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={!hasTerminal || !onTerminal}
                        title={hasTerminal ? "Terminal" : terminalUnavailableLabel(node, lang)}
                        aria-label={hasTerminal ? `Terminal ${node.name || ""}` : terminalUnavailableLabel(node, lang)}
                        onClick={(event) => {
                          event.stopPropagation();
                          onTerminal?.(node);
                        }}
                      >
                        <SquareTerminal />
                      </Button>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        );
      })}

      {!nodes.length ? (
        <p className="text-sm text-muted-foreground">{zh ? "暂无节点" : "No nodes"}</p>
      ) : null}
    </section>
  );
}
