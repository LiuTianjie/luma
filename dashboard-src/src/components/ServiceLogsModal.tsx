import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, Download, RefreshCw, X } from "lucide-react";
import { t } from "../i18n";
import { appendLogFrame, formatLogLine, logRetryDelay, readLogFrames, waitForLogRetry, type DisplayLogLine } from "../logStream";
import type { DashboardService, Lang } from "../types";
import { useOverlay } from "../useOverlay";
import { SelectControl, type SelectOption } from "./ui";

type LogsState = {
  service: string;
  logs: DisplayLogLine[];
  droppedLines?: number;
  updatedAt?: number;
};

type PullDiagnosticState = {
  status: "idle" | "running" | "done" | "fail";
  node?: string;
  image?: string;
  taskId?: string;
  ok?: boolean;
  exitCode?: number;
  lines: string[];
};

type RuntimeEvent = {
  source?: string;
  type?: string;
  task?: string;
  message?: string;
  time?: number;
  failed?: boolean;
};

type RuntimeEventsState = {
  service?: string;
  job?: string;
  task?: string;
  allocId?: string;
  node?: string;
  image?: string;
  status?: string;
  events?: RuntimeEvent[];
  updatedAt?: number;
};

type LogSource = { allocationId: string; task?: string; stream?: string };

type LogSession = { key: string; cursor: string; refresh: number };

type ConnectionState = "connecting" | "live" | "reconnecting" | "paused" | "stopped";

const MAX_LINES = 2000;

const LOGS_MODAL_ROOT = typeof document === "undefined" ? null : document.body;

function serviceTitle(service: DashboardService) {
  return service.stack ? `${service.stack}/${service.name || "-"}` : service.name || service.fullName || "-";
}

function appKey(service: DashboardService) {
  return service.stack || service.fullName || service.name || "-";
}

function logParams(service: string, tail: string, allocation: string) {
  const params = new URLSearchParams({ service, tail });
  if (allocation) params.set("allocation", allocation);
  return params;
}

function serviceLogFilename(service: string) {
  const safeName = service.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
  return `${safeName || "service"}.log`;
}

