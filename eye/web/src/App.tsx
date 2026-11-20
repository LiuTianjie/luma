import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router";
import { AlertCircle, ArrowLeft, ShieldCheck, X } from "@/components/icons";
import { api } from "@/api/client";
import type { AuthStatus, Project } from "@/api/types";
import { AppSidebar } from "@/components/AppSidebar";
import { Topbar } from "@/components/Topbar";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { WorkspaceContext } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { useTheme, type ThemeMode } from "@/hooks/useTheme";
import { paths } from "@/router/paths";
import { AccountSecurityView } from "@/views/AccountSecurityView";
import { CapabilitiesSetupView } from "@/views/CapabilitiesSetupView";
import { EventsStreamView } from "@/views/EventsStreamView";
import { FunnelRetentionView } from "@/views/FunnelRetentionView";
import { IssueDetailView } from "@/views/IssueDetailView";
import { IssuesView } from "@/views/IssuesView";
import { LoginView } from "@/views/LoginView";
import { LogsView } from "@/views/LogsView";
import { OverviewView } from "@/views/OverviewView";
import { PerformanceView } from "@/views/PerformanceView";
import { PortfolioView } from "@/views/PortfolioView";
import { SessionDetailView } from "@/views/SessionDetailView";
import { SessionsView } from "@/views/SessionsView";

function ProjectGuardLayout({
  projectsLoaded,
  projects,
}: {
  projectsLoaded: boolean;
  projects: Project[];
}) {
  const { projectSlug = "" } = useParams();
  if (!projectsLoaded) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-16 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  const exists = projects.some((p) => p.slug === projectSlug);
  if (!exists) {
    return (
      <Empty className="py-16">
        <EmptyHeader>
          <EmptyTitle>未找到监控项目「{projectSlug}」</EmptyTitle>
          <EmptyDescription>该项目尚未创建或已从注册表中移除。</EmptyDescription>
        </EmptyHeader>
        <Button variant="outline" size="sm" nativeButton={false} render={<Link to="/portfolio" />}>
          <ArrowLeft data-icon="inline-start" />
          返回多项目总览
        </Button>
      </Empty>
    );
  }
  return <Outlet />;
}

