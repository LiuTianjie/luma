import { observationQuery } from "../observeContext";
import { useEffect, useMemo } from "react";
import { localizeState } from "../i18n";
import {
  historyKey,
  type HistoryState,
  type HistoryTarget,
} from "../metricsApi";
import { useMetricsHistories } from "../useMetricsHistories";
import type {
  ActualResourceValues,
  DashboardNode,
  DashboardService,
  Lang,
  ResourceValues,
} from "../types";
import { SelectControl, StatePill, UsageMeter, usageTone } from "./primitives";
import { cn } from "@/lib/utils";
import { useObserveRefresh } from "./observeRefresh";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { AlertCircle } from "@/components/icons";
import { TrendChart } from "./charts";
import { useRouter, useSearchParams } from "../router";
import { formatBytes, formatPercent } from "../format";

const HISTORY_WINDOWS = [900, 3600, 21600];

function HistoryStatus({ lang, state, until }: { lang: Lang; state?: HistoryState; until?: number }) {
  const zh = lang === "zh";
  if (!state)
    return (
      <div aria-busy="true" className="flex flex-col gap-2">
        <p className="text-sm text-muted-foreground">
          {zh ? "正在读取历史…" : "Loading history…"}
        </p>
        <Skeleton className="h-4 w-56" />
      </div>
    );
  const points = Object.values(state.payload?.series || {}).flat();
  const latest =
    state.payload?.latestSampleAt ??
    (points.length ? Math.max(...points.map(([ts]) => ts)) : null);
  const earliest =
    state.payload?.availableFrom ??
    (points.length ? Math.min(...points.map(([ts]) => ts)) : null);
  const stale = latest !== null && (until ?? Date.now() / 1000) - latest > 180;
  const format = (ts: number) =>
    new Date(ts * 1000).toLocaleTimeString(zh ? "zh-CN" : "en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  const range =
    latest === null
      ? zh
        ? "当前窗口暂无采样"
        : "No samples in this window"
      : `${format(earliest ?? latest)}–${format(latest)}${state.payload?.sampleIntervalSeconds ? ` · ${state.payload.sampleIntervalSeconds}s ${zh ? "聚合" : "buckets"}` : ""}`;
  if (state.error || stale)
    return (
      <Alert variant={state.error ? "destructive" : "default"}>
        <AlertCircle />
        <AlertTitle>
          {state.error
            ? zh
              ? "历史刷新失败"
              : "History refresh failed"
            : zh
              ? "采样已过期"
              : "Stale samples"}
        </AlertTitle>
        <AlertDescription>
          {state.error ? (
            <>
              <p>
                {state.payload
                  ? zh
                    ? "保留上次数据，稍后自动重试。"
                    : "Showing previous data; retrying automatically."
                  : zh
                    ? "稍后自动重试。"
                    : "Retrying automatically."}{" "}
                {state.error}
              </p>
              <p>{range}</p>
            </>
          ) : (
            range
          )}
        </AlertDescription>
      </Alert>
    );
  return <p className="text-sm text-muted-foreground">{range}</p>;
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

function nodeTitle(node: DashboardNode) {
  return node.displayName || node.name || "-";
}

function nodeMeta(node: DashboardNode) {
  return [node.region, node.role, node.agentStatus].filter(
    (item): item is string => Boolean(item),
  );
}

function serviceTitle(service: DashboardService) {
  return service.stack
    ? `${service.stack}/${service.name || "-"}`
    : service.name || service.fullName || "-";
}

export function ObservabilityPanel({
  lang,
  token,
  nodes,
  services,
  mode = "metrics",
  investigation = false,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  services: DashboardService[];
  mode?: "metrics" | "logs";
  investigation?: boolean;
}) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const query = useSearchParams();
  const kind =
    query.get("kind") === "service" || !nodes.length ? "service" : "node";
  const available =
    kind === "node"
      ? nodes.flatMap((node) =>
          node.name ? [{ name: node.name, label: nodeTitle(node) }] : [],
        )
      : services.flatMap((service) =>
          service.fullName
            ? [{ name: service.fullName, label: serviceTitle(service) }]
            : [],
        );
  const requested = query.get("target") || (investigation ? query.get("service") : "") || "";
  const selected = requested || available[0]?.name || "";
  const valid = available.some((item) => item.name === selected);
  const shared = investigation || query.has("observeHours") || query.has("observe_until") || query.has("observe_since");
  const scope = observationQuery(query.toString());
  const rawWindow = shared ? Number(scope.until)-Number(scope.since) : Number(query.get("window") || 900);
  const historyWindow = shared ? rawWindow : HISTORY_WINDOWS.includes(rawWindow) ? rawWindow : 900;
  const targets = useMemo<HistoryTarget[]>(
    () =>
      mode === "metrics" && selected && valid ? [{ kind, name: selected }] : [],
    [mode, kind, selected, valid],
  );
  const histories = useMetricsHistories(token, targets, historyWindow, query.has("observe_until") ? Number(scope.until) : undefined);
  const history = histories[historyKey(kind, selected)];
  const refreshHost = useObserveRefresh();
  useEffect(() => {
    if (!history) return;
    if (history.error) refreshHost.setRefreshing(false);
    else refreshHost.markUpdated();
  }, [history, refreshHost]);
  const retention = history?.payload?.retentionSeconds;
  const series = history?.payload?.series || {};
  const node = nodes.find((item) => item.name === selected);
  const service = services.find((item) => item.fullName === selected);
  const update = (values: Record<string, string>) => {
    const next = new URLSearchParams(query);
    Object.entries(values).forEach(([key, value]) => {
      if (value) next.set(key, value);
      else next.delete(key);
    });
    navigate(`${path}?${next}`);
  };
  useEffect(() => {
    if (shared || mode !== "metrics" || !retention || historyWindow <= retention) return;
    const next = new URLSearchParams(query);
    next.set(
      "window",
      String(
        [...HISTORY_WINDOWS].reverse().find((value) => value <= retention) ||
          900,
      ),
    );
    if (next.get("window") !== String(historyWindow))
      navigate(`${path}?${next}`, { replace: true });
  }, [mode, retention, historyWindow, query, path, navigate]);
  const metricDefinitions =
    kind === "node"
      ? [
          { key: "cpuPercent", label: "CPU", format: formatPercent },
          {
            key: "memoryUsedPercent",
            label: zh ? "内存" : "Memory",
            format: formatPercent,
          },
          {
            key: "diskUsedPercent",
            label: zh ? "磁盘" : "Disk",
            format: formatPercent,
          },
          { key: "inodesUsedPercent", label: "Inodes", format: formatPercent },
        ]
      : [
          { key: "cpuPercent", label: "CPU", format: formatPercent },
          {
            key: "memoryUsageBytes",
            label: zh ? "内存" : "Memory",
            format: formatBytes,
          },
        ];
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label={zh ? "监测范围" : "Monitoring scope"}>
        <ToggleGroup
          variant="segmented"
          value={[kind]}
          onValueChange={(values) => {
            if (values[0]) update({ kind: values[0], target: "" });
          }}
          aria-label={zh ? "对象类型" : "Object type"}
        >
          {nodes.length > 0 && (
            <ToggleGroupItem value="node">
              {zh ? "节点" : "Node"}
            </ToggleGroupItem>
          )}
          <ToggleGroupItem value="service">
            {zh ? "服务" : "Service"}
          </ToggleGroupItem>
        </ToggleGroup>
        <div className="w-full min-w-0 sm:w-72">
          <SelectControl
            id="metrics-target"
            ariaLabel={zh ? "监测对象" : "Target"}
            className="min-w-0"
            disabled={!available.length}
            value={selected}
            onChange={(value) => update({ target: value, ...(investigation ? {service: value} : {}) })}
            options={[
              ...(!valid
                ? [
                    {
                      value: selected,
                      label: selected || (zh ? "暂无对象" : "No targets"),
                    },
                  ]
                : []),
              ...available.map((item) => ({
                value: item.name,
                label: item.label,
              })),
            ]}
          />
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {shared ? <span className="text-[0.8125rem] text-muted-foreground">{zh ? "时间范围跟随上方排查范围" : "Using the investigation range above"}</span> : <ToggleGroup
            variant="segmented"
            value={[String(historyWindow)]}
            onValueChange={(values) => {
              if (values[0]) update({ window: values[0] });
            }}
            aria-label={zh ? "时间范围" : "Time range"}
          >
            {HISTORY_WINDOWS.map((value) => (
              <ToggleGroupItem
                key={value}
                value={String(value)}
                disabled={
                  typeof retention === "number" && value > retention
                }
                title={typeof retention === "number" && value > retention ? (zh ? `超出历史保留（${Math.round(retention / 60)} 分钟）` : `Beyond retention (${Math.round(retention / 60)} min)`) : undefined}
              >
                {value < 3600 ? (zh ? `${value / 60} 分钟` : `${value / 60}m`) : (zh ? `${value / 3600} 小时` : `${value / 3600}h`)}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>}
        </div>
      </div>
      {investigation && (
        <p className="-mt-3 text-xs text-muted-foreground">
          {zh ? "资源数据按所选部署服务查看；环境与进程实例筛选用于业务遥测。" : "Resource data uses the selected deployed service; environment and process instance filters apply to application telemetry."}
        </p>
      )}
      {!valid ? (
        <Card>
          <CardHeader>
            <CardTitle>
              {zh ? "监测对象不可用" : "Target unavailable"}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Empty>
              <EmptyHeader>
                <EmptyTitle>
                  {zh ? "暂无可用数据" : "No data available"}
                </EmptyTitle>
                <EmptyDescription>
                  {zh
                    ? "该对象不存在或当前没有可用对象，请重新选择。"
                    : "This target is unavailable. Select another target."}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>
                {kind === "node" && node
                  ? nodeTitle(node)
                  : service
                    ? serviceTitle(service)
                    : selected}
              </CardTitle>
              <CardDescription>
                {kind === "node" && node
                  ? nodeMeta(node)
                      .map((value) => localizeState(lang, value))
                      .join(" · ")
                  : zh
                    ? "当前服务用量与任务状态"
                    : "Current service usage and task state"}
              </CardDescription>
              <CardAction>
                <StatePill
                  label={localizeState(
                    lang,
                    kind === "node" ? node?.state : service?.status,
                  )}
                  value={kind === "node" ? node?.state : service?.status}
                />
              </CardAction>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {kind === "node" && node ? (
                <dl
                  className="grid grid-cols-1 gap-3 sm:grid-cols-3"
                  aria-label={zh ? "当前快照" : "Current snapshot"}
                >
                  {[
                    { key: "cpu", label: "CPU", value: node.metrics?.cpuPercent ?? node.metrics?.loadPercent, detail: zh ? "当前使用率" : "Current utilization" },
                    { key: "memory", label: zh ? "内存" : "Memory", value: node.metrics?.memoryUsedPercent, detail: `${zh ? "总量" : "Total"} ${formatBytes(node.metrics?.memoryTotalBytes ?? node.capacity?.memoryBytes)}` },
                    { key: "disk", label: zh ? "磁盘" : "Disk", value: node.metrics?.diskUsedPercent, detail: `${zh ? "可用" : "Free"} ${formatBytes(node.metrics?.diskAvailableBytes)}${node.metrics?.diskTotalBytes ? ` / ${formatBytes(node.metrics.diskTotalBytes)}` : ""}` },
                  ].map((item) => (
                    <div key={item.key} className="flex min-w-0 flex-col gap-2 rounded-lg bg-muted/50 px-4 py-3 ring-1 ring-hairline">
                      <dt className="text-xs font-medium text-muted-foreground">{item.label}</dt>
                      <dd className="flex min-w-0 flex-col gap-2">
                        <span className={cn("text-xl/none font-semibold tracking-[-0.01em] tabular-nums", usageTone(item.value) === "critical" && "text-destructive-foreground", usageTone(item.value) === "warning" && "text-warning-foreground")}>{formatPercent(item.value)}</span>
                        <UsageMeter value={item.value} label={item.label} className="w-full [&>[role=meter]]:max-w-none [&>span:last-child]:hidden" />
                        <span className="truncate text-xs text-muted-foreground">{item.detail}</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                service && (
                  <>
                    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label={zh ? "当前快照" : "Current snapshot"}>
                      {[
                        { key: "actual", label: zh ? "实际用量" : "Actual usage", value: actualText(service.resources?.actual) },
                        { key: "limit", label: zh ? "限制" : "Limit", value: resourceText(service.resources?.limits) },
                        { key: "reservation", label: zh ? "预留" : "Reservation", value: resourceText(service.resources?.reservations) },
                      ].map((item) => (
                        <div key={item.key} className="flex min-w-0 flex-col gap-2 rounded-lg bg-muted/50 px-4 py-3 ring-1 ring-hairline">
                          <dt className="text-xs font-medium text-muted-foreground">{item.label}</dt>
                          <dd className="text-sm font-medium tabular-nums">{item.value}</dd>
                        </div>
                      ))}
                    </dl>
                    <div className="flex flex-wrap gap-2">
                      {(service.tasks || []).map((task, index) => (
                        <StatePill
                          key={task.id || index}
                          label={`${task.node || "—"} ${localizeState(lang, task.state)} ${formatPercent(task.cpuPercent)}`}
                          value={task.state}
                        />
                      ))}
                    </div>
                  </>
                )
              )}
              <HistoryStatus lang={lang} state={history} until={query.has("observe_until") ? Number(scope.until) : undefined} />
            </CardContent>
            {kind === "service" && (
              <CardFooter>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    navigate(
                      `/observe/logs?app=${encodeURIComponent(service?.stack || selected)}`,
                    )
                  }
                >
                  {zh ? "查看此服务日志" : "View service logs"}
                </Button>
              </CardFooter>
            )}
          </Card>
          <div className="grid min-w-0 gap-6 xl:grid-cols-2">
            {metricDefinitions.map(({ key, label, format }) => {
              const samples = series[key] || [];
              return (
                <Card className="min-w-0" key={key}>
                  <CardHeader>
                    <CardTitle>{label}</CardTitle>
                    <CardDescription>
                      {samples.length
                        ? zh ? "最后采样" : "Latest sample"
                        : zh ? "暂无采样" : "No samples yet"}
                    </CardDescription>
                    {samples.length ? (
                      <CardAction className="text-lg font-semibold tabular-nums">
                        {format(samples[samples.length - 1][1])}
                      </CardAction>
                    ) : null}
                  </CardHeader>
                  <CardContent>
                    {!history ? (
                      <Skeleton className="h-48 w-full" />
                    ) : (
                      <TrendChart
                        maxGapSeconds={90}
                        points={samples}
                        format={format}
                        label={label}
                        locale={zh ? "zh-CN" : "en-US"}
                        height={192}
                        emptyLabel={
                          zh ? "等待至少两个采样点" : "Waiting for two samples"
                        }
                      />
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
          <p className="text-xs text-muted-foreground">
            {zh
              ? "每 30 秒刷新。180 秒未更新的节点不计入服务汇总；超过 90 秒的采样间隔显示为断点，不做插值。"
              : "Refreshes every 30 seconds. Service totals exclude nodes stale for 180s; gaps over 90s break the line and are not interpolated."}
          </p>
        </>
      )}
    </div>
  );
}
