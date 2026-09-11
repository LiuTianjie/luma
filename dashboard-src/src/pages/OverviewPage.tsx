import { ArrowRight, Plus, X } from "lucide-react";
import { useMemo } from "react";
import { Badge, StatePill } from "../components/primitives";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
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
  const groups = useMemo(() => groupOverviewIssues(visibleIssues, vm.applications, vm.nodes), [visibleIssues, vm.applications, vm.nodes]);
  const hiddenCount = vm.issues.length - visibleIssues.length;
  const severityLabel = (value: string) => value === "critical" ? (zh ? "严重" : "Critical") : value === "warning" ? (zh ? "警告" : "Warning") : (zh ? "信息" : "Info");

  return (
    <div className="flex flex-col gap-6">
      <PageHeader meta={{
        eyebrow: t(lang, "controlPlane"),
        title: zh ? "集群总览" : "Cluster overview",
        description: zh ? "从需要关注的对象开始，进入应用或节点继续处理。" : "Start with objects that need attention, then continue in the application or node workspace.",
        metrics: [],
        action: (
          <Button onClick={() => onNavigate("deploy")}>
            <Plus data-icon="inline-start" />
            {t(lang, "createApplication")}
          </Button>
        ),
      }} />

      <section className="grid gap-4 md:grid-cols-3" aria-label={zh ? "集群运行摘要" : "Cluster summary"}>
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
            value: `${vm.issues.length}`,
            meta: "",
            hint: zh ? `${vm.issueCounts.critical} 严重 · ${vm.issueCounts.warning} 警告` : `${vm.issueCounts.critical} critical · ${vm.issueCounts.warning} warning`,
            onClick: () => onNavigate("observability"),
          },
        ].map((item) => (
          <Card
            key={item.label}
            size="sm"
            role="link"
            tabIndex={0}
            onClick={item.onClick}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                item.onClick();
              }
            }}
            className="cursor-pointer transition-colors hover:bg-muted/40"
          >
            <CardHeader>
              <CardDescription>{item.label}</CardDescription>
              <CardTitle className="text-2xl font-semibold tabular-nums text-card-foreground">
                {item.value}
                {item.meta ? <span className="ml-1 text-sm font-normal text-muted-foreground">{item.meta}</span> : null}
              </CardTitle>
            </CardHeader>
            <CardFooter className="text-xs text-muted-foreground">{item.hint}</CardFooter>
          </Card>
        ))}
      </section>

      <section className="grid gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(20rem,0.8fr)]" aria-label={zh ? "运维关注" : "Operations attention"}>
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "需要关注" : "Needs attention"}</CardTitle>
            <CardDescription>{zh ? "按对象归组，展开查看原始诊断；归组不代表相同根因。" : "Grouped by object. Expand for original diagnostics; grouping does not imply a shared cause."}</CardDescription>
            <CardAction><Badge value={zh ? `${groups.length} 个对象` : `${groups.length} objects`} /></CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {groups.map((group) => (
              <section className="rounded-lg border p-3" key={group.key}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-col gap-1">
                    <div className="flex items-center gap-2">
                      <StatePill value={group.severity === "critical" ? "failed" : group.severity === "warning" ? "pending" : "unknown"} label={severityLabel(group.severity)} />
                      <strong className="truncate">{group.target}</strong>
                    </div>
                    <small className="text-xs text-muted-foreground">{group.app ? (zh ? "应用" : "Application") : group.node ? (zh ? "节点" : "Node") : (zh ? "诊断对象" : "Diagnostic target")}</small>
                  </div>
                  {group.app || group.node ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => group.node ? onSelectNode(group.node) : onNavigate("applications", { selectApp: group.app!.stack })}
                    >
                      {zh ? "查看详情" : "View details"}
                      <ArrowRight data-icon="inline-end" />
                    </Button>
                  ) : null}
                </div>
                <details className="mt-2">
                  <summary className="cursor-pointer text-sm text-muted-foreground">
                    {zh ? `${group.issues.length} 条诊断 · 查看原始信息` : `${group.issues.length} diagnostics · view evidence`}
                  </summary>
                  <div className="mt-2 flex flex-col gap-2">
                    {group.issues.map((issue, index) => (
                      <div className="flex items-start justify-between gap-2 rounded-md bg-muted/40 p-2" key={`${issueKey(issue)}:${index}`}>
                        <div className="min-w-0">
                          <small className="text-xs text-muted-foreground">{severityLabel(issue.severity || "info")} · {issue.kind || "—"} · {issue.target || "—"}</small>
                          <p className="text-sm">{issue.message || (zh ? "未提供诊断信息" : "No diagnostic message provided")}</p>
                        </div>
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          title={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}
                          aria-label={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}
                          onClick={() => dismiss(issueKey(issue))}
                        >
                          <X />
                        </Button>
                      </div>
                    ))}
                  </div>
                </details>
              </section>
            ))}
            {!groups.length ? (
              <Empty className="py-8">
                <EmptyHeader>
                  <EmptyTitle>{hiddenCount ? (zh ? "诊断已临时隐藏" : "Diagnostics hidden") : (zh ? "当前未报告诊断问题。" : "No diagnostic issues currently reported.")}</EmptyTitle>
                  <EmptyDescription>
                    {hiddenCount
                      ? (zh ? "当前诊断已在此浏览器临时隐藏，并未解决。" : "Current diagnostics are temporarily hidden in this browser, not resolved.")
                      : (zh ? "集群当前没有需要立即处理的诊断项。" : "The cluster has no diagnostics that need immediate attention.")}
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : null}
          </CardContent>
          {hiddenCount ? (
            <CardFooter>
              <Button variant="ghost" size="sm" onClick={clear}>
                {zh ? `本机临时隐藏 ${hiddenCount} 条 · 全部恢复` : `${hiddenCount} hidden locally · restore all`}
              </Button>
            </CardFooter>
          ) : null}
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
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
                  {readiness.nomad?.leader ? <small className="text-xs text-muted-foreground">{readiness.nomad.leader}</small> : null}
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">DNS</span>
                <div className="flex flex-wrap items-center gap-2">
                  <StatePill
                    value={readiness.dns?.ready === undefined ? "unknown" : readiness.dns.ready ? "ready" : "pending"}
                    label={readiness.dns?.ready === undefined ? (zh ? "未检查" : "Not checked") : readiness.dns.ready ? (zh ? "配置就绪" : "Configured") : (zh ? "配置不完整" : "Configuration incomplete")}
                  />
                  {readiness.dns?.zone ? <small className="text-xs text-muted-foreground">{readiness.dns.zone}</small> : null}
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                {zh ? "DNS 状态仅检查凭据和区域配置，尚未验证解析与公网可达性。" : "DNS status checks credentials and zone configuration only; resolution and public reachability are unverified."}
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{zh ? "节点容量" : "Node capacity"}</CardTitle>
              <CardDescription>{zh ? "按 CPU、内存、磁盘的最高使用率排序" : "Sorted by highest CPU, memory, or disk usage"}</CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t(lang, "nodes")}</TableHead>
                    <TableHead>CPU</TableHead>
                    <TableHead>{zh ? "内存" : "Memory"}</TableHead>
                    <TableHead>{zh ? "磁盘" : "Disk"}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {nodes.map((node, index) => (
                    <TableRow key={node.name || index}>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <Button variant="ghost" size="sm" className="h-auto justify-start px-0" onClick={() => onSelectNode(node)}>
                            {node.displayName || node.name || "—"}
                          </Button>
                          <StatePill label={localizeState(lang, node.state)} value={node.state} />
                        </div>
                      </TableCell>
                      <TableCell>{percent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</TableCell>
                      <TableCell>{percent(node.metrics?.memoryUsedPercent)}</TableCell>
                      <TableCell>{percent(node.metrics?.diskUsedPercent)}</TableCell>
                    </TableRow>
                  ))}
                  {!nodes.length ? (
                    <TableRow>
                      <TableCell colSpan={4}>{zh ? "暂无节点数据" : "No node data"}</TableCell>
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

          <Card>
            <CardHeader>
              <CardTitle>{zh ? "交付追踪" : "Delivery tracking"}</CardTitle>
              <CardDescription>{zh ? "在交付记录中查看近期构建、部署结果与任务详情。" : "Review recent builds, deployment results, and task details in delivery history."}</CardDescription>
            </CardHeader>
            <CardFooter>
              <Button variant="ghost" size="sm" onClick={() => onNavigate("deployments")}>
                {zh ? "查看交付记录" : "View delivery history"}
                <ArrowRight data-icon="inline-end" />
              </Button>
            </CardFooter>
          </Card>
        </div>
      </section>
    </div>
  );
}
