import { ExternalAlerts, evidenceSummary } from "./ExternalAlerts";
import { ActiveSourceSilences, AlertSourceControls } from "./AlertSourceControls";
import { LogIncidentDetails } from "./LogIncidentDetails";
import { LogRuleFields } from "./LogRuleFields";
import { fetchAlertTab } from "../alertingApi";
import { useCallback, useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";

import type { FormEvent } from "react";
import type { Lang } from "../types";
import {
  alertEventLabel,
  localizeAlertPreset,
  alertStatusLabel,
  channelRequest,
  deleteAlerting,
  getAlerting,
  mergeAlertPages,
  newAlertRule,
  isTaskMetric,
  alertsOnMissingData,
  requiresTaskService,
  postAlerting,
  ruleRequest,
} from "../alertingApi";
import type {
  AlertChannel,
  AlertDelivery,
  AlertIncidentDetail,
  AlertSourceSilence,
  AlertIncident,
  AlertOverview,
  AlertPage,
  AlertPreset,
  AlertRule,
  AlertTab,
} from "../alertingApi";
import { spaLink, useRouter, useSearchParams } from "../router";
import { SelectControl } from "./primitives";
import { AlertCircle, ArrowLeft, Bell, BellSlash, CheckCircle2, MoreHorizontal, Pause, Pencil, Play, Plus } from "@/components/icons";
import { formatClock, useObserveRefresh } from "./observeRefresh";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox";
import { cn } from "@/lib/utils";
import { useConfirm } from "./ConfirmDialog";
import { EMPTY_VALUE, formatDateTime } from "../format";

function stamp(value: number | null | undefined, zh: boolean) {
  return value ? formatDateTime(value, zh ? "zh" : "en") : EMPTY_VALUE;
}
/** Compact elapsed time between two Unix-second stamps ("6 分钟" / "2h 5m"). */
export function formatDuration(from: number | null | undefined, to: number | null | undefined, zh: boolean) {
  if (!from) return EMPTY_VALUE;
  const seconds = Math.max(0, Math.round((to || Date.now() / 1000) - from));
  if (seconds < 60) return zh ? "不到 1 分钟" : "<1m";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return zh ? `${minutes} 分钟` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return zh ? `${hours} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ""}` : `${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ""}`;
  const days = Math.floor(hours / 24);
  return zh ? `${days} 天${hours % 24 ? ` ${hours % 24} 小时` : ""}` : `${days}d${hours % 24 ? ` ${hours % 24}h` : ""}`;
}
function formatSeconds(value: number | null | undefined, zh: boolean) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  if (seconds < 60 || seconds % 60) return `${seconds}${zh ? " 秒" : "s"}`;
  const minutes = seconds / 60;
  if (minutes < 60 || minutes % 60) return zh ? `${minutes} 分钟` : `${minutes}m`;
  return zh ? `${minutes / 60} 小时` : `${minutes / 60}h`;
}
function AlertStatus({ status, zh }: { status: string; zh: boolean }) {
  const variant = ["firing", "failed", "critical"].includes(status)
    ? "destructive"
    : ["pending", "warning", "retry", "sending"].includes(status)
      ? "warning"
      : ["sent", "resolved", "delivered"].includes(status)
        ? "success"
        : "outline";
  // Settled states read quietly; only states that need a look get a filled tint.
  return (
    <Badge variant={variant} appearance={variant === "success" || variant === "outline" ? "dot" : "filled"}>
      {alertStatusLabel(status, zh)}
    </Badge>
  );
}

function IncidentActions({ item, zh, busy, compact = false, onStop, onClose }: {
  item: AlertIncident;
  zh: boolean;
  busy: boolean;
  compact?: boolean;
  onStop: (item: AlertIncident) => void;
  onClose: (item: AlertIncident) => void;
}) {
  if (!["pending", "firing"].includes(item.status)) return null;
  return <>
    {!item.acknowledgedAt && (
      <Button type="button" variant="outline" size={compact ? "sm" : "default"}
        disabled={busy} onClick={() => onStop(item)}>
        {zh ? "停止提醒" : "Stop reminders"}
      </Button>
    )}
    <Button type="button" variant="outline" size={compact ? "sm" : "default"}
      disabled={busy} onClick={() => onClose(item)}>
      {zh ? "关闭告警" : "Close incident"}
    </Button>
  </>;
}

