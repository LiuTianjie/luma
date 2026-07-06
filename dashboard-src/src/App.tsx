import { useCallback, useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, serviceToDraft } from "./components/applicationModel";
import { LoginPanel } from "./components/LoginPanel";
import { TerminalDrawer } from "./components/TerminalDrawer";
import { Topbar } from "./components/Topbar";
import { AppRoutes } from "./AppRoutes";
import { Sidebar } from "./Sidebar";
import { DetailDrawer } from "./DetailDrawer";
import { nodeDetail, serviceDetail, type DetailState } from "./detailRecords";
import { useRouter } from "./router";
import { pageForPath, ROUTE_BY_PAGE } from "./routes";
import type { DeployUpdateContext } from "./pages/DeployPage";
import { createDashboardViewModel, type NavPage } from "./dashboardViewModel";
import { t } from "./i18n";
import type { DashboardNode, DashboardService, Lang, SyncStatus } from "./types";
import { useDashboardData } from "./useDashboardData";
import { useTheme } from "./useTheme";

const LANG_KEY = "luma.dashboard.lang";
const SIDEBAR_KEY = "luma.dashboard.sidebar";

export function App() {
  const router = useRouter();
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem(SIDEBAR_KEY) === "collapsed");
  const [deployTemplateLanding, setDeployTemplateLanding] = useState(true);
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [detail, setDetail] = useState<DetailState>(null);
  const [terminalNode, setTerminalNode] = useState<DashboardNode | null>(null);
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();
  const { mode: themeMode, theme, setMode: setThemeMode } = useTheme();
  const vm = useMemo(() => createDashboardViewModel(payload), [payload]);

  const resolvedPage = pageForPath(router.path);
  const activeNavPage: NavPage = resolvedPage === "notfound" ? "overview" : resolvedPage;

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const setLang = (nextLang: Lang) => {
    setLangState(nextLang);
    localStorage.setItem(LANG_KEY, nextLang);
  };

  const navigate = useCallback(
    (page: NavPage) => {
      setUpdateRequest(null);
      if (page === "deploy") setDeployTemplateLanding(true);
      router.navigate(ROUTE_BY_PAGE[page]);
    },
    [router],
  );

  const openUpdatePage = (request: ApplicationUpdateRequest) => {
    setUpdateRequest(request);
    setDeployTemplateLanding(false);
    router.navigate(ROUTE_BY_PAGE.deploy);
  };

  const closeUpdatePage = () => {
    setUpdateRequest(null);
    router.navigate(ROUTE_BY_PAGE.applications);
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

  const openNodeDetail = (node: DashboardNode) => setDetail(nodeDetail(node));
  const openServiceDetail = (service: DashboardService) => setDetail(serviceDetail(service));

  const visibleStatus: SyncStatus = token ? syncStatus : "notConnected";

  const toggleSidebar = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      if (next) localStorage.setItem(SIDEBAR_KEY, "collapsed");
      else localStorage.removeItem(SIDEBAR_KEY);
      return next;
    });
  };

  return (
    <div className={`dashboard-shell page-${activeNavPage}${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar
        lang={lang}
        vm={vm}
        activeNavPage={activeNavPage}
        sidebarCollapsed={sidebarCollapsed}
        onNavigate={navigate}
        onToggle={toggleSidebar}
      />

      <main className="workspace">
        <div className="topbar-wrapper">
          <Topbar
            clusterId={vm.clusterId}
            lang={lang}
            lastUpdated={lastUpdated}
            themeMode={themeMode}
            onLangChange={setLang}
            onThemeModeChange={setThemeMode}
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
              <AppRoutes
                page={resolvedPage}
                lang={lang}
                token={token}
                theme={theme}
                payload={payload}
                vm={vm}
                updateContext={updateContext}
                updateContextNode={updateContextNode}
                deployTemplateLanding={deployTemplateLanding}
                onNavigate={navigate}
                onSelectNode={openNodeDetail}
                onSelectService={openServiceDetail}
                onTerminal={setTerminalNode}
                onRefresh={loadDashboard}
                onCreateApplication={() => navigate("deploy")}
                onUpdateApplication={openUpdatePage}
                onCloseUpdate={closeUpdatePage}
                onTemplateLandingChange={setDeployTemplateLanding}
              />
            ) : (
              <section className="empty-state">
                <p>{t(lang, visibleStatus)}</p>
              </section>
            )}
          </>
        )}
      </main>

      <DetailDrawer lang={lang} detail={detail} onClose={() => setDetail(null)} />
      {terminalNode ? (
        <TerminalDrawer lang={lang} node={terminalNode} token={token} onClose={() => setTerminalNode(null)} />
      ) : null}
    </div>
  );
}
