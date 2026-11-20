import { Link, useParams } from "react-router";
import { Activity, AlertCircle, ArrowRight, TriangleAlert, Zap } from "@/components/icons";
import { Users } from "lucide-react";
import { api } from "@/api/client";
import { TrafficTrendChart } from "@/components/AnalyticsCharts";
import { DimensionCard } from "@/components/DimensionCard";
import { PageHeader } from "@/components/PageHeader";
import { StatTile } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatCompactNumber } from "@/utils/format";
import { evaluateVital, getCountryMeta } from "@/utils/geo";

export function OverviewView() {
  const { projectSlug = "" } = useParams();
  const { projects, since, refreshTick } = useWorkspace();

  const proj = projects.find((p) => p.slug === projectSlug);
  const { data: overview, loading, error } = useQuery(
    projectSlug ? `overview:${projectSlug}:${since}:${refreshTick}` : null,
    (signal) => api.overview(projectSlug, since, signal),
  );

  if (loading && !overview) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title={proj?.name || projectSlug} description="正在加载流量与地域分析数据…" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  return (
    <div className="@container flex min-w-0 flex-col gap-6">
      <PageHeader
        title={proj?.name || projectSlug}
        description="实时访问流量、国家地域分布、来源渠道与客户端环境画像。"
        metrics={[
          { label: "项目标识", value: <code>{projectSlug}</code> },
          { label: "5m 在线", value: (overview?.active_users_5m ?? 0).toLocaleString() },
          { label: "无崩溃率", value: `${(overview?.crash_free_rate ?? 100).toFixed(2)}%` },
        ]}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<Link to={paths.projectSection(projectSlug, "sessions", since)} />}
            >
              用户轨迹
            </Button>
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<Link to={paths.projectSection(projectSlug, "settings", since)} />}
            >
              接入配置
            </Button>
          </div>
        }
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>概览数据加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {overview ? (
        <>
          <section className="grid grid-cols-2 gap-3 @3xl:grid-cols-4" aria-label="核心流量指标">
            <StatTile
              label="独立访客 (UV)"
              icon={Users}
              value={formatCompactNumber(overview.unique_visitors)}
              detail={`· ${formatCompactNumber(overview.sessions)} 会话`}
              footer={`5m 实时在线 ${overview.active_users_5m.toLocaleString()} 人`}
              to={paths.projectSection(projectSlug, "sessions", since)}
            />
            <StatTile
              label="页面浏览量 (PV)"
              icon={Activity}
              value={formatCompactNumber(overview.pageviews)}
              footer="首屏加载与 SPA 路由切换自动采集"
              to={paths.projectSection(projectSlug, "events", since, { type: "pageview" })}
            />
            <StatTile
              label="自定义业务事件"
              icon={Zap}
              value={formatCompactNumber(overview.custom_events)}
              footer="点击查看事件明细与参数属性"
              to={paths.projectSection(projectSlug, "events", since, { type: "custom" })}
            />
            <StatTile
              label="异常报错次数"
              icon={TriangleAlert}
              value={overview.error_count.toLocaleString()}
              detail={`· ${overview.crash_free_rate.toFixed(1)}% 健康`}
              footer="点击查看崩溃前 50 步操作黑匣子"
              tone={overview.error_count > 0 ? "critical" : "normal"}
              to={paths.projectSection(projectSlug, "issues", since)}
            />
          </section>

          <Card>
            <CardHeader>
              <CardTitle>流量与异常时序趋势</CardTitle>
              <CardDescription>点击上方序列标签可单独过滤 PV、UV、事件或异常曲线。</CardDescription>
              <CardAction>
                <Button
                  variant="ghost"
                  size="xs"
                  nativeButton={false}
                  render={<Link to={paths.projectSection(projectSlug, "events", since)} />}
                >
                  实时事件流
                  <ArrowRight data-icon="inline-end" />
                </Button>
              </CardAction>
            </CardHeader>
            <CardContent>
              <TrafficTrendChart points={overview.timeseries || []} />
            </CardContent>
          </Card>

          {(overview.vitals_p75 || []).length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Web Vitals 性能分位 (P75)</CardTitle>
                <CardDescription>真实用户浏览器自动上报的首屏渲染与交互延迟 75 分位值。</CardDescription>
                <CardAction>
                  <Button
                    variant="ghost"
                    size="xs"
                    nativeButton={false}
                    render={<Link to={paths.projectSection(projectSlug, "performance", since)} />}
                  >
                    分页面详情
                    <ArrowRight data-icon="inline-end" />
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                  {overview.vitals_p75.map((v) => {
                    const ev = evaluateVital(v.metric_name, v.p75);
                    return (
                      <div
                        key={v.metric_name}
                        className="flex flex-col gap-1.5 rounded-lg border border-border bg-muted/30 px-3.5 py-3"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold text-foreground">{ev.metric}</span>
                          <Badge
                            variant={ev.badgeVariant}
                            appearance={ev.badgeVariant === "success" ? "dot" : "filled"}
                          >
                            {ev.badgeLabel}
                          </Badge>
                        </div>
                        <strong className="font-mono text-lg font-semibold tabular-nums">
                          {ev.formattedValue}
                        </strong>
                        <span className="text-[0.6875rem] text-muted-foreground tabular-nums">
                          {v.samples.toLocaleString()} 样本 · {ev.thresholdHint}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          ) : null}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <DimensionCard
              title="国家 / 地区分布"
              description="服务端离线 IP 库解析（原始 IP 仅内存加盐哈希，不落盘）"
              rows={overview.top_countries || []}
              formatKey={(k) => getCountryMeta(k).label}
              getItemHref={(k) =>
                k && k !== "(unknown)" && k !== "UNKNOWN"
                  ? paths.projectSection(projectSlug, "sessions", since, { country: k })
                  : paths.projectSection(projectSlug, "sessions", since)
              }
              footerNote={
                <span>
                  离线地理归属数据由{" "}
                  <a
                    href="https://db-ip.com"
                    target="_blank"
                    rel="noreferrer"
                    className="text-link hover:underline"
                  >
                    DB-IP
                  </a>{" "}
                  提供
                </span>
              }
            />

            <DimensionCard
              title="热门访问页面 (Top Pages)"
              description="点击页面路径可跳转检索该页面事件明细"
              rows={overview.top_pages || []}
              formatKey={(k) => <code className="font-mono">{k}</code>}
              getItemHref={(k) =>
                paths.projectSection(projectSlug, "events", since, { type: "pageview", q: k })
              }
            />

            <DimensionCard
              title="热门自定义事件"
              description="通过 track(name, props) 主动上报的交互动作"
              rows={overview.top_events || []}
              formatKey={(k) => <code className="font-mono">{k}</code>}
              getItemHref={(k) =>
                paths.projectSection(projectSlug, "events", since, { type: "custom", name: k })
              }
              emptyText="暂无自定义埋点事件"
            />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <DimensionCard
              title="访问来源与引荐域名"
              description="外部 Referrer 域名与流量入口分类"
              rows={
                (overview.top_referrers || []).length > 0
                  ? overview.top_referrers
                  : overview.top_channels || []
              }
            />

            <DimensionCard
              title="渠道与 UTM 归因"
              description="自动解析 URL 中 utm_source / medium / campaign"
              rows={[
                ...(overview.top_channels || [])
                  .filter((r) => r.key !== "(unknown)")
                  .map((r) => ({ ...r, key: `channel: ${r.key}` })),
                ...(overview.top_sources || [])
                  .filter((r) => r.key !== "(unknown)")
                  .map((r) => ({ ...r, key: `source: ${r.key}` })),
                ...(overview.top_campaigns || [])
                  .filter((r) => r.key !== "(unknown)")
                  .map((r) => ({ ...r, key: `campaign: ${r.key}` })),
              ]}
              emptyText="暂无带 UTM 参数的访问来源"
            />

            <DimensionCard
              title="客户端系统与浏览器"
              description="User-Agent 与移动端容器环境分布"
              rows={[
                ...(overview.top_browsers || []).slice(0, 4).map((r) => ({ ...r, key: `浏览器 · ${r.key}` })),
                ...(overview.top_os || []).slice(0, 4).map((r) => ({ ...r, key: `系统 · ${r.key}` })),
              ]}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
