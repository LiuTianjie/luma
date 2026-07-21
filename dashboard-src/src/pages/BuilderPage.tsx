import { GithubImportPanel } from "../deploy/GithubImportPanel";
import { PageHeader } from "./PageHeader";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import type { DashboardPayload, Lang } from "../types";

// Builder is the source-to-image entry: import a Git repository (clone → build →
// push → deploy). Build history lives in the Deployments timeline, so this page is
// import-only; a successful import jumps to that timeline.
export function BuilderPage({
  lang,
  token,
  payload,
  vm,
  onRefresh,
  onNavigate,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onRefresh: () => Promise<void> | void;
  onNavigate: (page: NavPage) => void;
}) {
  const zh = lang === "zh";
  const nodes = payload.nodes || [];

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "构建" : "Builder",
          title: zh ? "从仓库构建部署" : "Build from repository",
          description: zh
            ? "在构建节点上 clone 仓库、构建镜像、推送到集群内 registry，再走正常部署链路。构建历史在“部署记录”查看。"
            : "Clone a repo on a build node, build the image, push it to the in-cluster registry, then deploy. Build history lives under Deployments.",
          metrics: [
            { label: zh ? "构建节点" : "Build nodes", value: vm.builderNodes },
            { label: "registry", value: payload.build?.registryHost || "-" },
          ],
        }}
      />
      <section className="builder-page">
        <GithubImportPanel
          lang={lang}
          token={token}
          nodes={nodes}
          build={payload.build}
          onRefresh={onRefresh}
          onImported={() => onNavigate("deployments")}
        />
      </section>
    </>
  );
}
