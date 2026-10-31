import { ArrowRight, Boxes, CheckCircle2, CircleAlert, Info, Plus, Server, ServerCog, TriangleAlert, X, type Icon } from "@/components/icons";
import { useMemo } from "react";
import { StatePill, UsageMeter } from "../components/primitives";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { localizeState, t } from "../i18n";
import type { DashboardNode, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { issueKey, useDismissedIssues } from "../useDismissedIssues";
import { groupOverviewIssues, nodePressure } from "./overviewModel";
import { PageHeader } from "./PageHeader";
import { spaLink, useRouter } from "../router";

function severityVariant(severity: string) {
  return severity === "critical" ? "destructive" as const : severity === "warning" ? "warning" as const : "outline" as const;
}

function SeverityIcon({ severity }: { severity: string }) {
  if (severity === "critical") return <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden="true" />;
  if (severity === "warning") return <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />;
  return <Info className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
}

type StatTile = { label: string; value: string; meta?: string; hint: string; href: string; icon: Icon; tone: "normal" | "warning" | "critical" };

export function OverviewPage({ lang, payload, vm, onNavigate, onSelectNode }: {
  lang: Lang;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onNavigate: (page: NavPage, opts?: { selectApp?: string }) => void;
  onSelectNode: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const { navigate } = useRouter();
  const readiness = payload.readiness || {};
  const affectedApps = vm.applications.filter((app) => !["healthy", "running"].includes(app.status));
  const nodes = vm.nodes.slice().sort((a, b) => nodePressure(b) - nodePressure(a)).slice(0, 5);
  const { dismiss, clear, isDismissed } = useDismissedIssues(payload.cluster?.id || "", vm.issues);
  const visibleIssues = useMemo(() => vm.issues.filter((issue) => !isDismissed(issueKey(issue))), [vm.issues, isDismissed]);
  const groups = useMemo(() => groupOverviewIssues(visibleIssues, vm.applications, vm.nodes, vm.services), [visibleIssues, vm.applications, vm.nodes, vm.services]);
  const hiddenCount = vm.issues.length - visibleIssues.length;
  const visibleCounts = useMemo(() => visibleIssues.reduce((counts, issue) => {
    if (issue.severity === "critical") counts.critical += 1;
    else if (issue.severity === "warning") counts.warning += 1;
    else counts.info += 1;
    return counts;
  }, { critical: 0, warning: 0, info: 0 }), [visibleIssues]);
  const severityLabel = (value: string) => value === "critical" ? (zh ? "严重" : "Critical") : value === "warning" ? (zh ? "警告" : "Warning") : (zh ? "信息" : "Info");
  const groupKind = (group: typeof groups[number]) => group.app
    ? (zh ? "应用" : "Application")
    : group.node
      ? (zh ? "节点" : "Node")
      : group.platform
        ? (zh ? "平台服务" : "Platform service")
        : (zh ? "诊断对象" : "Diagnostic target");

  return (
    <div className="@container flex flex-col gap-6">
      <PageHeader meta={{
        eyebrow: "",
        title: zh ? "集群总览" : "Cluster overview",
        description: zh ? "应用健康、待处理诊断与节点容量。" : "Application health, open diagnostics, and node capacity.",
        metrics: [],
        action: vm.applications.length ? (
          <Button onClick={() => onNavigate("deploy")}>
            <Plus data-icon="inline-start" />
            {t(lang, "createApplication")}
          </Button>
        ) : undefined,
      }} />

      {vm.applications.length === 0 ? (
        <Alert>
          <Info />
          <AlertTitle>{zh ? "下一步：部署 hello-world" : "Next: deploy hello-world"}</AlertTitle>
          <AlertDescription>{zh ? "集群里还没有应用。用 “hello-world 首装验证” 确认 Nomad 调度即可，不必先配 Tailscale 或 Registry。" : "No applications yet. Use “hello-world first install” to prove Nomad scheduling. Tailscale and a registry are not required."}</AlertDescription>
          <AlertAction>
            <Button size="sm" onClick={() => onNavigate("deploy")}>
              <Plus data-icon="inline-start" />
              {zh ? "创建应用" : "Create application"}
            </Button>
          </AlertAction>
        </Alert>
      ) : null}

      <section className="grid grid-cols-3 gap-2 @lg:gap-4" aria-label={zh ? "集群运行摘要" : "Cluster summary"}>
        {([
          {
            label: zh ? "异常应用" : "Affected applications",
            value: `${affectedApps.length}`,
            meta: `/ ${vm.applications.length}`,
            hint: zh ? "依据副本与任务运行状态" : "Based on replica and task state",
            href: "/apps",
            icon: Boxes,
            tone: affectedApps.length ? "critical" : "normal",
          },
          {
            label: zh ? "就绪节点" : "Ready nodes",
            value: `${vm.activeNodes}`,
            meta: `/ ${vm.nodes.length}`,
            hint: zh ? "已排除排空中的节点" : "Excludes draining nodes",
            href: "/fleet",
            icon: ServerCog,
            tone: vm.nodes.length && vm.activeNodes < vm.nodes.length ? "warning" : "normal",
          },
          {
            label: zh ? "当前诊断" : "Current diagnostics",
            value: `${visibleIssues.length}`,
            meta: hiddenCount ? (zh ? `· ${hiddenCount} 已隐藏` : `· ${hiddenCount} hidden`) : "",
            hint: hiddenCount && !visibleIssues.length
              ? (zh ? "本机临时隐藏，并未解决" : "Temporarily hidden in this browser, not resolved")
              : (zh ? `${visibleCounts.critical} 严重 · ${visibleCounts.warning} 警告` : `${visibleCounts.critical} critical · ${visibleCounts.warning} warning`),
            href: "/observe",
            icon: TriangleAlert,
            tone: visibleCounts.critical ? "critical" : visibleCounts.warning ? "warning" : "normal",
          },
        ] satisfies StatTile[]).map((item) => {
          const Icon = item.icon;
          return (
            <a
              key={item.label}
              {...spaLink(item.href, navigate)}
              className="group flex min-w-0 flex-col gap-2 rounded-lg bg-card p-3 shadow-xs ring-1 ring-border outline-none transition-[box-shadow,background-color] hover:ring-foreground/20 focus-visible:ring-2 focus-visible:ring-ring @lg:gap-3 @lg:p-5"
            >
              <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground @lg:text-[0.8125rem]">
                <Icon className={cn("size-4", item.tone === "critical" ? "text-destructive" : item.tone === "warning" ? "text-warning" : "text-muted-foreground")} aria-hidden="true" />
                <span className="truncate font-medium">{item.label}</span>
                <ArrowRight className="ml-auto size-4 -translate-x-1 opacity-0 transition-[opacity,transform] group-hover:translate-x-0 group-hover:opacity-100" aria-hidden="true" />
              </span>
              <span className="flex items-baseline gap-1.5 tabular-nums">
                <strong className={cn("text-2xl/none font-semibold tracking-[-0.02em] @lg:text-[2rem]/none", item.tone === "critical" && "text-destructive-foreground", item.tone === "warning" && "text-warning-foreground")}>{item.value}</strong>
                {item.meta ? <span className="text-sm text-muted-foreground">{item.meta}</span> : null}
              </span>
              <span className="hidden text-xs text-muted-foreground @lg:block">{item.hint}</span>
            </a>
          );
        })}
      </section>

      {groups.length ? (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "需要关注" : "Needs attention"}</CardTitle>
            <CardDescription>{zh ? "按对象归组；归组不代表相同根因。" : "Grouped by object; grouping does not imply a shared cause."}</CardDescription>
            <CardAction><span className="text-xs text-muted-foreground tabular-nums">{zh ? `${groups.length} 个对象` : `${groups.length} objects`}</span></CardAction>
          </CardHeader>
          <CardContent className="-mb-(--card-spacing) px-0">
            <ul className="divide-y divide-hairline border-t border-hairline">
              {groups.map((group) => (
                <li key={group.key} className="flex flex-col gap-2 px-(--card-spacing) py-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="font-medium wrap-anywhere">{group.target}</span>
                      <span className="text-xs text-muted-foreground">{groupKind(group)}</span>
                      <Badge variant={severityVariant(group.severity)}>{severityLabel(group.severity)}</Badge>
                    </div>
                    {group.app || group.node ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => group.node ? onSelectNode(group.node) : onNavigate("applications", { selectApp: group.app!.stack })}
                      >
                        {zh ? "查看详情" : "View details"}
                        <ArrowRight data-icon="inline-end" />
                      </Button>
                    ) : null}
                  </div>
                  <ul className="flex flex-col gap-1">
                    {group.issues.map((issue, index) => (
                      <li key={`${issueKey(issue)}:${index}`} className="group/issue -mx-2 flex items-start gap-2.5 rounded-md px-2 py-1.5 hover:bg-muted/50">
                        <SeverityIcon severity={issue.severity || "info"} />
                        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="text-[0.8125rem] wrap-anywhere">{issue.message || (zh ? "未提供诊断信息" : "No diagnostic message provided")}</span>
                          {issue.kind ? <span className="font-mono text-[0.6875rem] text-muted-foreground wrap-anywhere">{issue.kind}</span> : null}
                        </div>
                        <Tooltip>
                          <TooltipTrigger render={<Button variant="ghost" size="icon-xs" className="text-muted-foreground opacity-60 group-hover/issue:opacity-100 focus-visible:opacity-100" aria-label={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"} onClick={() => dismiss(issueKey(issue))} />}>
                            <X />
                          </TooltipTrigger>
                          <TooltipContent>{zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}</TooltipContent>
                        </Tooltip>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </CardContent>
          {hiddenCount ? (
            <CardFooter>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary">{zh ? `本机临时隐藏 ${hiddenCount} 条` : `${hiddenCount} hidden locally`}</Badge>
                <Button variant="ghost" size="sm" onClick={clear}>{zh ? "全部恢复" : "Restore all"}</Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>
      ) : (
        <Alert variant={hiddenCount ? "neutral" : "success"}>
          <CheckCircle2 />
          <AlertTitle>{hiddenCount ? (zh ? "诊断已临时隐藏" : "Diagnostics hidden") : (zh ? "当前未报告诊断问题" : "No diagnostic issues currently reported")}</AlertTitle>
          <AlertDescription>
            {hiddenCount
              ? (zh ? "当前诊断已在此浏览器临时隐藏，并未解决。" : "Current diagnostics are temporarily hidden in this browser, not resolved.")
              : (zh ? "集群当前没有需要立即处理的诊断项。" : "The cluster has no diagnostics that need immediate attention.")}
          </AlertDescription>
          {hiddenCount ? (
            <AlertAction>
              <Button variant="ghost" size="sm" onClick={clear}>{zh ? "全部恢复" : "Restore all"}</Button>
            </AlertAction>
          ) : null}
        </Alert>
      )}

      <section className="grid items-start gap-6 @3xl:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]" aria-label={zh ? "控制面与容量" : "Control plane and capacity"}>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "控制面" : "Control plane"}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-6 gap-y-3 text-[0.8125rem]">
              <dt className="text-muted-foreground">{zh ? "调度器" : "Scheduler"}</dt>
              <dd className="flex min-w-0 flex-wrap items-center gap-2">
                <StatePill
                  value={readiness.nomad?.available === undefined ? "unknown" : readiness.nomad.available ? "ready" : "failed"}
                  label={readiness.nomad?.available === undefined ? (zh ? "未检查" : "Not checked") : readiness.nomad.available ? (zh ? "可连接" : "Reachable") : (zh ? "无法连接" : "Unreachable")}
                />
                {readiness.nomad?.leader ? <code className="min-w-0 font-mono text-xs wrap-anywhere text-muted-foreground">{readiness.nomad.leader}</code> : null}
              </dd>
              <dt className="text-muted-foreground">DNS</dt>
              <dd className="flex min-w-0 flex-wrap items-center gap-2">
                <StatePill
                  value={readiness.dns?.ready === undefined ? "unknown" : readiness.dns.ready ? "ready" : "pending"}
                  label={readiness.dns?.ready === undefined ? (zh ? "未检查" : "Not checked") : readiness.dns.ready ? (zh ? "配置就绪" : "Configured") : (zh ? "配置不完整" : "Configuration incomplete")}
                />
                {readiness.dns?.zone ? <code className="min-w-0 font-mono text-xs wrap-anywhere text-muted-foreground">{readiness.dns.zone}</code> : null}
              </dd>
            </dl>
            <p className="flex gap-1.5 text-xs text-muted-foreground"><Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />{zh ? "DNS 仅检查凭据和区域配置，不验证解析或公网可达。" : "DNS checks credentials and zone configuration only, not resolution or public reachability."}</p>
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "节点容量" : "Node capacity"}</CardTitle>
            <CardDescription>{zh ? "按 CPU、内存、磁盘的最高使用率排序" : "Sorted by highest CPU, memory, or disk usage"}</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t(lang, "nodes")}</TableHead>
                  <TableHead className="w-[22%]">CPU</TableHead>
                  <TableHead className="w-[22%]">{zh ? "内存" : "Memory"}</TableHead>
                  <TableHead className="w-[22%]">{zh ? "磁盘" : "Disk"}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {nodes.map((node, index) => (
                  <TableRow key={node.name || index}>
                    <TableCell>
                      <div className="flex min-w-0 items-center gap-2">
                        <span className={cn("size-1.5 shrink-0 rounded-full", (node.state || "").toLowerCase() === "ready" ? "bg-success" : "bg-warning")} title={localizeState(lang, node.state)} aria-hidden="true" />
                        <button type="button" className="max-w-40 truncate font-medium text-link outline-none hover:underline focus-visible:underline" title={node.displayName || node.name || "—"} onClick={() => onSelectNode(node)}>{node.displayName || node.name || "—"}</button>
                        <span className="sr-only">{localizeState(lang, node.state)}</span>
                      </div>
                    </TableCell>
                    <TableCell><UsageMeter value={node.metrics?.cpuPercent ?? node.metrics?.loadPercent} label={`${node.name} CPU`} /></TableCell>
                    <TableCell><UsageMeter value={node.metrics?.memoryUsedPercent} label={`${node.name} ${zh ? "内存" : "memory"}`} /></TableCell>
                    <TableCell><UsageMeter value={node.metrics?.diskUsedPercent} label={`${node.name} ${zh ? "磁盘" : "disk"}`} /></TableCell>
                  </TableRow>
                ))}
                {!nodes.length ? (
                  <TableRow>
                    <TableCell colSpan={4} className="whitespace-normal">
                      <Empty>
                        <EmptyHeader>
                          <EmptyMedia variant="icon"><Server /></EmptyMedia>
                          <EmptyTitle>{zh ? "暂无节点数据" : "No node data"}</EmptyTitle>
                          <EmptyDescription>{zh ? "节点加入后，这里会显示容量与运行状态。" : "Capacity and state appear here after nodes join the cluster."}</EmptyDescription>
                        </EmptyHeader>
                      </Empty>
                    </TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </CardContent>
          <CardFooter>
            <a {...spaLink("/fleet", navigate)} className="inline-flex items-center gap-1 text-[0.8125rem] font-medium text-link outline-none hover:underline focus-visible:underline">
              {zh ? `查看全部 ${vm.nodes.length} 个节点` : `View all ${vm.nodes.length} nodes`}
              <ArrowRight className="size-3.5" aria-hidden="true" />
            </a>
          </CardFooter>
        </Card>
      </section>
    </div>
  );
}
