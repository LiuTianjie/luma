import { useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, isServiceHealthy, serviceToDraft } from "./components/applicationModel";
import { IssuesPanel } from "./components/IssuesPanel";
import { LoginPanel } from "./components/LoginPanel";
import { NodeFleetMap } from "./components/NodeFleetMap";
import { NodeTopology } from "./components/NodeTopology";
import { NodesTable } from "./components/NodesTable";
import { ObservabilityPanel } from "./components/ObservabilityPanel";
import { ReadinessCards } from "./components/ReadinessCards";
import { ServicesTable } from "./components/ServicesTable";
import { StoragePanel } from "./components/StoragePanel";
import { TerminalDrawer } from "./components/TerminalDrawer";
import { Topbar } from "./components/Topbar";
import { TrafficPaths } from "./components/TrafficPaths";
import { DeployWorkspace } from "./deploy/DeployWorkspace";
import { DEPLOY_TEMPLATES } from "./deploy/templates";
import { t } from "./i18n";
import type { DashboardNode, DashboardService, Lang, SyncStatus } from "./types";
import { useDashboardData } from "./useDashboardData";
import lumaLogoMark from "./assets/luma-logo-mark.png";

const LANG_KEY = "luma.dashboard.lang";
type ActivePage = "deploy" | "status" | "topology" | "storage" | "observability" | "update";

type DetailState =
  | { kind: "node"; title: string; items: Record<string, string | number | boolean | undefined> }
  | { kind: "service"; title: string; items: Record<string, string | number | boolean | undefined> }
  | null;

type PageMetric = {
  label: string;
  value: string | number;
};

type PageHeaderMeta = {
  eyebrow: string;
  title: string;
  description: string;
  metrics: PageMetric[];
};

function PageHeader({ meta }: { meta: PageHeaderMeta }) {
  return (
    <section className="hero-strip" aria-labelledby="page-title">
      <div>
        <p className="eyebrow">{meta.eyebrow}</p>
        <h1 id="page-title">{meta.title}</h1>
        <p>{meta.description}</p>
      </div>
      <div className="hero-metrics" aria-label="Page metrics">
        {meta.metrics.map((metric) => (
          <span key={metric.label}>
            <strong>{metric.value}</strong>
            <small>{metric.label}</small>
          </span>
        ))}
      </div>
    </section>
  );
}

