import { GithubImportPanel } from "../deploy/GithubImportPanel";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import type { DashboardPayload, Lang } from "../types";

// Builder is the source-to-image entry: import a Git repository (clone → build →
// push → deploy). Build history lives in the Deployments timeline, so this page is
// import-only; results stay visible and link to durable task details.
export function BuilderPage({
  lang,
  token,
  payload,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onRefresh: () => Promise<void> | void;
  onNavigate: (page: NavPage) => void;
}) {
  const nodes = payload.nodes || [];

  return (
      <section className="builder-page">
        <GithubImportPanel
          lang={lang}
          token={token}
          nodes={nodes}
          build={payload.build}
          onRefresh={onRefresh}
        />
      </section>
  );
}
