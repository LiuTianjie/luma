import { hasAddonCapability, type DashboardAddonsSummary } from "../addonCapabilities";
import { AddonRequiredCard } from "../components/AddonRequiredCard";
import { GithubImportPanel } from "../deploy/GithubImportPanel";
import type { DashboardPayload, Lang } from "../types";
import { PageHeader } from "./PageHeader";

// Builder is the source-to-image entry: import a Git repository (clone → build →
// push → deploy). Build history lives in the Deployments timeline, so this page is
// import-only; results stay visible and link to durable task details.
export function BuilderPage({
  lang,
  token,
  payload,
  addons,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  addons?: DashboardAddonsSummary;
  onRefresh: () => Promise<void> | void;
}) {
  const effectiveAddons = addons || payload.addons;
  if (!hasAddonCapability(effectiveAddons, "builder.git_import")) {
    const zh = lang === "zh";
    return (
      <div className="flex min-w-0 flex-col gap-6">
        <PageHeader
          meta={{
            eyebrow: zh ? "应用 / 从 Git 构建" : "Applications / Build from Git",
            title: zh ? "从 Git 仓库构建" : "Build from a Git repository",
            description: zh ? "此能力由可插拔的 Luma Build 插件提供。" : "Provided by the optional Luma Build addon.",
            metrics: [],
          }}
        />
        <AddonRequiredCard addonId="builder" lang={lang} />
      </div>
    );
  }
  const builderUrl = effectiveAddons?.urls?.builder || effectiveAddons?.items?.builder?.consoleUrl || "";
  return (
    <section className="min-w-0">
      <GithubImportPanel
        lang={lang}
        token={token}
        nodes={payload.nodes || []}
        regionCatalog={payload.regions}
        builderUrl={builderUrl}
        onRefresh={onRefresh}
      />
    </section>
  );
}
