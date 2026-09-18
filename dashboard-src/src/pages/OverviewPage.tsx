import { ArrowRight, CheckCircle2, Info, Plus, Server, X } from "lucide-react";
import { useMemo } from "react";
import { StatePill } from "../components/primitives";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { localizeState, t } from "../i18n";
import type { DashboardNode, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { issueKey, useDismissedIssues } from "../useDismissedIssues";
import { groupOverviewIssues, nodePressure } from "./overviewModel";
import { PageHeader } from "./PageHeader";

function percent(value?: number) {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)}%` : "—";
}

export function OverviewPage({ lang, payload, vm, onNavigate, onSelectNode }: {
  lang: Lang;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onNavigate: (page: NavPage, opts?: { selectApp?: string }) => void;
  onSelectNode: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
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
        description: zh ? "先看集群是否健康，再进入应用或节点继续处理。" : "Check cluster health first, then continue in the application or node workspace.",
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
          <AlertDescription>{zh ? "集群里还没有应用。用 “hello-world 首装验证” 确认 Nomad 调度即可，不必先配 Tailscale、Registry 或 LAE。" : "No applications yet. Use “hello-world first install” to prove Nomad scheduling. Tailscale, registry, and LAE are not required."}</AlertDescription>
          <AlertAction>
            <Button size="sm" onClick={() => onNavigate("deploy")}>
              <Plus data-icon="inline-start" />
              {zh ? "创建应用" : "Create application"}
            </Button>
          </AlertAction>
        </Alert>
      ) : null}

      <section className="grid gap-4 @md:grid-cols-3" aria-label={zh ? "集群运行摘要" : "Cluster summary"}>
        {[
          {
            label: zh ? "异常应用" : "Affected applications",
            value: `${affectedApps.length}`,
            meta: `/ ${vm.applications.length}`,
            hint: zh ? "依据副本与任务运行状态" : "Based on replica and task state",
            onClick: () => onNavigate("applications"),
          },
          {
            label: zh ? "就绪节点" : "Ready nodes",
            value: `${vm.activeNodes}`,
            meta: `/ ${vm.nodes.length}`,
            hint: zh ? "已排除排空中的节点" : "Excludes draining nodes",
            onClick: () => onNavigate("nodes"),
          },
          {
            label: zh ? "当前诊断" : "Current diagnostics",
            value: `${visibleIssues.length}`,
            meta: hiddenCount ? (zh ? ` · ${hiddenCount} 已隐藏` : ` · ${hiddenCount} hidden`) : "",
            hint: hiddenCount && !visibleIssues.length
              ? (zh ? "本机临时隐藏，并未解决" : "Temporarily hidden in this browser, not resolved")
              : (zh ? `${visibleCounts.critical} 严重 · ${visibleCounts.warning} 警告` : `${visibleCounts.critical} critical · ${visibleCounts.warning} warning`),
            onClick: () => onNavigate("observability"),
          },
        ].map((item) => (
          <Card
            key={item.label}
            size="sm"
            className="cursor-pointer"
            role="link"
            tabIndex={0}
            onClick={item.onClick}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                item.onClick();
              }
            }}
          >
            <CardHeader>
              <CardTitle>{item.label}</CardTitle>
              <CardDescription>{item.hint}</CardDescription>
              <CardAction>
                <ArrowRight className="size-4 text-muted-foreground" aria-hidden="true" />
              </CardAction>
            </CardHeader>
            <CardContent>
              <p className="flex items-baseline gap-2 tabular-nums">
                <strong className="text-2xl font-semibold">{item.value}</strong>
                {item.meta ? <span className="text-muted-foreground">{item.meta}</span> : null}
              </p>
            </CardContent>
          </Card>
        ))}
      </section>

      {groups.length ? (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "需要关注" : "Needs attention"}</CardTitle>
            <CardDescription>{zh ? "按对象归组；归组不代表相同根因。" : "Grouped by object; grouping does not imply a shared cause."}</CardDescription>
            <CardAction><Badge variant="secondary">{zh ? `${groups.length} 个对象` : `${groups.length} objects`}</Badge></CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {groups.map((group) => (
              <Card size="sm" key={group.key}>
                <CardHeader>
                  <CardTitle className="min-w-0 wrap-anywhere">{group.target}</CardTitle>
                  <CardDescription>{groupKind(group)}</CardDescription>
                  <CardAction>
                    <Badge variant={group.severity === "critical" ? "destructive" : group.severity === "warning" ? "secondary" : "outline"}>
                      {severityLabel(group.severity)}
                    </Badge>
                  </CardAction>
                </CardHeader>
                <CardContent className="flex flex-col gap-3">
                  <div className="flex flex-col gap-2">
                    {group.issues.map((issue, index) => (
                      <Alert variant={issue.severity === "critical" ? "destructive" : "default"} key={`${issueKey(issue)}:${index}`}>
                        <AlertTitle className="min-w-0 wrap-anywhere">{issue.message || (zh ? "未提供诊断信息" : "No diagnostic message provided")}</AlertTitle>
                        <AlertDescription className="min-w-0 wrap-anywhere">{[severityLabel(issue.severity || "info"), issue.kind].filter(Boolean).join(" · ")}</AlertDescription>
                        <AlertAction>
                          <Tooltip>
                            <TooltipTrigger render={<Button variant="ghost" size="icon-xs" aria-label={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"} onClick={() => dismiss(issueKey(issue))} />}>
                              <X data-icon="inline-start" />
                            </TooltipTrigger>
                            <TooltipContent>{zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}</TooltipContent>
                          </Tooltip>
                        </AlertAction>
                      </Alert>
                    ))}
                  </div>
                  {group.app || group.node ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-fit"
                      onClick={() => group.node ? onSelectNode(group.node) : onNavigate("applications", { selectApp: group.app!.stack })}
                    >
                      {zh ? "查看详情" : "View details"}
                      <ArrowRight data-icon="inline-end" />
                    </Button>
                  ) : null}
                </CardContent>
              </Card>
            ))}
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
        <Alert>
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

      <section className="grid items-start gap-6 @lg:grid-cols-2" aria-label={zh ? "控制面与容量" : "Control plane and capacity"}>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "控制面" : "Control plane"}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">{zh ? "调度器" : "Scheduler"}</span>
              <div className="flex flex-wrap items-center gap-2">
                <StatePill
                  value={readiness.nomad?.available === undefined ? "unknown" : readiness.nomad.available ? "ready" : "failed"}
                  label={readiness.nomad?.available === undefined ? (zh ? "未检查" : "Not checked") : readiness.nomad.available ? (zh ? "可连接" : "Reachable") : (zh ? "无法连接" : "Unreachable")}
                />
                {readiness.nomad?.leader ? <small className="min-w-0 max-w-full text-xs wrap-anywhere text-muted-foreground">{readiness.nomad.leader}</small> : null}
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">DNS</span>
              <div className="flex flex-wrap items-center gap-2">
                <StatePill
                  value={readiness.dns?.ready === undefined ? "unknown" : readiness.dns.ready ? "ready" : "pending"}
                  label={readiness.dns?.ready === undefined ? (zh ? "未检查" : "Not checked") : readiness.dns.ready ? (zh ? "配置就绪" : "Configured") : (zh ? "配置不完整" : "Configuration incomplete")}
                />
                {readiness.dns?.zone ? <small className="min-w-0 max-w-full text-xs wrap-anywhere text-muted-foreground">{readiness.dns.zone}</small> : null}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{zh ? "DNS 仅检查凭据和区域配置，不验证解析或公网可达。" : "DNS checks credentials and zone configuration only, not resolution or public reachability."}</p>
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
                  <TableHead className="text-right">CPU</TableHead>
                  <TableHead className="text-right">{zh ? "内存" : "Memory"}</TableHead>
                  <TableHead className="text-right">{zh ? "磁盘" : "Disk"}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {nodes.map((node, index) => (
                  <TableRow key={node.name || index}>
                    <TableCell>
                      <div className="flex flex-col gap-1">
                        <Button variant="link" size="sm" className="w-fit max-w-40" onClick={() => onSelectNode(node)}>
                          <span className="truncate" title={node.displayName || node.name || "—"}>{node.displayName || node.name || "—"}</span>
                        </Button>
                        <StatePill label={localizeState(lang, node.state)} value={node.state} />
                      </div>
                    </TableCell>
                    <TableCell className="text-right">{percent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</TableCell>
                    <TableCell className="text-right">{percent(node.metrics?.memoryUsedPercent)}</TableCell>
                    <TableCell className="text-right">{percent(node.metrics?.diskUsedPercent)}</TableCell>
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
            <Button variant="ghost" size="sm" onClick={() => onNavigate("nodes")}>
              {zh ? `查看全部 ${vm.nodes.length} 个节点` : `View all ${vm.nodes.length} nodes`}
              <ArrowRight data-icon="inline-end" />
            </Button>
          </CardFooter>
        </Card>
      </section>
    </div>
  );
}
