import { localizeState } from "../i18n";
import type { DashboardNode, DashboardService, Lang } from "../types";

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
  const regions = regionsFor(nodes);
  const workloads = workloadCounts(services || []);
  const readyNodes = nodes.filter((node) => nodeHealth(node) === "good").length;
  const terminalNodes = nodes.filter(terminalReady).length;
  const pressuredNodes = nodes.filter((node) => pressureOf(node) >= 80).length;
  const maxPressure = nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);

  return (
    <section className="node-fleet-map" aria-label={lang === "zh" ? "节点态势" : "Node fleet"}>
      <div className="node-fleet-header">
        <div>
          <p className="eyebrow">{lang === "zh" ? "节点态势" : "Node posture"}</p>
          <h2>{lang === "zh" ? "服务器健康矩阵" : "Server health matrix"}</h2>
        </div>
        <div className="node-fleet-kpis" aria-label="Node summary">
          <span><b>{readyNodes}</b><small>{lang === "zh" ? "ready" : "ready"}</small></span>
          <span><b>{terminalNodes}</b><small>terminal</small></span>
          <span><b>{pressuredNodes}</b><small>{lang === "zh" ? "高负载" : "hot"}</small></span>
          <span><b>{formatPercent(maxPressure)}</b><small>{lang === "zh" ? "峰值" : "peak"}</small></span>
        </div>
      </div>

      <div className="node-region-grid">
        {regions.map((group) => {
          const groupReady = group.nodes.filter((node) => nodeHealth(node) === "good").length;
          const groupPressure = group.nodes.reduce((max, node) => Math.max(max, pressureOf(node)), 0);
          return (
            <article className="node-region-band" key={group.region}>
              <header>
                <div>
                  <strong>{group.region}</strong>
                  <small>{groupReady}/{group.nodes.length} {lang === "zh" ? "在线" : "online"}</small>
                </div>
                <span className={`region-pressure ${groupPressure >= 80 ? "hot" : groupPressure >= 60 ? "busy" : "steady"}`}>
                  {pressureLabel(groupPressure, lang)}
                </span>
              </header>
              <div className="node-tile-grid">
                {group.nodes.map((node, index) => {
                  const metrics = node.metrics || {};
                  const capacity = node.capacity || {};
                  const nodeName = node.name || "-";
                  const cpu = clampPercent(metrics.cpuPercent ?? metrics.loadPercent);
                  const memory = clampPercent(metrics.memoryUsedPercent);
                  const health = nodeHealth(node);
                  const hasTerminal = terminalReady(node);
                  const workload = workloads.get(nodeName) || { services: 0, tasks: 0 };
                  return (
                    <article
                      className={`node-tile ${health}`}
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
                      <span className="node-tile-head">
                        <span>
                          <i aria-hidden="true" />
                          <strong>{nodeName}</strong>
                        </span>
                        <b>{nodeStateLabel(node, lang)}</b>
                      </span>
                      <span className="node-tile-meta">
                        {[node.role, node.agentOs, node.availability].filter(Boolean).join(" / ") || "-"}
                      </span>
                      <span className="node-meter-pair">
                        <span>
                          <small>CPU</small>
                          <em>{formatPercent(metrics.cpuPercent ?? metrics.loadPercent)}</em>
                        </span>
                        <span className="node-meter"><span style={{ width: `${cpu}%` }} /></span>
                      </span>
                      <span className="node-meter-pair">
                        <span>
                          <small>MEM</small>
                          <em>{formatPercent(metrics.memoryUsedPercent)}</em>
                        </span>
                        <span className="node-meter"><span style={{ width: `${memory}%` }} /></span>
                      </span>
                      <span className="node-tile-foot">
                        <span>{workload.services} svc · {workload.tasks} task</span>
                        <span>{formatBytes(metrics.memoryTotalBytes || capacity.memoryBytes)}</span>
                        <button
                          type="button"
                          className="node-terminal-button"
                          disabled={!hasTerminal || !onTerminal}
                          title={hasTerminal ? "Terminal" : terminalUnavailableLabel(node, lang)}
                          aria-label={hasTerminal ? `Terminal ${node.name || ""}` : terminalUnavailableLabel(node, lang)}
                          onClick={(event) => {
                            event.stopPropagation();
                            onTerminal?.(node);
                          }}
                        >
                          &gt;_
                        </button>
                      </span>
                    </article>
                  );
                })}
              </div>
            </article>
          );
        })}
      </div>

      {!nodes.length ? (
        <div className="node-fleet-empty">{lang === "zh" ? "暂无节点" : "No nodes"}</div>
      ) : null}
    </section>
  );
}
