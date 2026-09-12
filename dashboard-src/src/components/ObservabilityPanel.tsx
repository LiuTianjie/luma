import { useEffect, useMemo } from "react";
import { localizeState } from "../i18n";
import { historyKey, type HistoryState, type HistoryTarget } from "../metricsApi";
import { useMetricsHistories } from "../useMetricsHistories";
import type { ActualResourceValues, DashboardNode, DashboardService, Lang, ResourceValues } from "../types";
import { Badge, SelectControl, StatePill } from "./primitives";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { TrendChart } from "./charts";
import "./ObservabilityPanel.css";
import { useRouter, useSearchParams } from "../router";

const HISTORY_WINDOWS = [900, 3600, 21600];

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
  return <div className="metrics-workspace">
    <div className="flex flex-wrap items-end gap-3">
      <Field className="w-44">
        <FieldLabel>{zh ? "对象类型" : "Object type"}</FieldLabel>
        <SelectControl
          className="min-w-0"
          value={kind}
          onChange={(value) => update({ kind: value, target: "" })}
          options={[
            ...(nodes.length > 0 ? [{ value: "node", label: zh ? "节点" : "Node" }] : []),
            { value: "service", label: zh ? "服务" : "Service" },
          ]}
        />
      </Field>
      <Field className="min-w-56 flex-1">
        <FieldLabel>{zh ? "监测对象" : "Target"}</FieldLabel>
        <SelectControl
          className="min-w-0"
          value={selected}
          onChange={(value) => update({ target: value })}
          options={[
            ...(!valid ? [{ value: selected, label: selected || (zh ? "暂无对象" : "No targets") }] : []),
            ...available.map((item) => ({ value: item.name, label: item.label })),
          ]}
        />
      </Field>
      <Field className="w-40">
        <FieldLabel>{zh ? "时间范围" : "Time range"}</FieldLabel>
        <SelectControl
          className="min-w-0"
          value={String(historyWindow)}
          onChange={(value) => update({ window: value })}
          options={HISTORY_WINDOWS.map((value) => ({
            value: String(value),
            label: value < 3600 ? `${value / 60} min` : `${value / 3600} h`,
            disabled: typeof retention === "number" && value > retention,
          }))}
        />
      </Field>
      <p className="pb-2 text-xs text-muted-foreground">{zh ? "每 30 秒刷新 · 采样断点不会插值" : "Refreshes every 30s · sample gaps remain visible"}</p>
    </div>
    {!valid ? <div className="panel">{zh ? "该对象不存在或当前没有可用对象，请重新选择。" : "This target is unavailable. Select another target."}</div> : <section className="panel">
      <div className="panel-heading"><h2>{kind === "node" && node ? nodeTitle(node) : service ? serviceTitle(service) : selected}</h2><StatePill label={localizeState(lang, node?.state || service?.status)} value={node?.state || service?.status} /></div>
      {kind === "node" && node ? <div className="node-snapshot">
        <div className="node-snapshot-heading">
          <span>{nodeMeta(node).map((value) => localizeState(lang, value)).join(" · ")}</span>
          <span>{zh ? "当前快照" : "Current snapshot"}</span>
        </div>
        <dl className="node-snapshot-grid" aria-label={zh ? "当前快照" : "Current snapshot"}>
          <div>
            <dt>CPU</dt>
            <dd>{formatPercent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</dd>
            <small>{zh ? "当前使用率" : "Current utilization"}</small>
          </div>
          <div>
            <dt>{zh ? "内存" : "Memory"}</dt>
            <dd>{formatPercent(node.metrics?.memoryUsedPercent)}</dd>
            <small>{zh ? "总量 " : "Total "}{formatBytes(node.metrics?.memoryTotalBytes ?? node.capacity?.memoryBytes)}</small>
          </div>
          <div>
            <dt>{zh ? "磁盘可用" : "Disk available"}</dt>
            <dd>{formatBytes(node.metrics?.diskAvailableBytes)}</dd>
            <small title={node.metrics?.metricsPath}>{node.metrics?.metricsPath || "—"}</small>
          </div>
        </dl>
      </div> : service && <><p>{zh ? "实际用量" : "Actual usage"} <Badge value={actualText(service.resources?.actual)} /> · {zh ? "限制" : "Limit"} {resourceText(service.resources?.limits)} · {zh ? "预留" : "Reservation"} {resourceText(service.resources?.reservations)}</p><div className="badge-group">{(service.tasks || []).map((task, index) => <StatePill key={task.id || index} label={`${task.node || "—"} ${localizeState(lang, task.state)} ${formatPercent(task.cpuPercent)}`} value={task.state} />)}</div></>}
      <div className="metrics-history-meta">
        <HistoryStatus lang={lang} state={history} />
        {retention ? <small>{zh ? "历史保留" : "History retention"} · {Math.round(retention / 60)} min</small> : null}
      </div>
      <div className="service-history-charts">{(kind === "node" ? [["cpuPercent", "CPU", formatPercent], ["memoryUsedPercent", zh ? "内存" : "Memory", formatPercent], ["diskUsedPercent", zh ? "磁盘" : "Disk", formatPercent], ["inodesUsedPercent", "Inodes", formatPercent]] : [["cpuPercent", "CPU", formatPercent], ["memoryUsageBytes", zh ? "内存" : "Memory", formatBytes]]).map(([key, label, formatter]) => {
        const samples = series[key as string] || [];
        const format = formatter as (value: number) => string;
        return <div className="metric-history-card" key={key as string}>
          <div className="metric-history-value"><h3>{label as string}</h3><strong>{samples.length ? format(samples[samples.length - 1][1]) : "—"}</strong><small>{zh ? "最后采样" : "Latest sample"}</small></div>
          <TrendChart maxGapSeconds={90} points={samples} format={format} label={label as string} locale={zh ? "zh-CN" : "en-US"} height={156} emptyLabel={zh ? "等待至少两个采样点" : "Waiting for two samples"} />
        </div>;
      })}</div>
      <small>{zh ? "180 秒未更新的节点不计入服务汇总；超过 90 秒的采样间隔显示为断点。" : "Service totals exclude nodes stale for 180s. Gaps over 90s break the line."}</small>
      {kind === "service" && <p><Button type="button" variant="outline" size="sm" onClick={() => navigate(`/observe/logs?app=${encodeURIComponent(service?.stack || selected)}`)}>{zh ? "查看此服务日志" : "View service logs"}</Button></p>}
    </section>}
  </div>;
}
