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
  postAlerting,
  ruleRequest,
} from "../alertingApi";
import type {
  AlertChannel,
  AlertDelivery,
  AlertEvent,
  AlertIncident,
  AlertOverview,
  AlertPage,
  AlertPreset,
  AlertRule,
  AlertTab,
} from "../alertingApi";
import { useRouter } from "../router";
import { SelectControl } from "./primitives";
import { AlertCircle } from "lucide-react";
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

function stamp(value: number | null | undefined, zh: boolean) {
  return value
    ? new Date(value * 1000).toLocaleString(zh ? "zh-CN" : "en-US", {
        hour12: false,
      })
    : "—";
}
function AlertStatus({ status, zh }: { status: string; zh: boolean }) {
  return (
    <Badge
      variant={
        ["firing", "failed", "critical"].includes(status)
          ? "destructive"
          : ["pending", "warning", "retry", "sending"].includes(status)
            ? "secondary"
            : "outline"
      }
    >
      {alertStatusLabel(status, zh)}
    </Badge>
  );
}

function AlertEmpty({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <Empty>
      <EmptyHeader>
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
    metric === "app.http_p95"
  )
    return `${formatted} ${zh ? "秒" : "s"}`;
  if (["node.cpu", "node.memory", "node.disk", "node.inode"].includes(metric))
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
}: {
  lang: Lang;
  token: string;
  tab: AlertTab;
  nodeNames?: string[];
  applicationNames?: string[];
}) {
  const zh = lang === "zh";
  const { confirm, element: confirmElement } = useConfirm(lang);
  const { path, navigate } = useRouter();
  const routeId = (() => {
    try {
      return decodeURIComponent(path.split("/")[3] || "");
    } catch {
      return path.split("/")[3] || "";
    }
  })();
  const basePath =
    tab === "rules"
      ? "/observe/rules"
      : tab === "notifications"
        ? "/observe/channels"
        : "/observe";
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
  const [incidentsFilter, setIncidentsFilter] = useState("firing");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rule, setRule] = useState<AlertRule | null>(null);
  const [channel, setChannel] = useState<AlertChannel | null>(null);
  const [secret, setSecret] = useState("");
  const [detail, setDetail] = useState<{
    incident: AlertIncident;
    events: AlertEvent[];
  }>();
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
        const next = await fetchAlertTab(tab, token, filter, controller.signal);
        if (cancelled) return;
        setOverview(next.overview);
        if (next.presets)
          setPresets(
            next.presets.items.map((preset) => localizeAlertPreset(preset, zh)),
          );
        if (next.rules) setRules(next.rules.items);
        if (next.channels) setChannels(next.channels.items);
        // Expanded history remains a browsing snapshot until explicit refresh.
        if (!expanded && next.incidents) {
          setIncidents(next.incidents);
          setIncidentsFilter(filter);
        }
        if (!expanded && next.deliveries) setDeliveries(next.deliveries);
        setLoaded(true);
        setError("");
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
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
  }, [token, tab, filter, refresh, expanded, zh]);

  useEffect(() => {
    scope.current += 1;
    actionController.current?.abort();
    setDetail(undefined);
    setActionError("");
    setFieldErrors({});
    setRule(null);
    setChannel(null);
    setSecret("");
  }, [tab, token, filter, path]);
  useEffect(() => {
    if (!loaded || initializedRoute.current === path) return;
    initializedRoute.current = path;
    setActionError("");
    setRule(null);
    setChannel(null);
    setSecret("");
    if (!routeId) return;
    if (tab === "rules") {
      const item =
        routeId === "new"
          ? presets[0]
            ? newAlertRule(presets[0])
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
    void getAlerting<{ incident: AlertIncident; events: AlertEvent[] }>(
      `incidents/${encodeURIComponent(routeId)}`,
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
  }, [tab, routeId, token, refresh]);
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
          channelRequest(
            channel.id,
            channel.name,
            channel.enabled,
            channel.appId,
            secret,
            channel.chatId,
          ),
          signal,
        );
        if (signal.aborted) return;
        setChannel(null);
        setSecret("");
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
            <Card>
              <CardHeader>
                <CardTitle>{zh ? "告警引擎" : "Alert engine"}</CardTitle>
                <CardDescription>
                  {zh ? "最近检查" : "Last evaluation"} ·{" "}
                  {stamp(overview.lastEvaluatedAt, zh)}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <div className="flex flex-wrap gap-2">
                  <Badge
                    variant={overview.counts.firing ? "destructive" : "outline"}
                  >
                    {zh ? "触发中" : "Firing"} {overview.counts.firing}
                  </Badge>
                  <Badge variant="secondary">
                    {zh ? "等待持续条件" : "Pending duration"}{" "}
                    {overview.counts.pending}
                  </Badge>
                  <Badge variant="outline">
                    {zh ? "启用规则" : "Enabled rules"} {overview.enabledRules}
                  </Badge>
                </div>
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
              </CardContent>
              <CardFooter className="flex flex-wrap gap-3">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <span>
                    {silenceActive
                      ? `${zh ? "通知静默至" : "Notifications silenced until"} ${stamp(overview.silencedUntil, zh)}`
                      : zh
                        ? "通知未静默"
                        : "Notifications are not silenced"}
                  </span>
                  <p className="text-sm text-muted-foreground">
                    {zh
                      ? "静默期间继续记录异常，暂停告警通知；手动测试仍会发送。"
                      : "Evaluation continues while notifications are silenced. Manual tests still send."}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy}
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
                    ? zh
                      ? "解除静默"
                      : "End silence"
                    : zh
                      ? "维护静默 1 小时"
                      : "Silence for 1 hour"}
                </Button>
              </CardFooter>
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
              <CardHeader>
                <CardTitle>
                  {zh ? "告警与历史" : "Incidents and history"}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <FieldGroup>
                  <Field>
                    <FieldLabel id="incident-status-label">
                      {zh ? "状态" : "Status"}
                    </FieldLabel>
                    <ToggleGroup
                      variant="outline"
                      value={[filter]}
                      onValueChange={(values) => {
                        if (!values.length) return;
                        setExpanded(false);
                        setFilter(values[0]);
                        setError("");
                      }}
                      aria-labelledby="incident-status-label"
                      className="flex-wrap"
                      spacing={1}
                    >
                      {[
                        { value: "firing", label: zh ? "触发中" : "Firing" },
                        {
                          value: "pending",
                          label: zh ? "等待持续条件" : "Pending duration",
                        },
                        {
                          value: "resolved",
                          label: zh ? "已恢复" : "Resolved",
                        },
                        {
                          value: "closed",
                          label: zh ? "管理关闭" : "Closed by configuration",
                        },
                        { value: "", label: zh ? "全部历史" : "All history" },
                      ].map((item) => (
                        <ToggleGroupItem key={item.value} value={item.value}>
                          {item.label}
                        </ToggleGroupItem>
                      ))}
                    </ToggleGroup>
                  </Field>
                </FieldGroup>
                {incidentsFilter !== filter ? (
                  error ? null : (
                    <div aria-busy="true" className="flex flex-col gap-3">
                      <p
                        role="status"
                        className="text-sm text-muted-foreground"
                      >
                        {zh
                          ? "正在读取筛选结果…"
                          : "Loading filtered incidents…"}
                      </p>
                      <Skeleton className="h-24 w-full" />
                    </div>
                  )
                ) : incidents.items.length ? (
                  <Table
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label": zh ? "告警事件列表" : "Alert incidents",
                    }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          {zh ? "规则 / 对象" : "Rule / target"}
                        </TableHead>
                        <TableHead>{zh ? "状态" : "State"}</TableHead>
                        <TableHead>{zh ? "观测值" : "Observed"}</TableHead>
                        <TableHead>
                          {zh ? "开始 / 结束" : "Started / ended"}
                        </TableHead>
                        <TableHead className="text-right">
                          {zh ? "操作" : "Actions"}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {incidents.items.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell>
                            <div className="flex flex-col items-start gap-1">
                              <span className="font-medium">
                                {item.ruleName}
                              </span>
                              <span className="text-muted-foreground">
                                {item.target}
                              </span>
                              <Badge
                                variant={
                                  item.severity === "critical"
                                    ? "destructive"
                                    : "secondary"
                                }
                              >
                                {item.severity === "critical"
                                  ? zh
                                    ? "严重"
                                    : "Critical"
                                  : zh
                                    ? "警告"
                                    : "Warning"}
                              </Badge>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-col items-start gap-1">
                              <AlertStatus status={item.status} zh={zh} />
                              {item.acknowledgedAt ? (
                                <span className="text-muted-foreground">
                                  {zh ? "已确认" : "Acknowledged"} ·{" "}
                                  {stamp(item.acknowledgedAt, zh)}
                                </span>
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell>
                            {formatAlertObservedValue(
                              item.metric,
                              item.value,
                              item.noData,
                              zh,
                            )}
                          </TableCell>
                          <TableCell>
                            <div>{stamp(item.startedAt, zh)}</div>
                            {item.resolvedAt ? (
                              <div className="text-muted-foreground">
                                {stamp(item.resolvedAt, zh)}
                              </div>
                            ) : null}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() =>
                                  navigate(
                                    `/observe/incidents/${encodeURIComponent(item.id)}`,
                                  )
                                }
                              >
                                {zh ? "时间线" : "Timeline"}
                              </Button>
                              {!item.acknowledgedAt &&
                                !["resolved", "closed"].includes(
                                  item.status,
                                ) && (
                                  <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    disabled={busy}
                                    onClick={() =>
                                      void act(
                                        (signal) =>
                                          postAlerting(
                                            `incidents/${encodeURIComponent(item.id)}/ack`,
                                            token,
                                            {},
                                            signal,
                                          ),
                                        zh
                                          ? "已确认；异常恢复前仍保留记录"
                                          : "Acknowledged; incident remains until recovery",
                                      )
                                    }
                                  >
                                    {zh ? "确认" : "Acknowledge"}
                                  </Button>
                                )}
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <AlertEmpty
                    title={zh ? "暂无告警" : "No incidents"}
                    description={
                      zh
                        ? "当前筛选下暂无告警。可在规则中配置需要关注的条件。"
                        : "No incidents match this filter. Configure rules for the conditions you want to monitor."
                    }
                  />
                )}
              </CardContent>
              {incidentsFilter === filter &&
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
                                `incidents?limit=50&cursor=${encodeURIComponent(incidents.nextCursor!)}${filter ? `&status=${filter}` : ""}`,
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
                <dl className="grid gap-4 sm:grid-cols-2">
                  <div className="flex flex-col gap-1">
                    <dt className="text-muted-foreground">
                      {zh ? "观测值" : "Observed"}
                    </dt>
                    <dd>
                      {formatAlertObservedValue(
                        detail.incident.metric,
                        detail.incident.value,
                        detail.incident.noData,
                        zh,
                      )}
                    </dd>
                  </div>
                  <div className="flex flex-col gap-1">
                    <dt className="text-muted-foreground">
                      {zh ? "开始时间" : "Started"}
                    </dt>
                    <dd>{stamp(detail.incident.startedAt, zh)}</dd>
                  </div>
                </dl>
                {detail.events.length ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{zh ? "事件" : "Event"}</TableHead>
                        <TableHead>{zh ? "时间" : "Time"}</TableHead>
                        <TableHead>{zh ? "详情" : "Details"}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {detail.events.map((event) => (
                        <TableRow key={event.id}>
                          <TableCell>
                            {alertEventLabel(event.kind, zh)}
                          </TableCell>
                          <TableCell>{stamp(event.at, zh)}</TableCell>
                          <TableCell className="max-w-lg whitespace-pre-wrap wrap-anywhere">
                            {typeof event.detail === "string"
                              ? event.detail
                              : JSON.stringify(event.detail)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
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
              {!detail.incident.acknowledgedAt &&
                !["resolved", "closed"].includes(detail.incident.status) && (
                  <CardFooter>
                    <Button
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        void act(
                          (signal) =>
                            postAlerting(
                              `incidents/${encodeURIComponent(detail.incident.id)}/ack`,
                              token,
                              {},
                              signal,
                            ),
                          zh ? "事件已确认" : "Incident acknowledged",
                        )
                      }
                    >
                      {busy && (
                        <Spinner data-icon="inline-start" aria-hidden="true" />
                      )}
                      {zh ? "确认事件" : "Acknowledge"}
                    </Button>
                  </CardFooter>
                )}
            </Card>
          )}
          {tab === "rules" && !editor && (
            <Card>
              <CardHeader>
                <CardTitle>{zh ? "规则" : "Rules"}</CardTitle>
                <CardDescription>
                  {zh
                    ? "条件持续满足后触发，恢复时通知。未分配渠道的规则只记录告警。"
                    : "Alerts fire after a condition holds and notify on recovery. Rules without channels only record incidents."}
                </CardDescription>
                <CardAction>
                  <Button
                    disabled={busy || !presets.length}
                    onClick={() => navigate("/observe/rules/new")}
                  >
                    {zh ? "新建规则" : "Create rule"}
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent>
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
                          {zh ? "规则 / 对象" : "Rule / target"}
                        </TableHead>
                        <TableHead>{zh ? "条件" : "Condition"}</TableHead>
                        <TableHead>{zh ? "通知渠道" : "Channels"}</TableHead>
                        <TableHead className="text-right">
                          {zh ? "操作" : "Actions"}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rules.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell>
                            <div className="flex flex-col items-start gap-1">
                              <span className="font-medium">{item.name}</span>
                              <span className="text-muted-foreground">
                                {item.target === "*"
                                  ? zh
                                    ? "全部对象"
                                    : "All targets"
                                  : item.target}
                              </span>
                              <Badge
                                variant={item.enabled ? "secondary" : "outline"}
                              >
                                {item.enabled
                                  ? zh
                                    ? "已启用"
                                    : "Enabled"
                                  : zh
                                    ? "已停用"
                                    : "Disabled"}
                              </Badge>
                              {(item.silencedUntil || 0) >
                                Date.now() / 1000 && (
                                <span className="text-muted-foreground">
                                  {zh ? "静默至" : "Silenced until"}{" "}
                                  {stamp(item.silencedUntil, zh)}
                                </span>
                              )}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div>
                              {presets.find((p) => p.metric === item.metric)
                                ?.name || item.metric}
                            </div>
                            <div className="text-muted-foreground">
                              &gt; {item.threshold}{" "}
                              {presets.find((p) => p.metric === item.metric)
                                ?.unit || ""}{" "}
                              · {item.forSeconds}s
                            </div>
                          </TableCell>
                          <TableCell>
                            {channelNames(item.channelIds) ||
                              (zh ? "仅记录" : "Record only")}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() =>
                                  navigate(
                                    `/observe/rules/${encodeURIComponent(item.id)}`,
                                  )
                                }
                              >
                                {zh ? "编辑" : "Edit"}
                              </Button>
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() =>
                                  void act((signal) =>
                                    postAlerting(
                                      "rules",
                                      token,
                                      { ...item, enabled: !item.enabled },
                                      signal,
                                    ),
                                  )
                                }
                              >
                                {item.enabled
                                  ? zh
                                    ? "停用"
                                    : "Disable"
                                  : zh
                                    ? "启用"
                                    : "Enable"}
                              </Button>
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() =>
                                  void act((signal) =>
                                    postAlerting(
                                      "rules",
                                      token,
                                      {
                                        ...item,
                                        silencedUntil:
                                          (item.silencedUntil || 0) >
                                          Date.now() / 1000
                                            ? 0
                                            : Math.floor(Date.now() / 1000) +
                                              3600,
                                      },
                                      signal,
                                    ),
                                  )
                                }
                              >
                                {(item.silencedUntil || 0) > Date.now() / 1000
                                  ? zh
                                    ? "解除静默"
                                    : "Unsilence"
                                  : zh
                                    ? "静默 1h"
                                    : "Silence 1h"}
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
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
              onInputCapture={clearFieldError}
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
                          {zh
                            ? "对象 ID（* 表示全部）"
                            : "Target ID (* for all)"}
                        </FieldLabel>
                        <Combobox
                          disabled={busy}
                          items={["*", ...new Set(targetNames)]}
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
                          {rule.metric.startsWith("node.")
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
                          variant="outline"
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
                          className="flex-wrap"
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
                          variant="outline"
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
                          className="flex-wrap"
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
                                variant={item.enabled ? "secondary" : "outline"}
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
                            <div className="flex flex-col items-start gap-1">
                              <span>App ID: {item.appId}</span>
                              <span className="text-muted-foreground">
                                {zh ? "群聊 ID" : "Chat ID"}: {item.chatId}
                              </span>
                              <Badge
                                variant={
                                  item.appSecretConfigured
                                    ? "outline"
                                    : "destructive"
                                }
                              >
                                {item.appSecretConfigured
                                  ? zh
                                    ? "App Secret 已配置"
                                    : "App Secret configured"
                                  : zh
                                    ? "App Secret 缺失"
                                    : "App Secret missing"}
                              </Badge>
                            </div>
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
              onInputCapture={clearFieldError}
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
                          onChange={(e) =>
                            setChannel({ ...channel, appId: e.target.value })
                          }
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
                            <div className="text-muted-foreground">
                              {alertEventLabel(item.kind, zh)}
                            </div>
                          </TableCell>
                          <TableCell>
                            <AlertStatus status={item.status} zh={zh} />
                          </TableCell>
                          <TableCell>
                            <div>
                              {stamp(item.sentAt || item.createdAt, zh)}
                            </div>
                            <div className="text-muted-foreground">
                              {item.attempts} {zh ? "次尝试" : "attempts"}
                            </div>
                          </TableCell>
                          <TableCell className="max-w-sm whitespace-normal wrap-anywhere">
                            <div>{item.lastError || "—"}</div>
                            {item.status !== "sent" && !!item.nextAttemptAt && (
                              <div className="text-muted-foreground">
                                {zh ? "下次尝试" : "Next attempt"} ·{" "}
                                {stamp(item.nextAttemptAt, zh)}
                              </div>
                            )}
                            <div className="text-muted-foreground">
                              {item.id}
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
      {!editor && (
        <div className="flex flex-wrap items-center justify-end gap-3">
          <span className="text-sm text-muted-foreground">
            {zh ? "每 15 秒自动刷新" : "Refreshes every 15 seconds"}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={refreshNow}
            disabled={busy}
          >
            {zh ? "刷新" : "Refresh"}
          </Button>
        </div>
      )}
    </div>
  );
}
