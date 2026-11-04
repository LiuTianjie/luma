import { useEffect, useMemo, useState } from "react";
import { apiGet } from "../apiClient";
import { useRouter, toHref } from "../router";
import { investigationPath, observationQuery } from "../observeContext";
import { formatBytes, formatDateTime } from "../format";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Alert, AlertDescription } from "./ui/alert";
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
  ["task_error_ratio", "任务执行失败比例", "Failed task attempts", "%"],
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
    { search } = useRouter();
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
    setSnapshot(undefined);
    void apiGet<Snapshot>(
      `/v1/dashboard/observe/status?${query}`,
      token,
      controller.signal,
    )
      .then((data) => {
        if (!disposed) setSnapshot(data);
      })
      .catch(() => {
        if (!disposed)
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
  }, [query, token, revision, zh]);
  const signals = snapshot?.signals || {},
    freshness = signals.freshness;
  const stale = freshness?.state === "ok" && (freshness.value || 0) > 120;
  const definitions = app ? APPLICATION : PLATFORM;
  const coverage = snapshot?.coverage;
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle>
              {app
                ? zh
                  ? "业务状态与数据可信度"
                  : "Application health and data coverage"
                : zh
                  ? "观测平台状态"
                  : "Observation platform health"}
            </CardTitle>
            <CardDescription>
              {snapshot
                ? `${zh ? "查询于" : "Read at"} ${formatDateTime(snapshot.sampledAt, lang)}${snapshot.historical ? (zh ? " · 固定历史窗口" : " · Fixed historical window") : ""}`
                : zh
                  ? "指标、日志和链路的采集与查询状态"
                  : "Collection and query health for metrics, logs and traces"}
            </CardDescription>
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={loading}
            onClick={() => setRevision((n) => n + 1)}
          >
            {zh ? "刷新状态" : "Refresh"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        {loading ? (
          <div className="grid gap-3 sm:grid-cols-3" aria-busy="true">
            {[1, 2, 3].map((n) => (
              <Skeleton key={n} className="h-20" />
            ))}
          </div>
        ) : error || snapshot?.state !== "ok" ? (
          <Alert variant="warning">
            <AlertDescription>
              {error ||
                (zh
                  ? "观测查询繁忙，请稍后重试。"
                  : "Observation queries are busy. Retry shortly.")}
            </AlertDescription>
          </Alert>
        ) : (
          <>
            {app && (
              <p className="text-sm">
                <a
                  className="text-link hover:underline"
                  href={toHref(
                    investigationPath(
                      `/apps/${encodeURIComponent(app)}/alerts`,
                      search,
                    ),
                  )}
                >
                  {snapshot.controlAlerts?.state === "ok"
                    ? snapshot.controlAlerts.enabledRules
                      ? zh
                        ? `当前 ${snapshot.controlAlerts.counts?.firing || 0} 条 Control 告警触发，${snapshot.controlAlerts.counts?.pending || 0} 条等待持续条件`
                        : `${snapshot.controlAlerts.counts?.firing || 0} Control alerts firing`
                      : zh
                        ? "尚未配置应用告警规则"
                        : "No application alert rules configured"
                    : zh
                      ? "应用告警状态未知"
                      : "Application alert state unknown"}
                </a>
              </p>
            )}
            <dl className="grid divide-y rounded-lg border sm:grid-cols-2 sm:divide-y-0 xl:grid-cols-3">
              {definitions.map(([key, cn, en, unit, threshold]) => {
                const signal = signals[key],
                  warning =
                    signal?.value != null &&
                    threshold != null &&
                    signal.value > threshold;
                return (
                  <div key={key} className="min-w-0 space-y-2 p-4">
                    <dt className="text-xs text-muted-foreground">
                      {zh ? cn : en}
                    </dt>
                    <dd className="flex flex-wrap items-baseline gap-2">
                      <span className="text-xl font-semibold tabular-nums">
                        {valueText(signal, unit)}
                      </span>
                      <Badge variant={warning ? "warning" : "secondary"}>
                        {warning
                          ? zh
                            ? "需关注"
                            : "Attention"
                          : stateLabel(
                              signal,
                              zh,
                              stale && !key.startsWith("http_"),
                            )}
                      </Badge>
                    </dd>
                  </div>
                );
              })}
            </dl>
            <div
              className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-muted-foreground"
              role="status"
            >
              <span>
                {app
                  ? zh
                    ? "任务指标新鲜度："
                    : "Task metric freshness: "
                  : zh
                    ? "Collector 指标新鲜度："
                    : "Collector metric freshness: "}
                {stateLabel(freshness, zh, stale)}
                {freshness?.state === "ok"
                  ? ` · ${valueText(freshness, "s")}`
                  : ""}
              </span>
              <span>
                {zh ? "查询来源：" : "Query sources: "}
                {
                  Object.values(signals).filter(
                    (s) => s.state === "ok" || s.state === "empty",
                  ).length
                }
                /{Object.keys(signals).length} {zh ? "响应成功" : "responded"}
              </span>
            </div>
            <p className="text-sm text-muted-foreground">
              {zh ? "日志覆盖（当前）：" : "Current log coverage: "}
              {coverage?.state === "ok"
                ? `${coverage.items?.filter((r) => r.at > 0 && snapshot.sampledAt - r.at < 120 && (!r.gapAt || snapshot.sampledAt - r.gapAt >= 900)).length || 0} / ${coverage.items?.length || 0} ${zh ? "服务已追平且近期无缺口" : "services caught up without recent gaps"}`
                : zh
                  ? "状态未知"
                  : "Unknown"}
            </p>
            <details>
              <summary className="w-fit cursor-pointer rounded text-sm font-medium focus-visible:outline-2 focus-visible:outline-ring">
                {zh
                  ? "日志覆盖与采集缺口（当前）"
                  : "Log coverage and collection gaps (current)"}
              </summary>
              <div className="mt-3">
                {coverage?.state !== "ok" ? (
                  <p className="text-sm text-muted-foreground">
                    {coverage?.state === "empty"
                      ? zh
                        ? "尚无此应用的日志覆盖记录。"
                        : "No coverage records for this application."
                      : zh
                        ? "日志覆盖状态暂不可用。"
                        : "Log coverage is unavailable."}
                  </p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>
                          {zh ? "应用 / 服务" : "Application / service"}
                        </TableHead>
                        <TableHead>
                          {zh ? "最近追平" : "Last caught up"}
                        </TableHead>
                        <TableHead>{zh ? "状态" : "State"}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {coverage.items?.map((row) => (
                        <TableRow key={`${row.app}/${row.task}`}>
                          <TableCell className="break-all">
                            {row.app} / {row.task}
                          </TableCell>
                          <TableCell>
                            {row.at ? formatDateTime(row.at, lang) : "—"}
                          </TableCell>
                          <TableCell>
                            {!row.at || snapshot.sampledAt - row.at > 120
                              ? zh
                                ? "覆盖不完整"
                                : "Incomplete"
                              : row.gapAt &&
                                  snapshot.sampledAt - row.gapAt < 900
                                ? zh
                                  ? "近期有缺口"
                                  : "Recent gap"
                                : zh
                                  ? "已追平"
                                  : "Caught up"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>
            </details>
            {!app && (
              <details>
                <summary className="w-fit cursor-pointer text-sm font-medium">
                  {zh ? "各组件采集状态" : "Component scrape status"}
                </summary>
                <ul className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                  {signals.targets?.items?.map((row, i) => (
                    <li key={i}>
                      {row.labels.job} ·{" "}
                      {row.value === 1
                        ? zh
                          ? "可采集"
                          : "Up"
                        : zh
                          ? "采集失败"
                          : "Down"}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {app && (
              <div className="flex flex-wrap gap-4 text-sm">
                <a
                  className="text-link hover:underline"
                  href={toHref(
                    investigationPath(
                      `/apps/${encodeURIComponent(app)}/dashboards`,
                      search,
                      { observeView: "overview" },
                    ),
                  )}
                >
                  {zh ? "查看任务详情" : "Task details"}
                </a>
                <a
                  className="text-link hover:underline"
                  href={toHref(
                    investigationPath(
                      `/apps/${encodeURIComponent(app)}/logs`,
                      search,
                    ),
                  )}
                >
                  {zh ? "检索相关日志" : "Related logs"}
                </a>
              </div>
            )}
          </>
        )}
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          {app
            ? zh
              ? "0 仅表示已观测值为零；没有采样不代表正常。HTTP 为整个应用的入口汇总，任务统计遵循时间与身份筛选。活跃任务和投递次数不能推算 Broker 积压。"
              : "Zero is an observed value; missing samples do not establish health. HTTP covers the application's ingress; task metrics use the selected time and identity. Active tasks and publications do not measure broker backlog."
            : zh
              ? "0 仅表示已观测值为零；没有采样不代表正常。日志覆盖显示当前状态，不能据此证明历史数据完整。"
              : "Zero is an observed value; missing samples do not establish health. Current log coverage does not establish historical completeness."}
        </p>
      </CardContent>
    </Card>
  );
}
