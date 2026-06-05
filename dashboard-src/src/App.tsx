import { useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, serviceToDraft } from "./components/applicationModel";
import { LoginPanel } from "./components/LoginPanel";
import { NodeTopology } from "./components/NodeTopology";
import { NodesTable } from "./components/NodesTable";
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

const LANG_KEY = "luma.dashboard.lang";

type DetailState =
  | { kind: "node"; title: string; items: Record<string, string | number | boolean | undefined> }
  | { kind: "service"; title: string; items: Record<string, string | number | boolean | undefined> }
  | null;

export function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [activePage, setActivePage] = useState<"deploy" | "status" | "update">("deploy");
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [detail, setDetail] = useState<DetailState>(null);
  const [theme, setThemeState] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("luma.dashboard.theme");
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    if (theme === "light") {
      document.documentElement.classList.add("light");
    } else {
      document.documentElement.classList.remove("light");
    }
  }, [theme]);

  const setLang = (nextLang: Lang) => {
    setLangState(nextLang);
    localStorage.setItem(LANG_KEY, nextLang);
  };

  const toggleTheme = () => {
    const nextTheme = theme === "light" ? "dark" : "light";
    setThemeState(nextTheme);
    localStorage.setItem("luma.dashboard.theme", nextTheme);
  };

  const visibleStatus: SyncStatus = token ? syncStatus : "notConnected";
  const clusterId = payload?.cluster?.id || "-";
  const nodes = payload?.nodes || [];
  const services = payload?.services || [];
  const paths = payload?.trafficPaths || [];
  const storageVolumes = payload?.storage?.volumes || [];
  const storageClasses = payload?.storage?.storageClasses || [];
  const storageWarnings = payload?.storage?.warnings || [];
  const activeNavPage = activePage === "update" ? "status" : activePage;

  const navItems = useMemo(
    () => [
      {
        id: "deploy" as const,
        label: lang === "zh" ? "部署" : "Deploy",
        value: DEPLOY_TEMPLATES.length,
        detail: lang === "zh" ? "模板与 YAML" : "Templates and YAML",
      },
      {
        id: "status" as const,
        label: lang === "zh" ? "状态查看" : "Status",
        value: services.length,
        detail: lang === "zh" ? `${nodes.length} 节点 · ${paths.length} 路径` : `${nodes.length} nodes · ${paths.length} paths`,
      },
    ],
    [lang, nodes.length, paths.length, services.length],
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
        storage: (service.storage || []).map((item) => `${item.name || "-"}:${item.kind || "unmanaged"}`).join(", "),
        diagnostics: (service.diagnostics || []).join("; "),
      },
    });
  };

  return (
    <div className="dashboard-shell">
      <aside className="sidebar">
        <div className="brand-mark" aria-hidden="true">L</div>
        <div className="sidebar-title">
          <span>Luma Control</span>
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
            theme={theme}
            onThemeToggle={toggleTheme}
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
                  <section className="hero-strip deploy-page-hero" id="section-deploy">
                    <div>
                      <p className="eyebrow">{lang === "zh" ? "部署工作台" : "Deploy workspace"}</p>
                      <h1>{lang === "zh" ? "选择模板，编辑表单或 YAML，提交前先校验。" : "Select a template, edit form or YAML, validate before submit."}</h1>
                      <p>{lang === "zh" ? "模板会生成 Luma 配置；页面不会在选择模板后自动部署。" : "Templates generate Luma config. Selecting a template does not deploy it."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Deploy summary">
                      <span>{lang === "zh" ? "单服务" : "Service"} {DEPLOY_TEMPLATES.filter((item) => item.mode === "service").length}</span>
                      <span>Compose {DEPLOY_TEMPLATES.filter((item) => item.mode === "compose").length}</span>
                      <span>storageClass {storageClasses.length}</span>
                    </div>
                  </section>
                  <DeployWorkspace lang={lang} token={token} payload={payload} onRefresh={loadDashboard} />
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
              ) : (
                <>
                  <section className="hero-strip status-page-hero" id="section-status">
                    <div>
                      <p className="eyebrow">{t(lang, "controlPlane")}</p>
                      <h1>{t(lang, "title")}</h1>
                      <p>{lang === "zh" ? "当前控制面、节点、服务、流量路径和存储状态。" : "Current control-plane, node, service, route, and storage state."}</p>
                    </div>
                    <div className="hero-metrics" aria-label="Cluster summary">
                      <span>{nodes.length} {t(lang, "nodes")}</span>
                      <span>{services.length} {t(lang, "services")}</span>
                      <span>{paths.length} {t(lang, "trafficPaths")}</span>
                    </div>
                  </section>

                  <ReadinessCards lang={lang} payload={payload} />
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
