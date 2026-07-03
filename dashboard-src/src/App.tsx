import { useEffect, useMemo, useState } from "react";
import { Activity, Boxes, GitBranch, HardDrive, KeyRound, LayoutDashboard, Plus, ServerCog } from "lucide-react";
import { ErrorBanner } from "./components/ErrorBanner";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, serviceToDraft } from "./components/applicationModel";
import { LoginPanel } from "./components/LoginPanel";
import { TerminalDrawer } from "./components/TerminalDrawer";
import { Topbar } from "./components/Topbar";
import { createDashboardViewModel, type NavPage, type PageId } from "./dashboardViewModel";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { DeployPage, type DeployUpdateContext } from "./pages/DeployPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { NodesPage } from "./pages/NodesPage";
import { ObservabilityPage } from "./pages/ObservabilityPage";
import { OverviewPage } from "./pages/OverviewPage";
import { StoragePage } from "./pages/StoragePage";
import { TopologyPage } from "./pages/TopologyPage";
import { t } from "./i18n";
import type { DashboardNode, DashboardService, Lang, SyncStatus } from "./types";
import { useDashboardData } from "./useDashboardData";
import lumaLogoMark from "./assets/luma-logo-mark.png";

const LANG_KEY = "luma.dashboard.lang";

type DetailState =
  | { kind: "node"; title: string; items: Record<string, string | number | boolean | undefined> }
  | { kind: "service"; title: string; items: Record<string, string | number | boolean | undefined> }
  | null;

