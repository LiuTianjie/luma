import { ArrowRight, GitBranch, HardDrive, Plus, Server, TerminalSquare, X } from "lucide-react";
import { useMemo, type CSSProperties } from "react";
import { Badge, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import { localizeState, t } from "../i18n";
import type { Application } from "../components/applicationModel";
import type { DashboardIssue, DashboardNode, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { issueKey, useDismissedIssues } from "../useDismissedIssues";

function accessHref(domain: string) {
  return domain.startsWith("http://") || domain.startsWith("https://") ? domain : `https://${domain}`;
}

function topApplications(applications: Application[]) {
  return applications.slice().sort((a, b) => {
    const score = (value: string) => (value === "failed" ? 0 : value === "pending" ? 1 : value === "degraded" ? 2 : 3);
    return score(a.status) - score(b.status) || a.stack.localeCompare(b.stack);
  }).slice(0, 5);
}

function severityLabel(issue: DashboardIssue, lang: Lang) {
  const value = (issue.severity || "info").toLowerCase();
  if (lang === "zh") {
    if (value === "critical") return "严重";
    if (value === "warning") return "警告";
    return "信息";
  }
  return value;
}

function nodePressure(node: DashboardNode) {
  const metrics = node.metrics || {};
  return Math.max(metrics.cpuPercent ?? metrics.loadPercent ?? 0, metrics.memoryUsedPercent ?? 0);
}

function percent(value?: number) {
  return typeof value === "number" ? `${Math.round(value)}%` : "-";
}

function boundedPercent(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
}

function healthLabel(score: number, lang: Lang) {
  if (score >= 85) return lang === "zh" ? "健康" : "Healthy";
  if (score >= 65) return lang === "zh" ? "有风险" : "At risk";
  return lang === "zh" ? "需处理" : "Needs work";
}

function readinessLabel(lang: Lang, ready?: boolean) {
  return ready ? (lang === "zh" ? "健康" : "Healthy") : (lang === "zh" ? "缺失" : "Missing");
}

export function OverviewPage({
  lang,
  payload,
  vm,
  onNavigate,
  onSelectNode,
}: {
  lang: Lang;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onNavigate: (page: NavPage) => void;
  onSelectNode: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const visibleApps = topApplications(vm.applications);
  const issueTotal = vm.issueCounts.critical + vm.issueCounts.warning + vm.issueCounts.info;
  const readiness = payload.readiness || {};
  const nodeCards = vm.nodes.slice().sort((a, b) => nodePressure(b) - nodePressure(a)).slice(0, 4);

  const { dismiss, clear, isDismissed } = useDismissedIssues();
  const visibleIssues = useMemo(() => vm.issues.filter((issue) => !isDismissed(issueKey(issue))), [vm.issues, isDismissed]);
  const hiddenCount = vm.issues.length - visibleIssues.length;

  return (
    <>
      <section className="ops-hero" aria-labelledby="overview-title">
        <div className="ops-hero-copy">
          <p className="eyebrow">{t(lang, "controlPlane")}</p>
          <h1 id="overview-title">{zh ? "集群运行中枢" : "Cluster operations hub"}</h1>
          <p>{zh ? "实时状态与风险总览" : "Real-time status and risk overview"}</p>
          <button type="button" className="ops-hero-action" onClick={() => onNavigate("deploy")}>
            <Plus size={16} aria-hidden="true" />
            {t(lang, "createApplication")}
          </button>
        </div>
        <div className="ops-hero-status">
          <div className="ops-hero-score" aria-label={zh ? "健康分" : "Health score"}>
            <div className="score-ring" style={{ "--score": `${vm.healthScore}%` } as CSSProperties}>
              <strong>{vm.healthScore}</strong>
            </div>
            <span>
              {zh ? "健康分" : "Health score"}
              <b>{healthLabel(vm.healthScore, lang)}</b>
            </span>
          </div>
          <div className="ops-hero-metrics">
            <span><strong>{issueTotal}</strong><small>{zh ? "待处理" : "Open issues"}</small></span>
            <span><strong>{vm.activeNodes}/{vm.nodes.length}</strong><small>{t(lang, "nodes")}</small></span>
            <span><strong>{vm.applications.length}</strong><small>{t(lang, "applications")}</small></span>
          </div>
        </div>
      </section>

      <section className="platform-strip" aria-label={zh ? "平台组件状态" : "Platform components"}>
        <span className="platform-strip-title">{zh ? "平台组件" : "Platform"}</span>
        <span className={`platform-item ${readiness.dns?.ready ? "" : "bad"}`} title={zh ? "域名解析（Cloudflare DNS）" : "DNS records (Cloudflare)"}>
          <i aria-hidden="true" />
          <b>DNS</b>
          <small>{readinessLabel(lang, readiness.dns?.ready)}{readiness.dns?.zone ? ` · ${readiness.dns.zone}` : ""}</small>
        </span>
        <span className={`platform-item ${readiness.nomad?.available ? "" : "bad"}`} title={zh ? "调度器（Nomad 集群）" : "Scheduler (Nomad cluster)"}>
          <i aria-hidden="true" />
          <b>{zh ? "调度器" : "Scheduler"}</b>
          <small>{readinessLabel(lang, readiness.nomad?.available)}{readiness.nomad?.leader ? ` · ${readiness.nomad.leader}` : ""}</small>
        </span>
      </section>

      <section className="overview-workbench" aria-label={zh ? "运维工作台" : "Operations workbench"}>
        <article className="panel overview-apps-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{t(lang, "applications")}</p>
              <h2>{zh ? "应用" : "Applications"}</h2>
            </div>
            <button type="button" className="ghost text-link-button" onClick={() => onNavigate("applications")}>
              {zh ? "查看全部" : "View all"}
              <ArrowRight size={15} aria-hidden="true" />
            </button>
          </div>
          <div className="overview-app-table-wrap">
            <table className="overview-app-table">
              <thead>
                <tr>
                  <th>{t(lang, "application")}</th>
                  <th>{t(lang, "services")}</th>
                  <th>{t(lang, "status")}</th>
                  <th>{t(lang, "region")}</th>
                  <th>{t(lang, "replicas")}</th>
                  <th>{t(lang, "accessAddress")}</th>
                </tr>
              </thead>
              <tbody>
                {visibleApps.length ? visibleApps.map((app) => (
                  <tr key={app.stack}>
                    <td><PrimaryCell title={app.stack} meta={app.services[0]?.image?.split(":").pop()} /></td>
                    <td>{app.services.length}</td>
                    <td><StatePill label={localizeState(lang, app.status)} value={app.status} /></td>
                    <td>{app.regions.join(", ") || "-"}</td>
                    <td>{app.running}/{app.desired}</td>
                    <td>
                      {app.domains.length ? (
                        <a href={accessHref(app.domains[0])} target="_blank" rel="noreferrer">
                          <CodeCell value={app.domains[0]} />
                        </a>
                      ) : (
                        <Badge value={t(lang, "internalOnly")} />
                      )}
                    </td>
                  </tr>
                )) : (
                  <tr><td colSpan={6}>{t(lang, "noApplications")}</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <small className="panel-footnote">{zh ? `显示 ${visibleApps.length}/${vm.applications.length} 个应用` : `Showing ${visibleApps.length} of ${vm.applications.length} applications`}</small>
        </article>

        <aside className="overview-side-stack">
          <article className="panel risk-queue-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">{zh ? "风险队列" : "Risk queue"}</p>
                <h2>{zh ? "需要关注" : "Needs attention"}</h2>
              </div>
              <div className="risk-badges">
                <Badge value={`${vm.issueCounts.critical} critical`} />
                <Badge value={`${vm.issueCounts.warning} warning`} />
              </div>
            </div>
            <div className="risk-queue-list">
              {visibleIssues.length ? visibleIssues.slice(0, 5).map((issue, index) => {
                const key = issueKey(issue);
                return (
                  <div className={`risk-queue-row ${issue.severity || "info"}`} key={key}>
                    <i aria-hidden="true" />
                    <div>
                      <strong>{issue.message || "-"}</strong>
                      <small>{severityLabel(issue, lang)} · {[issue.kind, issue.target].filter(Boolean).join(" / ") || "-"}</small>
                    </div>
                    <em>{index ? `${index * 5 + 2}m` : "now"}</em>
                    <button
                      type="button"
                      className="risk-queue-dismiss"
                      title={zh ? "标记为已处理" : "Mark as handled"}
                      aria-label={zh ? "标记为已处理" : "Mark as handled"}
                      onClick={() => dismiss(key)}
                    >
                      <X size={14} aria-hidden="true" />
                    </button>
                  </div>
                );
              }) : (
                <div className="empty-inline">{zh ? "暂无风险" : "No open risk"}</div>
              )}
            </div>
            {hiddenCount ? (
              <button type="button" className="ghost text-link-button risk-queue-restore" onClick={clear}>
                {zh ? `已隐藏 ${hiddenCount} 条 · 全部恢复` : `${hiddenCount} hidden · restore all`}
              </button>
            ) : null}
          </article>

          <article className="panel overview-node-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">{zh ? "节点舰队" : "Node fleet"}</p>
                <h2>{vm.nodes.length} {t(lang, "nodes")}</h2>
              </div>
              <button type="button" className="ghost text-link-button" onClick={() => onNavigate("nodes")}>
                {zh ? "查看节点" : "View fleet"}
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            </div>
            <div className="overview-node-grid">
              {nodeCards.map((node) => (
                <button className="overview-node-card" type="button" key={node.name || "-"} onClick={() => onSelectNode(node)}>
                  <span className="overview-node-title"><i aria-hidden="true" />{node.name || "-"}</span>
                  <small>{[node.role, node.region].filter(Boolean).join(" / ") || "-"}</small>
                  <div className="overview-node-metrics">
                    <span>
                      <em>CPU</em>
                      <b>{percent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}</b>
                      <i style={{ width: `${boundedPercent(node.metrics?.cpuPercent ?? node.metrics?.loadPercent)}%` }} aria-hidden="true" />
                    </span>
                    <span>
                      <em>MEM</em>
                      <b>{percent(node.metrics?.memoryUsedPercent)}</b>
                      <i style={{ width: `${boundedPercent(node.metrics?.memoryUsedPercent)}%` }} aria-hidden="true" />
                    </span>
                  </div>
                  <StatePill label={localizeState(lang, node.state)} value={node.state} />
                </button>
              ))}
            </div>
          </article>
        </aside>
      </section>

      <section className="overview-action-dock" aria-label={zh ? "快捷入口" : "Shortcuts"}>
        <button type="button" onClick={() => onNavigate("nodes")}>
          <GitBranch size={20} aria-hidden="true" />
          <span>{t(lang, "trafficPaths")}</span>
          <small>{zh ? "节点、拓扑与流量路径" : "Nodes, topology, and traffic paths"}</small>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("observability")}>
          <TerminalSquare size={20} aria-hidden="true" />
          <span>{zh ? "日志" : "Logs"}</span>
          <small>{zh ? "搜索并 tail 日志" : "Search and tail logs"}</small>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("storage")}>
          <HardDrive size={20} aria-hidden="true" />
          <span>{t(lang, "storage")}</span>
          <small>{zh ? "卷和分配关系" : "Volumes and allocations"}</small>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("applications")}>
          <Server size={20} aria-hidden="true" />
          <span>{t(lang, "applications")}</span>
          <small>{zh ? "浏览全部应用" : "Browse all applications"}</small>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
      </section>
    </>
  );
}