/** Event details are free text from Control; structured values read as `key: value`. */
function eventDetailText(detail: unknown): string {
  if (detail === null || detail === undefined || detail === "") return "—";
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && !Array.isArray(detail)) {
    const parts = Object.entries(detail as Record<string, unknown>).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`);
    return parts.length ? parts.join(" · ") : "—";
  }
  return JSON.stringify(detail);
}

function AlertEmpty({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <Empty className="in-data-[slot=card-content]:border-t in-data-[slot=card-content]:border-hairline">
      <EmptyHeader>
        <EmptyMedia variant="icon"><CheckCircle2 /></EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

export function formatAlertObservedValue(
  metric: string,
  value: number | null | undefined,
  noData: boolean | undefined,
  zh: boolean,
): string {
  if (noData || typeof value !== "number" || !Number.isFinite(value))
    return zh ? "缺少采样" : "No data";
  if (metric === "build.failed") {
    if (value === 1) return zh ? "失败" : "Failed";
    if (value === 0) return zh ? "未失败" : "Not failed";
  }
  const formatted = new Intl.NumberFormat(zh ? "zh-CN" : "en-US", {
    maximumFractionDigits: 1,
  }).format(value);
  if (
    metric === "node.offline" ||
    metric === "task.queue_age" ||
    metric === "observe.metrics_age" ||
    metric === "app.task_heartbeat_age" ||
    metric === "app.task_queue_wait_p95" ||
    metric === "app.http_p95"
  )
    return `${formatted} ${zh ? "秒" : "s"}`;
  if (["node.cpu", "node.memory", "node.disk", "node.inode", "app.memory"].includes(metric))
    return `${formatted}%`;
  if (metric === "app.http_5xx_ratio")
    return `${new Intl.NumberFormat(zh ? "zh-CN" : "en-US", { style: "percent", maximumFractionDigits: 1 }).format(value)}`;
  return formatted;
}

export function AlertingPanel({
  lang,
  token,
  tab,
  nodeNames = [],
  applicationNames = [],
  application,
}: {
  lang: Lang;
  token: string;
  tab: AlertTab;
  nodeNames?: string[];
  applicationNames?: string[];
  application?: string;
}) {
  const zh = lang === "zh";
  const refreshHost = useObserveRefresh();
  const { confirm, element: confirmElement } = useConfirm(lang);
  const { path, navigate } = useRouter();
  const query = useSearchParams();
  const appFilter = application || query.get("app") || "";
  const appQuery = appFilter ? `app=${encodeURIComponent(appFilter)}` : "";
  const [knownApps, setKnownApps] = useState<string[]>([]);
  useEffect(() => {
    if (application || tab === "notifications") return;
    const controller = new AbortController();
    void getAlerting<AlertPage<string>>("applications", token, controller.signal).then(result => setKnownApps(result.items)).catch(() => {});
    return () => controller.abort();
  }, [token, application, tab]);
  const routeId = (() => {
    try {
      return decodeURIComponent(path.split("/")[application ? 4 : 3] || "");
    } catch {
      return path.split("/")[application ? 4 : 3] || "";
    }
  })();
  const basePath = application ? `/apps/${encodeURIComponent(application)}/alerts` : (
    tab === "rules"
      ? "/observe/rules"
      : tab === "notifications"
        ? "/observe/channels"
        : "/observe") + (appQuery ? `?${appQuery}` : "");
  const editor = Boolean(routeId);
  const initializedRoute = useRef("");
  const [overview, setOverview] = useState<AlertOverview>();
  const [presets, setPresets] = useState<AlertPreset[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [channels, setChannels] = useState<AlertChannel[]>([]);
  const [incidents, setIncidents] = useState<AlertPage<AlertIncident>>({
    items: [],
  });
  const [deliveries, setDeliveries] = useState<AlertPage<AlertDelivery>>({
    items: [],
  });
  const [filter, setFilter] = useState("firing");
  const [incidentsFilter, setIncidentsFilter] = useState("");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rule, setRule] = useState<AlertRule | null>(null);
  const [channel, setChannel] = useState<AlertChannel | null>(null);
  const [secret, setSecret] = useState("");
  const [callbackToken, setCallbackToken] = useState("");
  const [callbackKey, setCallbackKey] = useState("");
  const [sourceSilences, setSourceSilences] = useState<AlertSourceSilence[]>([]);
  const [detail, setDetail] = useState<AlertIncidentDetail>();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const reload = () => {
      setExpanded(false);
      setRefresh((n) => n + 1);
    };
    window.addEventListener("luma:refresh", reload);
    return () => window.removeEventListener("luma:refresh", reload);
  }, []);
  const [expanded, setExpanded] = useState(false);
  const mounted = useRef(true);
  const actionController = useRef<AbortController | null>(null);
  const scope = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      actionController.current?.abort();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController;
    const load = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const next = await fetchAlertTab(tab, token, filter, controller.signal, appFilter);
        if (cancelled) return;
        setOverview(next.overview);
        if (next.presets)
          setPresets(
            next.presets.items.map((preset) => localizeAlertPreset(preset, zh)),
          );
        if (next.rules) setRules(next.rules.items);
        if (next.channels) setChannels(next.channels.items);
        if (next.sourceSilences) setSourceSilences(next.sourceSilences.items);
        // Expanded history remains a browsing snapshot until explicit refresh.
        if (!expanded && next.incidents) {
          setIncidents(next.incidents);
          setIncidentsFilter(`${filter}:${appFilter}`);
        }
        if (!expanded && next.deliveries) setDeliveries(next.deliveries);
        setLoaded(true);
        setError("");
        refreshHost.markUpdated();
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          refreshHost.setRefreshing(false);
        }
      } finally {
        window.clearTimeout(timeout);
        if (!cancelled) timer = window.setTimeout(() => void load(), 15000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearTimeout(timer);
    };
  }, [token, tab, filter, appFilter, refresh, expanded, zh, refreshHost]);

  useEffect(() => {
    scope.current += 1;
    actionController.current?.abort();
    setDetail(undefined);
    setActionError("");
    setFieldErrors({});
    setRule(null);
    setChannel(null);
    setSecret("");
    setCallbackToken("");
    setCallbackKey("");
  }, [tab, token, filter, appFilter, path]);
  useEffect(() => {
    if (!loaded || initializedRoute.current === path) return;
    initializedRoute.current = path;
    setActionError("");
    setRule(null);
    setChannel(null);
    setSecret("");
    setCallbackToken("");
    setCallbackKey("");
    if (!routeId) return;
    if (tab === "rules") {
      const item =
        routeId === "new"
          ? presets[0]
            ? { ...newAlertRule(presets[0]), ...(appFilter ? { target: appFilter } : {}) }
            : null
          : rules.find((item) => item.id === routeId);
      if (item) setRule({ ...item, channelIds: [...item.channelIds] });
      else setActionError(zh ? "规则不存在或已删除。" : "Rule not found.");
    }
    if (tab === "notifications") {
      const item =
        routeId === "new"
          ? {
              id: "",
              name: zh ? "飞书告警" : "Feishu alerts",
              type: "feishu" as const,
              enabled: true,
              appId: "",
              chatId: "",
              appSecretConfigured: false,
            }
          : channels.find((item) => item.id === routeId);
      if (item) setChannel({ ...item });
      else setActionError(zh ? "渠道不存在或已删除。" : "Channel not found.");
    }
  }, [loaded, path, routeId, tab, presets, rules, channels, zh]);
  useEffect(() => {
    if (tab !== "alerts" || !routeId) return;
    const controller = new AbortController();
    let cancelled = false;
    setDetail(undefined);
    void getAlerting<AlertIncidentDetail>(
      `incidents/${encodeURIComponent(routeId)}${appQuery ? `?${appQuery}` : ""}`,
      token,
      controller.signal,
    )
      .then((result) => {
        if (!cancelled) setDetail(result);
      })
      .catch((err) => {
        if (!cancelled) setActionError(String(err));
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [tab, routeId, token, refresh, appQuery]);
  const act = useCallback(
    async (
      action: (signal: AbortSignal) => Promise<unknown>,
      success?: string,
      refreshAfter = true,
    ) => {
      if (busy) return;
      const currentScope = scope.current;
      const controller = new AbortController();
      actionController.current = controller;
      setBusy(true);
      setActionError("");
      setNotice("");
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      try {
        await action(controller.signal);
        if (mounted.current && currentScope === scope.current) {
          if (success) setNotice(success);
          if (refreshAfter) {
            setExpanded(false);
            setRefresh((n) => n + 1);
          }
        }
      } catch (err) {
        if (mounted.current && currentScope === scope.current)
          setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        window.clearTimeout(timeout);
        if (mounted.current) setBusy(false);
      }
    },
    [busy],
  );
  function editChannel(item: AlertChannel) {
    navigate(`/observe/channels/${encodeURIComponent(item.id || "new")}`);
  }
  const silenceActive = (overview?.silencedUntil || 0) > Date.now() / 1000;
  const appFilterControl = !application ? (
    <div className="w-full sm:w-56">
      <Combobox items={["", ...new Set([...applicationNames, ...knownApps, appFilter].filter(Boolean))]}
        value={appFilter} onValueChange={(value) => {
          setExpanded(false);
          navigate(`${tab === "rules" ? "/observe/rules" : "/observe"}${value ? `?app=${encodeURIComponent(value)}` : ""}`);
        }}>
        <ComboboxInput id="alert-app-filter" className="w-full" aria-label={zh ? "筛选应用" : "Filter by application"} placeholder={zh ? "全部应用" : "All applications"} />
        <ComboboxContent><ComboboxEmpty>{zh ? "没有匹配的应用" : "No matching applications"}</ComboboxEmpty>
          <ComboboxList>{(name: string) => <ComboboxItem key={name || "all"} value={name}>{name || (zh ? "全部应用" : "All applications")}</ComboboxItem>}</ComboboxList>
        </ComboboxContent>
      </Combobox>
    </div>
  ) : null;
  const crossLink = appFilter && !application ? (
    <Button variant="ghost" size="sm" onClick={() => navigate(`${tab === "rules" ? "/observe" : "/observe/rules"}?${appQuery}`)}>
      {tab === "rules" ? (zh ? "查看此应用告警" : "View app incidents") : (zh ? "管理此应用规则" : "Manage app rules")}
    </Button>
  ) : null;
  const incidentHref = (item: AlertIncident) =>
    `/${application ? `apps/${encodeURIComponent(application)}/alerts` : "observe/incidents"}/${encodeURIComponent(item.id)}${appQuery ? `?${appQuery}` : ""}`;
  const channelNames = (ids: string[]) =>
    ids.map((id) => channels.find((c) => c.id === id)?.name || id).join(", ");
  const refreshNow = () => {
    setExpanded(false);
    setRefresh((n) => n + 1);
  };
  function validateForm(form: HTMLFormElement) {
    const errors: Record<string, string> = {};
    for (const element of Array.from(form.elements)) {
      if (
        element instanceof HTMLInputElement &&
        element.id &&
        !element.validity.valid
      )
        errors[element.id] = element.validationMessage;
    }
    setFieldErrors(errors);
    if (Object.keys(errors).length) {
      (
        form.querySelector(`#${Object.keys(errors)[0]}`) as HTMLElement | null
      )?.focus();
      return false;
    }
    return true;
  }
  function clearFieldError(event: FormEvent<HTMLFormElement>) {
    // Run during bubbling so controlled inputs commit their change before React
    // restores DOM values; capture-phase state updates can erase the first edit.
    const input = event.target;
    if (
      !(input instanceof HTMLInputElement) ||
      !input.id ||
      !fieldErrors[input.id] ||
      !input.validity.valid
    )
      return;
    setFieldErrors((previous) => {
      const next = { ...previous };
      delete next[input.id];
      return next;
    });
  }
  function saveRule(event: FormEvent) {
    event.preventDefault();
    if (!rule || !validateForm(event.currentTarget as HTMLFormElement)) return;
    void act(
      async (signal) => {
        await postAlerting("rules", token, ruleRequest(rule), signal);
        if (signal.aborted) return;
        setRule(null);
        setRefresh((n) => n + 1);
        navigate(basePath);
      },
      zh ? "规则已保存" : "Rule saved",
    );
  }
  function saveChannel(event: FormEvent) {
    event.preventDefault();
    if (!channel || !validateForm(event.currentTarget as HTMLFormElement))
      return;
    void act(
      async (signal) => {
        await postAlerting(
          "channels",
          token,
          { ...channelRequest(
            channel.id,
            channel.name,
            channel.enabled,
            channel.appId,
            secret,
            channel.chatId,
          ), cardActionsEnabled: Boolean(channel.cardActionsEnabled),
            ...(callbackToken ? { callbackVerificationToken: callbackToken } : {}),
            ...(callbackKey ? { callbackEncryptKey: callbackKey } : {}) },
          signal,
        );
        if (signal.aborted) return;
        setChannel(null);
        setSecret("");
        setCallbackToken("");
        setCallbackKey("");
        setRefresh((n) => n + 1);
        navigate(basePath);
      },
      zh
        ? "渠道已保存，尚未发送测试消息"
        : "Channel saved; no test message has been sent",
    );
  }

  const targetNames = rule?.metric.startsWith("node.")
    ? nodeNames
    : rule?.metric === "task.queue_age"
      ? ["agent", "builder", "build"]
      : applicationNames;
  const actOnIncident = (item: AlertIncident, action: "ack" | "close") => {
    void act(
      (signal) => postAlerting(
        `incidents/${encodeURIComponent(item.id)}/${action}`,
        token,
        { expectedBuildId: item.buildDetails?.buildId || "" },
        signal,
      ),
      action === "close"
        ? zh ? "告警已关闭，可在“已关闭”中查看历史" : "Incident closed; history is available under Closed"
        : zh ? "已停止本次故障提醒；告警仍在当前列表，恢复时会通知" : "Fault reminders stopped; this incident remains active and can notify on recovery",
    );
  };
  const closeIncident = async (item: AlertIncident) => {
    const currentScope = scope.current;
    if (!(await confirm({
      title: zh ? `关闭告警 #${item.id}？` : `Close incident #${item.id}?`,
      body: item.metric === "build.failed"
        ? zh
          ? `关闭「${item.target}」的本次告警，停止后续提醒并保留历史。以后新的构建失败仍会正常告警。`
          : `Close this incident for “${item.target}”, stop further reminders and keep its history. New failed builds will still alert.`
        : zh
          ? `关闭「${item.target}」的本次告警，停止后续提醒并保留历史。当前异常恢复后，再次异常仍会正常告警。`
          : `Close this incident for “${item.target}”, stop further reminders and keep its history. After the condition recovers, a new breach will alert again.`,
      confirmLabel: zh ? "关闭告警" : "Close incident",
      tone: "neutral",
    })) || currentScope !== scope.current) return;
    actOnIncident(item, "close");
  };
  const deleteRule = async () => {
    if (
      !rule ||
      !(await confirm({
        title: zh
          ? `删除规则「${rule.name}」？`
          : `Delete rule “${rule.name}”?`,
        body: zh
          ? "已有告警历史仍会保留。"
          : "Existing incident history will remain.",
        confirmLabel: zh ? "删除规则" : "Delete rule",
      }))
    )
      return;
    void act(async (signal) => {
      await deleteAlerting(
        `rules/${encodeURIComponent(rule.id)}`,
        token,
        signal,
      );
      if (signal.aborted) return;
      setRule(null);
      setRefresh((n) => n + 1);
      navigate(basePath);
    });
  };
  const deleteChannel = async () => {
    if (
      !channel ||
      !(await confirm({
        title: zh
          ? `删除渠道「${channel.name}」？`
          : `Delete channel “${channel.name}”?`,
        body: zh
          ? "依赖此渠道的规则将无法通过它发送通知。"
          : "Rules will no longer be able to notify through this channel.",
        confirmLabel: zh ? "删除渠道" : "Delete channel",
      }))
    )
      return;
    void act(async (signal) => {
      await deleteAlerting(
        `channels/${encodeURIComponent(channel.id)}`,
        token,
        signal,
      );
      if (signal.aborted) return;
      setChannel(null);
      setSecret("");
      setCallbackToken("");
      setCallbackKey("");
      setRefresh((n) => n + 1);
      navigate(basePath);
    });
  };

  return (
    <div className={cn("flex min-w-0 flex-col gap-6", editor && "max-w-4xl")}>
      {confirmElement}
      {editor && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-fit"
          onClick={() => navigate(basePath)}
        >
          <ArrowLeft data-icon="inline-start" aria-hidden="true" />
          {zh ? "返回列表" : "Back to list"}
        </Button>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>
            {loaded
              ? zh
                ? "刷新失败"
                : "Refresh failed"
              : zh
                ? "告警服务读取失败"
                : "Unable to load alerting"}
          </AlertTitle>
          <AlertDescription>
            {loaded
              ? zh
                ? "以下为上次成功读取的数据。"
                : "Showing previously loaded data."
              : null}{" "}
            {error}
          </AlertDescription>
          <AlertAction>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={refreshNow}
            >
              {zh ? "重试" : "Retry"}
            </Button>
          </AlertAction>
        </Alert>
      )}
      {actionError && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>{zh ? "操作失败" : "Action failed"}</AlertTitle>
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}
      {notice && (
        <Alert>
          <AlertTitle>{zh ? "操作结果" : "Action result"}</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      {!loaded && !error && (
        <Card aria-busy="true">
          <CardHeader>
            <CardTitle>
              {zh
                ? "正在读取告警配置与状态"
                : "Loading alert configuration and status"}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Skeleton className="h-5 w-1/3" />
            <Skeleton className="h-24 w-full" />
          </CardContent>
        </Card>
      )}
      {loaded && overview && (
        <>
          {!editor && tab === "alerts" && (
            <Card className="gap-0 py-0">
              <dl className="grid grid-cols-2 md:grid-cols-4 md:divide-x md:divide-hairline max-md:[&>div:nth-child(n+3)]:border-t max-md:[&>div:nth-child(even)]:border-l [&>div]:border-hairline">
                {[
                  { key: "firing", label: zh ? "触发中" : "Firing", value: overview.counts.firing, tone: overview.counts.firing ? "critical" : "normal", detail: overview.counts.firing ? (zh ? "需要处理" : "Needs attention") : (zh ? "没有触发中的告警" : "Nothing firing") },
                  { key: "pending", label: zh ? "等待持续条件" : "Pending duration", value: overview.counts.pending, tone: overview.counts.pending ? "warning" : "normal", detail: zh ? "条件满足但未达持续时间" : "Condition met, duration not reached" },
                  { key: "rules", label: zh ? "启用规则" : "Enabled rules", value: overview.enabledRules, tone: "normal", detail: overview.lastEvaluatedAt ? `${zh ? "最近检查" : "Last evaluated"} ${formatClock(overview.lastEvaluatedAt, lang)}` : (zh ? "等待首次检查" : "Awaiting first evaluation") },
                  { key: "notify", label: zh ? "通知" : "Notifications", value: silenceActive ? (zh ? "已静默" : "Silenced") : (zh ? "正常发送" : "Sending"), tone: silenceActive ? "warning" : "normal", detail: silenceActive ? `${zh ? "至" : "Until"} ${formatClock(overview.silencedUntil, lang)}` : (overview.channels ? (zh ? `${overview.channels} 个渠道` : `${overview.channels} channels`) : (zh ? "尚未配置渠道" : "No channels configured")) },
                ].map((item) => (
                  <div key={item.key} className="flex min-w-0 flex-col gap-1.5 px-5 py-4">
                    <dt className="flex min-h-5 items-center gap-1.5 text-xs font-medium text-muted-foreground">
                      {item.tone === "critical" ? <span className="size-1.5 rounded-full bg-destructive" aria-hidden="true" /> : item.tone === "warning" ? <span className="size-1.5 rounded-full bg-warning" aria-hidden="true" /> : null}
                      <span className="truncate">{item.label}</span>
                    </dt>
                    <dd className="flex min-w-0 flex-col gap-0.5">
                      <span className={cn("truncate text-xl/7 font-semibold tracking-[-0.01em] tabular-nums", item.tone === "critical" && "text-destructive-foreground", item.tone === "warning" && "text-warning-foreground")}>{item.value}</span>
                      <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                        <span className="truncate">{item.detail}</span>
                        {item.key === "notify" && !appFilter ? (
                          <Button
                            type="button"
                            variant="link"
                            size="xs"
                            className="h-auto px-0 text-xs"
                            disabled={busy}
                            title={silenceActive ? undefined : (zh ? "维护静默暂停通知，保留异常记录；到期自动恢复，手动测试仍会发送。" : "Silence pauses notifications but keeps recording incidents; it ends automatically and manual tests still send.")}
                            onClick={() =>
                              void act(
                                (signal) =>
                                  postAlerting(
                                    "silence",
                                    token,
                                    { seconds: silenceActive ? 0 : 3600 },
                                    signal,
                                  ),
                                zh ? "静默设置已更新" : "Silence updated",
                              )
                            }
                          >
                            {silenceActive
                              ? zh ? "解除静默" : "End silence"
                              : zh ? "维护静默 1 小时" : "Silence for 1 hour"}
                          </Button>
                        ) : null}
                      </span>
                    </dd>
                  </div>
                ))}
              </dl>
              {(!overview.lastEvaluatedAt || Date.now() / 1000 - overview.lastEvaluatedAt > 120) && <div className="flex flex-col gap-3 border-t border-hairline p-4">
                {!overview.lastEvaluatedAt && (
                  <Alert>
                    <AlertTitle>
                      {zh ? "等待首次检查" : "Waiting for first evaluation"}
                    </AlertTitle>
                    <AlertDescription>
                      {zh
                        ? "尚未完成后台检查，状态将自动刷新。"
                        : "No background evaluation has completed yet. Status refreshes automatically."}
                    </AlertDescription>
                  </Alert>
                )}
                {!!overview.lastEvaluatedAt &&
                  Date.now() / 1000 - overview.lastEvaluatedAt > 120 && (
                    <Alert variant="destructive">
                      <AlertCircle />
                      <AlertTitle>
                        {zh ? "检查滞后" : "Evaluation stale"}
                      </AlertTitle>
                      <AlertDescription>
                        {zh
                          ? "后台检查已超过 2 分钟未更新，请检查 Manager。"
                          : "No evaluation for over 2 minutes. Check the Manager."}
                      </AlertDescription>
                    </Alert>
                  )}
              </div>}
            </Card>
          )}
          {silenceActive && tab !== "alerts" && (
            <Alert>
              <AlertTitle>
                {zh ? "通知已静默" : "Notifications silenced"}
              </AlertTitle>
              <AlertDescription>
                {zh
                  ? "告警通知正在维护静默（手动测试消息仍会发送），截至 "
                  : "Alert notifications are silenced; manual tests still send. Silence ends at "}
                {stamp(overview.silencedUntil, zh)}
              </AlertDescription>
            </Alert>
          )}
          {tab === "alerts" && !editor && (
            <Card>
              <CardHeader className="border-b border-hairline">
                <CardTitle>
                  {zh ? "Control 告警" : "Control incidents"}
                </CardTitle>
                <CardDescription>
                  {zh ? "由告警规则产生，可停止提醒或关闭。" : "Raised by alert rules. Stop reminders or close them here."}
                </CardDescription>
              </CardHeader>
              <div className="-mt-(--card-spacing) flex flex-wrap items-center gap-2 border-b border-hairline bg-muted/40 px-(--card-spacing) py-2.5" role="toolbar" aria-label={zh ? "筛选告警" : "Filter incidents"}>
                <ToggleGroup
                  variant="segmented"
                  value={[filter || "all"]}
                  onValueChange={(values) => {
                    if (!values.length) return;
                    setExpanded(false);
                    setFilter(values[0] === "all" ? "" : values[0]);
                    setError("");
                  }}
                  aria-label={zh ? "告警状态" : "Incident state"}
                >
                  {[
                    { value: "firing", label: zh ? "触发中" : "Firing", count: overview.counts.firing },
                    { value: "pending", label: zh ? "等待持续条件" : "Pending", count: overview.counts.pending },
                    { value: "resolved", label: zh ? "已恢复" : "Resolved" },
                    { value: "closed", label: zh ? "已关闭" : "Closed" },
                    { value: "all", label: zh ? "全部历史" : "All history" },
                  ].map((item) => (
                    <ToggleGroupItem key={item.value} value={item.value}>
                      {item.label}
                      {item.count ? <span className="rounded-full bg-muted px-1.5 text-[0.6875rem]/4 tabular-nums text-muted-foreground">{item.count}</span> : null}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  {crossLink}
                  {appFilterControl}
                </div>
              </div>
              <CardContent className="-mt-(--card-spacing)">
                {incidentsFilter !== `${filter}:${appFilter}` ? (
                  error ? null : (
                    <div aria-busy="true" className="flex flex-col gap-3 px-(--card-spacing) py-4">
                      <span role="status" className="sr-only">
                        {zh ? "正在读取筛选结果…" : "Loading filtered incidents…"}
                      </span>
                      <Skeleton className="h-10 w-full" />
                      <Skeleton className="h-10 w-full" />
                    </div>
                  )
                ) : incidents.items.length ? (
                  <Table className="max-sm:block"
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label": zh ? "告警事件列表" : "Alert incidents",
                    }}
                  >
                    <TableHeader className="hidden sm:table-header-group">
                      <TableRow>
                        <TableHead>
                          {zh ? "告警" : "Incident"}
                        </TableHead>
                        <TableHead>{zh ? "状态" : "State"}</TableHead>
                        <TableHead className="text-right">{zh ? "观测值" : "Observed"}</TableHead>
                        <TableHead className="pl-8">
                          {zh ? "开始" : "Started"}
                        </TableHead>
                        <TableHead className="w-12">
                          <span className="sr-only">{zh ? "操作" : "Actions"}</span>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody className="max-sm:block">
                      {incidents.items.map((item) => (
                        <TableRow key={item.id} className="max-sm:relative max-sm:flex max-sm:flex-col max-sm:gap-2 max-sm:px-(--card-spacing) max-sm:py-3.5 max-sm:pr-12">
                          <TableCell className="max-sm:block max-sm:whitespace-normal max-sm:p-0">
                            <div className="flex min-w-0 flex-col items-start gap-1">
                              <span className="flex min-w-0 flex-wrap items-center gap-2">
                                <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", item.severity === "critical" ? "bg-destructive" : "bg-warning")} />
                                <a {...spaLink(incidentHref(item), navigate)} className="font-medium text-foreground underline-offset-4 hover:underline focus-visible:underline">
                                  {item.ruleName}
                                </a>
                                <span className="text-xs text-muted-foreground">
                                  {item.severity === "critical" ? (zh ? "严重" : "Critical") : (zh ? "警告" : "Warning")}
                                </span>
                              </span>
                              <span className="pl-4 text-[0.8125rem] text-muted-foreground">
                                {item.buildDetails?.service || item.target}
                              </span>
                              {item.logDetails?.summary && <span className="max-w-lg pl-4 line-clamp-2 wrap-anywhere text-xs text-muted-foreground">{evidenceSummary(item.logDetails.summary)}</span>}
                              {item.buildDetails?.reason && (
                                <span className="max-w-lg pl-4 line-clamp-2 wrap-anywhere text-xs text-muted-foreground">
                                  {item.buildDetails.stage ? `${item.buildDetails.stage}: ` : ""}{item.buildDetails.reason}
                                </span>
                              )}
                              <div className="mt-1 flex flex-wrap items-center gap-2 pl-4 sm:hidden">
                                <AlertStatus status={item.status} zh={zh} />
                                <span className="text-xs text-muted-foreground tabular-nums">{formatAlertObservedValue(item.metric, item.value, item.noData, zh)} · {formatDuration(item.startedAt, item.resolvedAt, zh)}</span>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell className="hidden sm:table-cell">
                            <div className="flex flex-col items-start gap-1">
                              <AlertStatus status={item.status} zh={zh} />
                              {item.acknowledgedAt ? (
                                <span className="text-xs text-muted-foreground">
                                  {zh ? "提醒已停止" : "Reminders stopped"}
                                </span>
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell className="hidden text-right font-medium tabular-nums sm:table-cell">
                            {formatAlertObservedValue(
                              item.metric,
                              item.value,
                              item.noData,
                              zh,
                            )}
                          </TableCell>
                          <TableCell className="hidden pl-8 sm:table-cell">
                            <div className="tabular-nums">{stamp(item.startedAt, zh)}</div>
                            <div className="text-xs text-muted-foreground">
                              {item.resolvedAt
                                ? `${item.status === "closed" ? (zh ? "已关闭" : "Closed") : (zh ? "已恢复" : "Resolved")} · ${zh ? "持续" : "lasted"} ${formatDuration(item.startedAt, item.resolvedAt, zh)}`
                                : `${zh ? "已持续" : "Ongoing for"} ${formatDuration(item.startedAt, null, zh)}`}
                            </div>
                          </TableCell>
                          <TableCell className="max-sm:absolute max-sm:top-2.5 max-sm:right-2 max-sm:p-0 sm:text-right">
                            <DropdownMenu>
                              <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" disabled={busy} aria-label={`${item.ruleName} · ${zh ? "更多操作" : "More actions"}`} />}>
                                <MoreHorizontal />
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end" className="w-52">
                                <DropdownMenuGroup>
                                  <DropdownMenuItem onClick={() => navigate(incidentHref(item))}>
                                    {zh ? "查看详情" : "View details"}
                                  </DropdownMenuItem>
                                  {["pending", "firing"].includes(item.status) && !item.acknowledgedAt && (
                                    <DropdownMenuItem onClick={() => actOnIncident(item, "ack")}>
                                      <span className="flex flex-col">
                                        {zh ? "停止提醒" : "Stop reminders"}
                                        <span className="text-xs text-muted-foreground">{zh ? "保留告警，恢复时仍通知" : "Keeps it open; notifies on recovery"}</span>
                                      </span>
                                    </DropdownMenuItem>
                                  )}
                                  {["pending", "firing"].includes(item.status) && (
                                    <DropdownMenuItem onClick={() => void closeIncident(item)}>
                                      <span className="flex flex-col">
                                        {zh ? "关闭告警" : "Close incident"}
                                        <span className="text-xs text-muted-foreground">{zh ? "结束本次告警并保留历史" : "Ends it and keeps history"}</span>
                                      </span>
                                    </DropdownMenuItem>
                                  )}
                                </DropdownMenuGroup>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <AlertEmpty
                    title={filter === "firing" ? (zh ? "没有触发中的告警" : "Nothing is firing") : (zh ? "暂无告警" : "No incidents")}
                    description={
                      zh
                        ? "当前筛选下暂无告警。可在告警规则中配置需要关注的条件。"
                        : "No incidents match this filter. Configure rules for the conditions you want to monitor."
                    }
                  />
                )}
              </CardContent>
              {incidentsFilter === `${filter}:${appFilter}` &&
                (incidents.nextCursor || expanded) && (
                  <CardFooter className="flex flex-wrap gap-3">
                    {incidents.nextCursor && (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          void act(
                            async (signal) => {
                              const page = await getAlerting<
                                AlertPage<AlertIncident>
                              >(
                                `incidents?limit=50&cursor=${encodeURIComponent(incidents.nextCursor!)}${appQuery ? `&${appQuery}` : ""}${filter ? `&status=${filter}` : ""}`,
                                token,
                                signal,
                              );
                              setIncidents((old) => ({
                                ...page,
                                items: mergeAlertPages(old.items, page.items),
                              }));
                              setExpanded(true);
                            },
                            undefined,
                            false,
                          )
                        }
                      >
                        {busy && (
                          <Spinner
                            data-icon="inline-start"
                            aria-hidden="true"
                          />
                        )}
                        {zh ? "加载更多历史" : "Load more history"}
                      </Button>
                    )}
                    {expanded && (
                      <span className="text-sm text-muted-foreground">
                        {zh
                          ? "分页浏览中，刷新可回到最新记录。"
                          : "Browsing history. Refresh to return to the latest records."}
                      </span>
                    )}
                  </CardFooter>
                )}
            </Card>
          )}
          {tab === "alerts" && !editor && <ExternalAlerts app={appFilter} lang={lang} token={token} />}
          {tab === "alerts" && !editor && <ActiveSourceSilences items={sourceSilences} zh={zh} busy={busy} stamp={stamp}
            onRemove={(item) => void act((signal) => postAlerting(`source-silences/remove${appQuery ? `?${appQuery}` : ""}`, token, item, signal), zh ? "已恢复此来源通知" : "Source notifications resumed")} />}
          {tab === "alerts" && editor && !detail && !actionError && (
            <Card aria-busy="true">
              <CardHeader>
                <CardTitle>
                  {zh ? "正在读取事件" : "Loading incident"}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <Skeleton className="h-40 w-full" />
              </CardContent>
            </Card>
          )}
          {tab === "alerts" && detail && (
            <Card>
              <CardHeader>
                <CardTitle>{detail.incident.ruleName}</CardTitle>
                <CardDescription>{detail.incident.target}</CardDescription>
                <CardAction>
                  <AlertStatus status={detail.incident.status} zh={zh} />
                </CardAction>
              </CardHeader>
              <CardContent className="flex flex-col gap-6">
                <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label={zh ? "告警概况" : "Incident summary"}>
                  {[
                    { key: "value", label: zh ? "观测值" : "Observed", value: formatAlertObservedValue(detail.incident.metric, detail.incident.value, detail.incident.noData, zh) },
                    { key: "started", label: zh ? "开始时间" : "Started", value: stamp(detail.incident.startedAt, zh) },
                    detail.incident.resolvedAt
                      ? {
                          key: "resolved",
                          label: detail.incident.status === "closed" ? (zh ? "关闭时间" : "Closed at") : (zh ? "恢复时间" : "Resolved at"),
                          value: `${stamp(detail.incident.resolvedAt, zh)} · ${zh ? "持续" : "lasted"} ${formatDuration(detail.incident.startedAt, detail.incident.resolvedAt, zh)}`,
                        }
                      : { key: "duration", label: zh ? "已持续" : "Ongoing for", value: formatDuration(detail.incident.startedAt, null, zh) },
                  ].map((item) => (
                    <div key={item.key} className="flex min-w-0 flex-col gap-2 rounded-lg bg-muted/50 px-4 py-3 ring-1 ring-hairline">
                      <dt className="text-xs font-medium text-muted-foreground">{item.label}</dt>
                      <dd className="text-sm font-medium tabular-nums">{item.value}</dd>
                    </div>
                  ))}
                </dl>
                {detail.incident.acknowledgedAt && (
                  <p className="text-sm text-muted-foreground">
                    {zh ? "停止提醒时间" : "Reminders stopped at"} · {stamp(detail.incident.acknowledgedAt, zh)}
                  </p>
                )}
                {detail.incident.metric === "app.log_matches" && <LogIncidentDetails details={detail.incident.logDetails} zh={zh} token={token} />}
                <AlertSourceControls detail={detail} zh={zh} busy={busy} stamp={stamp}
                  onSilence={(sourceKey, seconds) => void act((signal) => postAlerting(`incidents/${detail.incident.id}/silence-source${appQuery ? `?${appQuery}` : ""}`, token, { sourceKey, seconds }, signal), zh ? "此来源已限时静默" : "Source silenced")}
                  onRemove={(sourceKey) => void act((signal) => postAlerting(`source-silences/remove${appQuery ? `?${appQuery}` : ""}`, token, { ruleId: detail.incident.ruleId, target: detail.incident.target, sourceKey }, signal), zh ? "已恢复此来源通知" : "Source notifications resumed")} />
                {detail.incident.metric === "build.failed" && (
                  <div className="flex flex-col gap-4">
                    <p className="text-sm text-muted-foreground">
                      {zh ? "以下是关联失败构建的记录。持续提醒不代表发生了新构建；恢复后保留失败记录供追溯。" : "Details of the associated failed build. Reminders do not indicate a new build; failure details are retained after recovery."}
                    </p>
                    {detail.incident.buildDetails?.buildId ? <>
                      <dl className="grid gap-4 sm:grid-cols-2">
                        {[
                          [zh ? "服务 / 应用" : "Service / application", detail.incident.buildDetails.service],
                          [zh ? "构建编号" : "Build ID", detail.incident.buildDetails.buildId],
                          [zh ? "镜像完整引用" : "Full image reference", detail.incident.buildDetails.image],
                          [zh ? "目标镜像仓库" : "Target image repository", detail.incident.buildDetails.imageRepository],
                          [zh ? "构建节点" : "Builder", detail.incident.buildDetails.buildNode],
                          [zh ? "源码仓库" : "Source repository", detail.incident.buildDetails.repository],
                          [zh ? "分支 / 提交" : "Ref / commit", detail.incident.buildDetails.ref],
                          [zh ? "失败阶段" : "Failed stage", detail.incident.buildDetails.stage],
                          [zh ? "构建开始" : "Build started", stamp(detail.incident.buildDetails.createdAt, zh)],
                          [zh ? "失败时间" : "Failed at", stamp(detail.incident.buildDetails.failedAt, zh)],
                          ...Object.entries(detail.incident.buildDetails.images || {}).map(([service, image]) => [`${zh ? "镜像" : "Image"} · ${service}`, image]),
                        ].map(([label, value]) => <div key={label} className="flex flex-col gap-1">
                          <dt className="text-muted-foreground">{label}</dt>
                          <dd className="whitespace-pre-wrap wrap-anywhere">{value || (zh ? "未记录" : "Not recorded")}</dd>
                        </div>)}
                      </dl>
                      <div className="flex flex-col gap-1">
                        <span className="text-muted-foreground">{zh ? "失败原因" : "Failure reason"}</span>
                        <p className="whitespace-pre-wrap wrap-anywhere">{detail.incident.buildDetails.reason || (zh ? "未记录，请查看构建日志" : "Not recorded; see build logs")}</p>
                      </div>
                      <Button className="self-start" variant="outline" onClick={() => navigate(`/deployments/build/${encodeURIComponent(detail.incident.buildDetails!.buildId!)}`)}>
                        {zh ? "查看构建详情与日志" : "View build details and logs"}
                      </Button>
                    </> : <p className="text-muted-foreground">{zh ? "此历史告警尚无关联构建详情；仍在触发的告警将在下次评估时补充。" : "No build details are attached to this historical incident. Active failures are enriched on the next evaluation."}</p>}
                  </div>
                )}
                {detail.events.length ? (
                  <section className="flex flex-col gap-3" aria-labelledby="incident-events-heading">
                    <h3 id="incident-events-heading" className="text-sm font-medium">{zh ? "事件记录" : "Event log"}</h3>
                    <div className="overflow-hidden rounded-lg ring-1 ring-hairline">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="pl-4">{zh ? "事件" : "Event"}</TableHead>
                            <TableHead>{zh ? "时间" : "Time"}</TableHead>
                            <TableHead className="pr-4">{zh ? "详情" : "Details"}</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {detail.events.map((event) => (
                            <TableRow key={event.id}>
                              <TableCell className="pl-4 font-medium">{alertEventLabel(event.kind, zh)}</TableCell>
                              <TableCell className="text-muted-foreground tabular-nums">{stamp(event.at, zh)}</TableCell>
                              <TableCell className="max-w-lg pr-4 whitespace-pre-wrap wrap-anywhere text-muted-foreground">
                                {eventDetailText(event.detail)}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </section>
                ) : (
                  <AlertEmpty
                    title={zh ? "暂无事件" : "No events"}
                    description={
                      zh
                        ? "该告警的后续变化将在这里记录。"
                        : "Changes to this incident will appear here."
                    }
                  />
                )}
              </CardContent>
              {["pending", "firing"].includes(detail.incident.status) && (
                <CardFooter className="flex flex-wrap gap-2">
                  <IncidentActions item={detail.incident} zh={zh} busy={busy}
                    onStop={(item) => actOnIncident(item, "ack")}
                    onClose={(item) => void closeIncident(item)} />
                </CardFooter>
              )}
            </Card>
          )}
          {tab === "rules" && !editor && (
            <Card>
              <div className="-mt-(--card-spacing) flex flex-wrap items-center gap-2 border-b border-hairline px-(--card-spacing) py-3" role="toolbar" aria-label={zh ? "规则操作" : "Rule actions"}>
                <span className="text-[0.8125rem] text-muted-foreground tabular-nums">
                  {zh ? `${rules.length} 条规则 · ${rules.filter((item) => item.enabled).length} 条启用` : `${rules.length} rules · ${rules.filter((item) => item.enabled).length} enabled`}
                </span>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  {crossLink}
                  {appFilterControl}
                  <Button
                    disabled={busy || !presets.length}
                    onClick={() => navigate(`/observe/rules/new${appQuery ? `?${appQuery}` : ""}`)}
                  >
                    <Plus data-icon="inline-start" aria-hidden="true" />
                    {zh ? "新建规则" : "Create rule"}
                  </Button>
                </div>
              </div>
              <CardContent className="-mt-(--card-spacing)">
                {rules.length ? (
                  <Table
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label": zh ? "告警规则列表" : "Alert rules",
                    }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          {zh ? "规则" : "Rule"}
                        </TableHead>
                        <TableHead>{zh ? "条件" : "Condition"}</TableHead>
                        <TableHead>{zh ? "通知渠道" : "Channels"}</TableHead>
                        <TableHead>{zh ? "状态" : "State"}</TableHead>
                        <TableHead className="w-12">
                          <span className="sr-only">{zh ? "操作" : "Actions"}</span>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rules.map((item) => {
                        const silenced = (item.silencedUntil || 0) > Date.now() / 1000;
                        const preset = presets.find((p) => p.metric === item.metric);
                        const href = `/observe/rules/${encodeURIComponent(item.id)}${appQuery ? `?${appQuery}` : ""}`;
                        return (
                        <TableRow key={item.id} className={cn(!item.enabled && "text-muted-foreground")}>
                          <TableCell>
                            <div className="flex flex-col items-start gap-0.5">
                              <a {...spaLink(href, navigate)} className="font-medium text-foreground underline-offset-4 hover:underline focus-visible:underline">{item.name}</a>
                              <span className="text-[0.8125rem] text-muted-foreground">
                                {item.target === "*"
                                  ? zh
                                    ? "全部对象"
                                    : "All targets"
                                  : item.target}
                                {item.metric === "app.log_matches" && item.logQuery?.task ? ` · ${item.logQuery.task}` : ""}
                                {item.taskQuery?.service ? ` · OTel ${item.taskQuery.service}` : ""}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div>
                              {preset?.name || item.metric}
                            </div>
                            <div className="text-[0.8125rem] text-muted-foreground tabular-nums">
                              &gt; {item.threshold}{preset?.unit ? (preset.unit === "%" ? "%" : ` ${preset.unit}`) : ""} · {zh ? "持续" : "for"} {formatSeconds(item.forSeconds, zh)}
                            </div>
                            {item.metric === "app.log_matches" && item.logQuery && <div className="max-w-md whitespace-normal wrap-anywhere text-xs text-muted-foreground">
                              {[
                                `${zh ? "窗口" : "Window"} ${item.logQuery.windowSeconds}s`,
                                item.logQuery.mode === "task_failures" ? (zh ? "业务或执行失败（去重）" : "Business or execution failure (deduplicated)") : "",
                                item.logQuery.contains ? `${zh ? "包含" : "Contains"} “${item.logQuery.contains}”` : "",
                                item.logQuery.exclude ? `${zh ? "排除" : "Excludes"} “${item.logQuery.exclude}”` : "",
                                item.logQuery.field ? `${item.logQuery.field} = ${item.logQuery.equals}` : "",
                              ].filter(Boolean).join(" · ")}
                            </div>}
                          </TableCell>
                          <TableCell>
                            {channelNames(item.channelIds) || <span className="text-muted-foreground">{zh ? "仅记录" : "Record only"}</span>}
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-col items-start gap-1">
                              <Badge appearance="dot" variant={item.enabled ? (silenced ? "warning" : "success") : "outline"}>
                                {!item.enabled ? (zh ? "已停用" : "Disabled") : silenced ? (zh ? "已静默" : "Silenced") : (zh ? "已启用" : "Enabled")}
                              </Badge>
                              {silenced && (
                                <span className="text-xs text-muted-foreground tabular-nums">
                                  {zh ? "至" : "Until"} {formatClock(item.silencedUntil!, lang)}
                                </span>
                              )}
                            </div>
                          </TableCell>
                          <TableCell className="text-right">
                            <DropdownMenu>
                              <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" disabled={busy} aria-label={`${item.name} · ${zh ? "更多操作" : "More actions"}`} />}>
                                <MoreHorizontal />
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end" className="w-44">
                                <DropdownMenuGroup>
                                  <DropdownMenuItem onClick={() => navigate(href)}>
                                    <Pencil />{zh ? "编辑" : "Edit"}
                                  </DropdownMenuItem>
                                  <DropdownMenuItem onClick={() =>
                                    void act((signal) =>
                                      postAlerting("rules", token, { ...item, enabled: !item.enabled }, signal),
                                    )
                                  }>
                                    {item.enabled ? <Pause /> : <Play />}
                                    {item.enabled ? (zh ? "停用" : "Disable") : (zh ? "启用" : "Enable")}
                                  </DropdownMenuItem>
                                  <DropdownMenuItem onClick={() =>
                                    void act((signal) =>
                                      postAlerting(
                                        "rules",
                                        token,
                                        { ...item, silencedUntil: silenced ? 0 : Math.floor(Date.now() / 1000) + 3600 },
                                        signal,
                                      ),
                                    )
                                  }>
                                    {silenced ? <Bell /> : <BellSlash />}
                                    {silenced ? (zh ? "解除静默" : "Unsilence") : (zh ? "静默 1 小时" : "Silence 1 hour")}
                                  </DropdownMenuItem>
                                </DropdownMenuGroup>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </TableCell>
                        </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                ) : (
                  <AlertEmpty
                    title={zh ? "尚无告警规则" : "No alert rules"}
                    description={
                      zh
                        ? "可从磁盘、内存或节点失联等预设开始。"
                        : "Start with a disk, memory or node heartbeat preset."
                    }
                  />
                )}
              </CardContent>
            </Card>
          )}
          {tab === "rules" && rule && (
            <form
              noValidate
              onSubmit={saveRule}
              onInput={clearFieldError}
            >
              <Card>
                <CardHeader>
                  <CardTitle>
                    {rule.id
                      ? zh
                        ? "编辑规则"
                        : "Edit rule"
                      : zh
                        ? "新建规则"
                        : "Create rule"}
                  </CardTitle>
                  <CardDescription>
                    {zh
                      ? "设置监测对象、触发条件和通知方式。"
                      : "Choose a target, trigger conditions and notification behavior."}
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <FieldGroup>
                    <FieldGroup className="grid sm:grid-cols-2">
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(fieldErrors["alert-rule-name"])}
                      >
                        <FieldLabel htmlFor="alert-rule-name">
                          {zh ? "名称" : "Name"}
                        </FieldLabel>
                        <Input
                          id="alert-rule-name"
                          aria-invalid={Boolean(fieldErrors["alert-rule-name"])}
                          required
                          disabled={busy}
                          value={rule.name}
                          onChange={(e) =>
                            setRule({ ...rule, name: e.target.value })
                          }
                        />
                        <FieldError>
                          {fieldErrors["alert-rule-name"]}
                        </FieldError>
                      </Field>
                      <Field data-disabled={busy}>
                        <FieldLabel htmlFor="alert-rule-metric">
                          {zh ? "指标预设" : "Metric preset"}
                        </FieldLabel>
                        <SelectControl
                          id="alert-rule-metric"
                          disabled={busy}
                          value={rule.metric}
                          onChange={(value) => {
                            const preset = presets.find(
                              (p) => p.metric === value,
                            );
                            setRule({
                              ...rule,
                              metric: value,
                              logQuery: { windowSeconds: 300, ...rule.logQuery },
                              target: value === "observe.metrics_age" ? "metrics" : rule.metric === "observe.metrics_age" ? newAlertRule(preset).target : (value === "app.log_matches" || isTaskMetric(value)) && rule.target === "*" ? "" : rule.target,
                              noData: alertsOnMissingData(value) ? "alert" : "keep",
                              threshold: preset?.threshold ?? rule.threshold,
                              forSeconds: preset?.forSeconds ?? rule.forSeconds,
                            });
                          }}
                          options={presets.map((p) => ({
                            value: p.metric,
                            label: p.name,
                          }))}
                        />
                        <FieldDescription>
                          {
                            presets.find((p) => p.metric === rule.metric)
                              ?.description
                          }
                        </FieldDescription>
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(fieldErrors["alert-rule-target"])}
                      >
                        <FieldLabel htmlFor="alert-rule-target">
                          {(rule.metric === "app.log_matches" || isTaskMetric(rule.metric)) ? (zh ? "应用 ID" : "Application ID") : zh
                            ? "对象 ID（* 表示全部）"
                            : "Target ID (* for all)"}
                        </FieldLabel>
                        <Combobox
                          disabled={busy || rule.metric === "observe.metrics_age"}
                          items={rule.metric === "observe.metrics_age" ? ["metrics"] : [...((rule.metric === "app.log_matches" || isTaskMetric(rule.metric)) ? [] : ["*"]), ...new Set(targetNames)]}
                          value={rule.target}
                          inputValue={rule.target}
                          onInputValueChange={(value) =>
                            setRule({ ...rule, target: value })
                          }
                          onValueChange={(value) => {
                            if (value) setRule({ ...rule, target: value });
                          }}
                        >
                          <ComboboxInput
                            id="alert-rule-target"
                            aria-invalid={Boolean(
                              fieldErrors["alert-rule-target"],
                            )}
                            required
                            disabled={busy}
                          />
                          <ComboboxContent>
                            <ComboboxEmpty>
                              {zh
                                ? "可直接输入对象 ID"
                                : "Enter a target ID directly"}
                            </ComboboxEmpty>
                            <ComboboxList>
                              {(name: string) => (
                                <ComboboxItem key={name} value={name}>
                                  {name}
                                </ComboboxItem>
                              )}
                            </ComboboxList>
                          </ComboboxContent>
                        </Combobox>
                        <FieldDescription>
                          {rule.metric === "observe.metrics_age" ? (zh ? "监测共享 Collector 到指标存储的采样新鲜度；查询不可用时按缺失数据策略处理。" : "Monitor sample freshness from the shared Collector to metric storage; unavailable reads follow the no-data policy.") : isTaskMetric(rule.metric) ? (zh ? "选择单个应用；下方可限定 OTel 服务名。" : "Select one application; optionally scope by OTel service below.") : rule.metric === "app.log_matches" ? (zh ? "选择一个应用，下方可进一步限定服务名。" : "Select one application; optionally narrow by task below.") : rule.metric.startsWith("node.")
                            ? zh
                              ? "使用节点 ID，可从节点页面复制。"
                              : "Use a node ID from the Fleet page."
                            : rule.metric === "task.queue_age"
                              ? "agent / builder / build"
                              : zh
                                ? "填写应用标识；* 匹配全部应用。"
                                : "Application identifier; * matches all applications."}
                        </FieldDescription>
                        <FieldError>
                          {fieldErrors["alert-rule-target"]}
                        </FieldError>
                      </Field>
                      {isTaskMetric(rule.metric) && <Field data-disabled={busy} data-invalid={Boolean(fieldErrors["alert-task-service"])}>
                        <FieldLabel htmlFor="alert-task-service">{zh ? "OTel 服务名" : "OTel service name"}</FieldLabel>
                        <Input id="alert-task-service" aria-invalid={Boolean(fieldErrors["alert-task-service"])} disabled={busy} maxLength={160}
                          required={requiresTaskService(rule.metric)}
                          value={rule.taskQuery?.service || ""}
                          onChange={(event) => setRule({ ...rule, taskQuery: { ...rule.taskQuery, service: event.target.value } })} />
                        <FieldDescription>{requiresTaskService(rule.metric)
                          ? (zh ? "填写该应用预期持续运行的 OTel 服务名。按服务聚合所有进程；停止监测服务时停用对应规则。缺失可能是服务或上报通道异常。" : "Enter the expected continuously running OTel service. All processes are aggregated; disable this rule when retiring the service. Missing data can mean a service or telemetry outage.")
                          : (zh ? "可留空以覆盖整个应用。使用心跳表中的 service_name，与 Compose 服务名可能不同。" : "Optional: leave empty for the application. Use service_name from the heartbeat table; it may differ from the Compose name.")}</FieldDescription>
                        <FieldError>{fieldErrors["alert-task-service"]}</FieldError>
                      </Field>}
                      {rule.metric === "app.task_completion_missing" && <>
                        <Field data-disabled={busy}>
                          <FieldLabel htmlFor="alert-task-framework">{zh ? "任务框架" : "Task framework"}</FieldLabel>
                          <SelectControl id="alert-task-framework" disabled={busy} value={rule.taskQuery?.framework || ""}
                            options={[{ value: "taskiq", label: "Taskiq" }, { value: "celery", label: "Celery" }]}
                            onChange={(value) => setRule({ ...rule, taskQuery: { service: "", ...rule.taskQuery, framework: value as "taskiq" | "celery" } })} />
                        </Field>
                        <Field data-disabled={busy}>
                          <FieldLabel htmlFor="alert-task-name">{zh ? "预期周期任务名" : "Expected periodic task"}</FieldLabel>
                          <Input id="alert-task-name" required disabled={busy} maxLength={256} value={rule.taskQuery?.taskName || ""}
                            onChange={(event) => setRule({ ...rule, taskQuery: { service: "", ...rule.taskQuery, taskName: event.target.value } })} />
                          <FieldDescription>{zh ? "选择应持续完成的固定周期任务。统计窗口需大于正常调度间隔和执行耗时；按需触发的任务可能正常空闲。" : "Choose a regularly completing task. The window must exceed its normal schedule interval and runtime; on-demand tasks can legitimately be idle."}</FieldDescription>
                        </Field>
                        <Field data-disabled={busy}>
                          <FieldLabel htmlFor="alert-task-window">{zh ? "完成记录统计窗口（秒）" : "Completion window (seconds)"}</FieldLabel>
                          <Input id="alert-task-window" type="number" min={60} max={3600} step={1} required disabled={busy}
                            value={rule.taskQuery?.windowSeconds ?? 300}
                            onChange={(event) => setRule({ ...rule, taskQuery: { service: "", ...rule.taskQuery, windowSeconds: Number(event.target.value) } })} />
                          <FieldDescription>{zh ? "窗口内有完成记录时为 0，没有完成记录时为 1；阈值设为 0。采样完全缺失时应触发告警。" : "The value is 0 with completions and 1 without; use threshold 0. Configure missing samples to alert."}</FieldDescription>
                        </Field>
                      </>}
                      {rule.metric === "app.log_matches" && <LogRuleFields rule={rule} onChange={setRule} token={token} zh={zh} disabled={busy} />}
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(
                          fieldErrors["alert-rule-threshold"],
                        )}
                      >
                        <FieldLabel htmlFor="alert-rule-threshold">
                          {zh
                            ? "阈值（观测值大于此值）"
                            : "Threshold (observed value exceeds)"}{" "}
                          ·{" "}
                          {presets.find((p) => p.metric === rule.metric)?.unit}
                        </FieldLabel>
                        <Input
                          id="alert-rule-threshold"
                          aria-invalid={Boolean(
                            fieldErrors["alert-rule-threshold"],
                          )}
                          type="number"
                          required
                          disabled={busy}
                          min="0"
                          step="any"
                          value={
                            Number.isNaN(rule.threshold) ? "" : rule.threshold
                          }
                          onChange={(e) =>
                            setRule({
                              ...rule,
                              threshold:
                                e.target.value === ""
                                  ? NaN
                                  : Number(e.target.value),
                            })
                          }
                        />
                        <FieldError>
                          {fieldErrors["alert-rule-threshold"]}
                        </FieldError>
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(
                          fieldErrors["alert-rule-duration"],
                        )}
                      >
                        <FieldLabel htmlFor="alert-rule-duration">
                          {zh
                            ? "持续时间（秒）"
                            : "Condition duration (seconds)"}
                        </FieldLabel>
                        <Input
                          id="alert-rule-duration"
                          aria-invalid={Boolean(
                            fieldErrors["alert-rule-duration"],
                          )}
                          type="number"
                          required
                          disabled={busy}
                          min="0"
                          step="1"
                          value={
                            Number.isNaN(rule.forSeconds) ? "" : rule.forSeconds
                          }
                          onChange={(e) =>
                            setRule({
                              ...rule,
                              forSeconds:
                                e.target.value === ""
                                  ? NaN
                                  : Number(e.target.value),
                            })
                          }
                        />
                        <FieldError>
                          {fieldErrors["alert-rule-duration"]}
                        </FieldError>
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(fieldErrors["alert-rule-repeat"])}
                      >
                        <FieldLabel htmlFor="alert-rule-repeat">
                          {zh
                            ? "重复通知间隔（秒）"
                            : "Repeat interval (seconds)"}
                        </FieldLabel>
                        <Input
                          id="alert-rule-repeat"
                          aria-invalid={Boolean(
                            fieldErrors["alert-rule-repeat"],
                          )}
                          type="number"
                          required
                          disabled={busy}
                          min="60"
                          max="604800"
                          step="1"
                          value={
                            Number.isNaN(rule.repeatSeconds)
                              ? ""
                              : rule.repeatSeconds
                          }
                          onChange={(e) =>
                            setRule({
                              ...rule,
                              repeatSeconds:
                                e.target.value === ""
                                  ? NaN
                                  : Number(e.target.value),
                            })
                          }
                        />
                        <FieldError>
                          {fieldErrors["alert-rule-repeat"]}
                        </FieldError>
                      </Field>
                      <Field data-disabled={busy}>
                        <FieldLabel id="alert-rule-severity">
                          {zh ? "严重程度" : "Severity"}
                        </FieldLabel>
                        <ToggleGroup
                          variant="segmented"
                          value={[rule.severity]}
                          disabled={busy}
                          onValueChange={(values) => {
                            if (values[0])
                              setRule({
                                ...rule,
                                severity: values[0] as AlertRule["severity"],
                              });
                          }}
                          aria-labelledby="alert-rule-severity"
                        >
                          <ToggleGroupItem value="warning">
                            {zh ? "警告" : "Warning"}
                          </ToggleGroupItem>
                          <ToggleGroupItem value="critical">
                            {zh ? "严重" : "Critical"}
                          </ToggleGroupItem>
                        </ToggleGroup>
                      </Field>
                      <Field data-disabled={busy}>
                        <FieldLabel id="alert-rule-no-data">
                          {zh ? "采样缺失时" : "When samples are missing"}
                        </FieldLabel>
                        <ToggleGroup
                          variant="segmented"
                          value={[rule.noData]}
                          disabled={busy}
                          onValueChange={(values) => {
                            if (values[0])
                              setRule({
                                ...rule,
                                noData: values[0] as AlertRule["noData"],
                              });
                          }}
                          aria-labelledby="alert-rule-no-data"
                        >
                          <ToggleGroupItem value="keep">
                            {zh ? "保留当前状态" : "Keep current state"}
                          </ToggleGroupItem>
                          <ToggleGroupItem value="alert">
                            {zh ? "触发缺失告警" : "Alert on missing data"}
                          </ToggleGroupItem>
                        </ToggleGroup>
                      </Field>
                    </FieldGroup>
                    <FieldSet disabled={busy}>
                      <FieldLegend>
                        {zh ? "通知渠道" : "Notification channels"}
                      </FieldLegend>
                      <FieldGroup>
                        {channels.map((item) => (
                          <Field
                            orientation="horizontal"
                            data-disabled={busy}
                            key={item.id}
                          >
                            <Checkbox
                              id={`alert-rule-channel-${item.id}`}
                              disabled={busy}
                              checked={rule.channelIds.includes(item.id)}
                              onCheckedChange={(checked) =>
                                setRule({
                                  ...rule,
                                  channelIds: checked
                                    ? [...rule.channelIds, item.id]
                                    : rule.channelIds.filter(
                                        (id) => id !== item.id,
                                      ),
                                })
                              }
                            />
                            <FieldLabel
                              htmlFor={`alert-rule-channel-${item.id}`}
                            >
                              {item.name}
                              {!item.enabled
                                ? zh
                                  ? "（已停用）"
                                  : " (disabled)"
                                : ""}
                            </FieldLabel>
                          </Field>
                        ))}
                      </FieldGroup>
                      {!channels.length && (
                        <FieldDescription>
                          {zh
                            ? "尚无渠道，请先到「通知渠道」配置飞书。"
                            : "No channels. Configure Feishu under Notifications first."}
                        </FieldDescription>
                      )}
                    </FieldSet>
                    <Field orientation="horizontal" data-disabled={busy}>
                      <Checkbox
                        id="alert-rule-enabled"
                        disabled={busy}
                        checked={rule.enabled}
                        onCheckedChange={(checked) =>
                          setRule({ ...rule, enabled: checked })
                        }
                      />
                      <FieldLabel htmlFor="alert-rule-enabled">
                        {zh ? "启用规则" : "Enable rule"}
                      </FieldLabel>
                    </Field>
                  </FieldGroup>
                </CardContent>
                <CardFooter className="flex flex-wrap gap-2">
                  <Button type="submit" disabled={busy}>
                    {busy && (
                      <Spinner data-icon="inline-start" aria-hidden="true" />
                    )}
                    {busy
                      ? zh
                        ? "保存中…"
                        : "Saving…"
                      : zh
                        ? "保存规则"
                        : "Save rule"}
                  </Button>
                  <Button
                    variant="outline"
                    type="button"
                    disabled={busy}
                    onClick={() => navigate(basePath)}
                  >
                    {zh ? "取消" : "Cancel"}
                  </Button>
                  {rule.id && (
                    <Button
                      variant="destructive"
                      type="button"
                      disabled={busy}
                      onClick={() => void deleteRule()}
                    >
                      {zh ? "删除规则" : "Delete rule"}
                    </Button>
                  )}
                </CardFooter>
              </Card>
            </form>
          )}
          {tab === "notifications" && !editor && (
            <Card>
              <CardHeader>
                <CardTitle>
                  {zh ? "飞书通知渠道" : "Feishu notification channels"}
                </CardTitle>
                <CardDescription>
                  {zh
                    ? "应用需启用并发布机器人，开通消息发送权限，并将机器人加入目标群；保存后可手动测试。"
                    : "Enable and publish the app bot, grant message-sending permission and add it to the target group. Save, then send a test."}
                </CardDescription>
                <CardAction>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      editChannel({
                        id: "",
                        name: zh ? "飞书告警" : "Feishu alerts",
                        type: "feishu",
                        enabled: true,
                        appId: "",
                        chatId: "",
                        appSecretConfigured: false,
                      })
                    }
                  >
                    <Plus data-icon="inline-start" aria-hidden="true" />
                    {zh ? "添加渠道" : "Add channel"}
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent>
                {channels.length ? (
                  <Table
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label": zh
                        ? "通知渠道列表"
                        : "Notification channels",
                    }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHead>{zh ? "渠道" : "Channel"}</TableHead>
                        <TableHead>{zh ? "配置" : "Configuration"}</TableHead>
                        <TableHead className="text-right">
                          {zh ? "操作" : "Actions"}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {channels.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell>
                            <div className="flex flex-col items-start gap-1">
                              <span className="font-medium">{item.name}</span>
                              <Badge
                                appearance="dot"
                                variant={item.enabled ? "success" : "outline"}
                              >
                                {item.enabled
                                  ? zh
                                    ? "已启用"
                                    : "Enabled"
                                  : zh
                                    ? "已停用"
                                    : "Disabled"}
                              </Badge>
                            </div>
                          </TableCell>
                          <TableCell>
                            <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 text-[0.8125rem]">
                              <dt className="text-muted-foreground">App ID</dt>
                              <dd className="font-mono text-xs wrap-anywhere" translate="no">{item.appId}</dd>
                              <dt className="text-muted-foreground">{zh ? "群聊 ID" : "Chat ID"}</dt>
                              <dd className="font-mono text-xs wrap-anywhere" translate="no">{item.chatId}</dd>
                              <dt className="text-muted-foreground">App Secret</dt>
                              <dd>
                              <Badge
                                appearance={item.appSecretConfigured ? "dot" : "filled"}
                                variant={
                                  item.appSecretConfigured
                                    ? "success"
                                    : "destructive"
                                }
                              >
                                {item.appSecretConfigured
                                  ? zh
                                    ? "已配置"
                                    : "Configured"
                                  : zh
                                    ? "App Secret 缺失"
                                    : "App Secret missing"}
                              </Badge>
                              </dd>
                            </dl>
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() => editChannel(item)}
                              >
                                {zh ? "编辑" : "Edit"}
                              </Button>
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={
                                  busy ||
                                  !item.enabled ||
                                  !item.appSecretConfigured
                                }
                                onClick={() =>
                                  void act(async (signal) => {
                                    const result = await postAlerting<{
                                      delivery: AlertDelivery;
                                    }>(
                                      `channels/${encodeURIComponent(item.id)}/test`,
                                      token,
                                      {},
                                      signal,
                                    );
                                    setNotice(
                                      `${zh ? "测试消息已排队，发送结果见下方记录。ID：" : "Test queued; check delivery status below. ID: "}${result.delivery.id}`,
                                    );
                                  })
                                }
                              >
                                {zh ? "发送测试消息" : "Send test message"}
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <AlertEmpty
                    title={zh ? "尚无通知渠道" : "No notification channels"}
                    description={
                      zh
                        ? "告警可先记录，配置渠道后才能推送到飞书。"
                        : "Incidents can be recorded; Feishu delivery requires a channel."
                    }
                  />
                )}
              </CardContent>
            </Card>
          )}
          {tab === "notifications" && channel && (
            <form
              noValidate
              onSubmit={saveChannel}
              onInput={clearFieldError}
            >
              <Card>
                <CardHeader>
                  <CardTitle>
                    {channel.id
                      ? zh
                        ? "编辑渠道"
                        : "Edit channel"
                      : zh
                        ? "添加渠道"
                        : "Add channel"}
                  </CardTitle>
                  <CardDescription>
                    {zh
                      ? "填写 App ID、App Secret 和群聊 ID。App Secret 不会回显。"
                      : "Enter the App ID, App Secret and chat ID. The App Secret is never displayed."}
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <FieldGroup>
                    <FieldGroup className="grid sm:grid-cols-2">
                      <Field data-disabled={busy}>
                        <FieldLabel htmlFor="alert-channel-name">
                          {zh ? "渠道名称（可选）" : "Channel name (optional)"}
                        </FieldLabel>
                        <Input
                          id="alert-channel-name"
                          disabled={busy}
                          value={channel.name}
                          onChange={(e) =>
                            setChannel({ ...channel, name: e.target.value })
                          }
                        />
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(
                          fieldErrors["alert-channel-app-id"],
                        )}
                      >
                        <FieldLabel htmlFor="alert-channel-app-id">
                          App ID
                        </FieldLabel>
                        <Input
                          id="alert-channel-app-id"
                          aria-invalid={Boolean(
                            fieldErrors["alert-channel-app-id"],
                          )}
                          required
                          disabled={busy}
                          autoComplete="off"
                          value={channel.appId}
                          placeholder="cli_…"
                          onChange={(e) => {
                            const appId = e.target.value;
                            setCallbackToken("");
                            setCallbackKey("");
                            setChannel({ ...channel, appId, cardActionsEnabled: Boolean(channels.find((item) => item.appId === appId.trim())?.cardActionsEnabled) });
                          }}
                        />
                        <FieldError>
                          {fieldErrors["alert-channel-app-id"]}
                        </FieldError>
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(
                          fieldErrors["alert-channel-secret"],
                        )}
                      >
                        <FieldLabel htmlFor="alert-channel-secret">
                          App Secret
                        </FieldLabel>
                        <Input
                          id="alert-channel-secret"
                          aria-invalid={Boolean(
                            fieldErrors["alert-channel-secret"],
                          )}
                          type="password"
                          autoComplete="new-password"
                          disabled={busy}
                          required={!channel.appSecretConfigured}
                          value={secret}
                          placeholder={
                            channel.appSecretConfigured
                              ? zh
                                ? "已配置，留空保留"
                                : "Configured; leave blank to keep"
                              : ""
                          }
                          onChange={(e) => setSecret(e.target.value)}
                        />
                        <FieldDescription>
                          {zh
                            ? "密钥仅用于发送通知，不会在页面中显示。"
                            : "This secret is used for notifications and is never shown on this page."}
                        </FieldDescription>
                        <FieldError>
                          {fieldErrors["alert-channel-secret"]}
                        </FieldError>
                      </Field>
                      <Field
                        data-disabled={busy}
                        data-invalid={Boolean(
                          fieldErrors["alert-channel-chat"],
                        )}
                      >
                        <FieldLabel htmlFor="alert-channel-chat">
                          {zh ? "群聊 ID" : "Chat ID"}
                        </FieldLabel>
                        <Input
                          id="alert-channel-chat"
                          aria-invalid={Boolean(
                            fieldErrors["alert-channel-chat"],
                          )}
                          required
                          disabled={busy}
                          autoComplete="off"
                          value={channel.chatId}
                          placeholder="oc_…"
                          onChange={(e) =>
                            setChannel({ ...channel, chatId: e.target.value })
                          }
                        />
                        <FieldError>
                          {fieldErrors["alert-channel-chat"]}
                        </FieldError>
                      </Field>
                    </FieldGroup>
                    <Field orientation="horizontal" data-disabled={busy}>
                      <Checkbox
                        id="alert-channel-enabled"
                        disabled={busy}
                        checked={channel.enabled}
                        onCheckedChange={(checked) =>
                          setChannel({ ...channel, enabled: checked })
                        }
                      />
                      <FieldLabel htmlFor="alert-channel-enabled">
                        {zh ? "启用渠道" : "Enable channel"}
                      </FieldLabel>
                    </Field>
                    <FieldSet>
                      <FieldLegend>{zh ? "飞书卡片操作" : "Feishu card actions"}</FieldLegend>
                      <FieldDescription>{zh
                        ? "群成员可从卡片限时静默指定来源。同一 App ID 的渠道共用回调配置。"
                        : "Group members can silence a card's source temporarily. Channels with the same App ID share callback settings."}</FieldDescription>
                      <Field orientation="horizontal" data-disabled={busy}>
                        <Checkbox id="alert-card-actions" disabled={busy} checked={Boolean(channel.cardActionsEnabled)}
                          onCheckedChange={(checked) => setChannel({ ...channel, cardActionsEnabled: checked })} />
                        <FieldLabel htmlFor="alert-card-actions">{zh ? "启用来源静默按钮" : "Enable source silence buttons"}</FieldLabel>
                      </Field>
                      {channel.cardActionsEnabled && <FieldGroup>
                        <Field><FieldLabel htmlFor="alert-callback-url">{zh ? "回调地址" : "Callback URL"}</FieldLabel>
                          <Input id="alert-callback-url" readOnly value={`${window.location.origin}/v1/alerting/feishu/callback/${encodeURIComponent(channel.appId)}`} />
                          <FieldDescription>{zh
                            ? "保存后，在飞书开发者后台「事件与回调 → 回调配置」选择发送至开发者服务器，填写此地址并订阅 card.action.trigger。"
                            : "After saving, choose delivery to your server under Events & Callbacks → Callback configuration, enter this URL and subscribe to card.action.trigger."}</FieldDescription>
                        </Field>
                        <FieldGroup className="grid sm:grid-cols-2">
                          <Field><FieldLabel htmlFor="alert-callback-token">Verification Token</FieldLabel>
                            <Input id="alert-callback-token" type="password" autoComplete="new-password" disabled={busy} value={callbackToken}
                              placeholder={zh ? "填写飞书回调凭据，留空保留已有值" : "Feishu callback credential; blank keeps saved value"}
                              onChange={(e) => setCallbackToken(e.target.value)} />
                          </Field>
                          <Field><FieldLabel htmlFor="alert-callback-key">Encrypt Key</FieldLabel>
                            <Input id="alert-callback-key" type="password" autoComplete="new-password" disabled={busy} value={callbackKey}
                              placeholder={zh ? "填写飞书回调凭据，留空保留已有值" : "Feishu callback credential; blank keeps saved value"}
                              onChange={(e) => setCallbackKey(e.target.value)} />
                          </Field>
                        </FieldGroup>
                      </FieldGroup>}
                    </FieldSet>
                  </FieldGroup>
                </CardContent>
                <CardFooter className="flex flex-wrap gap-2">
                  <Button type="submit" disabled={busy}>
                    {busy && (
                      <Spinner data-icon="inline-start" aria-hidden="true" />
                    )}
                    {busy
                      ? zh
                        ? "保存中…"
                        : "Saving…"
                      : zh
                        ? "保存渠道"
                        : "Save channel"}
                  </Button>
                  <Button
                    variant="outline"
                    type="button"
                    disabled={busy}
                    onClick={() => navigate(basePath)}
                  >
                    {zh ? "取消" : "Cancel"}
                  </Button>
                  {channel.id && (
                    <Button
                      variant="destructive"
                      type="button"
                      disabled={busy}
                      onClick={() => void deleteChannel()}
                    >
                      {zh ? "删除渠道" : "Delete channel"}
                    </Button>
                  )}
                </CardFooter>
              </Card>
            </form>
          )}
          {tab === "notifications" && !editor && (
            <Card>
              <CardHeader>
                <CardTitle>
                  {zh ? "通知发送记录" : "Delivery history"}
                </CardTitle>
                <CardDescription>
                  {zh
                    ? "排队不代表发送成功；失败原因与重试时间会在这里更新。"
                    : "Queued does not mean sent. Failures and retry times appear here."}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {deliveries.items.length ? (
                  <Table
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label": zh
                        ? "通知发送记录"
                        : "Notification delivery history",
                    }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          {zh ? "渠道 / 类型" : "Channel / kind"}
                        </TableHead>
                        <TableHead>{zh ? "状态" : "State"}</TableHead>
                        <TableHead>
                          {zh ? "时间 / 尝试" : "Time / attempts"}
                        </TableHead>
                        <TableHead>{zh ? "详情" : "Details"}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {deliveries.items.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell>
                            <div>
                              {channels.find((c) => c.id === item.channelId)
                                ?.name || item.channelId}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              {alertEventLabel(item.kind, zh)}
                            </div>
                          </TableCell>
                          <TableCell>
                            <AlertStatus status={item.status} zh={zh} />
                          </TableCell>
                          <TableCell>
                            <div className="tabular-nums">
                              {stamp(item.sentAt || item.createdAt, zh)}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              {zh ? `${item.attempts} 次尝试` : `${item.attempts} ${item.attempts === 1 ? "attempt" : "attempts"}`}
                            </div>
                          </TableCell>
                          <TableCell className="max-w-sm whitespace-normal wrap-anywhere">
                            <div className={cn(item.lastError ? "text-destructive-foreground" : "text-muted-foreground")}>{item.lastError || (item.status === "sent" ? (zh ? "飞书已接收" : "Accepted by Feishu") : EMPTY_VALUE)}</div>
                            {item.status !== "sent" && !!item.nextAttemptAt && (
                              <div className="text-muted-foreground">
                                {zh ? "下次尝试" : "Next attempt"} ·{" "}
                                {stamp(item.nextAttemptAt, zh)}
                              </div>
                            )}
                            <div className="font-mono text-xs text-subtle-foreground">
                              #{item.id}
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <AlertEmpty
                    title={zh ? "暂无发送记录" : "No deliveries"}
                    description={
                      zh
                        ? "添加渠道后可发送一条测试消息。"
                        : "Add a channel and send a test message."
                    }
                  />
                )}
              </CardContent>
              {deliveries.nextCursor && (
                <CardFooter>
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      void act(
                        async (signal) => {
                          const page = await getAlerting<
                            AlertPage<AlertDelivery>
                          >(
                            `deliveries?limit=50&cursor=${encodeURIComponent(deliveries.nextCursor!)}`,
                            token,
                            signal,
                          );
                          setDeliveries((old) => ({
                            ...page,
                            items: mergeAlertPages(old.items, page.items),
                          }));
                          setExpanded(true);
                        },
                        undefined,
                        false,
                      )
                    }
                  >
                    {busy && (
                      <Spinner data-icon="inline-start" aria-hidden="true" />
                    )}
                    {zh ? "加载更多" : "Load more"}
                  </Button>
                </CardFooter>
              )}
            </Card>
          )}
        </>
      )}
      {!editor && !refreshHost.hosted && (
        <p className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
          {zh ? "每 15 秒自动刷新。" : "Refreshes every 15 seconds."}
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto px-0"
            onClick={refreshNow}
            disabled={busy}
          >
            {zh ? "立即刷新" : "Refresh now"}
          </Button>
        </p>
      )}
    </div>
  );
}
