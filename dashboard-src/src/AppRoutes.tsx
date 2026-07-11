import type { ReactNode } from "react";
import type { ApplicationUpdateRequest } from "./components/ApplicationManagementPanel";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { BuilderPage } from "./pages/BuilderPage";
import { DeploymentsPage } from "./pages/DeploymentsPage";
import { DeployPage, type DeployUpdateContext } from "./pages/DeployPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { NodesPage } from "./pages/NodesPage";
import { LaeAdminPage } from "./pages/LaeAdminPage";
import { NotFound } from "./pages/NotFound";
import { ObservabilityPage } from "./pages/ObservabilityPage";
import { OverviewPage } from "./pages/OverviewPage";
import { StoragePage } from "./pages/StoragePage";
import type { ResolvedPage } from "./routes";
import type { DashboardNode, DashboardPayload, DashboardService, Lang } from "./types";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";

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

  if (props.updateContext) {
    return (
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
  }

  switch (page) {
    case "overview":
      return <OverviewPage lang={lang} payload={payload} vm={vm} onNavigate={props.onNavigate} onSelectNode={props.onSelectNode} />;
    case "applications":
      return (
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
    case "deploy":
      return (
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
    case "builder":
      return <BuilderPage lang={lang} token={token} payload={payload} vm={vm} onRefresh={props.onRefresh} onNavigate={props.onNavigate} />;
    case "deployments":
      return <DeploymentsPage lang={lang} token={token} />;
    case "nodes":
      return <NodesPage lang={lang} vm={vm} theme={theme} token={token} onSelectNode={props.onSelectNode} onTerminal={props.onTerminal} onRefresh={props.onRefresh} />;
    case "lae":
      return <LaeAdminPage lang={lang} token={token} />;
    case "observability":
      return <ObservabilityPage lang={lang} token={token} vm={vm} />;
    case "storage":
      return <StoragePage lang={lang} vm={vm} />;
    case "credentials":
      return <CredentialsPage lang={lang} token={token} vm={vm} />;
    default:
      return <NotFound lang={lang} onHome={() => props.onNavigate("overview")} />;
  }
}
