import { ArrowRight, GitBranch, HardDrive, ListChecks, Plus, Server, TerminalSquare, XCircle } from "lucide-react";
import type { CSSProperties } from "react";
import { Badge, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import { localizeState, t } from "../i18n";
import type { Application } from "../components/applicationModel";
import type { DashboardIssue, DashboardNode, DashboardOperation, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";

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

function operationTarget(operation: Partial<DashboardOperation>) {
  return operation.target?.name || operation.target?.slug || operation.target?.repoUrl || operation.target?.sourceName || operation.id || "-";
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

  return (
    <>
      <section className="ops-hero" aria-labelledby="overview-title">
        <div className="ops-hero-copy">
          <p className="eyebrow">{t(lang, "controlPlane")}</p>
          <h1 id="overview-title">{zh ? "集群运行中枢" : "Cluster operations hub"}</h1>
          <p>{zh ? "实时状态与风险总览" : "Real-time status and risk overview"}</p>
        </div>
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
        <button type="button" className="ops-hero-action" onClick={() => onNavigate("deploy")}>
          <Plus size={17} aria-hidden="true" />
          {t(lang, "createApplication")}
        </button>
      </section>

      <section className="readiness-band" aria-label={zh ? "控制面就绪状态" : "Control plane readiness"}>
        <article>
          <span>DNS</span>
          <strong className={readiness.dns?.ready ? "ok" : "bad"}>{readinessLabel(lang, readiness.dns?.ready)}</strong>
          <small>{[readiness.dns?.provider, readiness.dns?.zone, readiness.dns?.target].filter(Boolean).join(" / ") || "-"}</small>
        </article>
        <article>
          <span>Nomad</span>
          <strong className={readiness.nomad?.available ? "ok" : "bad"}>{readinessLabel(lang, readiness.nomad?.available)}</strong>
          <small>{readiness.nomad?.leader ? `leader ${readiness.nomad.leader}` : readiness.nomad?.engine || "-"}</small>
        </article>
        <article>
          <span>{zh ? "控制面" : "Control plane"}</span>
          <strong className="ok">Nomad</strong>
          <small>{zh ? "控制面直接提交 Nomad job" : "Control submits Nomad jobs directly"}</small>
        </article>
      </section>

      <section className="overview-deployment-band" aria-label={zh ? "部署流水概览" : "Deployment flow overview"}>
        <button type="button" onClick={() => onNavigate("deployments")}>
          <ListChecks size={18} aria-hidden="true" />
          <span>
            <strong>{vm.operationsRunning.length}</strong>
            <small>{zh ? "正在部署" : "running deployments"}</small>
          </span>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("deployments")}>
          <XCircle size={18} aria-hidden="true" />
          <span>
            <strong>{vm.operationsFailed.length}</strong>
            <small>{zh ? "最近失败部署" : "recent failed deployments"}</small>
          </span>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("deployments")}>
          <TerminalSquare size={18} aria-hidden="true" />
          <span>
            <strong>{operationTarget(vm.operationsRecent[0] || {})}</strong>
            <small>{vm.operationsRecent[0]?.phase || (zh ? "暂无部署流水" : "No deployment flow yet")}</small>
          </span>
          <ArrowRight size={16} aria-hidden="true" />
        </button>
      </section>

      <section className="overview-workbench" aria-label={zh ? "运维工作台" : "Operations workbench"}>
        <article className="panel overview-apps-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{t(lang, "applications")}</p>
              <h2>{zh ? "关键应用" : "Key applications"}</h2>
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
              {vm.issues.length ? vm.issues.slice(0, 5).map((issue, index) => (
                <div className={`risk-queue-row ${issue.severity || "info"}`} key={`${issue.kind || "issue"}-${index}`}>
                  <span aria-hidden="true">!</span>
                  <div>
                    <b>{severityLabel(issue, lang)}</b>
                    <strong>{issue.message || "-"}</strong>
                    <small>{[issue.kind, issue.target].filter(Boolean).join(" / ") || "-"}</small>
                  </div>
                  <em>{index ? `${index * 5 + 2}m` : "now"}</em>
                </div>
              )) : (
                <div className="empty-inline">{zh ? "暂无风险" : "No open risk"}</div>
              )}
            </div>
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
        <button type="button" onClick={() => onNavigate("topology")}>
          <GitBranch size={20} aria-hidden="true" />
          <span>{t(lang, "trafficPaths")}</span>
          <small>{zh ? "检查路由和入口" : "Inspect routes and entrypoints"}</small>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("observability")}>
          <TerminalSquare size={20} aria-hidden="true" />
          <span>{zh ? "日志" : "Logs"}</span>
          <small>{zh ? "搜索并 tail 日志" : "Search and tail logs"}</small>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("storage")}>
          <HardDrive size={20} aria-hidden="true" />
          <span>{t(lang, "storage")}</span>
          <small>{zh ? "卷和分配关系" : "Volumes and allocations"}</small>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
        <button type="button" onClick={() => onNavigate("applications")}>
          <Server size={20} aria-hidden="true" />
          <span>{t(lang, "applications")}</span>
          <small>{zh ? "浏览全部应用" : "Browse all applications"}</small>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
      </section>
    </>
  );
}
