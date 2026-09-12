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
import { SelectControl, StatePill } from "./primitives";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
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
import { AlertCircle } from "lucide-react";
import { TrendChart } from "./charts";
import { useRouter, useSearchParams } from "../router";

const HISTORY_WINDOWS = [900, 3600, 21600];

function HistoryStatus({ lang, state }: { lang: Lang; state?: HistoryState }) {
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
  const stale = latest !== null && Date.now() / 1000 - latest > 180;
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

function formatBytes(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
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
  return typeof value === "number"
    ? `${value.toFixed(value >= 10 ? 0 : 1)}%`
    : "-";
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
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  services: DashboardService[];
  mode?: "metrics" | "logs";
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
  const requested = query.get("target") || "";
  const selected = requested || available[0]?.name || "";
  const valid = available.some((item) => item.name === selected);
  const rawWindow = Number(query.get("window") || 900);
  const historyWindow = HISTORY_WINDOWS.includes(rawWindow) ? rawWindow : 900;
  const targets = useMemo<HistoryTarget[]>(
    () =>
      mode === "metrics" && selected && valid ? [{ kind, name: selected }] : [],
    [mode, kind, selected, valid],
  );
  const histories = useMetricsHistories(token, targets, historyWindow);
  const history = histories[historyKey(kind, selected)];
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
    if (mode !== "metrics" || !retention || historyWindow <= retention) return;
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
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "监测范围" : "Monitoring scope"}</CardTitle>
          <CardDescription>
            {zh
              ? "每 30 秒刷新，采样断点不会插值。"
              : "Refreshes every 30 seconds. Gaps in samples remain visible."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FieldGroup className="grid md:grid-cols-[auto_minmax(0,1fr)_auto]">
            <Field>
              <FieldLabel id="metrics-kind-label">
                {zh ? "对象类型" : "Object type"}
              </FieldLabel>
              <ToggleGroup
                variant="outline"
                value={[kind]}
                onValueChange={(values) => {
                  if (values[0]) update({ kind: values[0], target: "" });
                }}
                aria-labelledby="metrics-kind-label"
                className="flex-wrap"
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
            </Field>
            <Field data-disabled={!available.length}>
              <FieldLabel htmlFor="metrics-target">
                {zh ? "监测对象" : "Target"}
              </FieldLabel>
              <SelectControl
                id="metrics-target"
                className="min-w-0"
                disabled={!available.length}
                value={selected}
                onChange={(value) => update({ target: value })}
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
            </Field>
            <Field>
              <FieldLabel id="metrics-window-label">
                {zh ? "时间范围" : "Time range"}
              </FieldLabel>
              <ToggleGroup
                variant="outline"
                value={[String(historyWindow)]}
                onValueChange={(values) => {
                  if (values[0]) update({ window: values[0] });
                }}
                aria-labelledby="metrics-window-label"
                className="flex-wrap"
              >
                {HISTORY_WINDOWS.map((value) => (
                  <ToggleGroupItem
                    key={value}
                    value={String(value)}
                    disabled={
                      typeof retention === "number" && value > retention
                    }
                  >
                    {value < 3600 ? `${value / 60} min` : `${value / 3600} h`}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              {retention && (
                <FieldDescription>
                  {zh ? "历史保留" : "History retention"} ·{" "}
                  {Math.round(retention / 60)} min
                </FieldDescription>
              )}
            </Field>
          </FieldGroup>
        </CardContent>
      </Card>
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
                  className="grid gap-6 sm:grid-cols-3"
                  aria-label={zh ? "当前快照" : "Current snapshot"}
                >
                  <div className="flex min-w-0 flex-col gap-1">
                    <dt className="text-sm text-muted-foreground">CPU</dt>
                    <dd className="text-2xl font-semibold tabular-nums">
                      {formatPercent(
                        node.metrics?.cpuPercent ?? node.metrics?.loadPercent,
                      )}
                    </dd>
                    <span className="text-sm text-muted-foreground">
                      {zh ? "当前使用率" : "Current utilization"}
                    </span>
                  </div>
                  <div className="flex min-w-0 flex-col gap-1">
                    <dt className="text-sm text-muted-foreground">
                      {zh ? "内存" : "Memory"}
                    </dt>
                    <dd className="text-2xl font-semibold tabular-nums">
                      {formatPercent(node.metrics?.memoryUsedPercent)}
                    </dd>
                    <span className="text-sm text-muted-foreground">
                      {zh ? "总量 " : "Total "}
                      {formatBytes(
                        node.metrics?.memoryTotalBytes ??
                          node.capacity?.memoryBytes,
                      )}
                    </span>
                  </div>
                  <div className="flex min-w-0 flex-col gap-1">
                    <dt className="text-sm text-muted-foreground">
                      {zh ? "磁盘可用" : "Disk available"}
                    </dt>
                    <dd className="text-2xl font-semibold tabular-nums">
                      {formatBytes(node.metrics?.diskAvailableBytes)}
                    </dd>
                    <span className="text-sm text-muted-foreground wrap-anywhere">
                      {node.metrics?.metricsPath || "—"}
                    </span>
                  </div>
                </dl>
              ) : (
                service && (
                  <>
                    <dl className="grid gap-4 sm:grid-cols-3">
                      <div className="flex flex-col gap-1">
                        <dt className="text-muted-foreground">
                          {zh ? "实际用量" : "Actual usage"}
                        </dt>
                        <dd>{actualText(service.resources?.actual)}</dd>
                      </div>
                      <div className="flex flex-col gap-1">
                        <dt className="text-muted-foreground">
                          {zh ? "限制" : "Limit"}
                        </dt>
                        <dd>{resourceText(service.resources?.limits)}</dd>
                      </div>
                      <div className="flex flex-col gap-1">
                        <dt className="text-muted-foreground">
                          {zh ? "预留" : "Reservation"}
                        </dt>
                        <dd>{resourceText(service.resources?.reservations)}</dd>
                      </div>
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
              <HistoryStatus lang={lang} state={history} />
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
                      {zh ? "最后采样" : "Latest sample"}
                    </CardDescription>
                    <CardAction>
                      <Badge variant="outline">
                        {samples.length
                          ? format(samples[samples.length - 1][1])
                          : "—"}
                      </Badge>
                    </CardAction>
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
          <p className="text-sm text-muted-foreground">
            {zh
              ? "180 秒未更新的节点不计入服务汇总；超过 90 秒的采样间隔显示为断点。"
              : "Service totals exclude nodes stale for 180s. Gaps over 90s break the line."}
          </p>
        </>
      )}
    </div>
  );
}
