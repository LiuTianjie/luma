import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Activity, Copy, Download, Info, Pause, Play, RefreshCw, Search, Terminal, WrapText, X } from "lucide-react";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableRow } from "@/components/ui/table";
import { Toggle } from "@/components/ui/toggle";
import { cn } from "@/lib/utils";
import { t } from "../i18n";
import { appendLogFrame, formatLogLine, logRetryDelay, readLogFrames, waitForLogRetry, type DisplayLogLine } from "../logStream";
import type { DashboardService, Lang } from "../types";
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
  const appOptions = useMemo<{ value: string; label: string }[]>(
    () => applications.flatMap((app) => app.key
      ? [{ value: app.key, label: app.key }]
      : []),
    [applications],
  );
  const serviceOptions = useMemo<{ value: string; label: string }[]>(
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
  const [expandedContext, setExpandedContext] = useState<string[]>([]);
  const fieldId = useId();
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

  const allocationOptions = useMemo<{ value: string; label: string }[]>(() => {
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
    setExpandedContext((values) => values.includes("diagnostic") ? values : [...values, "diagnostic"]);
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
  const emptyMessage = !selectedService
    ? (lang === "zh" ? "选择应用和服务后查看日志。" : "Select an application and service to view its logs.")
    : keyword.trim()
    ? (lang === "zh" ? "没有匹配的日志。尝试调整关键词。" : "No matching logs. Try a different keyword.")
    : logsLoading ? (lang === "zh" ? "正在连接日志源…" : "Connecting to log sources…")
    : paused ? (lang === "zh" ? "已暂停。继续后将补取可用日志。" : "Paused. Resume to retrieve available logs.")
    : runtimeEvents?.status && runtimeEvents.status !== "running"
      ? (lang === "zh" ? "容器尚未启动或尚未输出日志。展开运行事件查看原因。" : "The container has not started or emitted logs. Expand runtime events for details.")
      : (lang === "zh" ? "暂无日志，等待新的输出。" : "No logs yet. Waiting for output.");

  const Header = inline ? CardHeader : DialogHeader;
  const Title = inline ? CardTitle : DialogTitle;
  const Description = inline ? CardDescription : DialogDescription;
  const Content = inline ? CardContent : "div";
  const Footer = inline ? CardFooter : DialogFooter;
  const content = (
    <>
      <Header className="flex flex-row flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <Title id={`${fieldId}-title`} className="truncate" title={title}>{title}</Title>
          <Description>{lang === "zh" ? "服务日志" : "Service logs"}</Description>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={connection === "live" ? "success" : "secondary"} role="status" aria-live="polite">
            {(connection === "connecting" || connection === "reconnecting") && <Spinner aria-hidden="true" data-icon="inline-start" />}
            {connectionLabel}
          </Badge>
          {!inline && <Button type="button" variant="ghost" size="icon-sm" onClick={onClose} aria-label={t(lang, "close")}>
            <X data-icon="inline-start" />
          </Button>}
        </div>
      </Header>

      <Content className="flex min-w-0 flex-col gap-4">
        <FieldGroup className="log-console__filters" aria-label={lang === "zh" ? "日志筛选" : "Log filters"}>
          <Field data-disabled={!appOptions.length}>
            <FieldLabel htmlFor={`${fieldId}-app`}>{lang === "zh" ? "应用" : "Application"}</FieldLabel>
            <Select
              items={[{ value: null, label: lang === "zh" ? "无可用应用" : "No applications" }, ...appOptions]}
              value={selectedApp || null}
              onValueChange={(value) => { if (value !== null) setSelectedApp(value); }}
              disabled={!appOptions.length}
            >
              <SelectTrigger id={`${fieldId}-app`} className="w-full min-w-0" title={selectedApp}>
                <SelectValue className="min-w-0">{(value) => <span className="truncate">{value || (lang === "zh" ? "无可用应用" : "No applications")}</span>}</SelectValue>
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}><SelectGroup>
                {appOptions.map((option) => <SelectItem key={option.value} value={option.value}><span className="min-w-0 break-all whitespace-normal">{option.label}</span></SelectItem>)}
              </SelectGroup></SelectContent>
            </Select>
          </Field>
          <Field data-disabled={!serviceOptions.length}>
            <FieldLabel htmlFor={`${fieldId}-service`}>{lang === "zh" ? "服务" : "Service"}</FieldLabel>
            <Select
              items={[{ value: null, label: lang === "zh" ? "无可用服务" : "No services" }, ...serviceOptions]}
              value={selectedService || null}
              onValueChange={(value) => { if (value !== null) setSelectedService(value); }}
              disabled={!serviceOptions.length}
            >
              <SelectTrigger id={`${fieldId}-service`} className="w-full min-w-0" title={selectedService}>
                <SelectValue className="min-w-0">{(value) => <span className="truncate">{serviceOptions.find((option) => option.value === value)?.label || (lang === "zh" ? "无可用服务" : "No services")}</span>}</SelectValue>
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}><SelectGroup>
                {serviceOptions.map((option) => <SelectItem key={option.value} value={option.value}><span className="min-w-0 break-all whitespace-normal">{option.label}</span></SelectItem>)}
              </SelectGroup></SelectContent>
            </Select>
          </Field>
          <Field data-disabled={!selectedService}>
            <FieldLabel htmlFor={`${fieldId}-allocation`}>{lang === "zh" ? "实例" : "Instance"}</FieldLabel>
            <Select items={allocationOptions} value={allocation} onValueChange={(value) => { if (value !== null) setAllocation(value); }} disabled={!selectedService}>
              <SelectTrigger id={`${fieldId}-allocation`} className="w-full min-w-0" title={allocation}>
                <SelectValue className="min-w-0">{(value) => <span className="truncate">{allocationOptions.find((option) => option.value === value)?.label}</span>}</SelectValue>
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}><SelectGroup>
                {allocationOptions.map((option) => <SelectItem key={option.value} value={option.value} title={option.value || option.label}>{option.label}</SelectItem>)}
              </SelectGroup></SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor={`${fieldId}-keyword`}>{lang === "zh" ? "搜索当前日志" : "Search current logs"}</FieldLabel>
            <InputGroup>
              <InputGroupAddon><Search aria-hidden="true" /></InputGroupAddon>
              <InputGroupInput id={`${fieldId}-keyword`} value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder={lang === "zh" ? "输入关键词" : "Filter by keyword"} />
            </InputGroup>
          </Field>
        </FieldGroup>

        <div className="flex flex-wrap justify-between gap-3" aria-label={lang === "zh" ? "日志操作" : "Log actions"}>
          <div className="flex flex-wrap items-center gap-2">
            <Toggle variant="outline" size="sm" pressed={paused} onPressedChange={setPaused} disabled={!selectedService} aria-label={lang === "zh" ? "暂停日志跟随" : "Pause log follow"}>
              {paused ? <Play data-icon="inline-start" /> : <Pause data-icon="inline-start" />}
              {paused ? (lang === "zh" ? "继续" : "Resume") : (lang === "zh" ? "暂停" : "Pause")}
            </Toggle>
            <Button type="button" variant="outline" size="sm" disabled={!selectedService} onClick={() => { setPaused(false); setRefreshVersion((value) => value + 1); }}>
              {logsLoading || runtimeLoading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
              {logsLoading || runtimeLoading ? t(lang, "refreshing") : t(lang, "refresh")}
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={pullLoading || !selectedService} onClick={() => void diagnosePull()}>
              {pullLoading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Activity data-icon="inline-start" />}
              {pullLoading ? (lang === "zh" ? "诊断中" : "Diagnosing") : (lang === "zh" ? "诊断拉取" : "Pull diagnostic")}
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Toggle variant="outline" size="sm" pressed={wrapLines} onPressedChange={setWrapLines} aria-label={lang === "zh" ? "自动折行" : "Wrap lines"}>
              <WrapText data-icon="inline-start" />
              {lang === "zh" ? "自动折行" : "Wrap lines"}
            </Toggle>
            <Button type="button" variant="outline" size="sm" disabled={!filteredLogs.length} onClick={() => void copyLogs()}>
              <Copy data-icon="inline-start" />
              <span aria-live="polite">{copyState || (lang === "zh" ? "复制" : "Copy")}</span>
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={!selectedService || Boolean(downloadState)} onClick={() => void downloadLogs()}>
              <Download data-icon="inline-start" />
              <span aria-live="polite">{downloadState || (lang === "zh" ? "下载近期日志" : "Download recent")}</span>
            </Button>
          </div>
        </div>

        <Separator />
        {(runtimeEvents || runtimeLoading || pullDiagnostic) && <Accordion multiple value={expandedContext} onValueChange={(values) => setExpandedContext(values as string[])}>
          {(runtimeEvents || runtimeLoading) && <AccordionItem value="runtime">
            <AccordionTrigger>
              <span className="flex min-w-0 flex-wrap items-center gap-2">
                <span>{lang === "zh" ? "运行事件" : "Runtime events"}</span>
                <Badge variant={runtimeStatus === "running" ? "success" : runtimeStatus === "failed" ? "destructive" : "secondary"}>
                  {runtimeLoading && !runtimeEvents && <Spinner aria-hidden="true" data-icon="inline-start" />}
                  {runtimeLoading && !runtimeEvents ? (lang === "zh" ? "加载中" : "Loading") : runtimeStatusLabel}
                </Badge>
                <Badge variant="outline">{eventLines.length} {lang === "zh" ? "条事件" : "events"}</Badge>
              </span>
            </AccordionTrigger>
            <AccordionContent className="flex flex-col gap-3">
              {runtimeEvents ? <>
                <Table aria-label={lang === "zh" ? "最近实例信息" : "Latest instance details"}>
                  <TableBody>
                    <TableRow><TableHead scope="row">{lang === "zh" ? "实例" : "Instance"}</TableHead><TableCell><code className="break-all whitespace-normal">{runtimeEvents.allocId || "—"}</code></TableCell></TableRow>
                    <TableRow><TableHead scope="row">{lang === "zh" ? "节点" : "Node"}</TableHead><TableCell><code className="break-all whitespace-normal">{runtimeEvents.node || "—"}</code></TableCell></TableRow>
                    <TableRow><TableHead scope="row">{lang === "zh" ? "镜像" : "Image"}</TableHead><TableCell><code className="break-all whitespace-normal">{runtimeEvents.image || "—"}</code></TableCell></TableRow>
                  </TableBody>
                </Table>
                {eventLines.length ? <pre className="log-console__event-output" tabIndex={0} aria-label={lang === "zh" ? "运行事件全文" : "Full runtime events"}>{eventLines.map(runtimeEventLabel).join("\n")}</pre> : <Empty>
                  <EmptyHeader><EmptyTitle>{lang === "zh" ? "暂无运行事件" : "No runtime events yet"}</EmptyTitle></EmptyHeader>
                </Empty>}
              </> : <div className="flex flex-col gap-3" role="status" aria-label={lang === "zh" ? "加载运行事件" : "Loading runtime events"}>
                <Skeleton className="h-4 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-1/2" />
              </div>}
            </AccordionContent>
          </AccordionItem>}
          {pullDiagnostic && <AccordionItem value="diagnostic">
            <AccordionTrigger>
              <span className="flex min-w-0 flex-wrap items-center gap-2">
                <span>{lang === "zh" ? "镜像拉取诊断" : "Image pull diagnostic"}</span>
                <Badge variant={pullLoading ? "secondary" : pullDiagnostic.ok ? "success" : "destructive"}>
                  {pullLoading && <Spinner aria-hidden="true" data-icon="inline-start" />}
                  {pullLoading ? (lang === "zh" ? "进行中" : "Running") : pullDiagnostic.ok ? (lang === "zh" ? "成功" : "Succeeded") : (lang === "zh" ? "失败" : "Failed")}
                </Badge>
                {pullDiagnostic.exitCode !== undefined && <Badge variant="outline">exit {pullDiagnostic.exitCode}</Badge>}
              </span>
            </AccordionTrigger>
            <AccordionContent className="flex flex-col gap-3">
              <Table aria-label={lang === "zh" ? "镜像拉取诊断信息" : "Pull diagnostic details"}>
                <TableBody>
                  <TableRow><TableHead scope="row">{lang === "zh" ? "节点" : "Node"}</TableHead><TableCell><code className="break-all whitespace-normal">{pullDiagnostic.node || "—"}</code></TableCell></TableRow>
                  <TableRow><TableHead scope="row">{lang === "zh" ? "任务" : "Task"}</TableHead><TableCell><code className="break-all whitespace-normal">{pullDiagnostic.taskId || "—"}</code></TableCell></TableRow>
                  <TableRow><TableHead scope="row">{lang === "zh" ? "镜像" : "Image"}</TableHead><TableCell><code className="break-all whitespace-normal">{pullDiagnostic.image || "—"}</code></TableCell></TableRow>
                </TableBody>
              </Table>
              {pullDiagnostic.lines.length ? <pre className="log-console__event-output" tabIndex={0} aria-label={lang === "zh" ? "镜像拉取诊断全文" : "Full pull diagnostic"}>{pullDiagnostic.lines.join("\n")}</pre> : <Empty>
                <EmptyHeader>
                  <EmptyMedia variant="icon">{pullLoading ? <Spinner aria-hidden="true" /> : <Activity />}</EmptyMedia>
                  <EmptyTitle>{pullLoading ? (lang === "zh" ? "等待镜像拉取输出" : "Waiting for pull output") : (lang === "zh" ? "暂无诊断输出" : "No diagnostic output")}</EmptyTitle>
                </EmptyHeader>
              </Empty>}
            </AccordionContent>
          </AccordionItem>}
        </Accordion>}

        {warnings.length > 0 && <Alert>
          <Info />
          <AlertTitle>{lang === "zh" ? `日志源提示 · ${warnings.length} 项` : `${warnings.length} source notices`}</AlertTitle>
          <AlertDescription><ul className="flex max-h-40 list-disc flex-col gap-1 overflow-auto pl-4">{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></AlertDescription>
        </Alert>}
        {noticeMessages.length > 0 && <Alert variant="destructive">
          <Info />
          <AlertTitle>{lang === "zh" ? "日志读取异常" : "Log read error"}</AlertTitle>
          <AlertDescription className="max-h-40 overflow-auto">{noticeMessages.map((message, index) => <p key={`${index}-${message}`}>{message}</p>)}</AlertDescription>
        </Alert>}

        <div className="flex flex-wrap items-center justify-between gap-2">
          <span id={`${fieldId}-output`}>{lang === "zh" ? "日志输出" : "Log output"}</span>
          <Badge variant="outline">{filteredLogs.length} / {logsState?.logs?.length || 0} {lang === "zh" ? "行" : "lines"}{logsState?.updatedAt ? ` · ${new Date(logsState.updatedAt * 1000).toLocaleTimeString()}` : ""}</Badge>
        </div>
        {filteredLogs.length ? <pre ref={logTailRef} className={cn("log-console__viewport", wrapLines && "is-wrapped")} tabIndex={0} aria-labelledby={`${fieldId}-output`} onScroll={(event) => {
          const tail = event.currentTarget;
          followBottomRef.current = tail.scrollHeight - tail.scrollTop - tail.clientHeight < 48;
        }}>{filteredLogs.join("\n")}</pre> : <Empty className="min-h-60" role="status">
          <EmptyHeader>
            <EmptyMedia variant="icon">{logsLoading ? <Spinner aria-hidden="true" /> : keyword.trim() ? <Search /> : <Terminal />}</EmptyMedia>
            <EmptyTitle>{logsLoading ? (lang === "zh" ? "连接日志源" : "Connecting to log sources") : keyword.trim() ? (lang === "zh" ? "没有匹配的日志" : "No matching logs") : (lang === "zh" ? "暂无日志" : "No logs yet")}</EmptyTitle>
            <EmptyDescription>{emptyMessage}</EmptyDescription>
          </EmptyHeader>
        </Empty>}
      </Content>

      <Footer className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">{lang === "zh" ? `页面保留 ${MAX_LINES} 行` : `View retains ${MAX_LINES} lines`}{logsState?.droppedLines ? (lang === "zh" ? ` · 已移除 ${logsState.droppedLines} 行` : ` · ${logsState.droppedLines} older lines removed`) : ""}</p>
        <Popover>
          <PopoverTrigger render={<Button type="button" variant="ghost" size="sm" />}>
            <Info data-icon="inline-start" />{lang === "zh" ? "读取与保留范围" : "Read and retention limits"}
          </PopoverTrigger>
          <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)]">
            <PopoverHeader>
              <PopoverTitle>{lang === "zh" ? "读取与保留范围" : "Read and retention limits"}</PopoverTitle>
              <PopoverDescription>{lang === "zh" ? "初次读取最近约 200 行，下载最近约 500 行，均按日志源分配。历史取决于节点日志轮转，无法保证覆盖指定时间。暂停后继续将补取仍可用的日志。复制包含当前关键词筛选后的内容。" : "Initial read targets 200 recent lines; download targets 500, shared across sources. History depends on node rotation with no guaranteed time window. Resume retrieves logs still available. Copy includes the current keyword filter."}</PopoverDescription>
            </PopoverHeader>
          </PopoverContent>
        </Popover>
      </Footer>
    </>
  );
  return inline ? (
    <Card className="log-console min-w-0" aria-labelledby={`${fieldId}-title`}>{content}</Card>
  ) : (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="log-console max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[min(1280px,calc(100%-2rem))]" showCloseButton={false}>{content}</DialogContent>
    </Dialog>
  );
}
