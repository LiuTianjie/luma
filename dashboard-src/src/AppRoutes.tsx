import { lazy, Suspense, type ReactNode } from "react";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import type { DeployUpdateContext } from "./pages/DeployPage";
import type { ResolvedPage } from "./routes";
import type { DashboardNode, DashboardPayload, DashboardService, Lang } from "./types";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";

const ApplicationsPage = lazy(() => import("./pages/ApplicationsPage").then((module) => ({ default: module.ApplicationsPage })));
const BuilderPage = lazy(() => import("./pages/BuilderPage").then((module) => ({ default: module.BuilderPage })));
const DeploymentsPage = lazy(() => import("./pages/DeploymentsPage").then((module) => ({ default: module.DeploymentsPage })));
const DeployPage = lazy(() => import("./pages/DeployPage").then((module) => ({ default: module.DeployPage })));
const CredentialsPage = lazy(() => import("./pages/CredentialsPage").then((module) => ({ default: module.CredentialsPage })));
const NodesPage = lazy(() => import("./pages/NodesPage").then((module) => ({ default: module.NodesPage })));
const LaeAdminPage = lazy(() => import("./pages/LaeAdminPage").then((module) => ({ default: module.LaeAdminPage })));
const NotFound = lazy(() => import("./pages/NotFound").then((module) => ({ default: module.NotFound })));
const ObservabilityPage = lazy(() => import("./pages/ObservabilityPage").then((module) => ({ default: module.ObservabilityPage })));
const OverviewPage = lazy(() => import("./pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const StoragePage = lazy(() => import("./pages/StoragePage").then((module) => ({ default: module.StoragePage })));

export type AppRoutesProps = {
  page: ResolvedPage;
  lang: Lang;
  token: string;
  theme: "light" | "dark";
  payload: DashboardPayload;
  vm: DashboardViewModel;
  // Update pseudo-page state (Batch 6 will move this onto /apps/:stack/config).
  updateContext: DeployUpdateContext | null;
  updateContextNode: ReactNode;
  deployTemplateLanding: boolean;
  onNavigate: (page: NavPage, opts?: { selectApp?: string }) => void;
  onNavigateToDeployments: () => void;
  onSelectNode: (node: DashboardNode) => void;
  onSelectService: (service: DashboardService) => void;
  onTerminal: (node: DashboardNode) => void;
  onRefresh: () => Promise<void> | void;
  onCreateApplication: () => void;
  onUpdateApplication: (request: ApplicationUpdateRequest) => void;
  onCloseUpdate: () => void;
  onTemplateLandingChange: (isLanding: boolean) => void;
};

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
      content = <NodesPage lang={lang} vm={vm} theme={theme} token={token} controlVersion={payload.cluster?.version || ""} onSelectNode={props.onSelectNode} onTerminal={props.onTerminal} onRefresh={props.onRefresh} />;
      break;
    case "lae":
      content = <LaeAdminPage lang={lang} token={token} />;
      break;
    case "observability":
      content = <ObservabilityPage lang={lang} token={token} vm={vm} />;
      break;
    case "storage":
      content = <StoragePage lang={lang} vm={vm} />;
      break;
    case "credentials":
      content = <CredentialsPage lang={lang} token={token} vm={vm} />;
      break;
    default:
      content = <NotFound lang={lang} onHome={() => props.onNavigate("overview")} />;
  }

  return <Suspense fallback={<section className="empty-state" aria-busy="true" />}>{content}</Suspense>;
}
