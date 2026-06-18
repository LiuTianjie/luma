import { GitBranch, HardDrive, LayoutDashboard, Plus, Server, TerminalSquare } from "lucide-react";
import { IssuesPanel } from "../components/IssuesPanel";
import { NodeFleetMap } from "../components/NodeFleetMap";
import { ReadinessCards } from "../components/ReadinessCards";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import { localizeState, t } from "../i18n";
import type { Application } from "../components/applicationModel";
import type { DashboardNode, DashboardPayload, Lang } from "../types";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

function accessHref(domain: string) {
  return domain.startsWith("http://") || domain.startsWith("https://") ? domain : `https://${domain}`;
}

function topApplications(applications: Application[]) {
  return applications.slice().sort((a, b) => {
    const score = (value: string) => (value === "failed" ? 0 : value === "pending" ? 1 : value === "degraded" ? 2 : 3);
    return score(a.status) - score(b.status) || a.stack.localeCompare(b.stack);
  }).slice(0, 5);
}

export function OverviewPage({
  lang,
  token,
  payload,
  vm,
  onNavigate,
  onSelectNode,
  onTerminal,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onNavigate: (page: NavPage) => void;
  onSelectNode: (node: DashboardNode) => void;
  onTerminal: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const visibleApps = topApplications(vm.applications);
  const issueTotal = vm.issueCounts.critical + vm.issueCounts.warning + vm.issueCounts.info;

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: t(lang, "controlPlane"),
          title: zh ? "集群运行中枢" : "Cluster operations hub",
          description: zh
            ? "先看风险和受影响对象，再进入应用、节点、路径和日志。"
            : "Start with risk and affected objects, then drill into apps, nodes, paths, and logs.",
          metrics: [
            { label: zh ? "健康分" : "Health score", value: `${vm.healthScore}%` },
            { label: zh ? "待处理" : "Open issues", value: issueTotal },
            { label: t(lang, "nodes"), value: `${vm.activeNodes}/${vm.nodes.length}` },
            { label: t(lang, "applications"), value: vm.applications.length },
          ],
          action: (
            <button type="button" onClick={() => onNavigate("deploy")}>
              <Plus size={17} aria-hidden="true" />
              {t(lang, "createApplication")}
            </button>
          ),
        }}
      />

      <ReadinessCards lang={lang} payload={payload} />

      <section className="overview-priority-grid" aria-label={zh ? "运维工作面" : "Operations work surface"}>
        <div className="overview-left-rail">
          <IssuesPanel lang={lang} issues={vm.issues} token={token} />
          <article className="panel overview-apps-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">{t(lang, "applications")}</p>
                <h2>{zh ? "关键应用" : "Key applications"}</h2>
              </div>
              <button type="button" className="ghost" onClick={() => onNavigate("applications")}>
                <LayoutDashboard size={16} aria-hidden="true" />
                {t(lang, "openStatus")}
              </button>
            </div>
            {visibleApps.length ? (
              <div className="overview-app-list">
                {visibleApps.map((app) => (
                  <article className="overview-app-row" key={app.stack}>
                    <PrimaryCell title={app.stack} meta={`${app.services.length} ${t(lang, "services")}`} />
                    <StatePill label={localizeState(lang, app.status)} value={app.status} />
                    <BadgeGroup>
                      {app.regions.map((region) => <Badge key={region} value={region} />)}
                    </BadgeGroup>
                    <Badge value={`${app.running}/${app.desired}`} />
                    <div className="overview-app-access">
                      {app.domains.length ? (
                        app.domains.slice(0, 2).map((domain) => (
                          <a href={accessHref(domain)} key={domain} target="_blank" rel="noreferrer">
                            <CodeCell value={domain} />
                          </a>
                        ))
                      ) : (
                        <Badge value={t(lang, "internalOnly")} />
                      )}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="empty-inline">{t(lang, "noApplications")}</div>
            )}
          </article>
        </div>
        <NodeFleetMap lang={lang} nodes={vm.nodes} services={vm.services} onSelect={onSelectNode} onTerminal={onTerminal} />

        <section className="overview-resource-strip" aria-label={zh ? "资源入口" : "Resource shortcuts"}>
          <button type="button" className="resource-shortcut" onClick={() => onNavigate("topology")}>
            <GitBranch size={18} aria-hidden="true" />
            <span>{t(lang, "trafficPaths")}</span>
            <strong>{vm.trafficPaths.length}</strong>
          </button>
          <button type="button" className="resource-shortcut" onClick={() => onNavigate("observability")}>
            <TerminalSquare size={18} aria-hidden="true" />
            <span>{zh ? "日志流" : "Log streams"}</span>
            <strong>{vm.services.filter((service) => service.fullName).length}</strong>
          </button>
          <button type="button" className="resource-shortcut" onClick={() => onNavigate("storage")}>
            <HardDrive size={18} aria-hidden="true" />
            <span>{t(lang, "storage")}</span>
            <strong>{vm.storageClasses.length + vm.storageVolumes.length}</strong>
          </button>
          <button type="button" className="resource-shortcut" onClick={() => onNavigate("applications")}>
            <Server size={18} aria-hidden="true" />
            <span>{t(lang, "applications")}</span>
            <strong>{vm.applications.length}</strong>
          </button>
        </section>
      </section>
    </>
  );
}
