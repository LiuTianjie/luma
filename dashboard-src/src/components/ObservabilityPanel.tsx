import { useEffect, useMemo, useState } from "react";
import { FileText } from "lucide-react";
import { localizeState, t } from "../i18n";
import { fetchMetricsHistory } from "../metricsApi";
import type { ActualResourceValues, DashboardNode, DashboardService, Lang, MetricSeries, ResourceValues } from "../types";
import { Badge, BadgeGroup, CodeCell, StatePill } from "./ui";
import { Sparkline, TrendChart } from "./charts";
import { ServiceLogsModal } from "./ServiceLogsModal";

const HISTORY_WINDOW_SECONDS = 3600;
const HISTORY_REFRESH_MS = 30000;

type HistoryTarget = { kind: "node" | "service"; name: string };

function historyKey(kind: "node" | "service", name: string) {
  return `${kind}:${name}`;
}

/** Fetch trend history for a set of node/service targets, refreshed on the
 *  agent sample cadence. One in-flight batch at a time; aborts on unmount. */
function useMetricsHistories(token: string, targets: HistoryTarget[]) {
  const [histories, setHistories] = useState<Record<string, MetricSeries>>({});
  const signature = targets.map((item) => historyKey(item.kind, item.name)).sort().join(",");

  useEffect(() => {
    if (!token || !targets.length) {
      setHistories({});
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      const entries = await Promise.all(
        targets.map(async (target) => {
          try {
            const payload = await fetchMetricsHistory({
              token,
              kind: target.kind,
              name: target.name,
              window: HISTORY_WINDOW_SECONDS,
              signal: controller.signal,
            });
            return [historyKey(target.kind, target.name), payload.series || {}] as const;
          } catch {
            return [historyKey(target.kind, target.name), {} as MetricSeries] as const;
          }
        }),
      );
      if (!cancelled) setHistories(Object.fromEntries(entries));
    };
    void load();
    const timer = window.setInterval(() => void load(), HISTORY_REFRESH_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, signature]);

  return histories;
}


function formatBytes(value?: number) {
  if (!value) return "-";
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
    const items: HistoryTarget[] = nodes
      .map((node) => node.name)
      .filter((name): name is string => Boolean(name))
      .map((name) => ({ kind: "node" as const, name }));
    if (selectedService) items.push({ kind: "service", name: selectedService });
    return items;
  }, [nodes, selectedService]);
  const histories = useMetricsHistories(token, historyTargets);

  const selected = services.find((service) => service.fullName === selectedService);

  const openServiceLogs = (service: DashboardService) => {
    const fullName = service.fullName || "";
    if (!fullName) return;
    setSelectedApp(appKey(service));
    setSelectedService(fullName);
    setLogsModalService(fullName);
  };

  return (
    <>
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
            const nodeCpuHistory = node.name ? histories[historyKey("node", node.name)]?.cpuPercent || [] : [];
            return (
              <button className="node-metric-row" type="button" key={`${node.name || "node"}-${index}`}>
                <span>
                  <strong>{node.name || "-"}</strong>
                  <small>{[node.region, node.role, node.agentStatus].filter(Boolean).join(" / ") || "-"}</small>
                </span>
                <span className="metric-pair">
                  <b>CPU</b>
                  <strong>{formatPercent(metrics.cpuPercent ?? metrics.loadPercent)}</strong>
                  <Sparkline points={nodeCpuHistory} range={{ min: 0, max: 100 }} />
                </span>
                <span className="metric-pair">
                  <b>Memory</b>
                  <strong>{formatPercent(metrics.memoryUsedPercent)}</strong>
                  <small>{formatBytes(metrics.memoryTotalBytes || capacity.memoryBytes)}</small>
                </span>
                <StatePill label={localizeState(lang, node.state)} value={node.state} />
              </button>
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
                  <th>Actual</th>
                  <th>Declared</th>
                  <th>Tasks</th>
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
          {selected ? (
            <div className="service-trends">
              <div className="service-trend">
                <div className="service-trend-head">
                  <span>{serviceTitle(selected)} · CPU</span>
                  <span>{formatPercent(selected.resources?.actual?.cpuPercent)}</span>
                </div>
                <TrendChart
                  points={histories[historyKey("service", selectedService)]?.cpuPercent || []}
                  color="var(--blue)"
                  format={(v) => `${v.toFixed(0)}%`}
                  emptyLabel={lang === "zh" ? "暂无趋势数据" : "no trend data"}
                />
              </div>
              <div className="service-trend">
                <div className="service-trend-head">
                  <span>{serviceTitle(selected)} · {lang === "zh" ? "内存" : "Memory"}</span>
                  <span>{formatBytes(selected.resources?.actual?.memoryUsageBytes)}</span>
                </div>
                <TrendChart
                  points={histories[historyKey("service", selectedService)]?.memoryUsageBytes || []}
                  color="var(--orange)"
                  format={(v) => formatBytes(v)}
                  emptyLabel={lang === "zh" ? "暂无趋势数据" : "no trend data"}
                />
              </div>
            </div>
          ) : null}
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
