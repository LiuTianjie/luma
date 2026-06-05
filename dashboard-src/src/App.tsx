import { useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, serviceToDraft } from "./components/applicationModel";
import { IssuesPanel } from "./components/IssuesPanel";
import { LoginPanel } from "./components/LoginPanel";
import { NodeTopology } from "./components/NodeTopology";
import { NodesTable } from "./components/NodesTable";
import { ObservabilityPanel } from "./components/ObservabilityPanel";
import { ReadinessCards } from "./components/ReadinessCards";
import { ServicesTable } from "./components/ServicesTable";
import { StoragePanel } from "./components/StoragePanel";
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

export function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [activePage, setActivePage] = useState<ActivePage>("deploy");
  const [deployTemplateLanding, setDeployTemplateLanding] = useState(true);
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [detail, setDetail] = useState<DetailState>(null);
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
  const healthyServices = services.filter((service) => (service.health || "").toLowerCase() === "healthy" || (service.health || "").toLowerCase() === "running").length;
  const activeNodes = nodes.filter((node) => (node.state || "").toLowerCase() === "ready" && (node.availability || "").toLowerCase() !== "drain").length;

  const navItems = useMemo(
    () => [
      {
        id: "deploy" as const,
        label: lang === "zh" ? "创建应用" : "Create",
        value: DEPLOY_TEMPLATES.length,
        detail: lang === "zh" ? "模板、表单、YAML" : "Templates, form, YAML",
      },
      {
        id: "status" as const,
        label: lang === "zh" ? "状态" : "Status",
        value: services.length,
        detail: lang === "zh" ? `${healthyServices}/${services.length} 服务正常` : `${healthyServices}/${services.length} services ok`,
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
        <strong>当前应用</strong>
        <span>{updateContext.deploymentConfig?.manifest ? "已读取 Luma Control 登记的部署配置，提交后会按同名应用更新。" : "下面的配置从现有 stack 带入，提交后会按同名应用更新。"}</span>
        {updateContext.configWarning ? <span>{updateContext.configWarning}</span> : null}
      </div>
      <div className="application-update-context-grid">
        <article><span>Stack</span><strong>{updateContext.app.stack}</strong></article>
        <article><span>服务</span><strong>{updateContext.app.services.length}</strong></article>
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
        <div className="brand-mark" aria-hidden="true">
          <img src={lumaLogoMark} alt="" />
        </div>
        <div className="sidebar-title">
          <span>Luma</span>
          <strong>{t(lang, "title")}</strong>
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
                  {deployTemplateLanding ? (
                    <section className="hero-strip deploy-page-hero" id="section-deploy">
                      <div>
                        <p className="eyebrow">{lang === "zh" ? "部署工作台" : "Deploy workspace"}</p>
                        <h1>{lang === "zh" ? "从模板创建应用" : "Create from templates"}</h1>
                        <p>{lang === "zh" ? "选择模板后进入表单或 YAML，校验通过后再部署。" : "Select a template, edit form or YAML, then validate before deploy."}</p>
                      </div>
                      <div className="hero-metrics" aria-label="Deploy summary">
                        <span>{lang === "zh" ? "单服务" : "Service"} {DEPLOY_TEMPLATES.filter((item) => item.mode === "service").length}</span>
                        <span>Compose {DEPLOY_TEMPLATES.filter((item) => item.mode === "compose").length}</span>
                        <span>storageClass {storageClasses.length}</span>
                      </div>
                    </section>
                  ) : null}
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
                  <section className="hero-strip application-update-hero" id="section-update">
                    <div>
                      <p className="eyebrow">{lang === "zh" ? "应用更新" : "Application update"}</p>
                      <h1>{lang === "zh" ? `更新应用 · ${updateContext.app.stack}` : `Update application · ${updateContext.app.stack}`}</h1>
                      <p>{lang === "zh" ? "使用当前应用配置作为起点，提交时按同名 stack 更新。" : "Start from the current application config and update the same stack."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Update summary">
                      <span>Stack {updateContext.app.stack}</span>
                      <span>{updateContext.app.services.length} {t(lang, "services")}</span>
                      <span>{updateContext.app.running}/{updateContext.app.desired} {t(lang, "replicas")}</span>
                    </div>
                  </section>
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
                  <section className="hero-strip topology-page-hero" id="section-topology">
                    <div>
                      <p className="eyebrow">{lang === "zh" ? "拓扑视图" : "Topology"}</p>
                      <h1>{lang === "zh" ? "节点拓扑与流量路径。" : "Node placement and traffic paths."}</h1>
                      <p>{lang === "zh" ? "查看服务运行在哪些节点，以及公开入口、隧道、代理到后端服务的完整路径。" : "Inspect where services run and how domains, tunnels, proxies, and services connect."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Topology summary">
                      <span>{activeNodes}/{nodes.length} {t(lang, "nodes")}</span>
                      <span>{services.length} {t(lang, "services")}</span>
                      <span>{paths.length} {t(lang, "trafficPaths")}</span>
                    </div>
                  </section>
                  <section className="topology-split-grid">
                    <NodeTopology lang={lang} nodes={nodes} services={services} theme={theme} />
                    <TrafficPaths lang={lang} paths={paths} theme={theme} />
                  </section>
                  <section className="table-grid compact-status-grid">
                    <NodesTable lang={lang} nodes={nodes} onSelect={openNodeDetail} />
                    <ServicesTable lang={lang} services={services} onSelect={openServiceDetail} />
                  </section>
                </>
              ) : activePage === "storage" ? (
                <>
                  <section className="hero-strip storage-page-hero" id="section-storage">
                    <div>
                      <p className="eyebrow">{lang === "zh" ? "存储状态" : "Storage"}</p>
                      <h1>{lang === "zh" ? "storageClass、卷和绑定关系。" : "Storage classes, volumes, and bindings."}</h1>
                      <p>{lang === "zh" ? "查看控制面登记的存储类、卷来源、节点绑定以及使用这些卷的服务。" : "Review registered storage classes, volume placement, node bindings, and consuming services."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Storage summary">
                      <span>{storageClasses.length} storageClass</span>
                      <span>{storageVolumes.length} {t(lang, "volume")}</span>
                      <span>{storageWarnings.length} warnings</span>
                    </div>
                  </section>
                  <StoragePanel lang={lang} volumes={storageVolumes} storageClasses={storageClasses} warnings={storageWarnings} />
                  <ReadinessCards lang={lang} payload={payload} />
                </>
              ) : activePage === "observability" ? (
                <>
                  <section className="hero-strip observability-page-hero" id="section-observability">
                    <div>
                      <p className="eyebrow">{lang === "zh" ? "可观测性" : "Observability"}</p>
                      <h1>{lang === "zh" ? "节点资源与实时日志。" : "Node resources and live logs."}</h1>
                      <p>{lang === "zh" ? "查看节点 CPU、内存、服务任务和最近日志。" : "Review node CPU, memory, service tasks, and recent logs."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Observability summary">
                      <span>{nodes.length} {t(lang, "nodes")}</span>
                      <span>{services.length} {t(lang, "services")}</span>
                      <span>logs</span>
                    </div>
                  </section>
                  <IssuesPanel lang={lang} issues={issues} />
                  <ObservabilityPanel lang={lang} token={token} nodes={nodes} services={services} />
                </>
              ) : (
                <>
                  <section className="hero-strip status-page-hero" id="section-status">
                    <div>
                      <p className="eyebrow">{t(lang, "controlPlane")}</p>
                      <h1>{lang === "zh" ? "集群、节点和应用状态。" : "Cluster, node, and application status."}</h1>
                      <p>{lang === "zh" ? "当前控制面、节点健康、应用状态、流量路径和存储状态。" : "Current control-plane readiness, node health, application state, traffic routes, and storage."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Cluster summary">
                      <span>{nodes.length} {t(lang, "nodes")}</span>
                      <span>{services.length} {t(lang, "services")}</span>
                      <span>{paths.length} {t(lang, "trafficPaths")}</span>
                    </div>
                  </section>

                  <ReadinessCards lang={lang} payload={payload} />
                  <IssuesPanel lang={lang} issues={issues} />
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
                    <NodesTable lang={lang} nodes={nodes} onSelect={openNodeDetail} />
                    <ServicesTable lang={lang} services={services} onSelect={openServiceDetail} />
                  </section>
                  <NodeTopology lang={lang} nodes={nodes} services={services} theme={theme} />
                  <TrafficPaths lang={lang} paths={paths} theme={theme} />
                  <StoragePanel lang={lang} volumes={storageVolumes} storageClasses={storageClasses} warnings={storageWarnings} />
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
    </div>
  );
}