export function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [activePage, setActivePage] = useState<PageId>("overview");
  const [deployTemplateLanding, setDeployTemplateLanding] = useState(true);
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [detail, setDetail] = useState<DetailState>(null);
  const [terminalNode, setTerminalNode] = useState<DashboardNode | null>(null);
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();
  const vm = useMemo(() => createDashboardViewModel(payload), [payload]);

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    localStorage.removeItem("luma.dashboard.theme");
  }, []);

  const setLang = (nextLang: Lang) => {
    setLangState(nextLang);
    localStorage.setItem(LANG_KEY, nextLang);
  };

  const navigate = (page: NavPage) => {
    setUpdateRequest(null);
    setActivePage(page);
    if (page === "deploy") setDeployTemplateLanding(true);
  };

  const openUpdatePage = (request: ApplicationUpdateRequest) => {
    setUpdateRequest(request);
    setDeployTemplateLanding(false);
    setActivePage("update");
  };

  const closeUpdatePage = () => {
    setUpdateRequest(null);
    setActivePage("applications");
  };

  const updateContext = useMemo<DeployUpdateContext | null>(() => {
    if (!updateRequest) return null;
    const { app, deploymentConfig } = updateRequest;
    if (deploymentConfig?.manifest) {
      const isCompose = deploymentConfig.kind === "compose" || Boolean(deploymentConfig.composeContent);
      return {
        ...updateRequest,
        deployMode: isCompose ? "compose" : "service",
        serviceDraft: isCompose ? undefined : serviceToDraft(app),
        composeDraft: isCompose ? appToComposeDraft(app) : undefined,
      };
    }
    if (app.services.length <= 1) {
      return { ...updateRequest, deployMode: "service", serviceDraft: serviceToDraft(app), composeDraft: undefined };
    }
    return { ...updateRequest, deployMode: "compose", serviceDraft: undefined, composeDraft: appToComposeDraft(app) };
  }, [updateRequest]);

  const updateContextNode = updateContext ? (
    <section className="application-update-context">
      <div className="application-update-context-title">
        <strong>{lang === "zh" ? "当前应用" : "Current application"}</strong>
        <span>
          {updateContext.deploymentConfig?.manifest
            ? (lang === "zh" ? "已读取 Luma Control 登记的部署配置，提交后会按同名应用更新。" : "Loaded the deployment config registered in Luma Control. Submitting updates the application with the same name.")
            : (lang === "zh" ? "下面的配置从现有 stack 带入，提交后会按同名应用更新。" : "The config below is inferred from the current stack. Submitting updates the application with the same name.")}
        </span>
        {updateRequest?.configWarning ? <span>{updateRequest.configWarning}</span> : null}
      </div>
      <div className="application-update-context-grid">
        <article><span>Stack</span><strong>{updateRequest?.app.stack}</strong></article>
        <article><span>{lang === "zh" ? "服务" : "Services"}</span><strong>{updateRequest?.app.services.length}</strong></article>
        <article><span>{t(lang, "accessAddress")}</span><strong>{updateRequest?.app.domains.join(", ") || t(lang, "internalOnly")}</strong></article>
        <article><span>{t(lang, "replicas")}</span><strong>{updateRequest?.app.running}/{updateRequest?.app.desired}</strong></article>
      </div>
    </section>
  ) : null;

  const openNodeDetail = (node: DashboardNode) => {
    setDetail({
      kind: "node",
      title: node.name || "-",
      items: {
        displayName: node.displayName,
        region: node.region,
        role: node.role,
        state: node.state,
        availability: node.availability,
        leader: node.leader,
        agent: [node.agentStatus, node.agentOs, node.terminalStatus ? `terminal: ${node.terminalStatus}` : ""].filter(Boolean).join(" / "),
        cpu: node.metrics?.cpuPercent ?? node.metrics?.loadPercent,
        load1: node.metrics?.load1,
        memory: node.metrics?.memoryUsedPercent,
        memoryTotal: node.metrics?.memoryTotalBytes,
        cpuCapacity: node.capacity?.cpus,
        memoryCapacity: node.capacity?.memoryBytes,
      },
    });
  };

  const openServiceDetail = (service: DashboardService) => {
    setDetail({
      kind: "service",
      title: service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-",
      items: {
        fullName: service.fullName,
        region: service.region,
        exposure: service.exposure,
        image: service.image,
        replicas: `${service.running ?? 0}/${service.desired ?? 0}`,
        pending: service.pending,
        failed: service.failed,
        health: service.health,
        nodes: (service.nodes || []).join(", "),
        limits: [
          service.resources?.limits?.cpus ? `${service.resources.limits.cpus} CPU` : "",
          service.resources?.limits?.memoryBytes ? `${service.resources.limits.memoryBytes} bytes` : "",
        ].filter(Boolean).join(" / "),
        reservations: [
          service.resources?.reservations?.cpus ? `${service.resources.reservations.cpus} CPU` : "",
          service.resources?.reservations?.memoryBytes ? `${service.resources.reservations.memoryBytes} bytes` : "",
        ].filter(Boolean).join(" / "),
        tasks: (service.tasks || []).map((task) => `${task.node || "-"}:${task.state || "-"}`).join(", "),
        storage: (service.storage || []).map((item) => `${item.name || "-"}:${item.kind || "unmanaged"}`).join(", "),
        diagnostics: (service.diagnostics || []).join("; "),
      },
    });
  };

  const visibleStatus: SyncStatus = token ? syncStatus : "notConnected";
  const activeNavPage: NavPage = activePage === "update" ? "applications" : activePage;
  const navItems = [
    {
      id: "overview" as const,
      icon: LayoutDashboard,
      label: lang === "zh" ? "总览" : "Overview",
      value: vm.issueCounts.critical + vm.issueCounts.warning || vm.healthyServices,
      detail: lang === "zh" ? `${vm.healthyServices}/${vm.services.length} 服务正常` : `${vm.healthyServices}/${vm.services.length} services ok`,
    },
    {
      id: "applications" as const,
      icon: Boxes,
      label: lang === "zh" ? "应用" : "Apps",
      value: vm.applications.length,
      detail: lang === "zh" ? "生命周期 · 回滚" : "Lifecycle · rollback",
    },
    {
      id: "deploy" as const,
      icon: Plus,
      label: lang === "zh" ? "创建" : "Create",
      value: vm.templateCount,
      detail: lang === "zh" ? "模板、表单、YAML" : "Templates, form, YAML",
    },
    {
      id: "topology" as const,
      icon: GitBranch,
      label: lang === "zh" ? "拓扑" : "Topology",
      value: vm.trafficPaths.length,
      detail: lang === "zh" ? `${vm.nodes.length} 节点 · ${vm.trafficPaths.length} 路径` : `${vm.nodes.length} nodes · ${vm.trafficPaths.length} paths`,
    },
    {
      id: "nodes" as const,
      icon: ServerCog,
      label: lang === "zh" ? "节点" : "Fleet",
      value: vm.nodes.length,
      detail: lang === "zh" ? `${vm.activeNodes}/${vm.nodes.length} ready · agent` : `${vm.activeNodes}/${vm.nodes.length} ready · agent`,
    },
    {
      id: "observability" as const,
      icon: Activity,
      label: lang === "zh" ? "观察" : "Observe",
      value: vm.metricNodes,
      detail: lang === "zh" ? "节点资源 · 日志" : "Resources · logs",
    },
    {
      id: "storage" as const,
      icon: HardDrive,
      label: lang === "zh" ? "存储" : "Storage",
      value: vm.storageVolumes.length + vm.storageClasses.length,
      detail: lang === "zh" ? `${vm.storageClasses.length} 类 · ${vm.storageVolumes.length} 卷` : `${vm.storageClasses.length} classes · ${vm.storageVolumes.length} volumes`,
    },
    {
      id: "credentials" as const,
      icon: KeyRound,
      label: lang === "zh" ? "凭据" : "Credentials",
      value: vm.storageClasses.length,
      detail: lang === "zh" ? "Secret · Registry" : "Secrets · registry",
    },
  ];

  return (
    <div className={`dashboard-shell page-${activeNavPage}`}>
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true">
            <img src={lumaLogoMark} alt="" />
          </div>
          <div className="sidebar-title">
            <span>Luma</span>
            <strong>{t(lang, "title")}</strong>
          </div>
        </div>
        <nav aria-label="Dashboard">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={activeNavPage === item.id ? "nav-item active" : "nav-item"}
                type="button"
                key={item.id}
                onClick={() => navigate(item.id)}
              >
                <Icon size={17} aria-hidden="true" />
                <span>
                  <b>{item.label}</b>
                  <small>{item.detail}</small>
                </span>
                <strong>{item.value}</strong>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-status" aria-label={lang === "zh" ? "当前运行状态" : "Current runtime status"}>
          <span>{lang === "zh" ? "健康分" : "Health score"}</span>
          <strong>{vm.healthScore}%</strong>
          <small>{vm.activeNodes}/{vm.nodes.length || 0} {lang === "zh" ? "节点在线" : "nodes online"}</small>
        </div>
      </aside>

      <main className="workspace">
        <div className="topbar-wrapper">
          <Topbar
            clusterId={vm.clusterId}
            lang={lang}
            lastUpdated={lastUpdated}
            onLangChange={setLang}
            onRefresh={() => void loadDashboard()}
            onSignOut={signOut}
            syncStatus={visibleStatus}
          />
        </div>

        {!token ? (
          <div className="login-panel-container">
            <LoginPanel lang={lang} onSubmit={setToken} />
          </div>
        ) : (
          <>
            <ErrorBanner errors={errors} />
            {payload ? (
              activePage === "overview" ? (
                <OverviewPage
                  lang={lang}
                  payload={payload}
                  vm={vm}
                  onNavigate={navigate}
                  onSelectNode={openNodeDetail}
                />
              ) : activePage === "applications" ? (
                <ApplicationsPage
                  lang={lang}
                  token={token}
                  payload={payload}
                  onRefresh={loadDashboard}
                  onCreateApplication={() => navigate("deploy")}
                  onUpdateApplication={openUpdatePage}
                />
              ) : activePage === "deploy" || activePage === "update" ? (
                <DeployPage
                  lang={lang}
                  token={token}
                  payload={payload}
                  vm={vm}
                  updateContext={updateContext}
                  updateContextNode={updateContextNode}
                  deployTemplateLanding={deployTemplateLanding}
                  onRefresh={loadDashboard}
                  onCloseUpdate={closeUpdatePage}
                  onTemplateLandingChange={setDeployTemplateLanding}
                />
              ) : activePage === "topology" ? (
                <TopologyPage lang={lang} token={token} vm={vm} onRefresh={loadDashboard} />
              ) : activePage === "nodes" ? (
                <NodesPage
                  lang={lang}
                  vm={vm}
                  onSelectNode={openNodeDetail}
                  onTerminal={setTerminalNode}
                />
              ) : activePage === "observability" ? (
                <ObservabilityPage lang={lang} token={token} vm={vm} />
              ) : activePage === "credentials" ? (
                <CredentialsPage lang={lang} token={token} vm={vm} />
              ) : (
                <StoragePage lang={lang} vm={vm} />
              )
            ) : (
              <section className="empty-state">
                <p>{t(lang, visibleStatus)}</p>
              </section>
            )}
          </>
        )}
      </main>

      {detail ? (
        <div className="detail-backdrop" onClick={() => setDetail(null)}>
          <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <p className="eyebrow">{t(lang, "details")}</p>
                <h2>{detail.title}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setDetail(null)}>
                {t(lang, "close")}
              </button>
            </header>
            <dl>
              {Object.entries(detail.items).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{String(value || "-")}</dd>
                </div>
              ))}
            </dl>
          </aside>
        </div>
      ) : null}
      {terminalNode ? (
        <TerminalDrawer lang={lang} node={terminalNode} token={token} onClose={() => setTerminalNode(null)} />
      ) : null}
    </div>
  );
}
