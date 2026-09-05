import { ArrowRight, Plus, X } from "lucide-react";
import { useMemo, type CSSProperties } from "react";
import { Badge, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import { formatImageIdentity } from "../format";
import { localizeState, t } from "../i18n";
import { applicationEndpoints } from "../components/applicationEndpoints";
import type { Application } from "../components/applicationModel";
import type { DashboardIssue, DashboardNode, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { issueKey, useDismissedIssues } from "../useDismissedIssues";
import { PageHeader } from "./PageHeader";

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

function pressureClass(value?: number) {
  const n = boundedPercent(value);
  if (n >= 85) return "pressure-high";
  if (n >= 70) return "pressure-warn";
  return "";
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
  onNavigate: (page: NavPage, opts?: { selectApp?: string }) => void;
  onSelectNode: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const visibleApps = topApplications(vm.applications);
  const issueTotal = vm.issueCounts.critical + vm.issueCounts.warning + vm.issueCounts.info;
  const readiness = payload.readiness || {};
  const nodeCards = vm.nodes.slice().sort((a, b) => nodePressure(b) - nodePressure(a)).slice(0, 4);

  const { dismiss, clear, isDismissed } = useDismissedIssues(payload.cluster?.id || "", vm.issues);
  const visibleIssues = useMemo(() => vm.issues.filter((issue) => !isDismissed(issueKey(issue))), [vm.issues, isDismissed]);
  const hiddenCount = vm.issues.length - visibleIssues.length;

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: t(lang, "controlPlane"),
          title: zh ? "集群概览" : "Cluster overview",
          description: zh ? "应用运行状态、当前风险与节点容量" : "Application runtime, current risks, and node capacity",
          metrics: [
            { label: zh ? "待处理" : "Open issues", value: issueTotal },
            { label: t(lang, "nodes"), value: `${vm.activeNodes}/${vm.nodes.length}` },
            { label: zh ? "异常应用" : "Affected apps", value: vm.applications.filter((app) => !["healthy", "running"].includes(app.status)).length },
          ],
          action: (
            <button type="button" className="primary page-toolbar-cta" onClick={() => onNavigate("deploy")}>
              <Plus size={16} aria-hidden="true" />
              {t(lang, "createApplication")}
            </button>
          ),
        }}
      />

      <section className="platform-strip" aria-label={zh ? "平台组件状态" : "Platform components"}>
        <span className="platform-strip-title">{zh ? "平台组件" : "Platform"}</span>
        <span className={`platform-item ${readiness.dns?.ready === undefined ? "unknown" : readiness.dns.ready ? "" : "bad"}`} title={zh ? "仅检查 Cloudflare 凭据和区域配置，尚未验证 DNS 解析或公网可达性" : "Checks Cloudflare credentials and zone configuration only; DNS resolution and public reachability are unverified"}>
          <i aria-hidden="true" />
          <b>DNS</b>
          <small>{readiness.dns?.ready === undefined ? (zh ? "未检查" : "Not checked") : readiness.dns.ready ? (zh ? "配置就绪 · 解析未验证" : "Configured · resolution unverified") : (zh ? "配置不完整" : "Configuration incomplete")}{readiness.dns?.zone ? ` · ${readiness.dns.zone}` : ""}</small>
        </span>
        <span className={`platform-item ${readiness.nomad?.available === undefined ? "unknown" : readiness.nomad.available ? "" : "bad"}`} title={zh ? "调度器（Nomad 集群）" : "Scheduler (Nomad cluster)"}>
          <i aria-hidden="true" />
          <b>{zh ? "调度器" : "Scheduler"}</b>
          <small>{readiness.nomad?.available === undefined ? (zh ? "未检查" : "Not checked") : readiness.nomad.available ? (zh ? "可连接" : "Reachable") : (zh ? "无法连接" : "Unreachable")}{readiness.nomad?.leader ? ` · ${readiness.nomad.leader}` : ""}</small>
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
                  <th className="overview-app-name">{t(lang, "application")}</th>
                  <th className="overview-app-secondary overview-app-count">{t(lang, "services")}</th>
                  <th className="overview-app-status">{t(lang, "status")}</th>
                  <th className="overview-app-region">{t(lang, "region")}</th>
                  <th className="overview-app-replicas">{t(lang, "replicas")}</th>
                  <th className="overview-app-secondary overview-app-address">{t(lang, "accessAddress")}</th>
                </tr>
              </thead>
              <tbody>
                {visibleApps.length ? visibleApps.map((app) => {
                  const openApp = () => onNavigate("applications", { selectApp: app.stack });
                  const endpoint = applicationEndpoints(app.services)[0];
                  return (
                  <tr
                    aria-label={`${t(lang, "details")}: ${app.stack}`}
                    key={app.stack}
                    onClick={openApp}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openApp();
                      }
                    }}
                    role="button"
                    tabIndex={0}
                  >
                    <td title={[app.stack, app.services[0]?.image].filter(Boolean).join(" · ")}><PrimaryCell title={app.stack} meta={formatImageIdentity(app.services[0]?.image)} /></td>
                    <td className="overview-app-secondary">{app.services.length}</td>
                    <td><StatePill label={localizeState(lang, app.status)} value={app.status} /></td>
                    <td className="overview-app-region" title={app.regions.join(", ")}>{app.regions.join(", ") || "-"}</td>
                    <td>{app.running}/{app.desired}</td>
                    <td className="overview-app-secondary" onClick={(e) => e.stopPropagation()}>
                      {endpoint ? (
                        endpoint.href ? <a href={endpoint.href} target="_blank" rel="noreferrer" onKeyDown={(event) => event.stopPropagation()}>
                          <CodeCell value={endpoint.address} />
                        </a> : <span className="application-tcp-address"><Badge value="TCP" /><CodeCell value={endpoint.address} /></span>
                      ) : (
                        <Badge value={t(lang, "internalOnly")} />
                      )}
                    </td>
                  </tr>
                  );
                }) : (
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
                <Badge value={zh ? `${vm.issueCounts.critical} 严重` : `${vm.issueCounts.critical} critical`} />
                <Badge value={zh ? `${vm.issueCounts.warning} 警告` : `${vm.issueCounts.warning} warning`} />
              </div>
            </div>
            <div className="risk-queue-list">
              {visibleIssues.length ? visibleIssues.slice(0, 5).map((issue) => {
                const key = issueKey(issue);
                const node = issue.kind === "agent" || issue.kind?.startsWith("node-") ? vm.nodes.find((item) => item.name === issue.target) : undefined;
                const app = issue.kind === "deployment" || issue.kind?.startsWith("service-")
                  ? vm.applications.find((item) => item.services.some((service) => (service.fullName || service.name) === issue.target))
                  : undefined;
                return (
                  <div className={`risk-queue-row ${issue.severity || "info"}`} key={key}>
                    <i aria-hidden="true" />
                    <div>
                      <strong>{issue.message || "-"}</strong>
                      <small>{severityLabel(issue, lang)} · {issue.target || "-"}</small>
                      {node || app ? <button type="button" className="risk-queue-details" onClick={() => {
                        if (node) onSelectNode(node);
                        else if (app) onNavigate("applications", { selectApp: app.stack });
                      }}>{zh ? "查看详情" : "View details"}<ArrowRight size={12} aria-hidden="true" /></button> : null}
                    </div>
                    <button
                      type="button"
                      className="risk-queue-dismiss"
                      title={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}
                      aria-label={zh ? "仅在此浏览器隐藏 1 小时" : "Hide in this browser for 1 hour"}
                      onClick={() => dismiss(key)}
                    >
                      <X size={14} aria-hidden="true" />
                    </button>
                  </div>
                );
              }) : (
                <div className="empty-inline">{hiddenCount ? (zh ? "当前风险已在此浏览器临时隐藏" : "Current risks are temporarily hidden in this browser") : (zh ? "暂无风险" : "No open risk")}</div>
              )}
            </div>
            {hiddenCount ? (
              <button type="button" className="ghost text-link-button risk-queue-restore" onClick={clear}>
                {zh ? `本机临时隐藏 ${hiddenCount} 条 · 全部恢复` : `${hiddenCount} hidden locally · restore all`}
              </button>
            ) : null}
          </article>

          <article className="panel overview-node-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">{zh ? "节点容量" : "Node capacity"}</p>
                <h2>{vm.nodes.length} {t(lang, "nodes")}</h2>
              </div>
              <button type="button" className="ghost text-link-button" onClick={() => onNavigate("nodes")}>
                {zh ? "查看节点" : "View fleet"}
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            </div>
            <div className="overview-node-grid">
              {nodeCards.map((node) => {
                const cpu = node.metrics?.cpuPercent ?? node.metrics?.loadPercent;
                const mem = node.metrics?.memoryUsedPercent;
                return (
                  <button className="overview-node-card" type="button" key={node.name || "-"} onClick={() => onSelectNode(node)}>
                    <span className="overview-node-title"><i aria-hidden="true" />{node.name || "-"}</span>
                    <small>{[node.role, node.region].filter(Boolean).join(" / ") || "-"}</small>
                    <div className="overview-node-metrics">
                      <span>
                        <em>CPU</em>
                        <b>{percent(cpu)}</b>
                        <i className={pressureClass(cpu)} style={{ width: `${boundedPercent(cpu)}%` } as CSSProperties} aria-hidden="true" />
                      </span>
                      <span>
                        <em>MEM</em>
                        <b>{percent(mem)}</b>
                        <i className={pressureClass(mem)} style={{ width: `${boundedPercent(mem)}%` } as CSSProperties} aria-hidden="true" />
                      </span>
                    </div>
                    <StatePill label={localizeState(lang, node.state)} value={node.state} />
                  </button>
                );
              })}
            </div>
          </article>
        </aside>
      </section>
    </>
  );
}
