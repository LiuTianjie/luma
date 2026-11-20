import { Link, useNavigate } from "react-router";
import {
  Activity,
  Boxes,
  FingerprintPattern,
  HardDrive,
  Plus,
  ShieldCheck,
  TriangleAlert,
} from "@/components/icons";
import { Users } from "lucide-react";
import type { PortfolioProjectStats } from "@/api/types";
import { Sparkline } from "@/components/AnalyticsCharts";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, PrimaryCell, StatePill, StatTile, UsageMeter } from "@/components/primitives";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useWorkspace } from "@/context/WorkspaceContext";
import { paths } from "@/router/paths";
import { formatBytes, formatCompactNumber, formatRelativeTime } from "@/utils/format";
import { evaluateVital } from "@/utils/geo";

export function PortfolioView() {
  const { authStatus, portfolio, retention, since, openNewProjectModal } = useWorkspace();
  const navigate = useNavigate();

  const needsSecuritySetup =
    authStatus && !authStatus.has_password && (authStatus.passkey_count || 0) === 0;

  if (!portfolio) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title="多项目总览" description="跨应用流量、用户体验与线上异常健康状态。" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const projects = portfolio.projects || [];
  const usedBytes = retention?.total_used_bytes || 0;
  const maxBytes = retention?.max_storage_bytes || 15 * 1024 * 1024 * 1024;
  const storagePct = maxBytes > 0 ? Math.min(100, (usedBytes * 100) / maxBytes) : 0;

  return (
    <div className="@container flex min-w-0 flex-col gap-6">
      <PageHeader
        title="多项目总览"
        description="全局流量、会话健康度与节点存储水位。"
        metrics={[
          { label: "时间窗口", value: portfolio.window || since },
          { label: "活跃项目", value: `${portfolio.active_projects} / ${portfolio.total_projects}` },
          { label: "5m 在线", value: portfolio.active_users_5m.toLocaleString() },
        ]}
        action={
          <Button onClick={openNewProjectModal}>
            <Plus data-icon="inline-start" />
            新建项目
          </Button>
        }
      />

      {needsSecuritySetup ? (
        <Alert>
          <FingerprintPattern />
          <AlertTitle>建议配置 Passkey 或专属登录密码</AlertTitle>
          <AlertDescription>
            绑定设备生物识别（Touch ID / Face ID）或设置自定义密码后，后续访问无需再查找或输入管理 Token；容器重部署亦保持 30 天持久登录。
          </AlertDescription>
          <AlertAction>
            <Button size="sm" variant="outline" render={<Link to={paths.account()} />} nativeButton={false}>
              前往设置
            </Button>
          </AlertAction>
        </Alert>
      ) : null}

      <section
        className="grid grid-cols-2 gap-3 @3xl:grid-cols-4"
        aria-label="全局关键指标摘要"
      >
        <StatTile
          label="独立访客 (UV)"
          icon={Users}
          value={formatCompactNumber(portfolio.unique_visitors)}
          detail={`· ${formatCompactNumber(portfolio.sessions)} 会话`}
          footer={`实时 5 分钟在线 ${portfolio.active_users_5m.toLocaleString()} 人`}
        />
        <StatTile
          label="页面浏览 (PV)"
          icon={Activity}
          value={formatCompactNumber(portfolio.pageviews)}
          detail={`· ${formatCompactNumber(portfolio.custom_events)} 事件`}
          footer="首屏与 SPA 路由自动汇总"
        />
        <StatTile
          label="待处理异常 (Open)"
          icon={TriangleAlert}
          value={portfolio.open_issues.toLocaleString()}
          detail={`· ${portfolio.error_count.toLocaleString()} 次触发`}
          footer="按异常调用栈指纹去重聚合"
          tone={portfolio.open_issues > 0 ? "critical" : "normal"}
        />
        <StatTile
          label="会话无崩溃率"
          icon={ShieldCheck}
          value={`${portfolio.overall_crash_free_rate.toFixed(2)}%`}
          footer="无未捕获异常的健康会话占比"
          tone={portfolio.overall_crash_free_rate < 99 ? "warning" : "normal"}
        />
      </section>

      {retention ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              <HardDrive className="size-4 text-muted-foreground" />
              存储配额与滚动清理治理
            </CardTitle>
            <CardDescription className="text-xs">
              最长保留 {retention.retention_days} 天或总占用达到 {formatBytes(maxBytes)} 时自动按日分区淘汰历史数据，并保留最新 3 个 Release 的 SourceMap。
            </CardDescription>
            <CardAction>
              <span className="font-mono text-xs text-muted-foreground tabular-nums">
                {formatBytes(usedBytes)} / {formatBytes(maxBytes)}
              </span>
            </CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-2.5">
            <UsageMeter value={storagePct} label="节点存储使用率" className="w-full max-w-none" />
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground tabular-nums">
              <span>ClickHouse：{formatBytes(retention.clickhouse_bytes)}</span>
              <span>SourceMap：{formatBytes(retention.sourcemap_bytes)}</span>
              {retention.last_run_at ? (
                <span>最近巡检：{formatRelativeTime(retention.last_run_at)}</span>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Boxes className="size-4 text-muted-foreground" />
            监控项目列表
          </CardTitle>
          <CardDescription>点击任意项目进入独立子路由，支持按列排序对比流量与稳定性。</CardDescription>
          <CardAction>
            <span className="text-xs text-muted-foreground tabular-nums">{projects.length} 个项目</span>
          </CardAction>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <DataTable<PortfolioProjectStats>
            ariaLabel="监控项目列表"
            rows={projects}
            rowKey={(r) => r.project.slug}
            defaultSortKey="pv"
            onRowClick={(r) => navigate(paths.projectSection(r.project.slug, "overview", since))}
            emptyTitle="尚未创建监控项目"
            emptyDescription="创建一个监控项目即可获取前端上报公钥与兼容 Sentry 的 DSN。"
            emptyAction={
              <Button size="sm" onClick={openNewProjectModal}>
                <Plus data-icon="inline-start" />
                新建项目
              </Button>
            }
            columns={[
              {
                key: "name",
                header: "项目",
                sortable: true,
                sortValue: (r) => r.project.name || r.project.slug,
                render: (r) => (
                  <Link
                    to={paths.projectSection(r.project.slug, "overview", since)}
                    className="inline-block hover:underline"
                  >
                    <PrimaryCell
                      title={r.project.name || r.project.slug}
                      meta={r.project.name && r.project.name !== r.project.slug ? r.project.slug : undefined}
                    />
                  </Link>
                ),
              },
              {
                key: "status",
                header: "健康状态",
                sortable: true,
                sortValue: (r) => r.open_issues,
                render: (r) =>
                  r.open_issues > 0 ? (
                    <Link to={paths.projectSection(r.project.slug, "issues", since)}>
                      <StatePill label={`${r.open_issues} 待修复`} tone="destructive" />
                    </Link>
                  ) : (
                    <StatePill
                      label={r.active_users_5m > 0 ? `${r.active_users_5m} 在线` : "正常"}
                      tone="success"
                    />
                  ),
              },
              {
                key: "uv",
                header: "访客 UV",
                align: "right",
                sortable: true,
                sortValue: (r) => r.unique_visitors,
                render: (r) => <span className="tabular-nums">{r.unique_visitors.toLocaleString()}</span>,
              },
              {
                key: "pv",
                header: "浏览 PV",
                align: "right",
                sortable: true,
                sortValue: (r) => r.pageviews,
                render: (r) => <span className="font-medium tabular-nums">{r.pageviews.toLocaleString()}</span>,
              },
              {
                key: "events",
                header: "自定义事件",
                align: "right",
                sortable: true,
                sortValue: (r) => r.custom_events,
                render: (r) => <span className="tabular-nums">{r.custom_events.toLocaleString()}</span>,
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
                key: "cf",
                header: "无崩溃率",
                align: "right",
                sortable: true,
                sortValue: (r) => r.crash_free_rate,
                render: (r) => <span className="tabular-nums">{r.crash_free_rate.toFixed(2)}%</span>,
              },
              {
                key: "trend",
                header: "24h 趋势",
                render: (r) => (
                  <Sparkline
                    values={r.sparkline}
                    tone={r.open_issues > 0 ? "critical" : "normal"}
                    width={92}
                    height={26}
                  />
                ),
              },
              {
                key: "last",
                header: "最近上报",
                align: "right",
                sortable: true,
                sortValue: (r) => r.last_event_at || "",
                render: (r) => (
                  <span className="text-xs text-muted-foreground">{formatRelativeTime(r.last_event_at)}</span>
                ),
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
