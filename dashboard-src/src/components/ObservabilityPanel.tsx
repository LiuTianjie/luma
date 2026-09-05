import { useEffect, useMemo, useState } from "react";
import { localizeState } from "../i18n";
import { fetchMetricsHistory } from "../metricsApi";
import type { ActualResourceValues, DashboardNode, DashboardService, Lang, MetricsHistoryPayload, ResourceValues } from "../types";
import { Badge, StatePill } from "./ui";
import { TrendChart } from "./charts";
import { ServiceLogsModal } from "./ServiceLogsModal";
import "./ObservabilityPanel.css";
import { useRouter, useSearchParams } from "../router";

const HISTORY_WINDOWS = [900, 3600, 21600];
const HISTORY_REFRESH_MS = 30000;

type HistoryTarget = { kind: "node" | "service"; name: string };

function historyKey(kind: "node" | "service", name: string) {
  return `${kind}:${name}`;
}

type HistoryState = { payload?: MetricsHistoryPayload; error?: string };

/** Schedule the next batch only after completion; abort on navigation or timeout. */
function useMetricsHistories(token: string, targets: HistoryTarget[], historyWindow: number) {
  const [refresh, setRefresh] = useState(0);
  useEffect(() => { const reload = () => setRefresh((n) => n + 1); window.addEventListener("luma:refresh", reload); return () => window.removeEventListener("luma:refresh", reload); }, []);
  const [histories, setHistories] = useState<Record<string, HistoryState>>({});
  const signature = JSON.stringify(targets.map((item) => [item.kind, item.name]).sort());

  useEffect(() => {
    setHistories({});
    if (!token || !targets.length) return;
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController;
    const load = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      const entries = await Promise.all(targets.map(async (target) => {
        const key = historyKey(target.kind, target.name);
        try {
          const payload = await fetchMetricsHistory({ token, ...target, window: historyWindow, signal: controller.signal });
          return [key, { payload }] as const;
        } catch (error) {
          return [key, { error: error instanceof Error ? error.message : String(error) }] as const;
        }
      }));
      window.clearTimeout(timeout);
      if (cancelled) return;
      setHistories((previous) => Object.fromEntries(entries.map(([key, next]) => [
        key, "error" in next ? { payload: previous[key]?.payload, error: next.error } : next,
      ])));
      timer = window.setTimeout(() => void load(), HISTORY_REFRESH_MS);
    };
    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearTimeout(timer);
    };
    // The signature includes every target; do not restart on dashboard object refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, signature, historyWindow, refresh]);
  return histories;
}

function HistoryStatus({ lang, state }: { lang: Lang; state?: HistoryState }) {
  if (!state) return <small className="history-status">{lang === "zh" ? "正在读取历史…" : "Loading history…"}</small>;
  if (state.error && !state.payload) return <small className="history-status history-status-warning" title={state.error}>{lang === "zh" ? "历史获取失败，稍后自动重试" : "History unavailable; retrying automatically"}</small>;
  const points = Object.values(state.payload?.series || {}).flat();
  const latest = state.payload?.latestSampleAt ?? (points.length ? Math.max(...points.map(([ts]) => ts)) : null);
  const earliest = state.payload?.availableFrom ?? (points.length ? Math.min(...points.map(([ts]) => ts)) : null);
  const stale = latest !== null && Date.now() / 1000 - latest > 180;
  const format = (ts: number) => new Date(ts * 1000).toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  return <small className={`history-status${state.error || stale ? " history-status-warning" : ""}`} title={state.error}>
    {state.error ? (lang === "zh" ? "刷新失败，保留上次数据 · " : "Refresh failed; showing previous data · ") : ""}
    {latest === null ? (lang === "zh" ? "当前窗口暂无采样" : "No samples in this window") : <>
      {stale ? (lang === "zh" ? "采样已过期 · " : "Stale samples · ") : ""}
      {format(earliest!)}–{format(latest)}
      {state.payload?.sampleIntervalSeconds ? ` · ${state.payload.sampleIntervalSeconds}s ${lang === "zh" ? "聚合" : "buckets"}` : ""}
    </>}
  </small>;
}


