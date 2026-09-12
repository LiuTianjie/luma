import { lazy, Suspense, type ReactNode } from "react";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import type { DeployUpdateContext } from "./pages/DeployPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PageLoading } from "./pages/PageLoading";
import type { ResolvedPage } from "./routes";
import type { DashboardNode, DashboardPayload, DashboardService, Lang } from "./types";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";

const loadApplicationsPage = () => import("./pages/ApplicationsPage").then((module) => ({ default: module.ApplicationsPage }));
const loadBuilderPage = () => import("./pages/BuilderPage").then((module) => ({ default: module.BuilderPage }));
const loadDeploymentsPage = () => import("./pages/DeploymentsPage").then((module) => ({ default: module.DeploymentsPage }));
const loadDeployPage = () => import("./pages/DeployPage").then((module) => ({ default: module.DeployPage }));
const loadCredentialsPage = () => import("./pages/CredentialsPage").then((module) => ({ default: module.CredentialsPage }));
const loadSetupPage = () => import("./pages/SetupPage").then((module) => ({ default: module.SetupPage }));
const loadNodesPage = () => import("./pages/NodesPage").then((module) => ({ default: module.NodesPage }));
const loadLaeAdminPage = () => import("./pages/LaeAdminPage").then((module) => ({ default: module.LaeAdminPage }));
const loadObservabilityPage = () => import("./pages/ObservabilityPage").then((module) => ({ default: module.ObservabilityPage }));
const loadStoragePage = () => import("./pages/StoragePage").then((module) => ({ default: module.StoragePage }));
const loadRegistryPage = () => import("./pages/RegistryPage").then((module) => ({ default: module.RegistryPage }));
const ApplicationsPage = lazy(loadApplicationsPage);
const BuilderPage = lazy(loadBuilderPage);
const DeploymentsPage = lazy(loadDeploymentsPage);
const DeployPage = lazy(loadDeployPage);
const CredentialsPage = lazy(loadCredentialsPage);
const SetupPage = lazy(loadSetupPage);
const NodesPage = lazy(loadNodesPage);
const LaeAdminPage = lazy(loadLaeAdminPage);
const NotFound = lazy(() => import("./pages/NotFound").then((module) => ({ default: module.NotFound })));
const ObservabilityPage = lazy(loadObservabilityPage);
const StoragePage = lazy(loadStoragePage);
const RegistryPage = lazy(loadRegistryPage);

export type AppRoutesProps = {
  page: ResolvedPage;
  lang: Lang;
  token: string;
  theme: "light" | "dark";
  payload: DashboardPayload;
  vm: DashboardViewModel;
  // Loaded by App from the refresh-safe /apps/:stack/edit route.
  updateContext: DeployUpdateContext | null;
  updateContextNode: ReactNode;
  deployTemplateLanding: boolean;
  onNavigate: (page: NavPage, opts?: { selectApp?: string }) => void;
  onNavigateToDeployments: () => void;
  onSelectNode: (node: DashboardNode) => void;
  onSelectService: (service: DashboardService) => void;
  onTerminal: (node: DashboardNode) => void;
  onServiceTerminal: (service: DashboardService, stack: string) => void;
  onRefresh: () => Promise<void> | void;
  onCreateApplication: () => void;
  onUpdateApplication: (request: ApplicationUpdateRequest) => void;
  onCloseUpdate: () => void;
  onTemplateLandingChange: (isLanding: boolean) => void;
};

// Start route chunk downloads while the user is pointing at a destination. The
// promise is cached by the module loader, so rendering the lazy component later
// reuses the same request.
export function preloadPage(page: NavPage): void {
  const loader = page === "deployments" ? loadDeploymentsPage
    : page === "credentials" ? loadCredentialsPage
    : page === "setup" ? loadSetupPage
    : page === "applications" ? loadApplicationsPage
    : page === "builder" ? loadBuilderPage
    : page === "deploy" ? loadDeployPage
    : page === "nodes" ? loadNodesPage
    : page === "lae" ? loadLaeAdminPage
    : page === "observability" ? loadObservabilityPage
    : page === "storage" ? loadStoragePage
    : page === "registry" ? loadRegistryPage
    : null;
  if (loader) void loader().catch(() => {});
}

// Resolve the current page to its view. When an update request is active it takes over
// the DeployPage in update mode, mirroring the pre-router `activePage === "update"` path.
export function AppRoutes(props: AppRoutesProps): ReactNode {
  const { page, lang, token, theme, payload, vm } = props;

  let content: ReactNode;

  if (props.updateContext) {
    content = (
      <DeployPage
        lang={lang}
        token={token}
        payload={payload}
        vm={vm}
        updateContext={props.updateContext}
        updateContextNode={props.updateContextNode}
        deployTemplateLanding={false}
        onRefresh={props.onRefresh}
        onCloseUpdate={props.onCloseUpdate}
        onTemplateLandingChange={props.onTemplateLandingChange}
      />
    );
  } else switch (page) {
    case "overview":
      content = <OverviewPage lang={lang} payload={payload} vm={vm} onNavigate={props.onNavigate} onSelectNode={props.onSelectNode} />;
      break;
    case "applications":
      content = (
        <ApplicationsPage
          lang={lang}
          token={token}
          payload={payload}
          onRefresh={props.onRefresh}
          onCreateApplication={props.onCreateApplication}
          onUpdateApplication={props.onUpdateApplication}
          onNavigateToDeployments={props.onNavigateToDeployments}
          onServiceTerminal={props.onServiceTerminal}
        />
      );
      break;
    case "deploy":
      content = (
        <DeployPage
          lang={lang}
          token={token}
          payload={payload}
          vm={vm}
          updateContext={null}
          updateContextNode={null}
          deployTemplateLanding={props.deployTemplateLanding}
          onRefresh={props.onRefresh}
          onCloseUpdate={props.onCloseUpdate}
          onTemplateLandingChange={props.onTemplateLandingChange}
        />
      );
      break;
    case "builder":
      content = <BuilderPage lang={lang} token={token} payload={payload} vm={vm} onRefresh={props.onRefresh} onNavigate={props.onNavigate} />;
      break;
    case "deployments":
      content = <DeploymentsPage lang={lang} token={token} />;
      break;
    case "nodes":
      content = <NodesPage lang={lang} vm={vm} theme={theme} token={token} nodeJoin={payload.nodeJoin} controlVersion={payload.cluster?.version || ""} onSelectNode={props.onSelectNode} onTerminal={props.onTerminal} onRefresh={props.onRefresh} />;
      break;
    case "lae":
      content = <LaeAdminPage lang={lang} token={token} />;
      break;
    case "observability":
      content = <ObservabilityPage lang={lang} token={token} vm={vm} />;
      break;
    case "storage":
      content = <StoragePage lang={lang} vm={vm} token={token} />;
      break;
    case "registry":
      content = <RegistryPage lang={lang} token={token} />;
      break;
    case "credentials":
      content = <CredentialsPage lang={lang} token={token} vm={vm} />;
      break;
    case "setup":
      content = <SetupPage lang={lang} token={token} readiness={payload.readiness} onRefresh={props.onRefresh} />;
      break;
    default:
      content = <NotFound lang={lang} onHome={() => props.onNavigate("overview")} />;
  }

  return (
    <Suspense fallback={<PageLoading lang={lang} />}>
      <div className="flex flex-col gap-6" key={props.updateContext ? "update" : page}>
        {content}
      </div>
    </Suspense>
  );
}
