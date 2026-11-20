import { useState, type FormEvent } from "react";
import { useParams, useSearchParams } from "react-router";
import { AlertCircle, Plus } from "@/components/icons";
import { api } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import { SelectControl } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";

const WINDOW_OPTIONS = [
  { value: "900", label: "15 分钟内完成" },
  { value: "1800", label: "30 分钟内完成" },
  { value: "3600", label: "1 小时内完成" },
  { value: "86400", label: "24 小时内完成" },
  { value: "604800", label: "7 天内完成" },
];

export function FunnelRetentionView() {
  const { projectSlug = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick } = useWorkspace();

  const urlSteps = searchParams.get("steps") || "";
  const urlWindow = Number(searchParams.get("window") || "3600") || 3600;
  const retentionDays = Number(searchParams.get("days") || "7") === 14 ? 14 : 7;

  const [stepsInput, setStepsInput] = useState(urlSteps || "/, /pricing");
  const [windowSec, setWindowSec] = useState(String(urlWindow));
  const [formErr, setFormErr] = useState("");

  const baseKey = projectSlug
    ? `funnel-base:${projectSlug}:${since}:${retentionDays}:${refreshTick}`
    : null;

  const { data: baseData, loading: baseLoading, error: baseError } = useQuery(
    baseKey,
    async (signal) => {
      const [ov, ret] = await Promise.all([
        api.overview(projectSlug, since, signal),
        api.retention(projectSlug, retentionDays, signal),
      ]);
      return {
        topPages: ov.top_pages || [],
        topEvents: ov.top_events || [],
        retention: ret,
      };
    },
  );

  const parsedUrlSteps = urlSteps
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const funnelKey =
    projectSlug && parsedUrlSteps.length >= 2
      ? `funnel-run:${projectSlug}:${since}:${urlWindow}:${parsedUrlSteps.join(",")}:${refreshTick}`
      : null;

  const { data: funnel, loading: runningFunnel, error: funnelError } = useQuery(
    funnelKey,
    (signal) => api.funnel(projectSlug, parsedUrlSteps, urlWindow, since, signal),
  );

  const setRetentionDays = (days: number) => {
    const sp = new URLSearchParams(searchParams);
    if (days === 7) sp.delete("days");
    else sp.set("days", String(days));
    setSearchParams(sp);
  };

  const appendStep = (stepName: string) => {
    const parts = stepsInput
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    parts.push(stepName);
    setStepsInput(parts.join(", "));
  };

  const handleSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    setFormErr("");
    const steps = stepsInput
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (steps.length < 2) {
      setFormErr("请至少输入 2 个转化步骤（支持混合填写页面路径如 /pricing 或自定义事件名）。");
      return;
    }
    const sp = new URLSearchParams(searchParams);
    sp.set("steps", steps.join(","));
    sp.set("window", windowSec);
    setSearchParams(sp);
  };

  const quickCandidates = [
    ...(baseData?.topPages || []).slice(0, 6).map((p) => ({ kind: "页面", val: p.key })),
    ...(baseData?.topEvents || []).slice(0, 6).map((e) => ({ kind: "事件", val: e.key })),
  ];

  const retention = baseData?.retention;

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="转化漏斗与留存"
        description="使用 ClickHouse windowFunnel 计算有序路径转化率，并追踪每日新增访客群组 (Cohort) 的回访留存矩阵。"
      />

      {baseError || funnelError || formErr ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>查询异常</AlertTitle>
          <AlertDescription>{formErr || funnelError || baseError}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>有序转化漏斗 (windowFunnel)</CardTitle>
          <CardDescription>
            按独立用户 <code>actor_id</code> 统计在指定时间窗口内严格按顺序完成步骤 1 → N 的转化率；配置自动同步到 URL 参数。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {quickCandidates.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs text-muted-foreground">点击候选热门页面或事件快速追加步骤：</span>
              <div className="flex flex-wrap gap-1.5">
                {quickCandidates.map((c) => (
                  <Button
                    key={`${c.kind}-${c.val}`}
                    type="button"
                    size="xs"
                    variant="outline"
                    onClick={() => appendStep(c.val)}
                  >
                    <Plus data-icon="inline-start" />
                    <span className="text-muted-foreground">{c.kind}</span>
                    <code className="font-mono">{c.val}</code>
                  </Button>
                ))}
                <Button type="button" size="xs" variant="ghost" onClick={() => setStepsInput("")}>
                  清空
                </Button>
              </div>
            </div>
          ) : null}

          <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
            <Input
              type="text"
              className="min-w-60 flex-1 font-mono"
              value={stepsInput}
              onChange={(ev) => setStepsInput(ev.target.value)}
              placeholder="用逗号分隔有序步骤，例如：/, share_started, share_created"
            />
            <SelectControl
              ariaLabel="转化时间窗口"
              value={windowSec}
              options={WINDOW_OPTIONS}
              onChange={setWindowSec}
              className="w-40"
            />
            <Button type="submit" disabled={runningFunnel}>
              {runningFunnel ? "计算中…" : "计算转化漏斗"}
            </Button>
          </form>

          {funnel ? (
            <div className="mt-2 flex flex-col gap-3 rounded-lg border border-border bg-muted/20 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span>
                  总转化率：
                  <strong className="ml-1 font-mono text-sm font-semibold text-foreground tabular-nums">
                    {funnel.overall_conversion.toFixed(2)}%
                  </strong>
                </span>
                <span className="text-muted-foreground tabular-nums">
                  时间窗口上限 {funnel.window_seconds}s · 统计范围 {funnel.since}
                </span>
              </div>

              <div className="flex flex-col gap-2.5">
                {funnel.steps.map((st, idx) => {
                  const prevUsers = idx > 0 ? funnel.steps[idx - 1].users : st.users;
                  const dropOff = Math.max(0, prevUsers - st.users);
                  return (
                    <div key={`${st.step_index}-${st.step}`} className="flex flex-col gap-1.5">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                        <span className="flex items-center gap-2">
                          <Badge variant="outline">Step {st.step_index}</Badge>
                          <code className="font-mono font-medium">{st.step}</code>
                        </span>
                        <span className="font-mono text-xs tabular-nums">
                          <strong>{st.users.toLocaleString()}</strong> 人 · 总转化{" "}
                          <strong>{st.conversion_rate.toFixed(1)}%</strong>
                          {idx > 0 ? (
                            <span className="text-muted-foreground">
                              {" "}
                              (上步留存 {st.step_rate.toFixed(1)}% · 流失 {dropOff.toLocaleString()} 人)
                            </span>
                          ) : null}
                        </span>
                      </div>
                      <div className="relative h-2 w-full overflow-hidden rounded-full bg-secondary">
                        <div
                          className="h-full rounded-full bg-primary/85 transition-[width] duration-300"
                          style={{ width: `${Math.max(st.users > 0 ? 2 : 0, st.conversion_rate)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>新增访客群组日留存矩阵 (Cohort Retention)</CardTitle>
          <CardDescription>按自然日首次访问的用户群组，追踪后续第 1 ~ N 天的回访比例。</CardDescription>
          <CardAction>
            <div className="inline-flex rounded-md ring-1 ring-border" role="group" aria-label="留存周期切换">
              {[7, 14].map((d) => (
                <Button
                  key={d}
                  type="button"
                  size="xs"
                  variant={retentionDays === d ? "secondary" : "ghost"}
                  className="rounded-none first:rounded-l-md last:rounded-r-md"
                  onClick={() => setRetentionDays(d)}
                >
                  {d} 天矩阵
                </Button>
              ))}
            </div>
          </CardAction>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          {baseLoading && !retention ? (
            <div className="p-4">
              <Skeleton className="h-32 w-full" />
            </div>
          ) : !retention || retention.rows.length === 0 ? (
            <Empty className="py-10">
              <EmptyHeader>
                <EmptyTitle>暂无跨日留存样本</EmptyTitle>
                <EmptyDescription>项目产生连续多天访问记录后将自动生成群组留存热力表。</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Table
              aria-label="群组日留存热力表"
              containerProps={{
                tabIndex: 0,
                role: "region",
                "aria-label": "留存热力表（可横向滚动）",
              }}
            >
              <TableHeader>
                <TableRow>
                  <TableHead>首次访问日期</TableHead>
                  <TableHead className="text-right">新增 UV</TableHead>
                  {Array.from({ length: retention.days + 1 }, (_, d) => (
                    <TableHead key={d} className="text-right">
                      {d === 0 ? "当日" : `+${d}d`}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {retention.rows.map((row) => (
                  <TableRow key={row.cohort_date}>
                    <TableCell className="font-mono text-xs font-medium">{row.cohort_date}</TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {row.cohort_users.toLocaleString()}
                    </TableCell>
                    {row.day_rates.map((rate, i) => (
                      <TableCell
                        key={i}
                        className="text-right font-mono text-xs tabular-nums"
                        style={
                          rate > 0
                            ? {
                                backgroundColor: `color-mix(in oklch, var(--primary) ${Math.round(
                                  Math.min(36, 6 + (rate / 100) * 30),
                                )}%, transparent)`,
                              }
                            : undefined
                        }
                      >
                        {rate > 0 ? `${rate.toFixed(0)}%` : "—"}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
