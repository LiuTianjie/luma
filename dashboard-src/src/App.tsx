import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { appToComposeDraft, serviceToDraft } from "./components/applicationModel";
import { LoginPanel } from "./components/LoginPanel";
import { Topbar } from "./components/Topbar";
import { AppRoutes } from "./AppRoutes";
import { Sidebar } from "./Sidebar";
import { nodePath, servicePath, terminalPath, updatePath, parseObjectRoute } from "./objectRoutes";
import { ResourceDetailPage } from "./pages/ResourceDetailPage";
import { fetchDeploymentConfig } from "./deploymentConfigApi";
import { useRouter } from "./router";
import { pageForPath, ROUTE_BY_PAGE } from "./routes";
import type { DeployUpdateContext } from "./pages/DeployPage";
import { PageLoading } from "./pages/PageLoading";
import { createDashboardViewModel, type NavPage } from "./dashboardViewModel";
import type { TerminalSessionTarget } from "./components/TerminalDrawer";
import type { DashboardNode, DashboardService, Lang, SyncStatus } from "./types";
import { useDashboardData } from "./useDashboardData";
import { useTheme } from "./useTheme";

const TerminalDrawer = lazy(() => import("./components/TerminalDrawer").then((module) => ({ default: module.TerminalDrawer })));

const LANG_KEY = "luma.dashboard.lang";
const SIDEBAR_KEY = "luma.dashboard.sidebar";