function formatBytes(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let next = value;
  let unit = 0;
  while (next >= 1024 && unit < units.length - 1) {
    next /= 1024;
    unit += 1;
  }
  return `${next >= 10 ? next.toFixed(0) : next.toFixed(1)} ${units[unit]}`;
}

function formatPercent(value?: number) {
  return typeof value === "number" ? `${value.toFixed(value >= 10 ? 0 : 1)}%` : "-";
}

function resourceText(resources?: ResourceValues) {
  if (!resources) return "-";
  const parts = [];
  if (resources.cpus) parts.push(`${resources.cpus} CPU`);
  if (resources.memoryBytes) parts.push(formatBytes(resources.memoryBytes));
  return parts.join(" / ") || "-";
}

function actualText(resources?: ActualResourceValues) {
  if (!resources || !resources.containers) return "-";
  return `${formatPercent(resources.cpuPercent)} CPU / ${formatBytes(resources.memoryUsageBytes)} / ${resources.containers} ctr`;
}


function nodeTitle(node: DashboardNode) {
  return node.displayName || node.name || "-";
}

function nodeMeta(node: DashboardNode) {
  return [node.region, node.role, node.agentStatus].filter((item): item is string => Boolean(item));
}

function serviceTitle(service: DashboardService) {
  return service.stack ? `${service.stack}/${service.name || "-"}` : service.name || service.fullName || "-";
}

