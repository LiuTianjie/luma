import { ArrowRight, Plus, X } from "lucide-react";
import { useMemo } from "react";
import { Badge, StatePill } from "../components/ui";
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

  return <>
    <PageHeader meta={{
      eyebrow: t(lang, "controlPlane"), title: zh ? "集群总览" : "Cluster overview",
      description: zh ? "从需要关注的对象开始，进入应用或节点继续处理。" : "Start with objects that need attention, then continue in the application or node workspace.",
      metrics: [],
      action: <button type="button" className="primary page-toolbar-cta" onClick={() => onNavigate("deploy")}><Plus size={16} aria-hidden="true" />{t(lang, "createApplication")}</button>,
    }} />

    <section className="overview-summary-grid" aria-label={zh ? "集群运行摘要" : "Cluster summary"}>
      <button className="overview-summary-item" onClick={() => onNavigate("applications")} type="button"><span>{zh ? "异常应用" : "Affected applications"}</span><strong>{affectedApps.length}<small> / {vm.applications.length}</small></strong><small>{zh ? "依据副本与任务运行状态" : "Based on replica and task state"}</small></button>
      <button className="overview-summary-item" onClick={() => onNavigate("nodes")} type="button"><span>{zh ? "就绪节点" : "Ready nodes"}</span><strong>{vm.activeNodes}<small> / {vm.nodes.length}</small></strong><small>{zh ? "已排除排空中的节点" : "Excludes draining nodes"}</small></button>
      <button className="overview-summary-item" onClick={() => onNavigate("observability")} type="button"><span>{zh ? "当前诊断" : "Current diagnostics"}</span><strong>{vm.issues.length}</strong><small>{zh ? `${vm.issueCounts.critical} 严重 · ${vm.issueCounts.warning} 警告` : `${vm.issueCounts.critical} critical · ${vm.issueCounts.warning} warning`}</small></button>
    </section>

    <section className="overview-priority-layout" aria-label={zh ? "运维关注" : "Operations attention"}>
      <article className="panel overview-attention-panel">
        <div className="panel-heading"><div><h2>{zh ? "需要关注" : "Needs attention"}</h2><p>{zh ? "按对象归组，展开查看原始诊断；归组不代表相同根因。" : "Grouped by object. Expand for original diagnostics; grouping does not imply a shared cause."}</p></div><Badge value={zh ? `${groups.length} 个对象` : `${groups.length} objects`} /></div>
        <div className="overview-issue-groups">
          {groups.map((group) => <section className={`overview-issue-group ${group.severity}`} key={group.key}>
            <div className="overview-issue-heading"><div><StatePill value={group.severity === "critical" ? "failed" : group.severity === "warning" ? "pending" : "unknown"} label={severityLabel(group.severity)} /><strong>{group.target}</strong><small>{group.app ? (zh ? "应用" : "Application") : group.node ? (zh ? "节点" : "Node") : (zh ? "诊断对象" : "Diagnostic target")}</small></div>
              {group.app || group.node ? <button type="button" className="ghost text-link-button" onClick={() => group.node ? onSelectNode(group.node) : onNavigate("applications", { selectApp: group.app!.stack })}>{zh ? "查看详情" : "View details"}<ArrowRight size={15} aria-hidden="true" /></button> : null}
            </div>
            <details className="overview-issue-evidence"><summary>{zh ? `${group.issues.length} 条诊断 · 查看原始信息` : `${group.issues.length} diagnostics · view evidence`}</summary>
              {group.issues.map((issue, index) => <div className="overview-evidence-row" key={`${issueKey(issue)}:${index}`}><div><small>{severityLabel(issue.severity || "info")} · {issue.kind || "—"} · {issue.target || "—"}</small><p>{issue.message || (zh ? "未提供诊断信息" : "No diagnostic message provided")}</p></div><button type="button" className="ghost" title={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"} aria-label={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"} onClick={() => dismiss(issueKey(issue))}><X size={14} aria-hidden="true" /></button></div>)}
            </details>
          </section>)}
          {!groups.length ? <p className="empty-inline">{hiddenCount ? (zh ? "当前诊断已在此浏览器临时隐藏，并未解决。" : "Current diagnostics are temporarily hidden in this browser, not resolved.") : (zh ? "当前未报告诊断问题。" : "No diagnostic issues currently reported.")}</p> : null}
        </div>
        {hiddenCount ? <button type="button" className="ghost text-link-button" onClick={clear}>{zh ? `本机临时隐藏 ${hiddenCount} 条 · 全部恢复` : `${hiddenCount} hidden locally · restore all`}</button> : null}
      </article>

      <aside className="overview-side-stack">
        <article className="panel overview-control-panel"><div className="panel-heading"><h2>{zh ? "控制面" : "Control plane"}</h2></div>
          <dl className="overview-control-status"><div><dt>{zh ? "调度器" : "Scheduler"}</dt><dd><StatePill value={readiness.nomad?.available === undefined ? "unknown" : readiness.nomad.available ? "ready" : "failed"} label={readiness.nomad?.available === undefined ? (zh ? "未检查" : "Not checked") : readiness.nomad.available ? (zh ? "可连接" : "Reachable") : (zh ? "无法连接" : "Unreachable")} />{readiness.nomad?.leader ? <small>{readiness.nomad.leader}</small> : null}</dd></div>
            <div><dt>DNS</dt><dd><StatePill value={readiness.dns?.ready === undefined ? "unknown" : readiness.dns.ready ? "ready" : "pending"} label={readiness.dns?.ready === undefined ? (zh ? "未检查" : "Not checked") : readiness.dns.ready ? (zh ? "配置就绪" : "Configured") : (zh ? "配置不完整" : "Configuration incomplete")} />{readiness.dns?.zone ? <small>{readiness.dns.zone}</small> : null}</dd></div></dl>
          <p className="panel-footnote">{zh ? "DNS 状态仅检查凭据和区域配置，尚未验证解析与公网可达性。" : "DNS status checks credentials and zone configuration only; resolution and public reachability are unverified."}</p>
        </article>
        <article className="panel overview-node-panel"><div className="panel-heading"><div><h2>{zh ? "节点容量" : "Node capacity"}</h2><p>{zh ? "按 CPU、内存、磁盘的最高使用率排序" : "Sorted by highest CPU, memory, or disk usage"}</p></div></div>
          <div className="table-wrap"><table className="overview-capacity-table"><thead><tr><th>{t(lang, "nodes")}</th><th>CPU</th><th>{zh ? "内存" : "Memory"}</th><th>{zh ? "磁盘" : "Disk"}</th></tr></thead><tbody>{nodes.map((node, index) => <tr key={node.name || index}><td><button type="button" className="ghost text-link-button" onClick={() => onSelectNode(node)}>{node.displayName || node.name || "—"}</button><StatePill label={localizeState(lang, node.state)} value={node.state} /></td><td>{percent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</td><td>{percent(node.metrics?.memoryUsedPercent)}</td><td>{percent(node.metrics?.diskUsedPercent)}</td></tr>)}{!nodes.length ? <tr><td colSpan={4}>{zh ? "暂无节点数据" : "No node data"}</td></tr> : null}</tbody></table></div>
          <button type="button" className="ghost text-link-button" onClick={() => onNavigate("nodes")}>{zh ? `查看全部 ${vm.nodes.length} 个节点` : `View all ${vm.nodes.length} nodes`}<ArrowRight size={15} aria-hidden="true" /></button>
        </article>
        <article className="panel overview-delivery-panel"><div className="panel-heading"><h2>{zh ? "交付追踪" : "Delivery tracking"}</h2></div><p>{zh ? "在交付记录中查看近期构建、部署结果与任务详情。" : "Review recent builds, deployment results, and task details in delivery history."}</p><button type="button" className="ghost text-link-button" onClick={() => onNavigate("deployments")}>{zh ? "查看交付记录" : "View delivery history"}<ArrowRight size={15} aria-hidden="true" /></button></article>
      </aside>
    </section>
  </>;
}
