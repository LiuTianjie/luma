import { useEffect, useMemo, useState } from "react";
import { apiGet } from "../apiClient";
import { useRouter, toHref } from "../router";
import { investigationPath, observationQuery } from "../observeContext";
import { EMPTY_VALUE, formatBytes, formatDateTime } from "../format";
import { cn } from "@/lib/utils";
import { ArrowRight, Bell, CheckCircle2, Info, RefreshCw, TriangleAlert } from "@/components/icons";
import { useObserveRefresh, useRefreshEvent } from "./observeRefresh";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "./ui/alert";
import { Skeleton } from "./ui/skeleton";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "./ui/table";
import type { Lang } from "../types";

type Signal = {
  state: string;
  value?: number | null;
  durationMs?: number;
  counts?: Record<string, number>;
  source?: string;
  items?: { labels: Record<string, string>; value: number }[];
};
type Snapshot = {
  state: string;
  sampledAt: number;
  historical?: boolean;
  controlAlerts?: {
    state: string;
    counts?: { firing: number; pending: number };
    enabledRules?: number;
    window?: { fired: number; resolved: number; since: number; until: number };
  };
  signals: Record<string, Signal>;
  coverage?: {
    state: string;
    items?: { app: string; task: string; at: number; gapAt?: number }[];
  };
};
type Definition = [string, string, string, string, number?];
const PLATFORM: Definition[] = [
  ["input_metrics", "指标接收", "Metric points received", "/s"],
  ["input_spans", "Span 接收", "Spans received", "/s"],
  ["refused", "近 15 分钟拒收", "Refused · 15m", "", 0],
  ["export_failures", "近 15 分钟发送失败", "Send failures · 15m", "", 0],
  ["queue_ratio", "导出队列占用", "Export queue usage", "%", 0.8],
  ["log_drops", "近 15 分钟日志丢弃", "Dropped log lines · 15m", "", 0],
  ["unresolved", "未归属容器", "Unresolved containers", "", 0],
  ["query_p99", "指标查询 P99", "Metric query P99", "s"],
  ["logs_bytes", "日志存储", "Log storage", "bytes"],
  ["free_bytes", "存储可用空间", "Free storage", "bytes"],
  [
    "tempo_poll_errors",
    "近 15 分钟 Trace 索引错误",
    "Trace index errors · 15m",
    "",
    0,
  ],
  [
    "log_backlog_bytes",
    "本轮读取剩余日志",
    "Unread logs in last poll",
    "bytes",
  ],
];
const APPLICATION: Definition[] = [
  [
    "http_error_ratio",
    "应用 HTTP 错误率 · 5 分钟",
    "App HTTP errors · 5m",
    "%",
  ],
  ["http_p95", "应用 HTTP P95 · 5 分钟", "App HTTP P95 · 5m", "s"],
  ["task_error_ratio", "任务失败比例（含业务失败）", "Failed attempts (including business failures)", "%", 0],
  ["task_p95", "Taskiq 耗时 P95", "Taskiq duration P95", "s"],
  ["active", "活跃任务", "Active tasks", ""],
  ["heartbeat_age", "最旧进程心跳距采样", "Oldest heartbeat age", "s", 120],
];
function stateLabel(signal: Signal | undefined, zh: boolean, stale: boolean) {
  if (!signal || signal.state === "empty")
    return zh ? "没有采样" : "No samples";
  if (signal.state !== "ok")
    return zh
      ? signal.state === "rate_limited"
        ? "查询繁忙"
        : signal.state === "timeout"
          ? "查询超时"
          : "查询失败"
      : signal.state;
  return stale
    ? zh
      ? "采样过期"
      : "Stale samples"
    : zh
      ? "有采样"
      : "Available";
}
function valueText(signal: Signal | undefined, unit: string) {
  const n = signal?.value;
  if (signal?.state !== "ok" || n == null || !Number.isFinite(n)) return "—";
  if (unit === "bytes") return formatBytes(n);
  if (unit === "s" && n > 0 && n < 1) return `${Math.round(n * 1000)} ms`;
  return (
    new Intl.NumberFormat(undefined, {
      maximumFractionDigits: unit === "%" ? 2 : 1,
    }).format(unit === "%" ? n * 100 : n) +
    (unit === "%" ? "%" : unit ? ` ${unit}` : "")
  );
}
export function ObservationStatus({
  app = "",
  lang,
  token,
}: {
  app?: string;
  lang: Lang;
  token: string;
}) {
  const zh = lang === "zh",
    { search } = useRouter(),
    refreshHost = useObserveRefresh();
  const [revision, setRevision] = useState(0),
    [snapshot, setSnapshot] = useState<Snapshot>(),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  const query = useMemo(
    () =>
      new URLSearchParams(
        app
          ? {
              app,
              ...Object.fromEntries(
                Object.entries(observationQuery(search)).map(([k, v]) => [
                  k,
                  String(v),
                ]),
              ),
            }
          : {},
      ).toString(),
    [app, search, revision],
  );
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    const timeout = setTimeout(() => controller.abort(), 15000);
    setLoading(true);
    setError("");
    void apiGet<Snapshot>(
      `/v1/dashboard/observe/status?${query}`,
      token,
      controller.signal,
    )
      .then((data) => {
        if (disposed) return;
        setSnapshot(data);
        refreshHost.markUpdated(data.sampledAt);
      })
      .catch(() => {
        if (disposed) return;
        refreshHost.setRefreshing(false);
        setSnapshot(undefined);
        setError(
            zh
              ? "暂时无法读取观测状态，请重试。"
              : "Observation status is unavailable. Retry shortly.",
          );
      })
      .finally(() => {
        clearTimeout(timeout);
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timeout);
    };
  }, [query, token, revision, zh, refreshHost]);
  useRefreshEvent(() => setRevision((n) => n + 1));
  const signals = snapshot?.signals || {},
    freshness = signals.freshness;
  const stale = freshness?.state === "ok" && (freshness.value || 0) > 120;
  const definitions = app ? APPLICATION : PLATFORM;
  const coverage = snapshot?.coverage;
  const coverageRows = coverage?.items || [];
  const rowState = (row: { at: number; gapAt?: number }) =>
    !row.at || (snapshot?.sampledAt || 0) - row.at > 120
      ? "incomplete"
      : row.gapAt && (snapshot?.sampledAt || 0) - row.gapAt < 900
        ? "gap"
        : "ok";
  const caughtUp = coverageRows.filter((row) => rowState(row) === "ok").length;
  const targets = signals.targets?.items || [];
  const targetsDown = targets.filter((row) => row.value !== 1).length;
  const responded = Object.values(signals).filter((sig) => sig.state === "ok" || sig.state === "empty").length;
  const total = Object.keys(signals).length;
  const attention = definitions.filter(([key, , , , threshold]) => {
    const sig = signals[key];
    return sig?.value != null && threshold != null && sig.value > threshold;
  }).length;
  const failing = definitions.filter(([key]) => signals[key] && !["ok", "empty"].includes(signals[key].state)).length;
  // One headline verdict, computed from the same facts the tiles show.
  const verdict: { tone: "success" | "warning" | "destructive"; title: string; detail: string } | null =
    !snapshot || snapshot.state !== "ok"
      ? null
      : stale
        ? { tone: "destructive", title: zh ? "采集已停滞" : "Collection has stalled", detail: zh ? `最近一次指标采集在 ${valueText(freshness, "s")} 前，下方数值可能已过期。` : `The latest metric scrape was ${valueText(freshness, "s")} ago; values below may be stale.` }
        : targetsDown || failing || attention
          ? {
              tone: "warning",
              title: zh ? "部分信号需要关注" : "Some signals need attention",
              detail: [
                targetsDown ? (zh ? `${targetsDown} 个组件采集失败` : `${targetsDown} components down`) : "",
                failing ? (zh ? `${failing} 项查询失败` : `${failing} queries failed`) : "",
                attention ? (zh ? `${attention} 项超过阈值` : `${attention} over threshold`) : "",
              ].filter(Boolean).join(zh ? "，" : ", "),
            }
          : { tone: "success", title: app ? (zh ? "遥测数据正常" : "Telemetry is flowing") : (zh ? "观测平台运行正常" : "Observability is healthy"), detail: (zh ? `${responded}/${total} 个查询来源已响应` : `${responded}/${total} query sources responded`) + (freshness?.state === "ok" ? (zh ? `，最近采集于 ${valueText(freshness, "s")} 前。` : `; last scrape ${valueText(freshness, "s")} ago.`) : (zh ? "。" : ".")) };
  const hosted = refreshHost.hosted && !app;
  const readAt = snapshot ? `${zh ? "查询于" : "Read at"} ${formatDateTime(snapshot.sampledAt, lang)}${snapshot.historical ? (zh ? " · 固定历史窗口" : " · Fixed historical window") : ""}` : "";
  const footnote = app
    ? zh
      ? "失败比例按窗口内任务结束日志去重统计：业务失败或执行失败计一次失败，分母为成功加失败；重试中、取消和未知单列。没有记录不代表正常。HTTP 为应用入口汇总；日志覆盖为当前状态。"
      : "The ratio uses deduplicated completion logs: business or execution failure counts once; the denominator is successes plus failures. Retrying, cancelled and unknown are separate. Missing records do not establish health. HTTP covers app ingress; log coverage is current."
    : zh
      ? "0 仅表示已观测值为零；没有采样不代表正常。日志覆盖显示当前状态，不能据此证明历史数据完整。"
      : "Zero is an observed value; missing samples do not establish health. Current log coverage does not establish historical completeness.";
  const tiles = (
    <dl className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 [&>div]:border-hairline max-sm:[&>div+div]:border-t sm:max-xl:[&>div:nth-child(n+3)]:border-t sm:max-xl:[&>div:nth-child(even)]:border-l xl:[&>div:nth-child(n+4)]:border-t xl:[&>div:not(:nth-child(3n+1))]:border-l">
      {definitions.map(([key, zhLabel, en, unit, threshold]) => {
        const signal = signals[key],
          warning = signal?.value != null && threshold != null && signal.value > threshold,
          broken = signal && !["ok", "empty"].includes(signal.state),
          missing = !signal || signal.state === "empty",
          itemStale = stale && !key.startsWith("http_");
        return (
          <div key={key} className="flex min-w-0 flex-col gap-1.5 px-5 py-4">
            <dt className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              {warning || broken ? <span aria-hidden="true" className={cn("size-1.5 rounded-full", broken ? "bg-destructive" : "bg-warning")} /> : null}
              <span className="truncate">{zh ? zhLabel : en}</span>
            </dt>
            <dd className="flex min-w-0 flex-col gap-0.5">
              <span className={cn("truncate text-xl/7 font-semibold tracking-[-0.01em] tabular-nums", missing && "text-subtle-foreground", warning && "text-warning-foreground")}>
                {valueText(signal, unit)}
              </span>
              <span className={cn(key === "task_error_ratio" ? "text-xs whitespace-normal" : "truncate text-xs", warning ? "text-warning-foreground" : broken ? "text-destructive-foreground" : "text-muted-foreground")}>
                {key === "task_error_ratio" && signal?.counts
                  ? (zh ? `失败 ${signal.counts.failed} / 成功 ${signal.counts.succeeded} · 重试 ${signal.counts.retrying} · 取消 ${signal.counts.cancelled} · 未知 ${signal.counts.unknown}` : `Failed ${signal.counts.failed} / succeeded ${signal.counts.succeeded} · retrying ${signal.counts.retrying} · cancelled ${signal.counts.cancelled} · unknown ${signal.counts.unknown}`)
                  : warning
                  ? zh ? `超过阈值 ${valueText({ state: "ok", value: threshold }, unit)}` : `Above ${valueText({ state: "ok", value: threshold }, unit)}`
                  : broken || missing || itemStale
                    ? stateLabel(signal, zh, itemStale)
                    : threshold != null
                      ? zh ? `阈值 ${valueText({ state: "ok", value: threshold }, unit)}` : `Threshold ${valueText({ state: "ok", value: threshold }, unit)}`
                      : snapshot?.historical ? (zh ? "窗口结束时采样" : "Sample at window end") : zh ? "最新采样" : "Latest sample"}
              </span>
            </dd>
          </div>
        );
      })}
    </dl>
  );
  const coverageTable = coverage?.state !== "ok" ? (
    <p className="px-(--card-spacing) pb-(--card-spacing) text-[0.8125rem] text-muted-foreground">
      {coverage?.state === "empty"
        ? zh ? "尚无此应用的日志覆盖记录。" : "No coverage records for this application."
        : zh ? "日志覆盖状态暂不可用。" : "Log coverage is unavailable."}
    </p>
  ) : (
    <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "日志覆盖" : "Log coverage" }}>
      <TableHeader>
        <TableRow>
          <TableHead>{zh ? "应用 / 服务" : "Application / service"}</TableHead>
          <TableHead>{zh ? "最近追平" : "Last caught up"}</TableHead>
          <TableHead>{zh ? "状态" : "State"}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {coverageRows.map((row) => {
          const state = rowState(row);
          return (
            <TableRow key={`${row.app}/${row.task}`}>
              <TableCell className="break-all">
                <span className="font-medium">{row.app}</span>
                <span className="text-muted-foreground"> / {row.task}</span>
              </TableCell>
              <TableCell className="tabular-nums">{row.at ? formatDateTime(row.at, lang) : EMPTY_VALUE}</TableCell>
              <TableCell>
                <Badge appearance={state === "ok" ? "dot" : "filled"} variant={state === "ok" ? "success" : state === "gap" ? "warning" : "destructive"}>
                  {state === "incomplete" ? (zh ? "覆盖不完整" : "Incomplete") : state === "gap" ? (zh ? "近期有缺口" : "Recent gap") : (zh ? "已追平" : "Caught up")}
                </Badge>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
  return (
    <div className="flex min-w-0 flex-col gap-6">
      {loading && !snapshot ? (
        <Card aria-busy="true">
          <CardContent className="flex flex-col gap-4">
            <span role="status" className="sr-only">{zh ? "正在读取观测状态…" : "Loading observation status…"}</span>
            <Skeleton className="h-6 w-64" />
            <div className="grid gap-3 sm:grid-cols-3">
              {[1, 2, 3].map((n) => <Skeleton key={n} className="h-16" />)}
            </div>
          </CardContent>
        </Card>
      ) : error || snapshot?.state !== "ok" ? (
        <Alert variant="warning">
          <TriangleAlert />
          <AlertTitle>{zh ? "无法读取观测状态" : "Observation status unavailable"}</AlertTitle>
          <AlertDescription>
            {error || (zh ? "观测查询繁忙，请稍后重试。" : "Observation queries are busy. Retry shortly.")}
          </AlertDescription>
          <AlertAction>
            <Button size="sm" variant="outline" disabled={loading} onClick={() => setRevision((n) => n + 1)}>
              {zh ? "重试" : "Retry"}
            </Button>
          </AlertAction>
        </Alert>
      ) : (
        <>
          <Card className="gap-0 py-0">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-hairline px-5 py-4">
              {verdict && (
                <span className={cn("flex size-8 shrink-0 items-center justify-center rounded-full", verdict.tone === "success" ? "bg-success-soft text-success-foreground" : verdict.tone === "warning" ? "bg-warning-soft text-warning-foreground" : "bg-destructive-soft text-destructive-foreground")} aria-hidden="true">
                  {verdict.tone === "success" ? <CheckCircle2 className="size-4" /> : <TriangleAlert className="size-4" />}
                </span>
              )}
              <div className="flex min-w-0 flex-1 basis-64 flex-col gap-0.5">
                <h2 className="font-heading text-[0.9375rem] font-semibold">
                  {verdict?.title || (app ? (zh ? "业务状态与数据可信度" : "Application health and data coverage") : (zh ? "观测平台状态" : "Observation platform health"))}
                </h2>
                <p className="text-[0.8125rem] text-muted-foreground">{verdict?.detail}</p>
              </div>
              <div className="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
                {!hosted && readAt ? <span>{readAt}</span> : snapshot?.historical ? <span>{zh ? "固定历史窗口" : "Fixed historical window"}</span> : null}
                {!hosted && (
                  <Button size="sm" variant="outline" disabled={loading} onClick={() => setRevision((n) => n + 1)}>
                    <RefreshCw data-icon="inline-start" aria-hidden="true" className={cn(loading && "animate-spin")} />
                    {zh ? "刷新" : "Refresh"}
                  </Button>
                )}
              </div>
            </div>
            {app && snapshot && (
              <a
                className="flex items-center gap-2 border-b border-hairline px-5 py-3 text-[0.8125rem] hover:bg-muted/50"
                href={toHref(investigationPath(`/apps/${encodeURIComponent(app)}/alerts`, search))}
              >
                <Bell className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  {snapshot.controlAlerts?.state === "ok"
                    ? snapshot.controlAlerts.enabledRules
                      ? zh
                        ? `所选窗口内 ${snapshot.controlAlerts.window?.fired ?? "—"} 条告警曾触发；当前 ${snapshot.controlAlerts.counts?.firing ?? "—"} 条触发，${snapshot.controlAlerts.counts?.pending ?? "—"} 条等待持续条件（应用全部服务）`
                        : `${snapshot.controlAlerts.window?.fired ?? "—"} alerts fired in the selected window; ${snapshot.controlAlerts.counts?.firing ?? "—"} currently firing, ${snapshot.controlAlerts.counts?.pending ?? "—"} pending (all app services)`
                      : zh ? "尚未配置应用告警规则" : "No application alert rules configured"
                    : zh ? "应用告警状态未知" : "Application alert state unknown"}
                </span>
                <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
              </a>
            )}
            {tiles}
          </Card>
          <div className={cn("grid min-w-0 gap-6", !app && "xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]")}>
            <Card className="self-start">
              <CardHeader className={cn(coverage?.state === "ok" && "border-b border-hairline")}>
                <CardTitle>{zh ? "日志覆盖与采集缺口" : "Log coverage and collection gaps"}</CardTitle>
                <CardDescription>
                  {coverage?.state === "ok"
                    ? zh ? `${caughtUp} / ${coverageRows.length} 个服务已追平且近 15 分钟无缺口` : `${caughtUp} of ${coverageRows.length} services caught up without gaps in 15 minutes`
                    : zh ? "当前状态" : "Current state"}
                </CardDescription>
              </CardHeader>
              <CardContent className={cn(coverage?.state === "ok" && "-mt-(--card-spacing)")}>{coverageTable}</CardContent>
            </Card>
            {!app && (
              <Card className="self-start has-[>ul:last-child]:pb-0">
                <CardHeader className={cn(targets.length > 0 && "border-b border-hairline")}>
                  <CardTitle>{zh ? "组件采集" : "Component scrapes"}</CardTitle>
                  <CardDescription>
                    {targets.length
                      ? targetsDown
                        ? zh ? `${targetsDown} 个组件采集失败` : `${targetsDown} of ${targets.length} components down`
                        : zh ? `${targets.length} 个组件均可采集` : `All ${targets.length} components up`
                      : zh ? "没有组件采集记录" : "No scrape targets reported"}
                  </CardDescription>
                </CardHeader>
                {targets.length ? (
                  <ul className="-mt-(--card-spacing) divide-y divide-hairline text-[0.8125rem]">
                    {[...targets].sort((a, b) => a.value - b.value || String(a.labels.job).localeCompare(String(b.labels.job))).map((row, i) => (
                      <li key={`${row.labels.job}-${row.labels.instance || i}`} className="flex items-center gap-3 px-(--card-spacing) py-2.5">
                        <span className="min-w-0 flex-1 truncate font-mono text-xs" translate="no" title={row.labels.instance}>{row.labels.job}</span>
                        <Badge appearance={row.value === 1 ? "dot" : "filled"} variant={row.value === 1 ? "success" : "destructive"}>
                          {row.value === 1 ? (zh ? "可采集" : "Up") : (zh ? "采集失败" : "Down")}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </Card>
            )}
          </div>
          {app && (
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" nativeButton={false} render={<a href={toHref(investigationPath(`/apps/${encodeURIComponent(app)}/dashboards`, search, { observeView: "overview" }))} />}>
                {zh ? "查看任务详情" : "Task details"}
              </Button>
              <Button variant="outline" size="sm" nativeButton={false} render={<a href={toHref(investigationPath(`/apps/${encodeURIComponent(app)}/logs`, search))} />}>
                {zh ? "检索相关日志" : "Related logs"}
              </Button>
            </div>
          )}
        </>
      )}
      <p className="flex max-w-3xl gap-2 text-xs/5 text-muted-foreground">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <span>{footnote}</span>
      </p>
    </div>
  );
}
