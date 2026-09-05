import { useEffect, useMemo, useState } from "react";
import { FileText } from "lucide-react";
import { localizeState, t } from "../i18n";
import { fetchMetricsHistory } from "../metricsApi";
import type { ActualResourceValues, DashboardNode, DashboardService, Lang, MetricsHistoryPayload, ResourceValues } from "../types";
import { Badge, BadgeGroup, CodeCell, StatePill } from "./ui";
import { Sparkline, TrendChart } from "./charts";
import { ServiceLogsModal } from "./ServiceLogsModal";
import "./ObservabilityPanel.css";

const HISTORY_WINDOWS = [900, 3600, 21600];
const HISTORY_REFRESH_MS = 30000;

type HistoryTarget = { kind: "node" | "service"; name: string };

function historyKey(kind: "node" | "service", name: string) {
  return `${kind}:${name}`;
}

type HistoryState = { payload?: MetricsHistoryPayload; error?: string };

/** Schedule the next batch only after completion; abort on navigation or timeout. */
function useMetricsHistories(token: string, targets: HistoryTarget[], historyWindow: number) {
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
  }, [token, signature, historyWindow]);
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

function appKey(service: DashboardService) {
  return service.stack || service.fullName || service.name || "-";
}

export function ObservabilityPanel({
  lang,
  token,
  nodes,
  services,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  services: DashboardService[];
}) {
  const applications = useMemo(() => {
    const groups = new Map<string, DashboardService[]>();
    for (const service of services.filter((item) => item.fullName)) {
      const key = appKey(service);
      groups.set(key, [...(groups.get(key) || []), service]);
    }
    return Array.from(groups.entries()).map(([key, group]) => ({ key, services: group.sort((a, b) => serviceTitle(a).localeCompare(serviceTitle(b))) }));
  }, [services]);

  const [selectedApp, setSelectedApp] = useState(() => applications[0]?.key || "");
  const appServices = applications.find((item) => item.key === selectedApp)?.services || [];
  const [selectedService, setSelectedService] = useState(() => appServices[0]?.fullName || "");
  const [logsModalService, setLogsModalService] = useState("");
  const [historyWindow, setHistoryWindow] = useState(900);

  useEffect(() => {
    if (!applications.length) {
      setSelectedApp("");
      return;
    }
    if (!selectedApp || !applications.some((item) => item.key === selectedApp)) {
      setSelectedApp(applications[0].key);
    }
  }, [applications, selectedApp]);

  useEffect(() => {
    if (!appServices.length) {
      setSelectedService("");
      return;
    }
    if (!selectedService || !appServices.some((service) => service.fullName === selectedService)) {
      setSelectedService(appServices[0].fullName || "");
    }
  }, [appServices, selectedService]);

  const historyTargets = useMemo<HistoryTarget[]>(() => {
    const targets: HistoryTarget[] = nodes
      .map((node) => node.name)
      .filter((name): name is string => Boolean(name))
      .map((name) => ({ kind: "node" as const, name }));
    if (selectedService) targets.push({ kind: "service", name: selectedService });
    return targets;
  }, [nodes, selectedService]);
  const histories = useMetricsHistories(token, historyTargets, historyWindow);
  const retention = Math.min(...Object.values(histories).map((item) => item.payload?.retentionSeconds).filter((value): value is number => typeof value === "number"));
  const supportedWindows = HISTORY_WINDOWS.filter((value) => value <= (Number.isFinite(retention) ? retention : 900));
  const serviceHistory = histories[historyKey("service", selectedService)];
  const windowLabel = (seconds: number) => seconds < 3600 ? `${seconds / 60} ${lang === "zh" ? "分钟" : "min"}` : `${seconds / 3600} ${lang === "zh" ? "小时" : "h"}`;

  useEffect(() => {
    if (Number.isFinite(retention) && historyWindow > retention) {
      setHistoryWindow([...HISTORY_WINDOWS].reverse().find((value) => value <= retention) || 900);
    }
  }, [retention, historyWindow]);

  const openServiceLogs = (service: DashboardService) => {
    const fullName = service.fullName || "";
    if (!fullName) return;
    setSelectedApp(appKey(service));
    setSelectedService(fullName);
    setLogsModalService(fullName);
  };

  return (
    <>
      <div className="history-toolbar">
        <label>{lang === "zh" ? "资源趋势窗口" : "Resource history window"}
          <select value={historyWindow} onChange={(event) => setHistoryWindow(Number(event.target.value))}>
            {Array.from(new Set([...supportedWindows, historyWindow])).sort((a, b) => a - b).map((value) => <option value={value} key={value}>{windowLabel(value)}</option>)}
          </select>
        </label>
        <small>{Number.isFinite(retention) ? `${lang === "zh" ? "最多保留" : "Retention"} ${windowLabel(retention)} · ` : ""}{lang === "zh" ? "自动刷新 · 每项展示实际采样范围" : "Auto refresh · actual sample range shown per target"}</small>
      </div>
      <section className="observability-grid">
        <article className="panel node-metrics-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">{t(lang, "nodesEyebrow")}</p>
            <h2>{lang === "zh" ? "节点资源" : "Node Resources"}</h2>
          </div>
          <span>{nodes.length}</span>
        </div>
        <div className="node-metrics-list">
          {nodes.map((node, index) => {
            const metrics = node.metrics || {};
            const capacity = node.capacity || {};
            const nodeHistory = node.name ? histories[historyKey("node", node.name)] : undefined;
            const nodeSeries = nodeHistory?.payload?.series || {};
            const nodeCpuHistory = nodeSeries.cpuPercent || [];
            const title = nodeTitle(node);
            const meta = nodeMeta(node);
            return (
              <article className="node-metric-row" key={`${node.name || "node"}-${index}`}>
                <div className="node-identity">
                  <div className="node-title-line">
                    <strong title={title}>{title}</strong>
                    <StatePill label={localizeState(lang, node.state)} value={node.state} />
                  </div>
                  <div className="node-meta" aria-label={lang === "zh" ? "节点元数据" : "Node metadata"}>
                    {meta.length ? meta.map((item) => <span key={item}>{item}</span>) : <span>-</span>}
                  </div>
                </div>
                <div className="node-resource-grid">
                  <div className="metric-pair">
                    <span className="metric-head">
                      <b>CPU</b>
                      <strong>{formatPercent(metrics.cpuPercent ?? metrics.loadPercent)}</strong>
                    </span>
                    <Sparkline maxGapSeconds={90} points={nodeCpuHistory} range={{ min: 0, max: 100 }} />
                  </div>
                  <div className="metric-pair">
                    <span className="metric-head">
                      <b>{lang === "zh" ? "内存" : "Memory"}</b>
                      <strong>{formatPercent(metrics.memoryUsedPercent)}</strong>
                    </span>
                    <Sparkline maxGapSeconds={90} points={nodeSeries.memoryUsedPercent || []} range={{ min: 0, max: 100 }} />
                    <small>{formatBytes(metrics.memoryTotalBytes || capacity.memoryBytes)}</small>
                  </div>
                </div>
                <div className="node-storage-metrics" title={metrics.metricsPath || ""}>
                  <span>{lang === "zh" ? "磁盘" : "Disk"} <strong>{formatPercent(metrics.diskUsedPercent)}</strong></span>
                  <span>Inodes <strong>{formatPercent(metrics.inodesUsedPercent)}</strong></span>
                  <small>{typeof metrics.diskAvailableBytes === "number" ? `${lang === "zh" ? "可用" : "Available"} ${formatBytes(metrics.diskAvailableBytes)}` : (lang === "zh" ? "磁盘容量未上报" : "Disk capacity not reported")}{metrics.metricsPath ? ` · ${metrics.metricsPath}` : ""}</small>
                </div>
                <HistoryStatus lang={lang} state={nodeHistory} />
              </article>
            );
          })}
        </div>
        </article>

        <article className="panel service-runtime-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{t(lang, "servicesEyebrow")}</p>
              <h2>{lang === "zh" ? "服务运行态" : "Service Runtime"}</h2>
            </div>
            <span>{services.length}</span>
          </div>
          <div className="table-wrap">
            <table className="runtime-table">
              <thead>
                <tr>
                  <th>{t(lang, "service")}</th>
                  <th>{lang === "zh" ? "实际用量" : "Actual"}</th>
                  <th>{lang === "zh" ? "声明资源" : "Declared"}</th>
                  <th>{lang === "zh" ? "实例" : "Tasks"}</th>
                </tr>
              </thead>
              <tbody>
                {services.map((service, index) => {
                  const selectRuntime = () => {
                    setSelectedApp(appKey(service));
                    setSelectedService(service.fullName || "");
                  };
                  return (
                  <tr
                    aria-label={`${lang === "zh" ? "查看运行态" : "View runtime"}: ${serviceTitle(service)}`}
                    className={service.fullName && service.fullName === selectedService ? "is-selected" : undefined}
                    key={`${service.fullName || "service"}-${index}`}
                    onClick={selectRuntime}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectRuntime();
                      }
                    }}
                    role="button"
                    tabIndex={0}
                  >
                    <td>
                      <div className="runtime-service-cell">
                        <CodeCell value={serviceTitle(service)} />
                        <button
                          type="button"
                          className="ghost runtime-log-button"
                          disabled={!service.fullName}
                          onClick={(event) => {
                            event.stopPropagation();
                            openServiceLogs(service);
                          }}
                        >
                          <FileText size={15} aria-hidden="true" />
                          {lang === "zh" ? "查看日志" : "View logs"}
                        </button>
                      </div>
                    </td>
                    <td><Badge value={actualText(service.resources?.actual)} /></td>
                    <td>
                      <BadgeGroup>
                        <Badge value={`limit ${resourceText(service.resources?.limits)}`} />
                        <Badge value={`reserve ${resourceText(service.resources?.reservations)}`} />
                      </BadgeGroup>
                    </td>
                    <td>
                      <BadgeGroup>
                        {(service.tasks || []).slice(0, 4).map((task) => (
                          <StatePill
                            key={task.id || `${task.node}-${task.state}`}
                            label={`${task.node || "-"} ${localizeState(lang, task.state)} ${formatPercent(task.cpuPercent)}`}
                            value={task.state}
                          />
                        ))}
                        {(service.tasks || []).length > 4 ? <Badge value={`+${(service.tasks || []).length - 4}`} /> : null}
                      </BadgeGroup>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {selectedService ? <div className="service-history-detail">
            <div><strong>{selectedService}</strong><HistoryStatus lang={lang} state={serviceHistory} /></div>
            <div className="service-history-charts">
              <div><span>CPU</span><TrendChart maxGapSeconds={90} points={serviceHistory?.payload?.series?.cpuPercent || []} format={formatPercent} height={120} emptyLabel={lang === "zh" ? "等待至少两个 CPU 样本" : "Waiting for two CPU samples"} /></div>
              <div><span>{lang === "zh" ? "内存" : "Memory"}</span><TrendChart maxGapSeconds={90} points={serviceHistory?.payload?.series?.memoryUsageBytes || []} format={formatBytes} height={120} emptyLabel={lang === "zh" ? "等待至少两个内存样本" : "Waiting for two memory samples"} /></div>
            </div>
            <small>{lang === "zh" ? "服务曲线汇总有上报节点；180 秒未更新的节点不计入，90 秒以上采样间隔显示为断点。" : "Sums reporting nodes; contributions expire after 180s. Sample gaps over 90s break the line."}</small>
          </div> : null}
        </article>
      </section>
      {logsModalService ? (
        <ServiceLogsModal
          lang={lang}
          token={token}
          services={services}
          initialServiceName={logsModalService}
          onClose={() => setLogsModalService("")}
        />
      ) : null}
    </>
  );
}
