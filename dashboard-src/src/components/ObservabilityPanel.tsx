import { useCallback, useEffect, useMemo, useState } from "react";
import { localizeState, t } from "../i18n";
import type { ActualResourceValues, DashboardNode, DashboardService, Lang, ResourceValues } from "../types";
import { Badge, BadgeGroup, CodeCell, StatePill } from "./ui";

type LogsState = {
  service: string;
  logs: string[];
  since?: string;
  updatedAt?: number;
};

const SINCE_OPTIONS = [
  { label: "tail", seconds: 0 },
  { label: "5m", seconds: 5 * 60 },
  { label: "15m", seconds: 15 * 60 },
  { label: "1h", seconds: 60 * 60 },
  { label: "24h", seconds: 24 * 60 * 60 },
];

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

function sinceValue(label: string) {
  const option = SINCE_OPTIONS.find((item) => item.label === label);
  if (!option?.seconds) return "";
  return String(Math.floor(Date.now() / 1000) - option.seconds);
}

function logParams(service: string, sinceLabel: string, tail: string) {
  const params = new URLSearchParams({ service, tail });
  const since = sinceValue(sinceLabel);
  if (since) params.set("since", since);
  return params;
}

function serviceLogFilename(service: string) {
  const safeName = service.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
  return `${safeName || "service"}.log`;
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
  const [sinceLabel, setSinceLabel] = useState("tail");
  const [keyword, setKeyword] = useState("");
  const [paused, setPaused] = useState(false);
  const [copyState, setCopyState] = useState("");
  const [downloadState, setDownloadState] = useState("");
  const [logsState, setLogsState] = useState<LogsState | null>(null);
  const [logsError, setLogsError] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);

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

  const loadLogs = useCallback(async () => {
    if (!selectedService) return;
    setLogsLoading(true);
    try {
      const params = logParams(selectedService, sinceLabel, "200");
      const response = await fetch(`/v1/dashboard/logs?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      setLogsState(payload as LogsState);
      setLogsError("");
    } catch (error) {
      setLogsError(String(error instanceof Error ? error.message : error));
    } finally {
      setLogsLoading(false);
    }
  }, [selectedService, sinceLabel, token]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!selectedService || paused) return;
    const timer = window.setInterval(() => void loadLogs(), 5000);
    return () => window.clearInterval(timer);
  }, [loadLogs, paused, selectedService]);

  const selected = services.find((service) => service.fullName === selectedService);
  const filteredLogs = useMemo(() => {
    const logs = logsState?.logs || [];
    const query = keyword.trim().toLowerCase();
    if (!query) return logs;
    return logs.filter((line) => line.toLowerCase().includes(query));
  }, [keyword, logsState]);

  const copyLogs = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      const text = filteredLogs.join("\n");
      await navigator.clipboard.writeText(text);
      setCopyState(lang === "zh" ? "已复制" : "Copied");
    } catch (error) {
      setCopyState(lang === "zh" ? "复制失败" : "Copy failed");
      setLogsError(String(error instanceof Error ? error.message : error));
    } finally {
      window.setTimeout(() => setCopyState(""), 1600);
    }
  };

  const downloadLogs = async () => {
    if (!selectedService) return;
    setDownloadState(lang === "zh" ? "下载中" : "Downloading");
    try {
      const params = logParams(selectedService, sinceLabel, "500");
      params.set("download", "1");
      const response = await fetch(`/v1/dashboard/logs?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        const message = await response.text();
        throw new Error(message || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = serviceLogFilename(selectedService);
      link.click();
      URL.revokeObjectURL(url);
      setDownloadState(lang === "zh" ? "已下载" : "Downloaded");
      setLogsError("");
    } catch (error) {
      setDownloadState(lang === "zh" ? "下载失败" : "Download failed");
      setLogsError(String(error instanceof Error ? error.message : error));
    } finally {
      window.setTimeout(() => setDownloadState(""), 1600);
    }
  };

  return (
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
            return (
              <button className="node-metric-row" type="button" key={`${node.name || "node"}-${index}`}>
                <span>
                  <strong>{node.name || "-"}</strong>
                  <small>{[node.region, node.role, node.agentStatus].filter(Boolean).join(" / ") || "-"}</small>
                </span>
                <span className="metric-pair">
                  <b>CPU</b>
                  <strong>{formatPercent(metrics.cpuPercent ?? metrics.loadPercent)}</strong>
                  <small>{metrics.load1 ? `load ${metrics.load1}` : resourceText({ cpus: capacity.cpus || metrics.cpuCount })}</small>
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
              {services.map((service, index) => (
                <tr key={`${service.fullName || "service"}-${index}`} onClick={() => {
                  setSelectedApp(appKey(service));
                  setSelectedService(service.fullName || "");
                }}>
                  <td><CodeCell value={serviceTitle(service)} /></td>
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
              ))}
            </tbody>
          </table>
        </div>
      </article>

      <article className="panel live-logs-panel">
        <div className="panel-heading logs-heading">
          <div>
            <p className="eyebrow">Logs</p>
            <h2>{lang === "zh" ? "实时日志" : "Live Logs"}</h2>
          </div>
          <div className="logs-actions">
            <select value={selectedApp} onChange={(event) => setSelectedApp(event.target.value)} aria-label={lang === "zh" ? "应用" : "Application"}>
              {applications.map((app) => (
                <option key={app.key} value={app.key}>{app.key}</option>
              ))}
            </select>
            <select value={selectedService} onChange={(event) => setSelectedService(event.target.value)} aria-label={lang === "zh" ? "子服务" : "Sub-service"}>
              {appServices.map((service) => (
                <option key={service.fullName} value={service.fullName}>{service.name || service.fullName}</option>
              ))}
            </select>
            <select value={sinceLabel} onChange={(event) => setSinceLabel(event.target.value)} aria-label="since">
              {SINCE_OPTIONS.map((option) => <option key={option.label} value={option.label}>since {option.label}</option>)}
            </select>
            <input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder={lang === "zh" ? "关键词过滤" : "Filter keyword"}
            />
            <label className="logs-pause">
              <input type="checkbox" checked={paused} onChange={(event) => setPaused(event.target.checked)} />
              <span>{lang === "zh" ? "暂停" : "Pause"}</span>
            </label>
            <button type="button" className="ghost" onClick={() => void loadLogs()}>{logsLoading ? t(lang, "refreshing") : t(lang, "refresh")}</button>
            <button type="button" className="ghost" onClick={() => void copyLogs()}>{copyState || (lang === "zh" ? "复制" : "Copy")}</button>
            <button type="button" className="ghost" onClick={() => void downloadLogs()}>{downloadState || (lang === "zh" ? "下载" : "Download")}</button>
          </div>
        </div>
        <div className="logs-context">
          <span>{selected ? serviceTitle(selected) : "-"}</span>
          <span>{filteredLogs.length}/{logsState?.logs?.length || 0} lines</span>
          <span>{logsState?.updatedAt ? new Date(logsState.updatedAt * 1000).toLocaleTimeString() : "-"}</span>
        </div>
        {logsError ? <div className="logs-error">{logsError}</div> : null}
        <pre className="logs-tail">{filteredLogs.join("\n") || "-"}</pre>
      </article>
    </section>
  );
}