export function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [activePage, setActivePage] = useState<ActivePage>("status");
  const [deployTemplateLanding, setDeployTemplateLanding] = useState(true);
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [detail, setDetail] = useState<DetailState>(null);
  const [terminalNode, setTerminalNode] = useState<DashboardNode | null>(null);
  const theme = "dark";
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    document.documentElement.classList.remove("light");
    localStorage.removeItem("luma.dashboard.theme");
  }, []);

  const setLang = (nextLang: Lang) => {
    setLangState(nextLang);
    localStorage.setItem(LANG_KEY, nextLang);
  };

  const visibleStatus: SyncStatus = token ? syncStatus : "notConnected";
  const clusterId = payload?.cluster?.id || "-";
  const nodes = payload?.nodes || [];
  const services = payload?.services || [];
  const paths = payload?.trafficPaths || [];
  const issues = payload?.issues || [];
  const storageVolumes = payload?.storage?.volumes || [];
  const storageClasses = payload?.storage?.storageClasses || [];
  const storageWarnings = payload?.storage?.warnings || [];
  const activeNavPage = activePage === "update" ? "status" : activePage;
  const healthyServices = services.filter(isServiceHealthy).length;
  const activeNodes = nodes.filter((node) => (node.state || "").toLowerCase() === "ready" && (node.availability || "").toLowerCase() !== "drain").length;

  const navItems = useMemo(
    () => [
      {
        id: "status" as const,
        label: lang === "zh" ? "总览" : "Overview",
        value: services.length,
        detail: lang === "zh" ? `${healthyServices}/${services.length} 服务正常` : `${healthyServices}/${services.length} services ok`,
      },
      {
        id: "deploy" as const,
        label: lang === "zh" ? "创建" : "Create",
        value: DEPLOY_TEMPLATES.length,
        detail: lang === "zh" ? "模板、表单、YAML" : "Templates, form, YAML",
      },
      {
        id: "topology" as const,
        label: lang === "zh" ? "拓扑" : "Topology",
        value: paths.length,
        detail: lang === "zh" ? `${nodes.length} 节点 · ${paths.length} 路径` : `${nodes.length} nodes · ${paths.length} paths`,
      },
      {
        id: "observability" as const,
        label: lang === "zh" ? "观察" : "Observe",
        value: nodes.filter((node) => node.metrics?.cpuPercent || node.metrics?.memoryUsedPercent).length,
        detail: lang === "zh" ? "节点资源 · 日志" : "Resources · logs",
      },
      {
        id: "storage" as const,
        label: lang === "zh" ? "存储" : "Storage",
        value: storageVolumes.length + storageClasses.length,
        detail: lang === "zh" ? `${storageClasses.length} 类 · ${storageVolumes.length} 卷` : `${storageClasses.length} classes · ${storageVolumes.length} volumes`,
      },
    ],
    [healthyServices, lang, nodes, nodes.length, paths.length, services.length, storageClasses.length, storageVolumes.length],
  );

  const updateContext = useMemo(() => {
    if (!updateRequest) return null;
    const { app, deploymentConfig } = updateRequest;
    if (deploymentConfig?.manifest) {
      const isCompose = deploymentConfig.kind === "compose" || Boolean(deploymentConfig.composeContent);
      return {
        ...updateRequest,
        deployMode: isCompose ? "compose" as const : "service" as const,
        serviceDraft: isCompose ? undefined : serviceToDraft(app),
        composeDraft: isCompose ? appToComposeDraft(app) : undefined,
      };
    }
    if (app.services.length <= 1) {
      return { ...updateRequest, deployMode: "service" as const, serviceDraft: serviceToDraft(app), composeDraft: undefined };
    }
    return { ...updateRequest, deployMode: "compose" as const, serviceDraft: undefined, composeDraft: appToComposeDraft(app) };
  }, [updateRequest]);

  const pageMeta = useMemo<PageHeaderMeta>(() => {
    if (activePage === "deploy") {
      return {
        eyebrow: lang === "zh" ? "部署工作台" : "Deploy workspace",
        title: lang === "zh" ? "创建应用" : "Create application",
        description: lang === "zh" ? "模板、表单和 YAML 收敛在一个流程内，先校验再部署。" : "Templates, forms, and YAML stay in one flow with validation before deploy.",
        metrics: [
          { label: lang === "zh" ? "单服务" : "Service", value: DEPLOY_TEMPLATES.filter((item) => item.mode === "service").length },
          { label: "Compose", value: DEPLOY_TEMPLATES.filter((item) => item.mode === "compose").length },
          { label: "storageClass", value: storageClasses.length },
        ],
      };
    }
    if (activePage === "update" && updateContext) {
      return {
        eyebrow: lang === "zh" ? "应用更新" : "Application update",
        title: lang === "zh" ? `更新 ${updateContext.app.stack}` : `Update ${updateContext.app.stack}`,
        description: lang === "zh" ? "沿用当前应用配置作为起点，提交时按同名 stack 更新。" : "Start from the current application config and update the same stack.",
        metrics: [
          { label: "Stack", value: updateContext.app.stack },
          { label: t(lang, "services"), value: updateContext.app.services.length },
          { label: t(lang, "replicas"), value: `${updateContext.app.running}/${updateContext.app.desired}` },
        ],
      };
    }
    if (activePage === "topology") {
      return {
        eyebrow: lang === "zh" ? "拓扑视图" : "Topology",
        title: lang === "zh" ? "节点拓扑与流量路径" : "Node topology and traffic paths",
        description: lang === "zh" ? "按入口、代理、服务和节点梳理真实流向，快速定位路径断点。" : "Trace real ingress, proxy, service, and node placement to spot route breaks quickly.",
        metrics: [
          { label: t(lang, "nodes"), value: `${activeNodes}/${nodes.length}` },
          { label: t(lang, "services"), value: services.length },
          { label: t(lang, "trafficPaths"), value: paths.length },
        ],
      };
    }
    if (activePage === "storage") {
      return {
        eyebrow: lang === "zh" ? "存储状态" : "Storage",
        title: lang === "zh" ? "存储类、卷与绑定关系" : "Storage classes, volumes, and bindings",
        description: lang === "zh" ? "集中查看存储类、卷来源、节点绑定以及消费服务。" : "Review classes, volume sources, node bindings, and consuming services in one place.",
        metrics: [
          { label: "storageClass", value: storageClasses.length },
          { label: t(lang, "volume"), value: storageVolumes.length },
          { label: "Warnings", value: storageWarnings.length },
        ],
      };
    }
    if (activePage === "observability") {
      return {
        eyebrow: lang === "zh" ? "可观测性" : "Observability",
        title: lang === "zh" ? "资源趋势与实时日志" : "Resource trends and live logs",
        description: lang === "zh" ? "CPU、内存、任务状态和日志在同一工作面内联动。" : "CPU, memory, task state, and logs stay connected in one operational view.",
        metrics: [
          { label: t(lang, "nodes"), value: nodes.length },
          { label: t(lang, "services"), value: services.length },
          { label: "Streams", value: services.filter((service) => service.fullName).length },
        ],
      };
    }
    return {
      eyebrow: t(lang, "controlPlane"),
      title: lang === "zh" ? "集群、节点和应用状态" : "Cluster, node, and application status",
      description: lang === "zh" ? "总览只保留健康、问题、应用和关键表格，其他深度视图进入对应页面。" : "Overview keeps readiness, issues, applications, and critical tables while deeper views live in dedicated pages.",
      metrics: [
        { label: t(lang, "nodes"), value: nodes.length },
        { label: t(lang, "services"), value: services.length },
        { label: t(lang, "trafficPaths"), value: paths.length },
      ],
    };
  }, [activeNodes, activePage, lang, nodes.length, paths.length, services, services.length, storageClasses.length, storageVolumes.length, storageWarnings.length, updateContext]);

  const openUpdatePage = (request: ApplicationUpdateRequest) => {
    setUpdateRequest(request);
    setActivePage("update");
  };

  const closeUpdatePage = () => {
    setUpdateRequest(null);
    setActivePage("status");
  };

  const updateContextNode = updateContext ? (
    <section className="application-update-context">
      <div className="application-update-context-title">
        <strong>{lang === "zh" ? "当前应用" : "Current application"}</strong>
        <span>{updateContext.deploymentConfig?.manifest
          ? (lang === "zh" ? "已读取 Luma Control 登记的部署配置，提交后会按同名应用更新。" : "Loaded the deployment config registered in Luma Control. Submitting updates the application with the same name.")
          : (lang === "zh" ? "下面的配置从现有 stack 带入，提交后会按同名应用更新。" : "The config below is inferred from the current stack. Submitting updates the application with the same name.")}</span>
        {updateContext.configWarning ? <span>{updateContext.configWarning}</span> : null}
      </div>
      <div className="application-update-context-grid">
        <article><span>Stack</span><strong>{updateContext.app.stack}</strong></article>
        <article><span>{lang === "zh" ? "服务" : "Services"}</span><strong>{updateContext.app.services.length}</strong></article>
        <article><span>{t(lang, "accessAddress")}</span><strong>{updateContext.app.domains.join(", ") || t(lang, "internalOnly")}</strong></article>
        <article><span>{t(lang, "replicas")}</span><strong>{updateContext.app.running}/{updateContext.app.desired}</strong></article>
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

  return (
    <div className="dashboard-shell">
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
          {navItems.map((item) => (
            <button
              className={activeNavPage === item.id ? "nav-item active" : "nav-item"}
              type="button"
              key={item.id}
              onClick={() => {
                setUpdateRequest(null);
                setActivePage(item.id);
              }}
            >
              <span>
                <b>{item.label}</b>
                <small>{item.detail}</small>
              </span>
              <strong>{item.value}</strong>
            </button>
          ))}
        </nav>
      </aside>

      <main className="workspace">
        <div className="topbar-wrapper">
          <Topbar
            clusterId={clusterId}
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
              activePage === "deploy" ? (
                <>
                  {deployTemplateLanding ? <PageHeader meta={pageMeta} /> : null}
                  <DeployWorkspace
                    lang={lang}
                    token={token}
                    payload={payload}
                    onRefresh={loadDashboard}
                    onTemplateLandingChange={setDeployTemplateLanding}
                  />
                </>
              ) : activePage === "update" && updateContext ? (
                <>
                  <PageHeader meta={pageMeta} />
                  <DeployWorkspace
                    lang={lang}
                    token={token}
                    payload={payload}
                    initialMode={updateContext.deployMode}
                    initialServiceDraft={updateContext.serviceDraft}
                    initialComposeDraft={updateContext.composeDraft}
                    initialServiceYaml={updateContext.deployMode === "service" ? updateContext.deploymentConfig?.manifest : undefined}
                    initialSidecarYaml={updateContext.deployMode === "compose" ? updateContext.deploymentConfig?.manifest : undefined}
                    initialComposeYaml={updateContext.deployMode === "compose" ? updateContext.deploymentConfig?.composeContent : undefined}
                    initialSourceName={updateContext.deploymentConfig?.sourceName || undefined}
                    initialEditorMode={updateContext.deploymentConfig?.manifest ? "yaml" : "form"}
                    initialYamlDirty={Boolean(updateContext.deploymentConfig?.manifest)}
                    contextLabel={`更新 ${updateContext.app.stack}`}
                    modalTitle={lang === "zh" ? `更新应用 · ${updateContext.app.stack}` : `Update application · ${updateContext.app.stack}`}
                    modalSubtitle={lang === "zh" ? "提交后按同名应用更新，部署前仍会先预览生成结果。" : "Deploying updates the same application. Preview is still available before submit."}
                    modalContext={updateContextNode}
                    showTemplates={false}
                    onClose={closeUpdatePage}
                    onRefresh={async () => {
                      await loadDashboard();
                      closeUpdatePage();
                    }}
                  />
                </>
              ) : activePage === "topology" ? (
                <>
                  <PageHeader meta={pageMeta} />
                  <TrafficPaths lang={lang} paths={paths} theme={theme} />
                  <NodeTopology lang={lang} nodes={nodes} services={services} theme={theme} />
                </>
              ) : activePage === "storage" ? (
                <>
                  <PageHeader meta={pageMeta} />
                  <StoragePanel lang={lang} volumes={storageVolumes} storageClasses={storageClasses} warnings={storageWarnings} />
                </>
              ) : activePage === "observability" ? (
                <>
                  <PageHeader meta={pageMeta} />
                  <ObservabilityPanel lang={lang} token={token} nodes={nodes} services={services} />
                </>
              ) : (
                <>
                  <ReadinessCards lang={lang} payload={payload} />
                  <NodeFleetMap lang={lang} nodes={nodes} services={services} onSelect={openNodeDetail} onTerminal={setTerminalNode} />
                  <IssuesPanel lang={lang} issues={issues} token={token} />
                  <ApplicationManagementPanel
                    lang={lang}
                    token={token}
                    payload={payload}
                    onRefresh={loadDashboard}
                    onCreateApplication={() => {
                      setUpdateRequest(null);
                      setActivePage("deploy");
                    }}
                    onUpdateApplication={openUpdatePage}
                  />
                  <section className="table-grid">
                    <NodesTable lang={lang} nodes={nodes} onSelect={openNodeDetail} onTerminal={setTerminalNode} />
                    <ServicesTable lang={lang} services={services} onSelect={openServiceDetail} />
                  </section>
                </>
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