function runtimeEventLabel(event: RuntimeEvent) {
  const parts = [event.source, event.type, event.task].filter(Boolean);
  return parts.length ? `[${parts.join(" · ")}] ${event.message || ""}` : event.message || "";
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
  const appOptions = useMemo<SelectOption[]>(
    () => applications.flatMap((app) => app.key
      ? [{ value: app.key, label: app.key }]
      : []),
    [applications],
  );
  const serviceOptions = useMemo<SelectOption[]>(
    () => appServices.flatMap((service) => service.fullName
      ? [{ value: service.fullName, label: service.name || service.fullName }]
      : []),
    [appServices],
  );
  const overlayRef = useOverlay<HTMLElement>(onClose);
  const [selectedService, setSelectedService] = useState(() => initialService?.fullName || firstService);
  const [allocation, setAllocation] = useState("");
  const [logSources, setLogSources] = useState<LogSource[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [retryDelay, setRetryDelay] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [keyword, setKeyword] = useState("");
  const [paused, setPaused] = useState(false);
  const [copyState, setCopyState] = useState("");
  const [downloadState, setDownloadState] = useState("");
  const [logsState, setLogsState] = useState<LogsState | null>(null);
  const [logsError, setLogsError] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);
  const logTailRef = useRef<HTMLPreElement | null>(null);
  const followBottomRef = useRef(true);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEventsState | null>(null);
  const [runtimeError, setRuntimeError] = useState("");
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const [pullDiagnostic, setPullDiagnostic] = useState<PullDiagnosticState | null>(null);
  const [pullLoading, setPullLoading] = useState(false);

  // Sync the requested initial service only when initialServiceName itself
  // changes. Depending on `services` here re-ran this after every 30s dashboard
  // poll (new array reference) and silently yanked the user's selection back to
  // the initial service, reconnecting the log stream each time.
  const servicesRef = useRef(services);
  servicesRef.current = services;
  useEffect(() => {
    const next = servicesRef.current.find((service) => service.fullName === initialServiceName);
    if (!next?.fullName) return;
    setSelectedApp(appKey(next));
    setSelectedService(next.fullName);
  }, [initialServiceName]);

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

  useEffect(() => {
    setPullDiagnostic(null);
    setPullLoading(false);
    setRuntimeEvents(null);
    setRuntimeError("");
  }, [selectedService]);

  const sessionRef = useRef<LogSession>({ key: "", cursor: "", refresh: -1 });
  const currentServiceRef = useRef(selectedService);
  currentServiceRef.current = selectedService;
  const currentSelectionRef = useRef("");
  currentSelectionRef.current = `${selectedService}\0${allocation}`;
  const pullControllerRef = useRef<AbortController | null>(null);
  const downloadControllerRef = useRef<AbortController | null>(null);
  useEffect(() => {
    setDownloadState("");
    return () => downloadControllerRef.current?.abort();
  }, [selectedService, allocation]);
  useEffect(() => {
    setAllocation("");
    setLogSources([]);
    setWarnings([]);
    return () => pullControllerRef.current?.abort();
  }, [selectedService]);

  const allocationOptions = useMemo<SelectOption[]>(() => {
    const ids = [...new Set(logSources.map((item) => item.allocationId).filter(Boolean))];
    if (allocation && !ids.includes(allocation)) ids.push(allocation);
    return [
      { value: "", label: lang === "zh" ? "全部当前实例" : "All current instances" },
      ...ids.map((id) => ({ value: id, label: id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id })),
    ];
  }, [allocation, lang, logSources]);

  const selected = services.find((service) => service.fullName === selectedService);
  const filteredLogs = useMemo(() => {
    const logs = logsState?.service === selectedService ? logsState.logs.map(formatLogLine) : [];
    const query = keyword.trim().toLowerCase();
    if (!query) return logs;
    return logs.filter((line) => line.toLowerCase().includes(query));
  }, [keyword, logsState, selectedService]);

  useEffect(() => {
    const tail = logTailRef.current;
    if (tail && followBottomRef.current) tail.scrollTop = tail.scrollHeight;
  }, [filteredLogs]);

  const loadRuntimeEvents = useCallback(async (signal?: AbortSignal) => {
    if (!selectedService) return;
    setRuntimeLoading(true);
    try {
      const params = new URLSearchParams({ service: selectedService });
      const response = await fetch(`/v1/dashboard/runtime-events?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (signal?.aborted || currentServiceRef.current !== selectedService) return;
      setRuntimeEvents(payload as RuntimeEventsState);
      setRuntimeError("");
    } catch (error) {
      if ((error as Error)?.name === "AbortError") return;
      if (!signal?.aborted && currentServiceRef.current === selectedService) setRuntimeError(String(error instanceof Error ? error.message : error));
    } finally {
      if (!signal?.aborted && currentServiceRef.current === selectedService) setRuntimeLoading(false);
    }
  }, [selectedService, token]);

  useEffect(() => {
    if (!selectedService) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      await loadRuntimeEvents(controller.signal);
      if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 5000);
    };
    void poll();
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [selectedService, loadRuntimeEvents, refreshVersion]);

  // A cursor belongs to exactly one service/instance selection. Keep it across
  // pause and transient disconnects; a manual refresh explicitly reloads tail.
  useEffect(() => {
    const key = `${selectedService}\0${allocation}`;
    const reset = sessionRef.current.key !== key || sessionRef.current.refresh !== refreshVersion;
    if (reset) {
      sessionRef.current = { key, cursor: "", refresh: refreshVersion };
      followBottomRef.current = true;
      setLogsState({ service: selectedService, logs: [], droppedLines: 0 });
      setWarnings([]);
      setLogsError("");
    }
    if (!selectedService || paused) {
      setConnection(paused ? "paused" : "stopped");
      setLogsLoading(false);
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    const session = sessionRef.current;
    const active = () => !signal.aborted && sessionRef.current === session && currentSelectionRef.current === key;
    let attempt = 0;
    const addWarning = (message: string) => {
      if (!active() || !message) return;
      setWarnings((previous) => [...new Set([...previous, message])].slice(-8));
    };
    const run = async () => {
      while (active()) {
        setConnection(attempt ? "reconnecting" : "connecting");
        setLogsLoading(true);
        let fatal = false;
        let timedOut = false;
        const requestController = new AbortController();
        const abortRequest = () => requestController.abort();
        signal.addEventListener("abort", abortRequest, { once: true });
        let watchdog: ReturnType<typeof setTimeout> | undefined;
        const keepAlive = () => {
          clearTimeout(watchdog);
          watchdog = setTimeout(() => { timedOut = true; requestController.abort(); }, 30000);
        };
        keepAlive();
        try {
          const params = logParams(selectedService, "200", allocation);
          if (session.cursor) params.set("cursor", session.cursor);
          const response = await fetch(`/v1/dashboard/logs/stream?${params.toString()}`, {
            headers: { Authorization: `Bearer ${token}` }, signal: requestController.signal,
          });
          if (!active()) return;
          if (!response.ok || !response.body) {
            fatal = [400, 401, 403, 404].includes(response.status);
            let message = `HTTP ${response.status}`;
            try { const payload = await response.json(); message = payload.error || message; } catch { /* non-JSON proxy error */ }
            throw new Error(message);
          }
          setConnection("live");
          setLogsLoading(false);
          setLogsError("");
          const connectedAt = Date.now();
          await readLogFrames(response.body, (event) => {
            if (!active()) return;
            keepAlive();
            if (typeof event.cursor === "string") session.cursor = event.cursor;
            const sources = Array.isArray(event.sources) ? event.sources as LogSource[]
              : typeof event.allocationId === "string" ? [{ allocationId: event.allocationId }] : [];
            if (sources.length) setLogSources((previous) => {
              const byId = new Map(previous.map((item) => [item.allocationId, item]));
              for (const item of sources) if (item.allocationId) byId.set(item.allocationId, item);
              return [...byId.values()];
            });
            if (Array.isArray(event.warnings)) event.warnings.forEach((item) => addWarning(String(item)));
            if (event.status === "warning") addWarning(String(event.message || ""));
            if (event.status === "error") throw new Error(String(event.message || "Log stream failed"));
            if (typeof event.line === "string") {
              setLogsState((previous) => {
                const { lines, dropped } = appendLogFrame(previous?.service === selectedService ? previous.logs : [], event, MAX_LINES);
                return {
                  service: selectedService, logs: lines,
                  droppedLines: (previous?.droppedLines || 0) + dropped,
                  updatedAt: typeof event.observedAt === "number" ? event.observedAt : Date.now() / 1000,
                };
              });
            }
          }, requestController.signal);
          if (timedOut) throw new Error("Log stream timed out");
          if (Date.now() - connectedAt > 10000) attempt = 0;
          // A clean EOF is also a disconnection. Never leave a frozen view
          // marked live just because the server closed without an HTTP error.
        } catch (error) {
          if (!active()) return;
          setLogsError(timedOut ? "Log stream timed out" : error instanceof Error ? error.message : String(error));
        } finally {
          clearTimeout(watchdog);
          signal.removeEventListener("abort", abortRequest);
          requestController.abort();
        }
        if (!active()) return;
        setLogsLoading(false);
        if (fatal) { setConnection("stopped"); return; }
        const delay = logRetryDelay(attempt++);
        setRetryDelay(delay / 1000);
        setConnection("reconnecting");
        await waitForLogRetry(delay, signal);
      }
    };
    void run();
    return () => controller.abort();
  }, [selectedService, allocation, paused, token, refreshVersion]);

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
    downloadControllerRef.current?.abort();
    const controller = new AbortController();
    downloadControllerRef.current = controller;
    const key = `${selectedService}\0${allocation}`;
    const active = () => !controller.signal.aborted && currentSelectionRef.current === key;
    setDownloadState(lang === "zh" ? "下载中" : "Downloading");
    try {
      const params = logParams(selectedService, "500", allocation);
      params.set("download", "1");
      const response = await fetch(`/v1/dashboard/logs?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
      });
      if (!response.ok) {
        const message = await response.text();
        throw new Error(message || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      if (!active()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = serviceLogFilename(selectedService);
      link.click();
      URL.revokeObjectURL(url);
      setDownloadState(lang === "zh" ? "已下载" : "Downloaded");
      setLogsError("");
    } catch (error) {
      if (!active()) return;
      setDownloadState(lang === "zh" ? "下载失败" : "Download failed");
      setLogsError(String(error instanceof Error ? error.message : error));
    } finally {
      window.setTimeout(() => { if (active()) setDownloadState(""); }, 1600);
    }
  };

  const diagnosePull = async () => {
    if (!selectedService) return;
    pullControllerRef.current?.abort();
    const controller = new AbortController();
    pullControllerRef.current = controller;
    const active = () => !controller.signal.aborted && currentServiceRef.current === selectedService;
    setPullLoading(true);
    setLogsError("");
    setPullDiagnostic({ status: "running", lines: [] });
    try {
      const response = await fetch("/v1/dashboard/pull-diagnostics/stream", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ service: selectedService, timeout: 600 }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const applyEvent = (event: Record<string, unknown>) => {
        if (!active()) return;
        const status = String(event.status || "");
        if (status === "start") {
          setPullDiagnostic((prev) => ({
            status: "running",
            lines: prev?.lines || [],
            node: String(event.node || ""),
            image: String(event.image || ""),
            taskId: String(event.taskId || ""),
          }));
          return;
        }
        if (status === "progress") {
          const line = String(event.line || "");
          if (!line) return;
          setPullDiagnostic((prev) => {
            const nextLines = [...(prev?.lines || []), line];
            return { ...(prev || { status: "running" as const, lines: [] }), status: "running", lines: nextLines.slice(-300) };
          });
          return;
        }
        if (status === "done") {
          const result = event.result && typeof event.result === "object" ? event.result as Record<string, unknown> : {};
          setPullDiagnostic((prev) => {
            const resultLines = Array.isArray(result.lines) ? result.lines.map(String) : [];
            const combined = [...(prev?.lines || [])];
            for (const line of resultLines) {
              if (line && !combined.includes(line)) combined.push(line);
            }
            return {
              status: "done",
              node: String(result.node || prev?.node || ""),
              image: String(result.image || prev?.image || ""),
              taskId: String(result.taskId || prev?.taskId || ""),
              ok: Boolean(result.ok),
              exitCode: Number(result.exitCode ?? 0),
              lines: combined.slice(-300),
            };
          });
          return;
        }
        if (status === "fail") {
          const message = String(event.message || "Docker pull diagnostic failed");
          setPullDiagnostic((prev) => ({
            ...(prev || { lines: [] }),
            status: "fail",
            ok: false,
            lines: [...(prev?.lines || []), message].slice(-300),
          }));
        }
      };
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          if (!part.trim()) continue;
          applyEvent(JSON.parse(part));
        }
      }
      if (buffer.trim()) applyEvent(JSON.parse(buffer));
    } catch (error) {
      if (!active()) return;
      const message = String(error instanceof Error ? error.message : error);
      setLogsError(message);
      setPullDiagnostic((prev) => ({ ...(prev || { lines: [] }), status: "fail", ok: false, lines: [...(prev?.lines || []), message].slice(-300) }));
    } finally {
      if (active()) setPullLoading(false);
    }
  };

  if (!LOGS_MODAL_ROOT) return null;

  return createPortal(
    <div className="logs-modal-backdrop" onClick={onClose}>
      <section className="logs-modal" ref={overlayRef} aria-modal="true" role="dialog" aria-labelledby="logs-modal-title" onClick={(event) => event.stopPropagation()}>
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
            <SelectControl
              value={selectedApp}
              onChange={setSelectedApp}
              ariaLabel={lang === "zh" ? "应用" : "Application"}
              options={appOptions}
            />
            <SelectControl
              value={selectedService}
              onChange={setSelectedService}
              ariaLabel={lang === "zh" ? "子服务" : "Sub-service"}
              options={serviceOptions}
            />
            <SelectControl
              value={allocation}
              onChange={setAllocation}
              ariaLabel={lang === "zh" ? "日志实例" : "Log instance"}
              options={allocationOptions}
            />
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
            <button type="button" className="logs-tool-button" onClick={() => { setPaused(false); setRefreshVersion((value) => value + 1); }}>
              <RefreshCw size={14} aria-hidden="true" />
              {logsLoading || runtimeLoading ? t(lang, "refreshing") : t(lang, "refresh")}
            </button>
            <button type="button" className="logs-tool-button" disabled={pullLoading || !selectedService} onClick={() => void diagnosePull()}>
              <RefreshCw size={14} aria-hidden="true" />
              {pullLoading ? (lang === "zh" ? "诊断中" : "Diagnosing") : (lang === "zh" ? "诊断拉取" : "Pull diag")}
            </button>
            <button type="button" className="logs-tool-button" onClick={() => void copyLogs()}>
              <Copy size={14} aria-hidden="true" />
              {copyState || (lang === "zh" ? "复制" : "Copy")}
            </button>
            <button type="button" className="logs-tool-button" onClick={() => void downloadLogs()}>
              <Download size={14} aria-hidden="true" />
              {downloadState || (lang === "zh" ? "下载近期日志" : "Download recent logs")}
            </button>
          </div>
        </div>
        <div className="logs-context">
          <span role="status" aria-live="polite">{({
            connecting: lang === "zh" ? "连接中" : "Connecting",
            live: lang === "zh" ? "实时跟随" : "Live",
            reconnecting: lang === "zh" ? `正在重连 · 重试间隔 ${retryDelay} 秒` : `Reconnecting · retry interval ${retryDelay}s`,
            paused: lang === "zh" ? "已暂停 · 继续时补取可用日志" : "Paused · available logs resume on reconnect",
            stopped: lang === "zh" ? "已停止 · 点击刷新重试" : "Stopped · refresh to retry",
          })[connection]}</span>
          <span>{logsState?.updatedAt ? new Date(logsState.updatedAt * 1000).toLocaleTimeString() : "-"}</span>
        </div>
        <div className="logs-context">
          <span>{lang === "zh"
            ? `初次读取最近约 200 行，按日志源分配；页面最多保留 ${MAX_LINES} 行，已移除 ${logsState?.droppedLines || 0} 行。下载最近约 500 行，按日志源分配。`
            : `Starts with about 200 recent lines shared across sources. View retains ${MAX_LINES} lines; ${logsState?.droppedLines || 0} older lines removed. Download targets 500 recent lines shared across sources.`}</span>
          <span>{lang === "zh" ? "历史取决于节点日志轮转，无法保证覆盖指定时间。运行事件显示最近实例。" : "History depends on node log rotation; no guaranteed time window. Runtime events show the latest instance."}</span>
        </div>
        {warnings.length ? <div className="logs-error">{warnings.join(" · ")}</div> : null}
        {runtimeEvents ? (
          <div className={runtimeEvents.status === "running" ? "logs-runtime-events running" : "logs-runtime-events"}>
            <div className="logs-pull-summary">
              <strong>{lang === "zh" ? "运行事件" : "Runtime events"}</strong>
              <span>
                {[runtimeEvents.status, runtimeEvents.allocId, runtimeEvents.node, runtimeEvents.image].filter(Boolean).join(" · ") || "-"}
              </span>
            </div>
            <pre>{(runtimeEvents.events || []).map(runtimeEventLabel).filter(Boolean).join("\n") || (lang === "zh" ? "暂无运行事件" : "No runtime events yet")}</pre>
          </div>
        ) : null}
        {pullDiagnostic ? (
          <div className={pullDiagnostic.status === "done" && pullDiagnostic.ok ? "logs-pull-diagnostics ok" : pullDiagnostic.status === "fail" || pullDiagnostic.ok === false ? "logs-pull-diagnostics bad" : "logs-pull-diagnostics"}>
            <div className="logs-pull-summary">
              <strong>{lang === "zh" ? "镜像拉取诊断" : "Image pull diagnostic"}</strong>
              <span>{pullDiagnostic.node || "-"} · {pullDiagnostic.image || "-"} · {pullDiagnostic.status}{pullDiagnostic.exitCode !== undefined ? ` · exit ${pullDiagnostic.exitCode}` : ""}</span>
            </div>
            <pre>{pullDiagnostic.lines.join("\n") || (lang === "zh" ? "已下发到节点，等待 docker pull 输出..." : "Task queued on node, waiting for docker pull output...")}</pre>
          </div>
        ) : null}
        {runtimeError ? <div className="logs-error">{runtimeError}</div> : null}
        {logsError ? <div className="logs-error">{logsError}</div> : null}
        <pre ref={logTailRef} className="logs-tail logs-modal-tail" onScroll={(event) => {
          const tail = event.currentTarget;
          followBottomRef.current = tail.scrollHeight - tail.scrollTop - tail.clientHeight < 48;
        }}>{filteredLogs.join("\n") || (runtimeEvents?.status && runtimeEvents.status !== "running" ? (lang === "zh" ? "容器尚未启动或尚未输出日志，见上方运行事件。" : "Container has not started or has not emitted logs yet. See runtime events above.") : "-")}</pre>
      </section>
    </div>,
    LOGS_MODAL_ROOT,
  );
}
