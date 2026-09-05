import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Activity, ChevronDown, Copy, Download, Info, Pause, Play, RefreshCw, Search, Terminal, WrapText, X } from "lucide-react";
import { t } from "../i18n";
import { appendLogFrame, formatLogLine, logRetryDelay, readLogFrames, waitForLogRetry, type DisplayLogLine } from "../logStream";
import type { DashboardService, Lang } from "../types";
import { useOverlay } from "../useOverlay";
import { SelectControl, type SelectOption } from "./ui";
import "./serviceLogs.css";

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

function LogsOverlay({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const ref = useOverlay<HTMLDivElement>(onClose);
  return <div className="log-console-backdrop" onClick={onClose}><div className="log-console-dialog" ref={ref} role="dialog" aria-modal="true" aria-labelledby="logs-modal-title" onClick={(event) => event.stopPropagation()}>{children}</div></div>;
}

export function ServiceLogsModal({
  lang,
  token,
  services,
  initialServiceName,
  onClose = () => {},
  inline = false,
}: {
  lang: Lang;
  token: string;
  services: DashboardService[];
  initialServiceName: string;
  onClose?: () => void;
  inline?: boolean;
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
  const [selectedService, setSelectedService] = useState(() => initialService?.fullName || firstService);
  const [allocation, setAllocation] = useState("");
  const [logSources, setLogSources] = useState<LogSource[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [retryDelay, setRetryDelay] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => { const refresh = () => setRefreshVersion((value) => value + 1); window.addEventListener("luma:refresh", refresh); return () => window.removeEventListener("luma:refresh", refresh); }, []);
  const [keyword, setKeyword] = useState("");
  const [paused, setPaused] = useState(false);
  const [wrapLines, setWrapLines] = useState(false);
  const [diagnosticExpanded, setDiagnosticExpanded] = useState(false);
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
  }, [filteredLogs, wrapLines]);

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
    setDiagnosticExpanded(true);
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

  const title = selected ? serviceTitle(selected) : (lang === "zh" ? "服务日志" : "Service logs");
  const connectionLabel = ({
    connecting: lang === "zh" ? "连接中" : "Connecting",
    live: lang === "zh" ? "实时跟随" : "Live",
    reconnecting: lang === "zh" ? `重连中 · 间隔 ${retryDelay} 秒` : `Reconnecting · ${retryDelay}s interval`,
    paused: lang === "zh" ? "已暂停" : "Paused",
    stopped: lang === "zh" ? "已停止" : "Stopped",
  })[connection];
  const runtimeStatus = runtimeEvents?.status || "unknown";
  const runtimeStatusLabel = lang === "zh" ? ({
    running: "运行中", pending: "等待启动", failed: "失败", complete: "已结束", dead: "已停止", unknown: "状态未知",
  } as Record<string, string>)[runtimeStatus] || runtimeStatus : runtimeStatus;
  const eventLines = (runtimeEvents?.events || []).filter((event) => runtimeEventLabel(event));
  const noticeMessages = [runtimeError, logsError].filter(Boolean);
  const emptyMessage = keyword.trim()
    ? (lang === "zh" ? "没有匹配的日志。尝试调整关键词。" : "No matching logs. Try a different keyword.")
    : logsLoading ? (lang === "zh" ? "正在连接日志源…" : "Connecting to log sources…")
    : paused ? (lang === "zh" ? "已暂停。继续后将补取可用日志。" : "Paused. Resume to retrieve available logs.")
    : runtimeEvents?.status && runtimeEvents.status !== "running"
      ? (lang === "zh" ? "容器尚未启动或尚未输出日志。展开运行事件查看原因。" : "The container has not started or emitted logs. Expand runtime events for details.")
      : (lang === "zh" ? "暂无日志，等待新的输出。" : "No logs yet. Waiting for output.");

  const content = (
    <section className={`log-console ${inline ? "logs-workspace" : "log-console--modal"}`} aria-labelledby="logs-modal-title">
      <header className="log-console__header">
        <div className="log-console__identity">
          <Terminal size={18} aria-hidden="true" />
          <div>
            <p>{lang === "zh" ? "服务日志" : "Service logs"}</p>
            <h2 id="logs-modal-title" title={title}>{title}</h2>
          </div>
        </div>
        <div className="log-console__header-actions">
          <span className={`log-console__connection is-${connection}`} role="status" aria-live="polite">{connectionLabel}</span>
          {!inline && <button type="button" className="log-console__button log-console__close" onClick={onClose} aria-label={t(lang, "close")}>
            <X size={16} aria-hidden="true" />
          </button>}
        </div>
      </header>

      <div className="log-console__filters" aria-label={lang === "zh" ? "日志筛选" : "Log filters"}>
        <div className="log-console__field">
          <span>{lang === "zh" ? "应用" : "Application"}</span>
          <SelectControl value={selectedApp} onChange={setSelectedApp} ariaLabel={lang === "zh" ? "应用" : "Application"} options={appOptions} />
        </div>
        <div className="log-console__field">
          <span>{lang === "zh" ? "服务" : "Service"}</span>
          <SelectControl value={selectedService} onChange={setSelectedService} ariaLabel={lang === "zh" ? "子服务" : "Sub-service"} options={serviceOptions} />
        </div>
        <div className="log-console__field">
          <span>{lang === "zh" ? "实例" : "Instance"}</span>
          <SelectControl value={allocation} onChange={setAllocation} ariaLabel={lang === "zh" ? "日志实例" : "Log instance"} options={allocationOptions} />
        </div>
        <label className="log-console__field log-console__search-field">
          <span>{lang === "zh" ? "搜索当前日志" : "Search current logs"}</span>
          <span className="log-console__search">
            <Search size={16} aria-hidden="true" />
            <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder={lang === "zh" ? "输入关键词" : "Filter by keyword"} />
          </span>
        </label>
      </div>

      <div className="log-console__toolbar" aria-label={lang === "zh" ? "日志操作" : "Log actions"}>
        <div className="log-console__action-group">
          <button type="button" className="log-console__button" aria-pressed={paused} onClick={() => setPaused((value) => !value)}>
            {paused ? <Play size={15} aria-hidden="true" /> : <Pause size={15} aria-hidden="true" />}
            {paused ? (lang === "zh" ? "继续" : "Resume") : (lang === "zh" ? "暂停" : "Pause")}
          </button>
          <button type="button" className="log-console__button" onClick={() => { setPaused(false); setRefreshVersion((value) => value + 1); }}>
            <RefreshCw size={15} aria-hidden="true" />
            {logsLoading || runtimeLoading ? t(lang, "refreshing") : t(lang, "refresh")}
          </button>
          <button type="button" className="log-console__button" disabled={pullLoading || !selectedService} onClick={() => void diagnosePull()}>
            <Activity size={15} aria-hidden="true" />
            {pullLoading ? (lang === "zh" ? "诊断中" : "Diagnosing") : (lang === "zh" ? "诊断拉取" : "Pull diagnostic")}
          </button>
        </div>
        <div className="log-console__action-group">
          <button type="button" className="log-console__button" aria-pressed={wrapLines} onClick={() => setWrapLines((value) => !value)}>
            <WrapText size={15} aria-hidden="true" />
            {lang === "zh" ? "自动折行" : "Wrap lines"}
          </button>
          <button type="button" className="log-console__button" disabled={!filteredLogs.length} onClick={() => void copyLogs()}>
            <Copy size={15} aria-hidden="true" />
            {copyState || (lang === "zh" ? "复制" : "Copy")}
          </button>
          <button type="button" className="log-console__button" disabled={!selectedService || Boolean(downloadState)} onClick={() => void downloadLogs()}>
            <Download size={15} aria-hidden="true" />
            {downloadState || (lang === "zh" ? "下载近期日志" : "Download recent")}
          </button>
        </div>
      </div>

      {(runtimeEvents || pullDiagnostic) && <div className="log-console__context">
        {runtimeEvents && <details className="log-console__disclosure" key={`runtime-${selectedService}`}>
          <summary>
            <ChevronDown size={15} className="log-console__chevron" aria-hidden="true" />
            <strong>{lang === "zh" ? "运行事件" : "Runtime events"}</strong>
            <span className={`log-console__runtime-status ${runtimeStatus === "running" ? "is-running" : ""}`}>{runtimeStatusLabel}</span>
            <span className="log-console__summary-meta">{eventLines.length} {lang === "zh" ? "条事件 · 最近实例" : "events · latest instance"}</span>
          </summary>
          <div className="log-console__disclosure-body">
            <dl className="log-console__metadata">
              <div><dt>{lang === "zh" ? "实例" : "Instance"}</dt><dd><code>{runtimeEvents.allocId || "—"}</code></dd></div>
              <div><dt>{lang === "zh" ? "节点" : "Node"}</dt><dd><code>{runtimeEvents.node || "—"}</code></dd></div>
              <div className="log-console__metadata-wide"><dt>{lang === "zh" ? "镜像" : "Image"}</dt><dd><code>{runtimeEvents.image || "—"}</code></dd></div>
            </dl>
            <pre className="log-console__event-output" tabIndex={0} aria-label={lang === "zh" ? "运行事件全文" : "Full runtime events"}>{eventLines.map(runtimeEventLabel).join("\n") || (lang === "zh" ? "暂无运行事件" : "No runtime events yet")}</pre>
          </div>
        </details>}
        {pullDiagnostic && <details className="log-console__disclosure" open={diagnosticExpanded} onToggle={(event) => setDiagnosticExpanded(event.currentTarget.open)}>
          <summary>
            <ChevronDown size={15} className="log-console__chevron" aria-hidden="true" />
            <strong>{lang === "zh" ? "镜像拉取诊断" : "Image pull diagnostic"}</strong>
            <span className={`log-console__runtime-status ${pullDiagnostic.ok ? "is-running" : ""}`}>{pullLoading ? (lang === "zh" ? "进行中" : "Running") : pullDiagnostic.ok ? (lang === "zh" ? "成功" : "Succeeded") : (lang === "zh" ? "失败" : "Failed")}</span>
            {pullDiagnostic.exitCode !== undefined && <span className="log-console__summary-meta">exit {pullDiagnostic.exitCode}</span>}
          </summary>
          <div className="log-console__disclosure-body">
            <dl className="log-console__metadata">
              <div><dt>{lang === "zh" ? "节点" : "Node"}</dt><dd><code>{pullDiagnostic.node || "—"}</code></dd></div>
              <div><dt>{lang === "zh" ? "任务" : "Task"}</dt><dd><code>{pullDiagnostic.taskId || "—"}</code></dd></div>
              <div className="log-console__metadata-wide"><dt>{lang === "zh" ? "镜像" : "Image"}</dt><dd><code>{pullDiagnostic.image || "—"}</code></dd></div>
            </dl>
            <pre className="log-console__event-output" tabIndex={0} aria-label={lang === "zh" ? "镜像拉取诊断全文" : "Full pull diagnostic"}>{pullDiagnostic.lines.join("\n") || (lang === "zh" ? "已下发到节点，等待 docker pull 输出…" : "Task queued on node, waiting for docker pull output…")}</pre>
          </div>
        </details>}
      </div>}

      {warnings.length > 0 && <details className="log-console__warning-details">
        <summary><Info size={15} aria-hidden="true" /><strong>{lang === "zh" ? `日志源提示 · ${warnings.length} 项` : `${warnings.length} source notices`}</strong><ChevronDown size={15} className="log-console__chevron" aria-hidden="true" /></summary>
        <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      </details>}
      {noticeMessages.length > 0 && <div className="log-console__notice" role="alert">{noticeMessages.map((message, index) => <p key={`${index}-${message}`}>{message}</p>)}</div>}

      <div className="log-console__stream-heading">
        <span>{lang === "zh" ? "日志输出" : "Log output"}</span>
        <span>{filteredLogs.length} / {logsState?.logs?.length || 0} {lang === "zh" ? "行" : "lines"}{logsState?.updatedAt ? ` · ${new Date(logsState.updatedAt * 1000).toLocaleTimeString()}` : ""}</span>
      </div>
      <pre ref={logTailRef} className={`log-console__viewport ${wrapLines ? "is-wrapped" : ""} ${filteredLogs.length ? "" : "is-empty"}`} tabIndex={0} aria-label={lang === "zh" ? "服务日志输出" : "Service log output"} onScroll={(event) => {
        const tail = event.currentTarget;
        followBottomRef.current = tail.scrollHeight - tail.scrollTop - tail.clientHeight < 48;
      }}>{filteredLogs.join("\n") || emptyMessage}</pre>

      <footer className="log-console__footer">
        <span>{lang === "zh" ? `页面保留 ${MAX_LINES} 行` : `View retains ${MAX_LINES} lines`}{logsState?.droppedLines ? (lang === "zh" ? ` · 已移除 ${logsState.droppedLines} 行` : ` · ${logsState.droppedLines} older lines removed`) : ""}</span>
        <details className="log-console__retention">
          <summary><Info size={14} aria-hidden="true" />{lang === "zh" ? "读取与保留范围" : "Read and retention limits"}</summary>
          <p>{lang === "zh" ? "初次读取最近约 200 行，下载最近约 500 行，均按日志源分配。历史取决于节点日志轮转，无法保证覆盖指定时间。暂停后继续将补取仍可用的日志。复制包含当前关键词筛选后的内容。" : "Initial read targets 200 recent lines; download targets 500, shared across sources. History depends on node rotation with no guaranteed time window. Resume retrieves logs still available. Copy includes the current keyword filter."}</p>
        </details>
      </footer>
    </section>
  );
  return inline ? content : LOGS_MODAL_ROOT ? createPortal(<LogsOverlay onClose={onClose}>{content}</LogsOverlay>, LOGS_MODAL_ROOT) : null;
}
