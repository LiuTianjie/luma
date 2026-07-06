import { useState } from "react";
import { GitBranch, History } from "lucide-react";
import { BuildHistoryPanel, GithubImportPanel } from "../deploy/GithubImportPanel";
import { PageHeader } from "./PageHeader";
import type { DashboardViewModel } from "../dashboardViewModel";
import type { DashboardPayload, Lang } from "../types";

// Builder groups the source-to-image concern that used to live inside the Create
// flow: import a Git repository (clone → build → push → deploy) and inspect/retry
// past build runs. Kept out of Create so that page stays purely template/form/YAML.
export function BuilderPage({
  lang,
  token,
  payload,
  vm,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  const [tab, setTab] = useState<"import" | "history">("import");
  const nodes = payload.nodes || [];

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "构建" : "Builder",
          title: zh ? "从仓库构建部署" : "Build from repository",
          description: zh
            ? "在构建节点上 clone 仓库、构建镜像、推送到集群内 registry，再走正常部署链路。"
            : "Clone a repo on a build node, build the image, push it to the in-cluster registry, then deploy.",
          metrics: [
            { label: zh ? "构建节点" : "Build nodes", value: vm.builderNodes },
            { label: "registry", value: payload.build?.registryHost || "-" },
          ],
        }}
      />
      <section className="builder-page" id="section-6">
        <div className="credentials-tabs" role="tablist" aria-label={zh ? "构建视图" : "Builder views"}>
          <button type="button" className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>
            <GitBranch size={15} aria-hidden="true" />
            {zh ? "仓库导入" : "Repository import"}
          </button>
          <button type="button" className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>
            <History size={15} aria-hidden="true" />
            {zh ? "构建历史" : "Build history"}
          </button>
        </div>
        {tab === "import" ? (
          <GithubImportPanel
            lang={lang}
            token={token}
            nodes={nodes}
            build={payload.build}
            onRefresh={onRefresh}
            onImported={() => setTab("history")}
          />
        ) : (
          <BuildHistoryPanel lang={lang} token={token} onRefresh={onRefresh} />
        )}
      </section>
    </>
  );
}