export function App() {
  const router = useRouter();
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem(SIDEBAR_KEY) === "collapsed");
  const [deployTemplateLanding, setDeployTemplateLanding] = useState(true);
  const [updateRequest, setUpdateRequest] = useState<ApplicationUpdateRequest | null>(null);
  const [updateError, setUpdateError] = useState("");
  const [updateAttempt, setUpdateAttempt] = useState(0);
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();
  const { mode: themeMode, theme, setMode: setThemeMode } = useTheme();
  const vm = useMemo(() => createDashboardViewModel(payload), [payload]);

  const objectRoute = parseObjectRoute(router.path);
  const editName = objectRoute?.kind === "update" ? objectRoute.name : "";
  const currentUpdateRequest = editName && updateRequest?.app.stack === editName ? updateRequest : null;
  const routeNode = objectRoute?.kind === "node" || objectRoute?.kind === "node-terminal" ? vm.nodes.find(node => node.name === objectRoute.name) : undefined;
  const routeService = objectRoute?.kind === "service" || objectRoute?.kind === "service-terminal" ? vm.services.find(service => (service.fullName || service.name) === objectRoute.name) : undefined;
  const terminalTarget: TerminalSessionTarget | null = objectRoute?.kind === "node-terminal" && routeNode ? { kind: "node", node: routeNode }
    : objectRoute?.kind === "service-terminal" && routeService ? { kind: "container", service: routeService, stack: new URLSearchParams(router.search).get("stack") || routeService.stack } : null;
  const refreshPage = useCallback(async () => {
    window.dispatchEvent(new CustomEvent("luma:refresh"));
    await loadDashboard();
  }, [loadDashboard]);
  useEffect(() => {
    if (!editName || !token || currentUpdateRequest || !payload) return;
    const app = vm.applications.find(item => item.stack === editName);
    if (!app) return;
    let active = true;
    setUpdateError("");
    void fetchDeploymentConfig({ token, name: editName }).then(deploymentConfig => {
      if (active) setUpdateRequest({ app, deploymentConfig });
    }).catch(error => { if (active) setUpdateError(error instanceof Error ? error.message : String(error)); });
    return () => { active = false; };
  }, [editName, token, currentUpdateRequest, payload, vm.applications, updateAttempt]);

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
    (page: NavPage, opts?: { selectApp?: string }) => {
      setUpdateRequest(null);
      if (page === "deploy") setDeployTemplateLanding(true);
      let route = ROUTE_BY_PAGE[page];
      if (opts?.selectApp) {
        route = `/apps/${encodeURIComponent(opts.selectApp)}/overview`;
      }
      router.navigate(route);
    },
    [router],
  );

  const openUpdatePage = (request: ApplicationUpdateRequest) => {
    setUpdateRequest(request);
    setDeployTemplateLanding(false);
    router.navigate(updatePath(request.app.stack));
  };

  const closeUpdatePage = () => {
    setUpdateRequest(null);
    router.navigate(editName ? `/apps/${encodeURIComponent(editName)}/config` : ROUTE_BY_PAGE.applications);
  };

  const updateContext = useMemo<DeployUpdateContext | null>(() => {
    if (!currentUpdateRequest) return null;
    const updateRequest = currentUpdateRequest;
    const { app, deploymentConfig } = updateRequest;
    if (deploymentConfig?.manifest || deploymentConfig?.composeContent) {
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
  }, [currentUpdateRequest]);

  const updateContextNode = updateContext ? <div className="inline-banner">
    {lang === "zh" ? "正在更新应用：" : "Updating application: "}<strong>{editName}</strong>
    {currentUpdateRequest?.configWarning ? <span>{currentUpdateRequest.configWarning}</span> : null}
  </div> : null;
  const openNodeDetail = (node: DashboardNode) => router.navigate(nodePath(node.name || ""));
  const openServiceDetail = (service: DashboardService) => router.navigate(servicePath(service.fullName || service.name || ""));
  const openNodeTerminal = (node: DashboardNode) => router.navigate(terminalPath("node", node.name || ""));
  const openServiceTerminal = (service: DashboardService, stack: string) => router.navigate(terminalPath("service", service.fullName || service.name || "", stack));
  const closeTerminal = () => router.navigate(routeNode ? nodePath(routeNode.name || "") : routeService?.stack && vm.applications.some(app => app.stack === routeService.stack) ? `/apps/${encodeURIComponent(routeService.stack)}/services` : routeService ? servicePath(routeService.fullName || routeService.name || "") : "/fleet");

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
      <a className="skip-link" href="#main">
        {lang === "zh" ? "跳到主内容" : "Skip to main content"}
      </a>
      <Sidebar
        lang={lang}
        vm={vm}
        activeNavPage={activeNavPage}
        sidebarCollapsed={sidebarCollapsed}
        onNavigate={navigate}
        onToggle={toggleSidebar}
      />

      <main id="main" className="workspace" tabIndex={-1}>
        <div className="topbar-wrapper">
          <Topbar
            clusterId={vm.clusterId}
            lang={lang}
            lastUpdated={lastUpdated}
            themeMode={themeMode}
            onLangChange={setLang}
            onThemeModeChange={setThemeMode}
            onRefresh={() => void refreshPage()}
            onSignOut={signOut}
            syncStatus={visibleStatus}
          />
        </div>

        <div className="workspace-body">
          {!token ? (
            <div className="login-panel-container">
              <LoginPanel lang={lang} onSubmit={setToken} />
            </div>
          ) : (
            <>
              <ErrorBanner errors={errors} />
              {payload ? (
                terminalTarget ? <Suspense fallback={<PageLoading lang={lang} />}><TerminalDrawer key={router.path} lang={lang} target={terminalTarget} token={token} onClose={closeTerminal} inline /></Suspense>
                : objectRoute && objectRoute.kind !== "update" ? (routeNode || routeService ? <ResourceDetailPage lang={lang} node={routeNode} service={routeService} services={vm.services} applicationNames={vm.applications.map(app => app.stack)} onTerminal={() => { if (routeNode) openNodeTerminal(routeNode); else if (routeService) openServiceTerminal(routeService, routeService.stack || ""); }} />
                  : <section className="detail-page"><h1>{lang === "zh" ? "对象不存在或已移除" : "Object not found or removed"}</h1><button onClick={() => navigate("overview")}>{lang === "zh" ? "返回总览" : "Back to overview"}</button></section>)
                : editName && !updateContext ? <section className="detail-page"><button className="page-back" onClick={closeUpdatePage}>{lang === "zh" ? "← 返回应用" : "← Back to application"}</button><h1>{lang === "zh" ? "更新应用" : "Update application"} · {editName}</h1>{updateError ? <><p role="alert">{updateError}</p><button onClick={() => setUpdateAttempt(value => value + 1)}>{lang === "zh" ? "重试读取配置" : "Retry loading config"}</button></> : !vm.applications.some(app => app.stack === editName) ? <p>{lang === "zh" ? "应用不存在或已移除" : "Application not found or removed"}</p> : <PageLoading lang={lang} />}</section>
                : <AppRoutes
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
                  onNavigateToDeployments={() => navigate("deployments")}
                  onSelectNode={openNodeDetail}
                  onSelectService={openServiceDetail}
                  onTerminal={openNodeTerminal}
                  onServiceTerminal={openServiceTerminal}
                  onRefresh={refreshPage}
                  onCreateApplication={() => navigate("deploy")}
                  onUpdateApplication={openUpdatePage}
                  onCloseUpdate={closeUpdatePage}
                  onTemplateLandingChange={setDeployTemplateLanding}
                />
              ) : (
                <PageLoading lang={lang} />
              )}
            </>
          )}
        </div>
      </main>

    </div>
  );
}