export function ObservabilityPanel({ lang, token, nodes, services, mode = "metrics" }: {
  lang: Lang; token: string; nodes: DashboardNode[]; services: DashboardService[]; mode?: "metrics" | "logs";
}) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const query = useSearchParams();
  const kind = query.get("kind") === "service" || !nodes.length ? "service" : "node";
  const available = kind === "node" ? nodes.flatMap((node) => node.name ? [{ name: node.name, label: nodeTitle(node) }] : []) : services.flatMap((service) => service.fullName ? [{ name: service.fullName, label: serviceTitle(service) }] : []);
  const requested = query.get("target") || "";
  const selected = requested || available[0]?.name || "";
  const valid = available.some((item) => item.name === selected);
  const rawWindow = Number(query.get("window") || 900);
  const historyWindow = HISTORY_WINDOWS.includes(rawWindow) ? rawWindow : 900;
  const targets = useMemo<HistoryTarget[]>(() => mode === "metrics" && selected && valid ? [{ kind, name: selected }] : [], [mode, kind, selected, valid]);
  const histories = useMetricsHistories(token, targets, historyWindow);
  const history = histories[historyKey(kind, selected)];
  const retention = history?.payload?.retentionSeconds;
  const series = history?.payload?.series || {};
  const node = nodes.find((item) => item.name === selected);
  const service = services.find((item) => item.fullName === selected);
  const update = (values: Record<string, string>) => { const next = new URLSearchParams(query); Object.entries(values).forEach(([key, value]) => { if (value) next.set(key, value); else next.delete(key); }); navigate(`${path}?${next}`); };
  useEffect(() => {
    if (mode !== "metrics" || !retention || historyWindow <= retention) return;
    const next = new URLSearchParams(query);
    next.set("window", String([...HISTORY_WINDOWS].reverse().find((value) => value <= retention) || 900));
    if (next.get("window") !== String(historyWindow)) navigate(`${path}?${next}`, { replace: true });
  }, [mode, retention, historyWindow, query, path, navigate]);
  if (mode === "logs") return <ServiceLogsModal inline lang={lang} token={token} services={services} initialServiceName={query.get("target") || query.get("service") || ""} />;
  return <>
    <div className="history-toolbar">
      <label>{zh ? "对象类型" : "Object type"}<select value={kind} onChange={(event) => update({ kind: event.target.value, target: "" })}>{nodes.length > 0 && <option value="node">{zh ? "节点" : "Node"}</option>}<option value="service">{zh ? "服务" : "Service"}</option></select></label>
      <label>{zh ? "监测对象" : "Target"}<select value={selected} onChange={(event) => update({ target: event.target.value })}>{!valid && <option value={selected}>{selected || (zh ? "暂无对象" : "No targets")}</option>}{available.map((item) => <option key={item.name} value={item.name}>{item.label}</option>)}</select></label>
      <label>{zh ? "时间范围" : "Time range"}<select value={historyWindow} onChange={(event) => update({ window: event.target.value })}>{HISTORY_WINDOWS.map((value) => <option value={value} key={value} disabled={typeof retention === "number" && value > retention}>{value < 3600 ? `${value / 60} min` : `${value / 3600} h`}</option>)}</select></label>
      <small>{zh ? "每 30 秒刷新 · 采样断点不会插值" : "Refreshes every 30s · sample gaps remain visible"}</small>
    </div>
    {!valid ? <div className="panel">{zh ? "该对象不存在或当前没有可用对象，请重新选择。" : "This target is unavailable. Select another target."}</div> : <section className="panel">
      <div className="panel-heading"><h2>{kind === "node" && node ? nodeTitle(node) : service ? serviceTitle(service) : selected}</h2><StatePill label={localizeState(lang, node?.state || service?.status)} value={node?.state || service?.status} /></div>
      {kind === "node" && node ? <><p>{nodeMeta(node).join(" · ")}</p><div className="alert-summary"><span>CPU <strong>{formatPercent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</strong></span><span>{zh ? "内存" : "Memory"}<strong>{formatPercent(node.metrics?.memoryUsedPercent)}</strong><small>{formatBytes(node.metrics?.memoryTotalBytes || node.capacity?.memoryBytes)}</small></span><span>{zh ? "磁盘可用" : "Disk available"}<strong>{formatBytes(node.metrics?.diskAvailableBytes)}</strong><small>{node.metrics?.metricsPath}</small></span></div></> : service && <><p>{zh ? "实际用量" : "Actual usage"} <Badge value={actualText(service.resources?.actual)} /> · {zh ? "限制" : "Limit"} {resourceText(service.resources?.limits)} · {zh ? "预留" : "Reservation"} {resourceText(service.resources?.reservations)}</p><div className="badge-group">{(service.tasks || []).map((task, index) => <StatePill key={task.id || index} label={`${task.node || "—"} ${localizeState(lang, task.state)} ${formatPercent(task.cpuPercent)}`} value={task.state} />)}</div></>}
      <HistoryStatus lang={lang} state={history} />
      {retention && <small>{zh ? "历史保留" : "History retention"} · {Math.round(retention / 60)} min</small>}
      <div className="service-history-charts">{(kind === "node" ? [["cpuPercent", "CPU", formatPercent], ["memoryUsedPercent", zh ? "内存" : "Memory", formatPercent], ["diskUsedPercent", zh ? "磁盘" : "Disk", formatPercent], ["inodesUsedPercent", "Inodes", formatPercent]] : [["cpuPercent", "CPU", formatPercent], ["memoryUsageBytes", zh ? "内存" : "Memory", formatBytes]]).map(([key, label, format]) => <div key={key as string}><h3>{label as string}</h3><TrendChart maxGapSeconds={90} points={series[key as string] || []} format={format as (value: number) => string} height={180} emptyLabel={zh ? "等待至少两个采样点" : "Waiting for two samples"} /></div>)}</div>
      <small>{zh ? "180 秒未更新的节点不计入服务汇总；超过 90 秒的采样间隔显示为断点。" : "Service totals exclude nodes stale for 180s. Gaps over 90s break the line."}</small>
      {kind === "service" && <p><button className="ghost" onClick={() => navigate(`/observe/logs?target=${encodeURIComponent(selected)}`)}>{zh ? "查看此服务日志" : "View service logs"}</button></p>}
    </section>}
  </>;
}
