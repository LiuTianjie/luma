import { Link, useNavigate, useParams } from "react-router";
import { AlertCircle } from "@/components/icons";
import { api } from "@/api/client";
import type { PageMetricRow } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, StatePill } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatDurationMs } from "@/utils/format";
import { evaluateVital } from "@/utils/geo";

const CORE_METRICS = ["LCP", "INP", "CLS", "FCP", "TTFB"];

export function PerformanceView() {
  const { projectSlug = "" } = useParams();
  const navigate = useNavigate();
  const { since, refreshTick } = useWorkspace();

  const queryKey = projectSlug ? `perf:${projectSlug}:${since}:${refreshTick}` : null;

  const { data, loading, error } = useQuery(queryKey, async (signal) => {
    const [ov, pg] = await Promise.all([
      api.overview(projectSlug, since, signal),
      api.pages(projectSlug, since, 60, signal),
    ]);
    return {
      vitals: ov.vitals_p75 || [],
      pages: pg.pages || [],
    };
  });

  const vitals = data?.vitals || [];
  const pages = data?.pages || [];
  const vitalMap = new Map(vitals.map((v) => [v.metric_name.toUpperCase(), v]));

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="Web Vitals 性能"
        description="基于真实用户浏览器 PerformanceObserver 自动采集的 P75 核心体验指标与分页面停留分析。"
        metrics={[{ label: "采样页面数", value: pages.length.toLocaleString() }]}
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>性能数据加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5" aria-label="Core Web Vitals P75">
        {CORE_METRICS.map((name) => {
          const sample = vitalMap.get(name);
          const placeholder = evaluateVital(name, 0);
          if (!sample) {
            return (
              <Card key={name} className="bg-card">
                <CardContent className="flex flex-col gap-2 pt-4">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-foreground">{placeholder.metric}</span>
                    <Badge variant="outline">待采集</Badge>
                  </div>
                  <strong className="font-mono text-2xl font-semibold text-muted-foreground">—</strong>
                  <p className="text-xs text-muted-foreground">{placeholder.description}</p>
                  <span className="text-[0.6875rem] text-subtle-foreground">{placeholder.thresholdHint}</span>
                </CardContent>
              </Card>
            );
          }

          const ev = evaluateVital(name, sample.p75);
          return (
            <Card key={name}>
              <CardContent className="flex flex-col gap-2 pt-4">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-foreground">{ev.metric}</span>
                  <Badge
                    variant={ev.badgeVariant}
                    appearance={ev.badgeVariant === "success" ? "dot" : "filled"}
                  >
                    {ev.badgeLabel}
                  </Badge>
                </div>
                <strong className="font-mono text-2xl font-semibold tabular-nums">
                  {ev.formattedValue}
                </strong>
                <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-secondary">
                  <div
                    className={
                      ev.rating === "good"
                        ? "h-full rounded-full bg-success"
                        : ev.rating === "needs-improvement"
                          ? "h-full rounded-full bg-warning"
                          : "h-full rounded-full bg-destructive"
                    }
                    style={{ width: `${ev.fillPercent}%` }}
                  />
                </div>
                <p className="text-xs text-muted-foreground">{ev.description}</p>
                <span className="text-[0.6875rem] text-muted-foreground tabular-nums">
                  {sample.samples.toLocaleString()} 样本 · {ev.thresholdHint}
                </span>
              </CardContent>
            </Card>
          );
        })}
      </section>

      <Card>
        <CardHeader>
          <CardTitle>分页面性能与前台可见停留时长</CardTitle>
          <CardDescription>
            停留时长自动通过 <code>visibilitychange</code> 剔除切后台挂机耗时；点击任意页面可跳转查看该路径事件流。
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <DataTable<PageMetricRow>
            ariaLabel="分页面性能统计表"
            rows={pages}
            rowKey={(r) => r.page_path}
            defaultSortKey="pv"
            loading={loading}
            onRowClick={(r) =>
              navigate(paths.projectSection(projectSlug, "events", since, { type: "pageview", q: r.page_path }))
            }
            emptyTitle="当前时间窗口内暂无页面指标"
            emptyDescription="页面产生浏览访问后将自动统计 PV、UV、停留时长与 Web Vitals。"
            columns={[
              {
                key: "path",
                header: "页面路径 (Path)",
                sortable: true,
                sortValue: (r) => r.page_path,
                render: (r) => (
                  <Link
                    to={paths.projectSection(projectSlug, "events", since, { type: "pageview", q: r.page_path })}
                    className="font-mono text-xs text-link hover:underline"
                  >
                    {r.page_path}
                  </Link>
                ),
              },
              {
                key: "pv",
                header: "浏览量 PV",
                align: "right",
                sortable: true,
                sortValue: (r) => r.pageviews,
                render: (r) => <span className="font-mono tabular-nums">{r.pageviews.toLocaleString()}</span>,
              },
              {
                key: "uv",
                header: "访客 UV",
                align: "right",
                sortable: true,
                sortValue: (r) => r.users,
                render: (r) => <span className="font-mono tabular-nums">{r.users.toLocaleString()}</span>,
              },
              {
                key: "avg_dur",
                header: "平均停留",
                align: "right",
                sortable: true,
                sortValue: (r) => r.avg_duration_ms,
                render: (r) => (
                  <span className="font-mono text-xs tabular-nums">{formatDurationMs(r.avg_duration_ms)}</span>
                ),
              },
              {
                key: "p75_dur",
                header: "P75 停留",
                align: "right",
                sortable: true,
                sortValue: (r) => r.p75_duration_ms,
                render: (r) => (
                  <span className="font-mono text-xs tabular-nums">{formatDurationMs(r.p75_duration_ms)}</span>
                ),
              },
              {
                key: "lcp",
                header: "LCP P75",
                align: "right",
                sortable: true,
                sortValue: (r) => r.lcp_p75 || 0,
                render: (r) => {
                  if (!r.lcp_p75) return <span className="text-subtle-foreground">—</span>;
                  const ev = evaluateVital("LCP", r.lcp_p75);
                  return (
                    <Badge variant={ev.badgeVariant} appearance={ev.badgeVariant === "success" ? "dot" : "filled"}>
                      {ev.formattedValue}
                    </Badge>
                  );
                },
              },
              {
                key: "inp",
                header: "INP P75",
                align: "right",
                sortable: true,
                sortValue: (r) => r.inp_p75 || 0,
                render: (r) => {
                  if (!r.inp_p75) return <span className="text-subtle-foreground">—</span>;
                  const ev = evaluateVital("INP", r.inp_p75);
                  return (
                    <Badge variant={ev.badgeVariant} appearance={ev.badgeVariant === "success" ? "dot" : "filled"}>
                      {ev.formattedValue}
                    </Badge>
                  );
                },
              },
              {
                key: "cls",
                header: "CLS P75",
                align: "right",
                sortable: true,
                sortValue: (r) => r.cls_p75 || 0,
                render: (r) => {
                  if (!r.cls_p75) return <span className="text-subtle-foreground">—</span>;
                  const ev = evaluateVital("CLS", r.cls_p75);
                  return (
                    <Badge variant={ev.badgeVariant} appearance={ev.badgeVariant === "success" ? "dot" : "filled"}>
                      {ev.formattedValue}
                    </Badge>
                  );
                },
              },
              {
                key: "errors",
                header: "报错数",
                align: "right",
                sortable: true,
                sortValue: (r) => r.error_count,
                render: (r) =>
                  r.error_count > 0 ? (
                    <StatePill label={r.error_count.toLocaleString()} tone="destructive" />
                  ) : (
                    <span className="font-mono text-xs text-subtle-foreground">0</span>
                  ),
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