function WorkspaceShell({
  authStatus,
  refreshAuthStatus,
  themeMode,
  setThemeMode,
  onLoggedOut,
}: {
  authStatus: AuthStatus | null;
  refreshAuthStatus: () => Promise<void>;
  themeMode: ThemeMode;
  setThemeMode: (mode: ThemeMode) => void;
  onLoggedOut: () => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const contentRef = useRef<HTMLElement>(null);

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [publicUrl, setPublicUrl] = useState("");
  const [lastSelectedSlug, setLastSelectedSlug] = useState("");

  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [banner, setBanner] = useState<{ msg: string; tone: "success" | "error" } | null>(null);

  const [newProjModal, setNewProjModal] = useState(false);
  const [creatingProj, setCreatingProj] = useState(false);
  const [newSlug, setNewSlug] = useState("");
  const [newName, setNewName] = useState("");
  const [newOrigins, setNewOrigins] = useState("");

  const since = searchParams.get("since") || "24h";
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;

  const setSince = useCallback(
    (next: string) => {
      const sp = new URLSearchParams(searchParamsRef.current);
      if (next === "24h") sp.delete("since");
      else sp.set("since", next);
      setSearchParams(sp);
    },
    [setSearchParams],
  );

  const notify = useCallback((msg: string, tone: "success" | "error" = "success") => {
    setBanner({ msg, tone });
  }, []);

  const triggerRefresh = useCallback(() => {
    setRefreshTick((t) => t + 1);
  }, []);

  const openNewProjectModal = useCallback(() => {
    setNewProjModal(true);
  }, []);

  const routeProjectSlug = useMemo(() => {
    const segs = location.pathname.split("/").filter(Boolean);
    if (segs[0] === "projects" && segs[1]) {
      return decodeURIComponent(segs[1]);
    }
    return "";
  }, [location.pathname]);

  useEffect(() => {
    if (routeProjectSlug) {
      setLastSelectedSlug(routeProjectSlug);
    }
  }, [routeProjectSlug]);

  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }, [location.pathname]);

  const reloadProjects = useCallback(async () => {
    const res = await api.listProjects();
    const list = res.projects || [];
    setProjects(list);
    setPublicUrl(res.public_url || window.location.origin);
    setProjectsLoaded(true);
    setLastSelectedSlug((prev) => {
      if (prev && list.some((p) => p.slug === prev)) return prev;
      return list[0]?.slug || "";
    });
    return list;
  }, []);

  useEffect(() => {
    reloadProjects().catch((err) => {
      notify(err instanceof Error ? err.message : String(err), "error");
    });
  }, [reloadProjects, notify]);

  const { data: portfolio, error: portfolioError } = useQuery(
    `portfolio:${since}:${refreshTick}:${projects.length}`,
    (signal) => api.portfolio(since, signal),
  );

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => {
      setRefreshTick((t) => t + 1);
    }, 15_000);
    return () => window.clearInterval(id);
  }, [autoRefresh]);

  const activeSidebarSlug = routeProjectSlug || lastSelectedSlug || projects[0]?.slug || "";
  const activeProjPortfolio = portfolio?.projects?.find((p) => p.project.slug === activeSidebarSlug);

  const handleCreateProject = async (ev: FormEvent) => {
    ev.preventDefault();
    setCreatingProj(true);
    try {
      const origins = newOrigins
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const created = await api.ensureProject(newSlug.trim(), newName.trim(), origins);
      setNewProjModal(false);
      setNewSlug("");
      setNewName("");
      setNewOrigins("");
      await reloadProjects();
      notify(`已创建监控项目 ${created.project.slug}。`);
      navigate(paths.projectSection(created.project.slug, "settings", since));
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setCreatingProj(false);
    }
  };

  const handleLogout = useCallback(async () => {
    await api.logout().catch(() => {});
    onLoggedOut();
    navigate("/login", { replace: true });
  }, [navigate, onLoggedOut]);

  const retentionStatus = authStatus?.retention;

  const ctxValue = useMemo(
    () => ({
      authStatus,
      refreshAuthStatus,
      projects,
      publicUrl,
      retention: retentionStatus,
      portfolio,
      since,
      setSince,
      refreshTick,
      triggerRefresh,
      reloadProjects,
      openNewProjectModal,
      notify,
      themeMode,
      setThemeMode,
      logout: handleLogout,
    }),
    [
      authStatus,
      refreshAuthStatus,
      projects,
      publicUrl,
      retentionStatus,
      portfolio,
      since,
      setSince,
      refreshTick,
      triggerRefresh,
      reloadProjects,
      openNewProjectModal,
      notify,
      themeMode,
      setThemeMode,
      handleLogout,
    ],
  );

  return (
    <WorkspaceContext.Provider value={ctxValue}>
      <TooltipProvider>
        <SidebarProvider className="h-dvh overflow-hidden">
          <AppSidebar
            projects={projects}
            activeProjectSlug={activeSidebarSlug}
            since={since}
            openIssuesCount={activeProjPortfolio?.open_issues || 0}
            retention={retentionStatus}
            onOpenNewProject={openNewProjectModal}
          />

          <SidebarInset className="h-dvh min-w-0 overflow-hidden">
            <Topbar
              projects={projects}
              authStatus={authStatus}
              since={since}
              onChangeSince={setSince}
              autoRefresh={autoRefresh}
              onToggleAutoRefresh={() => setAutoRefresh((v) => !v)}
              onRefresh={triggerRefresh}
              themeMode={themeMode}
              onThemeModeChange={setThemeMode}
              onSignOut={() => void handleLogout()}
            />

            <main ref={contentRef} id="main" className="console-content">
              <div className="console-page">
                {portfolioError ? (
                  <Alert variant="destructive">
                    <AlertCircle />
                    <AlertTitle>全局指标同步失败</AlertTitle>
                    <AlertDescription>{portfolioError}</AlertDescription>
                  </Alert>
                ) : null}

                {banner ? (
                  <Alert variant={banner.tone === "error" ? "destructive" : "default"}>
                    {banner.tone === "error" ? <AlertCircle /> : <ShieldCheck />}
                    <AlertTitle>{banner.tone === "error" ? "操作失败" : "操作提示"}</AlertTitle>
                    <AlertDescription>{banner.msg}</AlertDescription>
                    <AlertAction>
                      <Button
                        type="button"
                        size="icon-xs"
                        variant="ghost"
                        aria-label="关闭提示"
                        onClick={() => setBanner(null)}
                      >
                        <X className="size-3.5" />
                      </Button>
                    </AlertAction>
                  </Alert>
                ) : null}

                <Routes>
                  <Route index element={<Navigate to={paths.portfolio(since)} replace />} />
                  <Route path="portfolio" element={<PortfolioView />} />
                  <Route path="account" element={<AccountSecurityView />} />
                  <Route
                    path="projects/:projectSlug"
                    element={<ProjectGuardLayout projectsLoaded={projectsLoaded} projects={projects} />}
                  >
                    <Route index element={<Navigate to="overview" replace />} />
                    <Route path="overview" element={<OverviewView />} />
                    <Route path="performance" element={<PerformanceView />} />
                    <Route path="sessions" element={<SessionsView />} />
                    <Route path="sessions/:sessionId" element={<SessionDetailView mode="session" />} />
                    <Route path="users/:actorId" element={<SessionDetailView mode="actor" />} />
                    <Route path="events" element={<EventsStreamView />} />
                    <Route path="issues" element={<IssuesView />} />
                    <Route path="issues/:fingerprint" element={<IssueDetailView />} />
                    <Route path="funnel" element={<FunnelRetentionView />} />
                    <Route path="logs" element={<LogsView />} />
                    <Route path="settings" element={<CapabilitiesSetupView />} />
                    <Route path="*" element={<Navigate to="overview" replace />} />
                  </Route>
                  <Route path="*" element={<Navigate to={paths.portfolio(since)} replace />} />
                </Routes>
              </div>
            </main>
          </SidebarInset>

          <Dialog open={newProjModal} onOpenChange={setNewProjModal}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建监控项目</DialogTitle>
                <DialogDescription>
                  创建独立项目空间，生成前端只写公钥与兼容 Sentry SDK 的 DSN。
                </DialogDescription>
              </DialogHeader>

              <form onSubmit={(ev) => void handleCreateProject(ev)}>
                <FieldGroup className="gap-4">
                  <Field>
                    <FieldLabel htmlFor="new-proj-slug">项目标识 (Slug)</FieldLabel>
                    <Input
                      id="new-proj-slug"
                      type="text"
                      required
                      pattern="^[a-z0-9][a-z0-9_-]{0,62}$"
                      placeholder="例如 my-web-app"
                      spellCheck={false}
                      autoCapitalize="none"
                      autoCorrect="off"
                      value={newSlug}
                      onChange={(ev) => setNewSlug(ev.target.value)}
                    />
                    <FieldDescription>仅限小写字母、数字、连字符或下划线。</FieldDescription>
                  </Field>

                  <Field>
                    <FieldLabel htmlFor="new-proj-name">展示名称（可选）</FieldLabel>
                    <Input
                      id="new-proj-name"
                      type="text"
                      placeholder="例如 官网主站"
                      value={newName}
                      onChange={(ev) => setNewName(ev.target.value)}
                    />
                  </Field>

                  <Field>
                    <FieldLabel htmlFor="new-proj-origins">CORS 允许跨域白名单（可选）</FieldLabel>
                    <Input
                      id="new-proj-origins"
                      type="text"
                      placeholder="https://app.example.com, https://www.example.com"
                      value={newOrigins}
                      onChange={(ev) => setNewOrigins(ev.target.value)}
                    />
                    <FieldDescription>多个域名以逗号分隔；留空允许全部来源。</FieldDescription>
                  </Field>

                  <DialogFooter>
                    <Button type="button" variant="outline" onClick={() => setNewProjModal(false)}>
                      取消
                    </Button>
                    <Button type="submit" disabled={creatingProj}>
                      {creatingProj ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                      创建项目
                    </Button>
                  </DialogFooter>
                </FieldGroup>
              </form>
            </DialogContent>
          </Dialog>
        </SidebarProvider>
      </TooltipProvider>
    </WorkspaceContext.Provider>
  );
}

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { mode: themeMode, setMode: setThemeMode } = useTheme();

  const [authChecked, setAuthChecked] = useState(false);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);

  const refreshAuthStatus = useCallback(async () => {
    const st = await api.authStatus();
    setAuthStatus(st);
  }, []);

  useEffect(() => {
    refreshAuthStatus()
      .catch(() => setAuthStatus(null))
      .finally(() => setAuthChecked(true));

    const onUnauthorized = () => {
      setAuthStatus((prev) => (prev ? { ...prev, authenticated: false } : null));
    };
    window.addEventListener("luma-eye:unauthorized", onUnauthorized);
    return () => window.removeEventListener("luma-eye:unauthorized", onUnauthorized);
  }, [refreshAuthStatus]);

  if (!authChecked) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-canvas text-sm text-muted-foreground">
        <Spinner className="mr-2 size-4" aria-hidden="true" />
        正在加载控制台…
      </main>
    );
  }

  const authenticated = Boolean(authStatus?.authenticated);
  const isLoginRoute = location.pathname === "/login";

  if (!authenticated) {
    if (!isLoginRoute) {
      const intended = location.pathname + location.search;
      const target =
        intended && intended !== "/" && intended !== "/portfolio"
          ? `/login?redirect=${encodeURIComponent(intended)}`
          : "/login";
      return <Navigate to={target} replace />;
    }
    const finishLoginRedirect = async () => {
      await refreshAuthStatus();
      const redir = searchParams.get("redirect") || "/portfolio";
      navigate(redir.startsWith("/") ? redir : "/portfolio", { replace: true });
    };
    return (
      <LoginView
        authHint={authStatus}
        onPasswordOrTokenLogin={async (username, secret) => {
          await api.login({ username, password: secret });
          await finishLoginRedirect();
        }}
        onPasskeyAuthenticated={finishLoginRedirect}
      />
    );
  }

  if (isLoginRoute) {
    const redir = searchParams.get("redirect") || "/portfolio";
    return <Navigate to={redir.startsWith("/") ? redir : "/portfolio"} replace />;
  }

  return (
    <WorkspaceShell
      authStatus={authStatus}
      refreshAuthStatus={refreshAuthStatus}
      themeMode={themeMode}
      setThemeMode={setThemeMode}
      onLoggedOut={() =>
        setAuthStatus((prev) => (prev ? { ...prev, authenticated: false } : null))
      }
    />
  );
}
