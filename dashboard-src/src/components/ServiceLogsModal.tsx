import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, Download, RefreshCw, X } from "lucide-react";
import { t } from "../i18n";
import type { DashboardService, Lang } from "../types";

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

const LOGS_MODAL_ROOT = typeof document === "undefined" ? null : document.body;

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

export function ServiceLogsModal({
  lang,
  token,
  services,
  initialServiceName,
  onClose,
}: {
  lang: Lang;
  token: string;
  services: DashboardService[];
  initialServiceName: string;
  onClose: () => void;
}) {
  const applications = useMemo(() => {
    const groups = new Map<string, DashboardService[]>();
    for (const service of services.filter((item) => item.fullName)) {
      const key = appKey(service);
      groups.set(key, [...(groups.get(key) || []), service]);
    }
    return Array.from(groups.entries()).map(([key, group]) => ({
      key,
      services: group.sort((a, b) => serviceTitle(a).localeCompare(serviceTitle(b))),
    }));
  }, [services]);
  const firstService = applications[0]?.services[0]?.fullName || "";
  const initialService = services.find((service) => service.fullName === initialServiceName);
  const [selectedApp, setSelectedApp] = useState(() => initialService ? appKey(initialService) : applications[0]?.key || "");
  const appServices = applications.find((item) => item.key === selectedApp)?.services || [];
  const [selectedService, setSelectedService] = useState(() => initialService?.fullName || firstService);
  const [sinceLabel, setSinceLabel] = useState("tail");
  const [keyword, setKeyword] = useState("");
  const [paused, setPaused] = useState(false);
  const [copyState, setCopyState] = useState("");
  const [downloadState, setDownloadState] = useState("");
  const [logsState, setLogsState] = useState<LogsState | null>(null);
  const [logsError, setLogsError] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);

  useEffect(() => {
    const next = services.find((service) => service.fullName === initialServiceName);
    if (!next?.fullName) return;
    setSelectedApp(appKey(next));
    setSelectedService(next.fullName);
  }, [initialServiceName, services]);

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

  const selected = services.find((service) => service.fullName === selectedService);
  const filteredLogs = useMemo(() => {
    const logs = logsState?.logs || [];
    const query = keyword.trim().toLowerCase();
    if (!query) return logs;
    return logs.filter((line) => line.toLowerCase().includes(query));
  }, [keyword, logsState]);

  const loadLogs = useCallback(async (signal?: AbortSignal) => {
    if (!selectedService) return;
    setLogsLoading(true);
    try {
      const params = logParams(selectedService, sinceLabel, "200");
      const response = await fetch(`/v1/dashboard/logs?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      setLogsState(payload as LogsState);
      setLogsError("");
    } catch (error) {
      if ((error as Error)?.name === "AbortError") return;
      setLogsError(String(error instanceof Error ? error.message : error));
    } finally {
      setLogsLoading(false);
    }
  }, [selectedService, sinceLabel, token]);

  // Live tail: one long-lived NDJSON stream per selected service.
  useEffect(() => {
    if (!selectedService || paused) return;
    const controller = new AbortController();
    let cancelled = false;
    const MAX_LINES = 2000;

    const run = async () => {
      setLogsLoading(true);
      setLogsState({ service: selectedService, logs: [], updatedAt: Math.floor(Date.now() / 1000) });
      try {
        const params = logParams(selectedService, sinceLabel, "200");
        const response = await fetch(`/v1/dashboard/logs/stream?${params.toString()}`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(response.ok ? "stream unavailable" : `HTTP ${response.status}`);
        }
        setLogsError("");
        setLogsLoading(false);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        const append = (lines: string[]) => {
          if (!lines.length) return;
          setLogsState((prev) => {
            const merged = [...(prev?.logs || []), ...lines];
            const trimmed = merged.length > MAX_LINES ? merged.slice(merged.length - MAX_LINES) : merged;
            return { service: selectedService, logs: trimmed, updatedAt: Math.floor(Date.now() / 1000) };
          });
        };
        for (;;) {
          const { done, value } = await reader.read();
          if (done || cancelled) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n");
          buffer = parts.pop() || "";
          const newLines: string[] = [];
          for (const part of parts) {
            if (!part.trim()) continue;
            try {
              const event = JSON.parse(part);
              if (typeof event.line === "string") newLines.push(event.line);
            } catch {
              // Ignore malformed NDJSON chunks from partial stream frames.
            }
          }
          append(newLines);
        }
      } catch (error) {
        if ((error as Error)?.name === "AbortError" || cancelled) return;
        void loadLogs(controller.signal);
      }
    };
    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [selectedService, sinceLabel, paused, token, loadLogs]);

  const copyLogs = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(filteredLogs.join("\n"));
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

  if (!LOGS_MODAL_ROOT) return null;

  return createPortal(
    <div className="logs-modal-backdrop" onClick={onClose}>
      <section className="logs-modal" aria-modal="true" role="dialog" aria-labelledby="logs-modal-title" onClick={(event) => event.stopPropagation()}>
        <header className="logs-modal-header">
          <div>
            <p className="eyebrow">Logs</p>
            <h2 id="logs-modal-title">{selected ? serviceTitle(selected) : (lang === "zh" ? "服务日志" : "Service logs")}</h2>
            <span>{filteredLogs.length}/{logsState?.logs?.length || 0} lines</span>
          </div>
          <button type="button" className="logs-close-button" onClick={onClose} aria-label={t(lang, "close")}>
            <X size={16} aria-hidden="true" />
            {t(lang, "close")}
          </button>
        </header>
        <div className="logs-modal-toolbar">
          <div className="logs-filter-grid">
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
          </div>
          <div className="logs-action-group" aria-label={lang === "zh" ? "日志操作" : "Log actions"}>
            <button type="button" className={paused ? "logs-tool-button logs-toggle active" : "logs-tool-button logs-toggle"} onClick={() => setPaused((value) => !value)}>
              <span className={paused ? "logs-live-icon play" : "logs-live-icon pause"} aria-hidden="true" />
              {paused ? (lang === "zh" ? "继续" : "Resume") : (lang === "zh" ? "暂停" : "Pause")}
            </button>
            <button type="button" className="logs-tool-button" onClick={() => void loadLogs()}>
              <RefreshCw size={14} aria-hidden="true" />
              {logsLoading ? t(lang, "refreshing") : t(lang, "refresh")}
            </button>
            <button type="button" className="logs-tool-button" onClick={() => void copyLogs()}>
              <Copy size={14} aria-hidden="true" />
              {copyState || (lang === "zh" ? "复制" : "Copy")}
            </button>
            <button type="button" className="logs-tool-button" onClick={() => void downloadLogs()}>
              <Download size={14} aria-hidden="true" />
              {downloadState || (lang === "zh" ? "下载" : "Download")}
            </button>
          </div>
        </div>
        <div className="logs-context">
          <span>{selected ? serviceTitle(selected) : "-"}</span>
          <span>{logsState?.updatedAt ? new Date(logsState.updatedAt * 1000).toLocaleTimeString() : "-"}</span>
        </div>
        {logsError ? <div className="logs-error">{logsError}</div> : null}
        <pre className="logs-tail logs-modal-tail">{filteredLogs.join("\n") || "-"}</pre>
      </section>
    </div>,
    LOGS_MODAL_ROOT,
  );
}
